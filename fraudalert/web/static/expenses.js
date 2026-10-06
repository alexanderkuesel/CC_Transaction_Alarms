// Expenses: the raw list. Filters live in the URL so a view can be bookmarked; the CSV link follows them.
(function () {
  "use strict";
  const { h, money, api } = window.FTA;
  const $ = id => document.getElementById(id);
  const form = $("exp-filters");
  const state = { sort: "when", dir: "desc", offset: 0, limit: 100, categories: [] };
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const SOURCE = { card: "Card", fixed: "Fixed" };
  const STATE = { alarm: "Alarm", fraud: "Fraud", reviewed: "Reviewed", ok: "" };

  function flash(text, error = false) {
    const m = $("exp-msg"); m.textContent = text; m.hidden = false; m.classList.toggle("error", error);
    clearTimeout(flash.t); flash.t = setTimeout(() => (m.hidden = true), 4000);
  }

  function periodRange(p) {
    const now = new Date(), y = now.getFullYear(), m = now.getMonth();
    switch (p) {
      case "this": return [iso(new Date(y, m, 1)), iso(now)];
      case "last": return [iso(new Date(y, m - 1, 1)), iso(new Date(y, m, 0))];
      case "90": return [iso(new Date(y, m, now.getDate() - 89)), iso(now)];
      case "year": return [iso(new Date(y, 0, 1)), iso(now)];
      case "12m": return [iso(new Date(y - 1, m, now.getDate() + 1)), iso(now)];
      case "all": return ["", ""];
      default: return null; // custom: keep the date inputs
    }
  }

  function params() {
    const q = new URLSearchParams();
    const range = periodRange(form.period.value);
    if (range) { form.from.value = range[0]; form.to.value = range[1]; }
    for (const name of ["from", "to", "category", "card", "source", "q", "min", "max"]) {
      const v = form[name].value.trim();
      if (v && !(name === "source" && v === "all")) q.set(name, v);
    }
    q.set("sort", state.sort); q.set("dir", state.dir);
    return q;
  }

  async function load() {
    const q = params();
    document.querySelectorAll(".ef-custom").forEach(e => e.classList.toggle("off", form.period.value !== "custom"));
    const view = new URLSearchParams(q); view.set("period", form.period.value);
    history.replaceState(null, "", "?" + view);
    $("exp-csv").href = "/expenses.csv?" + q;
    q.set("offset", state.offset); q.set("limit", state.limit);
    $("exp-table").style.opacity = .6;
    const d = await api("/api/expenses?" + q);
    $("exp-table").style.opacity = 1;
    document.querySelectorAll('[data-bind="currency"]').forEach(e => (e.textContent = d.currency));
    const parts = [`${d.count.toLocaleString()} expense${d.count === 1 ? "" : "s"}`, `${money(d.total)} ${d.currency}`];
    if (d.by_source.fixed && d.by_source.card) parts.push(`cards ${money(d.by_source.card, 0)} · fixed ${money(d.by_source.fixed, 0)}`);
    if (d.fraud_excluded) parts.push(`${d.fraud_excluded} marked as fraud not counted`);
    $("exp-total").textContent = parts.join(" · ");
    render(d);
  }

  function categorySelect(r) {
    const s = h("select"); s.setAttribute("aria-label", `Category for ${r.merchant}`);
    for (const [id, name] of state.categories) { const o = h("option", name); o.value = String(id); s.append(o); }
    const u = h("option", "Uncategorized"); u.value = ""; s.append(u);
    s.value = r.category_id == null ? "" : String(r.category_id);
    s.addEventListener("change", async () => {
      try {
        const n = await window.FTA.assignCategory([r.key], s.value);
        flash(n > 1 ? `Moved ${r.merchant} and ${n - 1} similar merchant${n > 2 ? "s" : ""}.` : `Moved ${r.merchant}.`);
        await load();
      } catch (e) { flash(e.message, true); }
    });
    return s;
  }

  function render(d) {
    const tbody = $("exp-table").tBodies[0]; tbody.replaceChildren();
    if (!d.rows.length) { const c = tbody.insertRow().insertCell(); c.colSpan = 8; c.className = "empty"; c.textContent = "No expenses match these filters."; }
    for (const r of d.rows) {
      const row = tbody.insertRow(); if (r.state === "fraud") row.className = "row-excluded";
      const when = r.when.length > 10 ? new Date(r.when) : new Date(r.when + "T12:00:00");
      const c0 = row.insertCell(); c0.className = "nowrap muted";
      c0.textContent = r.when.length > 10
        ? when.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
        : when.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
      const c1 = row.insertCell(); c1.textContent = r.merchant;
      if (r.comment) c1.append(h("div", r.comment, "muted small"));
      const c2 = row.insertCell(); c2.append(categorySelect(r));
      if (r.assigned_by === "learned") c2.append(h("span", "learned", "tb-you"));
      const c3 = row.insertCell(); c3.className = "num"; c3.textContent = `${money(r.amount)} ${r.currency}`;
      const c4 = row.insertCell(); c4.className = "num"; c4.textContent = money(r.home);
      row.insertCell().textContent = r.card ? `…${r.card}` : "";
      row.insertCell().textContent = SOURCE[r.source] || r.source;
      const c7 = row.insertCell(); c7.textContent = STATE[r.state] ?? r.state;
      if (r.state === "alarm") { c7.className = "nowrap"; const a = h("a", "Alarm"); a.href = "/alarms?view=unack"; c7.replaceChildren(a); }
    }
    const pager = $("exp-pager"); pager.replaceChildren();
    if (d.count > d.limit) {
      const prev = h("button", "← previous", "link"), next = h("button", "next →", "link");
      prev.disabled = d.offset === 0; next.disabled = d.offset + d.limit >= d.count;
      prev.addEventListener("click", () => { state.offset = Math.max(0, d.offset - d.limit); load(); });
      next.addEventListener("click", () => { state.offset = d.offset + d.limit; load(); });
      pager.append(prev, h("span", `${d.offset + 1}–${Math.min(d.offset + d.limit, d.count)} of ${d.count}`, "muted"), next);
    }
    document.querySelectorAll(".sort").forEach(b => {
      const on = b.dataset.sort === state.sort;
      b.closest("th").setAttribute("aria-sort", on ? (state.dir === "asc" ? "ascending" : "descending") : "none");
      b.dataset.arrow = on ? (state.dir === "asc" ? "▲" : "▼") : "";
    });
  }

  // restore a bookmarked view
  const init = new URLSearchParams(location.search);
  for (const name of ["period", "from", "to", "category", "card", "source", "q", "min", "max"]) if (init.get(name)) form[name].value = init.get(name);
  if (init.get("from") && !init.get("period")) form.period.value = "custom";
  if (init.get("sort")) state.sort = init.get("sort");
  if (init.get("dir")) state.dir = init.get("dir");
  state.categories = [...$("ef-cat").options].filter(o => /^\d+$/.test(o.value)).map(o => [o.value, o.textContent]);

  let t;
  form.addEventListener("input", ev => { if (ev.target.name === "q" || ev.target.classList.contains("ef-num")) { clearTimeout(t); t = setTimeout(() => { state.offset = 0; load(); }, 300); } });
  form.addEventListener("change", ev => { if (ev.target.name !== "q") { state.offset = 0; load(); } });
  form.addEventListener("submit", ev => ev.preventDefault());
  document.querySelectorAll(".sort").forEach(b => b.addEventListener("click", () => {
    if (state.sort === b.dataset.sort) state.dir = state.dir === "asc" ? "desc" : "asc";
    else { state.sort = b.dataset.sort; state.dir = b.dataset.sort === "merchant" || b.dataset.sort === "category" ? "asc" : "desc"; }
    state.offset = 0; load();
  }));
  load().catch(e => flash(e.message, true));
})();
