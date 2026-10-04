// Statements: card billing vs captured alerts, account totals, balances by month.
// Labels come from bank PDFs: inserted with textContent only.
(function () {
  "use strict";
  const { h, money, longMonth, api, sparkline } = window.FTA;
  const $ = id => document.getElementById(id);
  const LOW = 0.95; // coverage below this is an abnormal condition (purchases without alerts)
  const shortDate = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const kindName = { account: "Accounts", card: "Credit cards" };

  function cell(row, text, cls) { const c = row.insertCell(); c.textContent = text; if (cls) c.className = cls; return c; }
  function empty(tbody, cols, text) { tbody.replaceChildren(); const c = tbody.insertRow().insertCell(); c.colSpan = cols; c.className = "empty"; c.textContent = text; }

  // Coverage meter: grey while normal; amber with a LOW label when alerts missed purchases.
  function coverage(c) {
    const wrap = h("span", null, "cov");
    if (c == null) { wrap.append(h("span", "—", "muted")); return wrap; }
    const low = c < LOW, bar = h("span", null, "cov-bar" + (low ? " low" : "")), fill = h("span");
    fill.style.width = Math.min(c, 1) * 100 + "%"; bar.append(fill);
    wrap.append(bar, h("span", `${Math.round(c * 100)}%`, "cov-val"));
    if (low) { const b = h("span", "LOW", "lim-badge hi"); b.title = "Some purchases on this statement never produced an alert"; wrap.append(b); }
    if (c > 1.02) wrap.title = "Alerts add up to more than the statement billed: usually authorisations that were reversed, or that post on the next statement";
    return wrap;
  }

  function renderCards(statements) {
    const tbody = $("stm-cards").tBodies[0]; tbody.replaceChildren();
    const rows = statements.filter(s => s.kind === "card").flatMap(s => s.lines.filter(l => l.kind === "card").map(l => ({ s, l })));
    if (!rows.length) return empty(tbody, 9, "No card statements yet.");
    for (const { s, l } of rows) {
      const r = tbody.insertRow();
      cell(r, longMonth(s.month));
      const card = cell(r, l.label);
      if (l.cards.length && !(l.cards.length === 1 && l.cards[0] === l.last4)) card.append(h("div", `card ****${l.cards.join(", ****")}`, "muted small"));
      cell(r, s.period_start ? `${shortDate(s.period_start)} – ${shortDate(s.period_end)}` : shortDate(s.period_end), "nowrap muted");
      cell(r, l.currency);
      cell(r, money(l.debits), "num");
      const cap = cell(r, money(l.captured), "num"); cap.append(h("div", `${l.captured_count} alert${l.captured_count === 1 ? "" : "s"}`, "muted small"));
      r.insertCell().append(coverage(l.coverage));
      cell(r, l.missing != null && l.missing > 0.005 ? money(l.missing) : "—", "num");
      cell(r, money(l.closing), "num");
    }
  }

  function renderAccounts(statements) {
    const tbody = $("stm-accounts").tBodies[0]; tbody.replaceChildren();
    const latest = statements.find(s => s.kind === "account");
    if (!latest) return empty(tbody, 8, "No account statements yet.");
    for (const l of latest.lines) {
      const r = tbody.insertRow(), total = l.kind !== "account";
      if (total) r.className = "total-row";
      cell(r, longMonth(latest.month));
      cell(r, total ? `${l.label} (total)` : l.label);
      cell(r, l.currency);
      cell(r, total ? "" : money(l.opening), "num");
      cell(r, total ? "" : money(l.debits), "num");
      cell(r, total ? "" : money(l.credits), "num");
      cell(r, money(l.closing), "num");
      const ok = cell(r, l.verified ? "✓" : l.verified === false ? "doesn't add up" : "");
      if (l.verified === false) ok.className = "danger-text";
      if (l.verified) ok.title = "opening − out + in = closing";
    }
  }

  function renderBalances(series) {
    const table = $("stm-balances");
    if (!series.length) { table.tHead.replaceChildren(); return empty(table.tBodies[0], 1, "No balances yet."); }
    const months = [...new Set(series.flatMap(s => s.points.map(p => p[0])))].sort().slice(-12);
    table.tHead.replaceChildren();
    const head = table.tHead.insertRow();
    ["Account", "Cur."].forEach(x => head.append(h("th", x)));
    months.forEach(m => { const th = h("th", new Date(m + "T12:00:00").toLocaleDateString(undefined, { month: "short", year: "2-digit" })); th.style.textAlign = "right"; head.append(th); });
    head.append(h("th", "Trend"));
    const tbody = table.tBodies[0]; tbody.replaceChildren();
    for (const s of series) {
      const r = tbody.insertRow(), byMonth = Object.fromEntries(s.points);
      if (s.kind !== "account") r.className = "total-row";
      cell(r, s.kind === "account" ? s.label : `${s.label} (total)`); cell(r, s.currency);
      months.forEach(m => cell(r, byMonth[m] != null ? money(byMonth[m], 0) : "—", "num"));
      const t = r.insertCell(); if (s.points.length > 1) t.append(sparkline(s.points.map(p => p[1]), 80, 20));
    }
  }

  function renderList(statements) {
    const tbody = $("stm-list").tBodies[0]; tbody.replaceChildren();
    if (!statements.length) return empty(tbody, 6, "Nothing imported yet.");
    for (const s of statements) {
      const r = tbody.insertRow();
      cell(r, longMonth(s.month)); cell(r, kindName[s.kind] || s.kind); cell(r, s.bank);
      cell(r, s.period_start ? `${shortDate(s.period_start)} – ${shortDate(s.period_end)}` : `to ${shortDate(s.period_end)}`, "nowrap");
      cell(r, s.filename || "", "muted small");
      const form = h("form"); form.method = "post"; form.action = `/statements/${s.id}/delete`; form.className = "inline";
      const btn = h("button", "Remove", "link danger"); form.append(btn);
      form.addEventListener("submit", ev => { if (!confirm(`Remove the ${longMonth(s.month)} ${s.kind} statement? Import it again any time.`)) ev.preventDefault(); });
      r.insertCell().append(form);
    }
  }

  api("/api/statements").then(d => {
    renderCards(d.statements); renderAccounts(d.statements); renderBalances(d.balances); renderList(d.statements);
  }).catch(e => empty($("stm-cards").tBodies[0], 9, e.message));
})();
