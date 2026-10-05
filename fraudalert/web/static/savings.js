// Savings loop: faceplate (SP / PV / OUT), feedforward breakdown, trend against the plan, setup forms.
(function () {
  "use strict";
  const { el, h, money, signed, api, niceMax, dayLabel, showTip, hideTip } = window.FTA;
  const $ = id => document.getElementById(id);
  let data = null, cur = "";
  const STATUS_TEXT = { ok: "OK", hi: "HI", hihi: "HIHI", early: "EARLY", setup: "SETUP" };

  function flash(text, error = false) {
    const m = $("sav-msg"); m.textContent = text; m.hidden = false; m.classList.toggle("error", error);
    clearTimeout(flash.t); flash.t = setTimeout(() => (m.hidden = true), 4000);
  }

  function renderFaceplate() {
    const d = data, st = $("fp-status");
    st.textContent = STATUS_TEXT[d.status] || d.status;
    st.className = "fp-status " + d.status;
    $("fp").className = "card faceplate fp-" + d.status;
    const goalText = !d.goal ? "not set" : d.goal.mode === "percent" ? `${d.goal.percent}% of income` : "fixed";
    $("fp-sp").textContent = d.target != null ? `${money(d.target, 0)} ${cur}` : "—";
    $("fp-sp").append(h("small", ` (${goalText})`, "muted"));
    $("fp-pv").textContent = d.projected_savings != null ? `${money(d.projected_savings, 0)} ${cur}` : "—";
    $("fp-out").textContent = d.allowance != null ? `${money(d.allowance, 0)} ${cur} / day` : "—";
    if (d.allowance != null) $("fp-out").append(h("small", ` · ${d.days_left} day${d.days_left === 1 ? "" : "s"} left`, "muted"));

    // indicator: projected savings on a 0..max scale, ticks at the HI threshold and the target
    const bar = $("fp-bar"), fill = bar.querySelector(".fp-fill");
    if (d.target != null && d.projected_savings != null) {
      const max = Math.max(d.target * 1.5, d.projected_savings, 1), pct = v => Math.max(0, Math.min(v / max, 1)) * 100 + "%";
      fill.style.width = pct(d.projected_savings);
      bar.querySelector(".fp-tick.hi").style.left = pct(d.target * 0.85);
      bar.querySelector(".fp-tick.sp").style.left = pct(d.target);
      bar.hidden = false;
    } else bar.hidden = true;

    const notes = [];
    if (d.status === "setup") notes.push(d.target == null ? "Set a savings target below." : "Add your income below: the loop needs it.");
    if (d.status === "early") notes.push("A projection needs a week of this month or a complete earlier month of history.");
    if (d.error != null && d.status !== "setup") notes.push(d.error >= 0 ? `${money(d.error, 0)} ${cur} under plan so far` : `${money(-d.error, 0)} ${cur} over plan so far`);
    if (d.pace != null) notes.push(`last 7 days: ${Math.round(d.pace * 100)}% of plan${d.pace_warning ? " ⚠ spending is speeding up" : ""}`);
    $("fp-note").textContent = notes.join(" · ");
  }

  function renderBudget() {
    const d = data, tbody = $("ff-table").tBodies[0]; tbody.replaceChildren();
    const row = (label, value, cls, title) => {
      const r = tbody.insertRow(); if (cls) r.className = cls; if (title) r.title = title;
      r.insertCell().textContent = label; const c = r.insertCell(); c.className = "num"; c.textContent = value;
    };
    row("Income this month", `${money(d.income, 0)} ${cur}`, null, "Your recurring income entries (feedforward)");
    row("− Fixed expenses", money(d.fixed, 0), null, "Scheduled fixed expenses (feedforward)");
    row("− Savings target", d.target != null ? money(d.target, 0) : "—");
    row("= To spend on cards", d.budget != null ? money(d.budget, 0) : "—", "total-row");
    row("Spent on cards so far", money(d.card_to_date, 0));
    row("Left to spend", d.remaining != null ? money(d.remaining, 0) : "—", d.remaining != null && d.remaining < 0 ? "total-row over" : "total-row");
    $("ff-note").textContent = d.coverage != null && d.correction > 1
      ? `Card spending is scaled up ×${d.correction.toFixed(2)}: your last card statement shows alerts captured ${Math.round(d.coverage * 100)}% of purchases (alerts so far: ${money(d.card_to_date_captured, 0)}).`
      : d.coverage != null ? `Alerts captured ${Math.round(d.coverage * 100)}% of the last card statement's purchases: no correction needed.` : "";
  }

  function drawTrend() {
    const d = data, f = window.FTA.frame($("sav-chart"), 260), n = d.days, t = d.actual.length;
    const plan = d.plan, fc = d.forecast, lim = d.limits;
    const max = niceMax(Math.max(...d.actual, ...(plan || [0]), ...(fc || [0]), lim ? lim.hihi * 1.02 : 0, 1));
    window.FTA.yAxis(f, max);
    const X = day => f.m.l + (day - 1) / Math.max(n - 1, 1) * f.iw, Y = v => f.m.t + f.ih - (v / max) * f.ih;
    for (const day of [1, 8, 15, 22, n]) el("text", { x: X(day), y: f.H - 8, class: "tick", "text-anchor": "middle" }, f.svg).textContent = String(day);
    if (lim) {
      window.FTA.limitLine(f, lim.hihi, max, "hihi", "HIHI · all of your income");
      window.FTA.limitLine(f, lim.hi, max, "hi", "HI · 15% short of target");
    }
    if (plan) el("polyline", { class: "expected", points: plan.map((v, i) => `${X(i + 1)},${Y(v)}`).join(" ") }, f.svg);
    if (fc) el("polyline", { class: "projection", points: fc.map((v, i) => `${X(t + i)},${Y(v)}`).join(" ") }, f.svg);
    if (t) {
      el("polyline", { class: "pen", points: d.actual.map((v, i) => `${X(i + 1)},${Y(v)}`).join(" ") }, f.svg);
      el("circle", { class: "pen-dot" + (d.status === "hi" || d.status === "hihi" ? " " + d.status : ""), cx: X(t), cy: Y(d.actual[t - 1]), r: 4.5 }, f.svg);
      el("text", { x: Math.min(X(t) + 8, f.W - f.m.r - 60), y: Y(d.actual[t - 1]) - 8, class: "end-label" }, f.svg).textContent = money(d.actual[t - 1], 0);
    }
    $("key-fc").hidden = !fc;
    $("trend-sub").textContent = plan
      ? `Spent ${money(d.spent, 0)} ${cur} including fixed expenses; the plan allowed ${money(plan[t - 1], 0)} by today. Month-end forecast ${d.projected_spending != null ? money(d.projected_spending, 0) : "—"} of a planned ${money(plan[n - 1], 0)}.`
      : `Spent ${money(d.spent, 0)} ${cur} so far. Set a target and your income to get a plan.`;
    const cross = el("line", { class: "cross", y1: f.m.t, y2: f.m.t + f.ih, visibility: "hidden" }, f.svg);
    const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.iw, height: f.ih, class: "hit area-hit" }, f.svg);
    hit.addEventListener("pointermove", ev => {
      const box = f.svg.getBoundingClientRect(), day = Math.min(Math.max(Math.round((ev.clientX - box.left - f.m.l) / f.iw * (n - 1)) + 1, 1), n);
      cross.setAttribute("x1", X(day)); cross.setAttribute("x2", X(day)); cross.setAttribute("visibility", "visible");
      const date = dayLabel(d.month.slice(0, 8) + String(day).padStart(2, "0")), lines = [];
      if (day <= t) {
        lines.push(`${money(d.actual[day - 1])} ${cur}`, `Spent to ${date}`);
        if (plan) lines.push(`Plan ${money(plan[day - 1], 0)} · ${signed(plan[day - 1] - d.actual[day - 1])} ${plan[day - 1] >= d.actual[day - 1] ? "to spare" : "over"}`);
      } else {
        lines.push(fc ? `${money(fc[day - t], 0)} ${cur}` : date, fc ? `Forecast by ${date}` : "No forecast yet");
        if (plan) lines.push(`Plan ${money(plan[day - 1], 0)}`);
      }
      showTip(ev, lines);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  }

  function renderGoal() {
    const form = $("goal-form"), g = data.goal || { mode: "percent", percent: 20 };
    form.mode.value = g.mode;
    form.amount.value = g.amount ?? ""; form.currency.value = g.currency || cur; form.percent.value = g.percent ?? "";
    syncGoalRows();
  }
  function syncGoalRows() {
    const mode = $("goal-form").mode.value;
    document.querySelectorAll(".goal-row").forEach(r => r.classList.toggle("off", r.dataset.mode !== mode));
  }

  const INC = ["name", "amount", "currency", "day_of_month", "start_month", "end_month"];
  function renderIncome() {
    const tbody = $("inc-table").tBodies[0]; tbody.replaceChildren();
    for (const e of data.income_entries) {
      const r = tbody.insertRow(), inputs = {};
      for (const k of INC) {
        const i = h("input"); i.name = k; i.value = e[k] ?? ""; i.setAttribute("aria-label", `${k.replace(/_/g, " ")} for ${e.name}`);
        if (k === "currency") { i.maxLength = 3; i.className = "fx-cur"; }
        if (k === "day_of_month") { i.type = "number"; i.min = 1; i.max = 31; i.className = "fx-day"; }
        if (k === "start_month" || k === "end_month") i.type = "month";
        inputs[k] = i; r.insertCell().append(i);
      }
      const act = r.insertCell(); act.className = "nowrap";
      const save = h("button", "Save", "btn btn-sm"), del = h("button", "Delete", "link danger");
      save.addEventListener("click", async () => {
        try { await api(`/api/savings/income/${e.id}`, { method: "PATCH", body: JSON.stringify(Object.fromEntries(INC.map(k => [k, inputs[k].value]))) });
          flash(`Saved “${inputs.name.value}”.`); await load(); } catch (err) { flash(err.message, true); }
      });
      del.addEventListener("click", async () => {
        if (!confirm(`Delete “${e.name}”? It disappears from every month. To stop it from now on, set “Until” instead.`)) return;
        try { await api(`/api/savings/income/${e.id}`, { method: "DELETE" }); flash(`Deleted “${e.name}”.`); await load(); } catch (err) { flash(err.message, true); }
      });
      act.append(save, del);
    }
    if (!data.income_entries.length) { const c = tbody.insertRow().insertCell(); c.colSpan = 7; c.className = "empty"; c.textContent = "No income yet. Add your salary below."; }
    const row = document.querySelector(".inc-new");
    if (!row.querySelector('[name="currency"]').value) row.querySelector('[name="currency"]').value = cur;
    if (!row.querySelector('[name="start_month"]').value) row.querySelector('[name="start_month"]').value = data.month.slice(0, 7);
  }

  async function load() {
    data = await api("/api/savings");
    cur = data.currency;
    renderFaceplate(); renderBudget(); drawTrend(); renderGoal(); renderIncome();
  }

  $("goal-form").addEventListener("change", ev => { if (ev.target.name === "mode") syncGoalRows(); });
  $("goal-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    const f = ev.target;
    try { await api("/api/savings/goal", { method: "PUT", body: JSON.stringify({ mode: f.mode.value, amount: f.amount.value, currency: f.currency.value, percent: f.percent.value }) });
      flash("Savings target saved."); await load(); } catch (err) { flash(err.message, true); }
  });
  $("inc-add").addEventListener("click", async () => {
    const row = document.querySelector(".inc-new"), values = {};
    row.querySelectorAll("[name]").forEach(i => (values[i.name] = i.value));
    try { await api("/api/savings/income", { method: "POST", body: JSON.stringify(values) });
      row.querySelectorAll('[name="name"],[name="amount"],[name="end_month"]').forEach(i => (i.value = ""));
      flash(`Added “${values.name}”.`); await load(); } catch (err) { flash(err.message, true); }
  });
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => data && drawTrend(), 200); });
  load().catch(err => flash(err.message, true));
})();
