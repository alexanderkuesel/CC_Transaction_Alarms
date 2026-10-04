"""Saving parsed statements, fetching them from the mailbox, and comparing them with captured data."""

import hashlib
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fraudalert.models import BankStatement, StatementTotal, Transaction
from fraudalert.statements import StatementError, parse_pdf

log = logging.getLogger(__name__)


def _dec(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(round(v, 2)))


def save_statement(session: Session, data: bytes, *, filename: str | None = None, message_id: str | None = None,
                   received_at: datetime | None = None, password: str = "") -> tuple[BankStatement, bool]:
    """Parse a statement PDF and store its totals. Returns (statement, created); the same file twice is a
    no-op. Raises StatementError for PDFs that aren't a recognised statement."""
    sha = hashlib.sha256(data).hexdigest()
    existing = session.scalar(select(BankStatement).where(BankStatement.sha256 == sha))
    if existing:
        return existing, False
    parsed = parse_pdf(data, password)
    st = BankStatement(sha256=sha, message_id=message_id, filename=(filename or "")[:255] or None, bank=parsed.bank,
                       kind=parsed.kind, month=parsed.month, period_start=parsed.period_start,
                       period_end=parsed.period_end, received_at=received_at)
    for line in parsed.lines:
        st.lines.append(StatementTotal(
            kind=line.kind, label=line.label, last4=line.last4, cards=line.cards or None, currency=line.currency,
            opening=_dec(line.opening), closing=_dec(line.closing), debits=_dec(line.debits),
            credits=_dec(line.credits), verified=line.verified))
    session.add(st)
    session.flush()
    return st, True


def sync_statements(settings, since: date, result) -> None:
    """Fetch statement emails (FRAUDALERT_STATEMENT_SENDER_FILTER) since `since` and store their PDFs.
    `result` is a pipeline.SyncResult: counts go to .statements, problems to .errors."""
    from fraudalert.db import session_scope
    from fraudalert.ingest.imap_client import fetch_messages

    def seen(mid: str) -> bool:
        with session_scope() as s:
            return s.scalar(select(BankStatement.id).where(BankStatement.message_id == mid).limit(1)) is not None

    for msg in fetch_messages(settings.for_statements(), since, seen):
        for filename, data in msg.pdfs:
            try:
                with session_scope() as session:
                    _, created = save_statement(session, data, filename=filename, message_id=msg.message_id,
                                                received_at=msg.received_at, password=settings.statement_password)
                result.statements += created
            except StatementError as exc:
                log.warning("statement %s in %s: %s", filename, msg.message_id, exc)
                result.errors.append(f"statement {filename}: {exc}")


# ---- comparison -----------------------------------------------------------------------------------

def _f(v) -> float | None:
    return None if v is None else float(v)


def overview(session: Session, env) -> dict:
    """Every statement with its totals; card lines carry what the alert emails captured for the same cards,
    currency and period, so you can see how much of the billing the alerts caught."""
    statements = session.scalars(select(BankStatement).options(selectinload(BankStatement.lines))
                                 .order_by(BankStatement.period_end.desc(), BankStatement.kind)).all()
    out = []
    for st in statements:
        lines = []
        for line in st.lines:
            item = {"kind": line.kind, "label": line.label, "last4": line.last4, "cards": line.cards or [],
                    "currency": line.currency, "opening": _f(line.opening), "closing": _f(line.closing),
                    "debits": _f(line.debits), "credits": _f(line.credits), "verified": line.verified}
            if line.kind == "card" and st.period_start:
                item.update(_captured(session, env, line.cards or [line.last4], line.currency, st.period_start,
                                      st.period_end, _f(line.debits)))
            lines.append(item)
        out.append({"id": st.id, "bank": st.bank, "kind": st.kind, "month": st.month.isoformat(),
                    "period_start": st.period_start.isoformat() if st.period_start else None,
                    "period_end": st.period_end.isoformat(), "filename": st.filename, "lines": lines})
    return {"statements": out, "balances": _balance_history(statements)}


def _captured(session: Session, env, cards: list[str], currency: str, start: date, end: date,
              billed: float | None) -> dict:
    """Card transactions captured from alerts for these cards and currency, on the statement's dates (local)."""
    lo = datetime.combine(start, time.min, tzinfo=env.tz)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=env.tz)
    rows = session.execute(select(Transaction.amount).where(
        Transaction.card_last4.in_([c for c in cards if c]), Transaction.currency == currency,
        Transaction.occurred_at >= lo, Transaction.occurred_at < hi)).all()
    captured = round(sum(float(a) for (a,) in rows), 2)
    coverage = round(captured / billed, 3) if billed and billed > 0 else None
    return {"captured": captured, "captured_count": len(rows), "coverage": coverage,
            "missing": round(billed - captured, 2) if billed is not None else None}


def _balance_history(statements) -> list[dict]:
    """Closing balance per account / total and currency, month by month (oldest first)."""
    series: dict[tuple, dict] = defaultdict(dict)
    for st in statements:
        for line in st.lines:
            if line.kind in ("account", "assets", "liabilities") and line.closing is not None:
                series[(line.kind, line.label, line.currency)][st.month.isoformat()] = float(line.closing)
    return [{"kind": k, "label": label, "currency": cur, "points": sorted(points.items())}
            for (k, label, cur), points in sorted(series.items())]
