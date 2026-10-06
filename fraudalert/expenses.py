"""The raw expense list: every card transaction and every booked fixed expense, one row each, with its
category. Filtered, sorted, totalled and paged for the Expenses page, and exported as CSV."""

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from fraudalert import spending
from fraudalert.anomaly.features import merchant_key
from fraudalert.models import Category, ManualExpense, MerchantTag, Transaction

SORTS = ("when", "merchant", "category", "amount")
SOURCES = ("all", "card", "fixed")


@dataclass
class Filter:
    start: date | None = None
    end: date | None = None
    category: str | None = None  # category id, "uncategorized", or None for all
    q: str = ""
    card: str = ""
    source: str = "all"
    amin: float | None = None  # in the home currency
    amax: float | None = None
    sort: str = "when"
    desc: bool = True


def _state(t: Transaction) -> str:
    if t.label_fraud:
        return "fraud"  # acknowledged as fraud: listed, but not counted as spending
    if t.label_fraud is False:
        return "reviewed"
    return "alarm" if t.flagged else "ok"


def rows(session: Session, env, f: Filter) -> list[dict]:
    spending.sync_tags(session)
    names = {c.id: c.name for c in session.scalars(select(Category))}
    tags = {t.merchant_key: t for t in session.scalars(select(MerchantTag))}
    lo = datetime.combine(f.start, time.min, tzinfo=env.tz) if f.start else None
    hi = datetime.combine(f.end + timedelta(days=1), time.min, tzinfo=env.tz) if f.end else None
    out: list[dict] = []

    if f.source in ("all", "card"):
        q = select(Transaction)
        if lo:
            q = q.where(Transaction.occurred_at >= lo)
        if hi:
            q = q.where(Transaction.occurred_at < hi)
        if f.card:
            q = q.where(Transaction.card_last4 == f.card)
        for t in session.scalars(q):
            key = merchant_key(t.merchant or "")
            tag = tags.get(key)
            cid = tag.category_id if tag else None
            occurred = t.occurred_at if t.occurred_at.tzinfo else t.occurred_at.replace(tzinfo=timezone.utc)
            out.append({
                "id": t.id, "when": occurred.astimezone(env.tz).isoformat(timespec="minutes"), "merchant": t.merchant,
                "key": key, "category_id": cid, "category": names.get(cid, spending.UNCATEGORIZED),
                "assigned_by": tag.assigned_by if tag else "auto", "amount": float(t.amount), "currency": t.currency,
                "home": env.fx.to_home(float(t.amount), t.currency), "card": t.card_last4, "source": "card",
                "state": _state(t), "comment": t.comment or ""})

    if f.source in ("all", "fixed") and not f.card:
        today = datetime.now(env.tz).date()
        first = f.start or date(2000, 1, 1)
        last = min(f.end or today, today)  # nothing booked in the future
        for e in session.scalars(select(ManualExpense)):
            for day in spending._occurrences(e, first, last):
                out.append({
                    "id": f"{spending.MANUAL}{e.id}@{day.isoformat()}", "when": day.isoformat(), "merchant": e.name,
                    "key": f"{spending.MANUAL}{e.id}", "category_id": e.category_id,
                    "category": names.get(e.category_id, spending.UNCATEGORIZED), "assigned_by": "manual",
                    "amount": float(e.amount), "currency": e.currency, "home": env.fx.to_home(float(e.amount), e.currency),
                    "card": None, "source": "fixed", "state": "ok", "comment": e.note or ""})

    q = f.q.strip().casefold()
    want = None if f.category in (None, "") else (None if f.category == "uncategorized" else int(f.category))
    out = [r for r in out
           if (not q or q in (r["merchant"] or "").casefold() or q in r["comment"].casefold())
           and (f.category in (None, "") or r["category_id"] == want)
           and (f.amin is None or r["home"] >= f.amin) and (f.amax is None or r["home"] <= f.amax)]
    key = {"when": lambda r: r["when"], "merchant": lambda r: (r["merchant"] or "").casefold(),
           "category": lambda r: (r["category"].casefold(), r["when"]), "amount": lambda r: r["home"]}[f.sort]
    out.sort(key=key, reverse=f.desc)
    return out


def page(session: Session, env, f: Filter, offset: int = 0, limit: int = 100) -> dict:
    all_rows = rows(session, env, f)
    counted = [r for r in all_rows if r["state"] != "fraud"]
    return {
        "currency": env.home_currency, "count": len(all_rows),
        "total": round(sum(r["home"] for r in counted), 2),
        "by_source": {s: round(sum(r["home"] for r in counted if r["source"] == s), 2) for s in ("card", "fixed")},
        "fraud_excluded": sum(1 for r in all_rows if r["state"] == "fraud"),
        "rows": all_rows[offset: offset + limit], "offset": offset, "limit": limit,
    }


def to_csv(rows_: list[dict], home: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "merchant", "category", "amount", "currency", f"amount_{home.lower()}", "card", "source",
                "status", "comment"])
    for r in rows_:
        w.writerow([r["when"], r["merchant"], r["category"], f'{r["amount"]:.2f}', r["currency"], f'{r["home"]:.2f}',
                    r["card"] or "", r["source"], r["state"], r["comment"]])
    return buf.getvalue()
