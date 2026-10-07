// Anomaly lens: the model's score as a landscape over two factors, a curve per factor, and the reasons.
// Plain SVG. Merchant names come from bank emails: always inserted with textContent.
(function () {
  "use strict";
  const { el, h, money, compact, api, showTip, hideTip } = window.FTA;
  const $ = id => document.getElementById(id);
  const root = $("lens"), CUR = root.dataset.currency;
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  // Score bands, light (normal) to dark (unusual). The last band is the alarm zone (from the rule's limit).
  const BANDS = [0.5, 0.7, 0.8, 0.9, 0.95];
  const state = { x: "hour", y: "amount", txn: null, points: [], limit: 0.97, slice: null, knobs: {} };

  // ---------- knob formatting and scales ----------
  const KNOB = {
    hour: { tf: v => v, fmt: v => clock(v), ticks: [0, 6, 12, 18, 24] },
    weekday: { tf: v => v, fmt: v => DAYS[Math.round(v) % 7], ticks: [0, 1, 2, 3, 4, 5, 6] },
    amount: { tf: v => Math.log(Math.max(v, 0.01)), fmt: v => `${compact(v)} ${CUR}`, short: v => compact(v), log: true },
    gap: { tf: v => Math.log(Math.max(v, 0.01)), fmt: v => gap(v), ticks: [0.1, 1, 24, 24 * 7, 24 * 30] },
    repeats: { tf: v => Math.log1p(Math.max(v, 0)), fmt: v => `${Math.round(v)}×`, log: true },
    burst: { tf: v => v, fmt: v => `${Math.round(v)}` },
    foreign: { tf: v => v, fmt: v => (v >= 0.5 ? "Yes" : "No"), ticks: [0, 1] },
  };
  function clock(v) {
    const hr = Math.floor(v) % 24, mi = Math.round((v - Math.floor(v)) * 60);
    const ampm = hr < 12 ? "am" : "pm", h12 = hr % 12 || 12;
    return mi ? `${h12}:${String(mi).padStart(2, "0")} ${ampm}` : `${h12} ${ampm}`;
  }
  function gap(hrs) {
    if (hrs < 1) return `${Math.max(1, Math.round(hrs * 60))} min`;
    if (hrs < 48) return `${Math.round(hrs)} h`;
    return `${Math.round(hrs / 24)} d`;
  }
  const score2 = s => (s == null ? "—" : s.toFixed(2));
  const band = s => (s == null ? -1 : s >= state.limit ? BANDS.length : BANDS.findIndex(b => s < b) === -1 ? BANDS.length - 1 : BANDS.findIndex(b => s < b));
  const bandLabel = i => i === BANDS.length ? `≥ ${state.limit.toFixed(2)} · alarm`
    : i === 0 ? `< ${BANDS[0].toFixed(2)}` : `${BANDS[i - 1].toFixed(2)}–${(i === BANDS.length - 1 ? state.limit : BANDS[i]).toFixed(2)}`;

  // A continuous scale over a sampled axis: cell edges halfway between samples.
  function axis(key, values, lo, hi) {
    const tf = KNOB[key].tf, t = values.map(tf);
    const edges = t.map((v, i) => (i ? (v + t[i - 1]) / 2 : v - ((t[1] ?? v + 1) - v) / 2));
    edges.push(t[t.length - 1] + (t[t.length - 1] - (t[t.length - 2] ?? t[0] - 1)) / 2);
    const a = edges[0], b = edges[edges.length - 1];
    const px = v => lo + ((Math.min(Math.max(tf(v), a), b) - a) / (b - a)) * (hi - lo);
    const ex = e => lo + ((e - a) / (b - a)) * (hi - lo);
    return { px, edges: edges.map(ex), values, outside: v => tf(v) < a || tf(v) > b };
  }
  function ticksFor(key, values, max = 7) {
    if (KNOB[key].ticks) return KNOB[key].ticks.filter(v => v >= values[0] - 1e-9 && v <= values[values.length - 1] + 1e-9);
    if (KNOB[key].log) {
      const lo = values[0], hi = values[values.length - 1], out = [];
      const floor = key === "repeats" ? 1 : 0.1; // purchases come in whole numbers
      for (let p = Math.floor(Math.log10(Math.max(lo, floor))); p <= Math.ceil(Math.log10(hi)); p++)
        for (const m of [1, 3]) { const v = m * 10 ** p; if (v >= Math.max(lo, floor) && v <= hi) out.push(v); }
      if (key === "repeats" && values[0] === 0) out.unshift(0);
      let t = out;
      while (t.length > max) t = t.filter((_, i) => i % 2 === 0);
      return t;
    }
    const n = values.length, step = Math.max(1, Math.ceil(n / Math.min(6, max)));
    return values.filter((_, i) => i % step === 0);
  }

  // ---------- data ----------
  async function loadPoints() {
    const d = await api("/api/lens/points");
    state.points = d.points; state.limit = d.limit;
    $("lens-model").textContent = d.isolation_forest ? `Isolation Forest (${d.model.split("@")[1] || "trained"})` : `${d.model} (statistical baseline until 50 purchases)`;
    $("lens-limit").textContent = d.limit.toFixed(2);
    renderTable();
  }
  async function loadSlice() {
    const q = new URLSearchParams({ x: state.x, y: state.y });
    if (state.txn != null) q.set("txn", state.txn);
    root.classList.add("busy");
    try { state.slice = await api("/api/lens/slice?" + q); }
    finally { root.classList.remove("busy"); }
    const url = new URLSearchParams(location.search);
    for (const [k, v] of [["x", state.x], ["y", state.y], ["txn", state.txn]]) v == null ? url.delete(k) : url.set(k, v);
    history.replaceState(null, "", "?" + url);
    renderAll();
  }
  function select(id) { state.txn = id; loadSlice().catch(fail); }
  function fail(e) { const m = $("lens-err"); m.textContent = e.message; m.hidden = false; }

  // ---------- landscape ----------
  function renderLand() {
    const d = state.slice, box = $("land");
    box.replaceChildren(); hideTip();
    if (d.base.score == null) { box.append(h("p", "Not enough history to score yet: the model needs about 10 purchases.", "muted")); return; }
    const W = Math.max(box.clientWidth, 300), H = Math.max(Math.min(W * 0.62, 460), 280), m = { l: 64, r: 14, t: 10, b: 38 };
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-labelledby": "land-title" }, box);
    el("title", { id: "land-title" }, svg).textContent =
      `Anomaly score by ${state.knobs[d.x].label.toLowerCase()} and ${state.knobs[d.y].label.toLowerCase()}`;
    const defs = el("defs", {}, svg);
    const pat = el("pattern", { id: "alarm-hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
    el("line", { x1: 0, y1: 0, x2: 0, y2: 6, class: "hatch" }, pat);
    const X = axis(d.x, d.xs, m.l, W - m.r), Y = axis(d.y, d.ys, H - m.b, m.t);

    // cells
    const gCells = el("g", { class: "cells" }, svg);
    const bandOf = d.grid.map(row => row.map(band));
    d.grid.forEach((row, j) => row.forEach((s, i) => {
      const x0 = X.edges[i], x1 = X.edges[i + 1], y0 = Y.edges[j + 1], y1 = Y.edges[j], b = bandOf[j][i];
      const r = el("rect", { x: x0, y: y0, width: Math.max(x1 - x0, 0) + 0.5, height: Math.max(y1 - y0, 0) + 0.5, class: `cell b${b}` }, gCells);
      if (b === BANDS.length) el("rect", { x: x0, y: y0, width: x1 - x0 + 0.5, height: y1 - y0 + 0.5, fill: "url(#alarm-hatch)", "pointer-events": "none" }, gCells);
      r.addEventListener("mousemove", ev => showTip(ev, [`Score ${score2(s)}${s >= state.limit ? " · alarm" : ""}`,
        `${state.knobs[d.x].label}: ${KNOB[d.x].fmt(d.xs[i])}`, `${state.knobs[d.y].label}: ${KNOB[d.y].fmt(d.ys[j])}`]));
      r.addEventListener("mouseleave", hideTip);
    }));

    // the alarm limit as a stepped contour around the alarm zone
    let path = "";
    for (let j = 0; j < bandOf.length; j++) for (let i = 0; i < bandOf[j].length; i++) {
      const inside = bandOf[j][i] === BANDS.length;
      const x0 = X.edges[i], x1 = X.edges[i + 1], y0 = Y.edges[j + 1], y1 = Y.edges[j];
      if (i + 1 < bandOf[j].length && inside !== (bandOf[j][i + 1] === BANDS.length)) path += `M${x1},${y0}V${y1}`;
      if (j + 1 < bandOf.length && inside !== (bandOf[j + 1][i] === BANDS.length)) path += `M${x0},${y0}H${x1}`;
    }
    if (path) { el("path", { d: path, class: "contour-halo" }, svg); el("path", { d: path, class: "contour" }, svg); }

    // axes
    const gAx = el("g", { class: "axes" }, svg);
    for (const v of ticksFor(d.x, d.xs)) {
      const x = X.px(v);
      el("line", { x1: x, x2: x, y1: H - m.b, y2: H - m.b + 4, class: "tickmark" }, gAx);
      el("text", { x, y: H - m.b + 16, class: "tick", "text-anchor": "middle" }, gAx).textContent = KNOB[d.x].fmt(v);
    }
    for (const v of ticksFor(d.y, d.ys)) {
      const y = Y.px(v);
      el("line", { x1: m.l - 4, x2: m.l, y1: y, y2: y, class: "tickmark" }, gAx);
      el("text", { x: m.l - 7, y: y + 4, class: "tick", "text-anchor": "end" }, gAx).textContent = KNOB[d.y].fmt(v);
    }
    el("text", { x: (m.l + W - m.r) / 2, y: H - 4, class: "axis-label", "text-anchor": "middle" }, gAx).textContent = state.knobs[d.x].label;
    el("text", { x: 12, y: (m.t + H - m.b) / 2, class: "axis-label", "text-anchor": "middle", transform: `rotate(-90 12 ${(m.t + H - m.b) / 2})` }, gAx).textContent = state.knobs[d.y].label;

    // your purchases
    const gDots = el("g", { class: "dots" }, svg);
    const sel = d.base.txn ? d.base.txn.id : null;
    for (const p of state.points) {
      if (p.id === sel) continue;
      const cx = X.px(p.k[d.x]), cy = Y.px(p.k[d.y]), hot = p.score != null && p.score >= state.limit;
      const c = el("circle", { cx, cy, r: hot ? 4.5 : 2.6, class: hot ? "dot hot" : "dot", tabindex: hot ? 0 : -1 }, gDots);
      c.addEventListener("mousemove", ev => showTip(ev, [p.merchant || "(unknown merchant)",
        `${money(p.amount)} ${p.currency} · ${new Date(p.when).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`,
        `Score ${score2(p.score)}${hot ? " · alarm" : ""}`, "Click to look through this purchase"]));
      c.addEventListener("mouseleave", hideTip);
      c.addEventListener("click", () => select(p.id));
      c.addEventListener("keydown", ev => { if (ev.key === "Enter") select(p.id); });
    }
    // the purchase being looked through: crosshair
    const bx = X.px(d.base.k[d.x]), by = Y.px(d.base.k[d.y]);
    el("line", { x1: bx, x2: bx, y1: m.t, y2: H - m.b, class: "cross" }, svg);
    el("line", { x1: m.l, x2: W - m.r, y1: by, y2: by, class: "cross" }, svg);
    el("circle", { cx: bx, cy: by, r: 8, class: "focus-ring" }, svg);
    el("circle", { cx: bx, cy: by, r: 3, class: "focus-dot" }, svg);

    $("land-note").textContent = d.base.txn
      ? `Everything else held at this purchase's values (${others(d)}). Dots are your purchases; click one to look through it.`
      : "Everything else held at a typical purchase of yours. Dots are your purchases; click one to look through it.";
    renderLegend();
  }
  function others(d) {
    return Object.keys(KNOB).filter(k => k !== d.x && k !== d.y).slice(0, 4)
      .map(k => `${state.knobs[k].label.toLowerCase()} ${KNOB[k].fmt(d.base.k[k])}`).join(", ");
  }
  function renderLegend() {
    const ul = $("land-legend"); ul.replaceChildren();
    for (let i = 0; i <= BANDS.length; i++) {
      const li = h("li"), sw = h("span", null, `sw b${i}${i === BANDS.length ? " alarm" : ""}`);
      sw.setAttribute("aria-hidden", "true"); li.append(sw, h("span", bandLabel(i))); ul.append(li);
    }
    const dot = h("li"); dot.append(h("span", null, "sw-dot"), h("span", "your purchase"));
    const hot = h("li"); hot.append(h("span", null, "sw-dot hot"), h("span", "above the limit"));
    const line = h("li"); line.append(h("span", null, "sw-line"), h("span", "alarm limit"));
    ul.append(dot, hot, line);
  }

  // ---------- faceplate ----------
  function renderFace() {
    const d = state.slice, box = $("face"); box.replaceChildren();
    const t = d.base.txn;
    $("lens-subject").textContent = t ? `${t.merchant || "(unknown)"} · ${money(t.amount)} ${t.currency}` : "a typical purchase";
    $("lens-reset").hidden = !t;
    const head = h("div", null, "face-head");
    if (t) {
      head.append(h("b", t.merchant || "(unknown merchant)"),
        h("div", `${money(t.amount)} ${t.currency}${t.currency !== CUR ? ` ≈ ${money(t.home)} ${CUR}` : ""}`, "face-amt"),
        h("div", `${new Date(t.when).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}${t.sinpe ? " · SINPE" : t.card ? ` · card …${t.card}` : ""}`, "muted small"));
    } else {
      head.append(h("b", "A typical purchase"), h("div", "The middle of your history on every factor.", "muted small"));
    }
    box.append(head);

    const s = d.base.score, hot = s != null && s >= state.limit;
    const big = h("div", null, "face-score" + (hot ? " hot" : ""));
    big.append(h("span", score2(s), "num"), h("span", hot ? "above the alarm limit" : s == null ? "not scored" : `more unusual than ${Math.round((s || 0) * 100)}% of your purchases`, "small"));
    box.append(big);
    // gauge 0..1 with the limit
    const g = h("div", null, "face-gauge"); g.setAttribute("role", "meter"); g.setAttribute("aria-valuemin", "0");
    g.setAttribute("aria-valuemax", "1"); g.setAttribute("aria-valuenow", String(s ?? 0)); g.setAttribute("aria-label", "Anomaly score");
    const fill = h("span", null, "fill" + (hot ? " hot" : "")); fill.style.width = `${(s || 0) * 100}%`;
    const lim = h("span", null, "lim"); lim.style.left = `${state.limit * 100}%`;
    g.append(fill, lim); box.append(g);

    box.append(h("h3", "Why this score"));
    if (!d.reasons.length) box.append(h("p", t ? "Nothing about this purchase stands out to the model." : "Pick a purchase to see what drives its score.", "muted small"));
    const ul = h("ul", null, "why");
    for (const r of d.reasons) {
      const li = h("li"), bar = h("span", null, "why-bar"); bar.style.width = `${Math.round(r.weight * 100)}%`;
      const btn = h("button", r.text, "link"); btn.type = "button";
      btn.title = "Show this factor's curve";
      btn.addEventListener("click", () => { const c = document.querySelector(`[data-knob="${r.knob}"]`); if (c) { c.scrollIntoView({ behavior: "smooth", block: "center" }); c.classList.add("flash-on"); setTimeout(() => c.classList.remove("flash-on"), 1200); } });
      li.append(h("span", `${Math.round(r.weight * 100)}%`, "why-pct"), btn, h("span", null, "why-track")); li.lastChild.append(bar);
      ul.append(li);
    }
    box.append(ul);
    if (t && t.state !== "none") {
      const a = h("a", t.state === "unack" ? "Review this alarm →" : "Open in the alarm journal →"); a.href = `/alarms?view=journal&q=${encodeURIComponent(t.merchant)}`;
      box.append(h("p", null, "small")); box.lastChild.append(a);
    }
  }

  // ---------- curves ----------
  function renderCurves() {
    const d = state.slice, box = $("curves"); box.replaceChildren();
    const order = Object.keys(KNOB).map(k => {
      const sc = d.curves[k].scores.filter(v => v != null);
      return { k, range: sc.length ? Math.max(...sc) - Math.min(...sc) : 0 };
    }).sort((a, b) => b.range - a.range);
    for (const { k, range } of order) box.append(curve(k, range));
  }
  function curve(key, range) {
    const d = state.slice, c = d.curves[key], knob = state.knobs[key];
    const card = h("figure", null, "lens-curve"); card.dataset.knob = key;
    const cap = h("figcaption"); cap.append(h("b", knob.label), h("span", `moves it ${range.toFixed(2)}`, "muted small"));
    cap.title = "How far this factor alone can move the score, from its lowest to its highest point";
    card.append(cap);
    const W = 280, H = 128, m = { l: 30, r: 10, t: 8, b: 24 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": `Score as ${knob.label.toLowerCase()} changes` }, card);
    // Scores are percentiles, so near the top they bunch up: zoom the axis when the whole curve is high,
    // or an unusual purchase's curves all look flat at 1.0.
    const vals = c.scores.filter(v => v != null).concat(d.base.score ?? []);
    const lo = Math.min(...vals), y0 = lo > 0.6 ? Math.max(0, Math.floor((Math.min(lo, state.limit) - 0.02) * 50) / 50) : 0;
    const X = axis(key, c.xs, m.l, W - m.r), y = s => m.t + (1 - ((s ?? y0) - y0) / (1 - y0)) * (H - m.t - m.b);
    for (const v of [y0, (y0 + 1) / 2, 1]) {
      el("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: v > y0 ? "grid" : "base" }, svg);
      el("text", { x: m.l - 5, y: y(v) + 4, class: "tick", "text-anchor": "end" }, svg).textContent = v ? (y0 ? v.toFixed(2) : v.toFixed(1)) : "0";
    }
    if (y0) card.classList.add("zoomed");
    el("line", { x1: m.l, x2: W - m.r, y1: y(state.limit), y2: y(state.limit), class: "limit-line" }, svg);
    const short = KNOB[key].short || KNOB[key].fmt;
    for (const v of ticksFor(key, c.xs, 5)) el("text", { x: X.px(v), y: H - 6, class: "tick", "text-anchor": "middle" }, svg).textContent = short(v);
    const pts = c.xs.map((v, i) => [X.px(v), y(c.scores[i])]);
    const discrete = key === "weekday" || key === "foreign";
    if (!discrete) {
      el("path", { d: "M" + pts.map(p => p.join(",")).join("L") + `L${pts[pts.length - 1][0]},${y(y0)}L${pts[0][0]},${y(y0)}Z`, class: "curve-wash" }, svg);
      el("path", { d: "M" + pts.map(p => p.join(",")).join("L"), class: "curve" }, svg);
    } else {
      pts.forEach(([px, py]) => { el("line", { x1: px, x2: px, y1: y(y0), y2: py, class: "stem" }, svg); el("circle", { cx: px, cy: py, r: 3.5, class: "stem-dot" }, svg); });
    }
    // typical value and this purchase's value
    const tv = d.base.typical[key];
    if (d.base.txn && tv != null && !X.outside(tv)) el("line", { x1: X.px(tv), x2: X.px(tv), y1: m.t, y2: H - m.b, class: "typical" }, svg);
    const bv = d.base.k[key], bx = X.px(bv);
    el("line", { x1: bx, x2: bx, y1: m.t, y2: H - m.b, class: "cross" }, svg);
    el("circle", { cx: bx, cy: y(d.base.score), r: 4.5, class: "focus-dot" }, svg);
    // hover: nearest sample
    const hit = el("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, class: "hit" }, svg);
    const hover = el("circle", { r: 4, class: "hover-dot", visibility: "hidden" }, svg);
    hit.addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect(), mx = (ev.clientX - r.left) * (W / r.width);
      let best = 0; pts.forEach((p, i) => { if (Math.abs(p[0] - mx) < Math.abs(pts[best][0] - mx)) best = i; });
      hover.setAttribute("cx", pts[best][0]); hover.setAttribute("cy", pts[best][1]); hover.setAttribute("visibility", "visible");
      showTip(ev, [`Score ${score2(c.scores[best])}`, `${knob.label}: ${KNOB[key].fmt(c.xs[best])}`,
        best === nearest(c.xs, bv) ? "(this purchase)" : null]);
    });
    hit.addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); hideTip(); });
    const foot = h("div", `${d.base.txn ? "This purchase" : "Typical"}: ${KNOB[key].fmt(bv)}${d.base.txn && tv != null ? ` · usual: ${KNOB[key].fmt(tv)}` : ""}`, "muted small");
    card.append(foot);
    return card;
  }
  const nearest = (xs, v) => xs.reduce((b, x, i) => (Math.abs(x - v) < Math.abs(xs[b] - v) ? i : b), 0);

  // ---------- table ----------
  function renderTable() {
    const q = $("odd-q").value.trim().toLowerCase(), tbody = $("odd").tBodies[0];
    const rows = state.points.filter(p => p.score != null && (!q || (p.merchant || "").toLowerCase().includes(q)))
      .sort((a, b) => b.score - a.score).slice(0, 25);
    tbody.replaceChildren();
    if (!rows.length) { const c = tbody.insertRow().insertCell(); c.colSpan = 6; c.className = "empty"; c.textContent = state.points.length ? "No scored purchases match." : "No scored purchases yet."; return; }
    for (const p of rows) {
      const tr = tbody.insertRow(), hot = p.score >= state.limit;
      if (state.txn === p.id) tr.className = "sel";
      const s = tr.insertCell(); s.className = "nowrap";
      const meter = h("span", null, "mini-meter" + (hot ? " hot" : "")); const f = h("span"); f.style.width = `${p.score * 100}%`; meter.append(f);
      s.append(h("b", score2(p.score), "mono"), meter);
      tr.insertCell().textContent = p.merchant || "(unknown)";
      const a = tr.insertCell(); a.className = "num nowrap"; a.textContent = `${money(p.amount)} ${p.currency}`;
      const w = tr.insertCell(); w.className = "nowrap muted"; w.textContent = new Date(p.when).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      tr.insertCell().textContent = (p.reasons || []).map(r => r.text).join(" · ");
      const b = h("button", state.txn === p.id ? "Looking" : "Look through", "btn btn-sm"); b.type = "button";
      b.addEventListener("click", () => { select(p.id); root.scrollIntoView({ behavior: "smooth" }); });
      tr.insertCell().append(b);
    }
  }

  function renderAll() { renderLand(); renderFace(); renderCurves(); renderTable(); }

  // ---------- wiring ----------
  for (const k of JSON.parse(document.getElementById("lens-knobs").textContent)) state.knobs[k.key] = k;
  const init = new URLSearchParams(location.search);
  if (init.get("x") in KNOB) state.x = init.get("x");
  if (init.get("y") in KNOB) state.y = init.get("y");
  if (/^\d+$/.test(init.get("txn") || "")) state.txn = Number(init.get("txn"));
  $("lens-x").value = state.x; $("lens-y").value = state.y;
  function axisChange(which) {
    const other = which === "x" ? "y" : "x", v = $(`lens-${which}`).value;
    if (v === state[other]) { state[other] = state[which]; $(`lens-${other}`).value = state[other]; } // swap rather than refuse
    state[which] = v; loadSlice().catch(fail);
  }
  $("lens-x").addEventListener("change", () => axisChange("x"));
  $("lens-y").addEventListener("change", () => axisChange("y"));
  $("lens-reset").addEventListener("click", () => select(null));
  $("odd-q").addEventListener("input", renderTable);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => state.slice && renderLand(), 150); });
  loadPoints().then(loadSlice).catch(fail);
})();
