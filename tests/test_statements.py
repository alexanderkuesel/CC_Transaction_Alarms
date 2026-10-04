from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fraudalert import pipeline
from fraudalert.config import get_settings
from fraudalert.models import BankStatement, StatementTotal, Transaction
from fraudalert.statements import StatementError, amount, parse_pdf, parse_text
from fraudalert.statements.store import overview, save_statement
from fraudalert.web.app import create_app

from .statements import ACCOUNT_TEXT, CARD_TEXT, make_pdf


def test_amounts_repair_pdf_quirks():
    assert amount("1,336,296.17") == 1336296.17
    assert amount("12074,067.15") == 12074067.15  # a thousands separator lost in PDF text extraction
    assert amount("12,737.35-") == -12737.35 and amount(".30") == 0.3 and amount("-4.50") == -4.5


def test_bac_account_statement():
    st = parse_text(ACCOUNT_TEXT)
    assert (st.bank, st.kind, st.month, st.period_end) == ("BAC Credomatic", "account", date(2026, 9, 1), date(2026, 9, 30))
    accounts = [line for line in st.lines if line.kind == "account"]
    assert [(a.label, a.currency) for a in accounts] == [
        ("Account ****2233", "CRC"), ("Account ****5566", "CRC"), ("Account ****8899", "USD")]
    first = accounts[0]
    assert (first.opening, first.debits, first.credits, first.closing) == (2234567.89, 1234567.89, 1500000.0, 2500000.0)
    assert all(a.verified for a in accounts)  # opening - debits + credits == closing, for every account
    usd = accounts[2]  # the interest-rate tier lines before its summary are skipped
    assert (usd.debits, usd.credits, usd.closing) == (400.0, 0.25, 150.25)
    totals = {(line.kind, line.currency): line.closing for line in st.lines if line.kind != "account"}
    assert totals == {("assets", "CRC"): 3500000.0, ("assets", "USD"): 150.25, ("liabilities", "CRC"): 50000000.0}


def test_bac_card_statement():
    st = parse_text(CARD_TEXT)
    assert (st.kind, st.month, st.period_start, st.period_end) == ("card", date(2026, 9, 1), date(2026, 8, 25), date(2026, 9, 24))
    lines = {(line.label, line.currency): line for line in st.lines}
    mc = lines[("Master Card ****5555", "CRC")]
    assert (mc.opening, mc.debits, mc.credits, mc.closing, mc.cards) == (90000.0, 50000.0, 90000.0, 50000.0, ["5555"])
    amex = lines[("American Express ****1111", "USD")]
    # the account number differs from the card used to pay: purchases are billed to card ****4321
    assert amex.last4 == "1111" and amex.cards == ["4321"] and (amex.debits, amex.closing) == (35.5, 45.5)
    assert ("Master Card ****5555", "USD") in lines  # a currency with any non-zero total is kept


def test_pdf_round_trip_and_unknown_layouts():
    assert [line.debits for line in parse_pdf(make_pdf(CARD_TEXT)).lines][:1] == [50000.0]
    with pytest.raises(StatementError, match="not a statement layout"):
        parse_pdf(make_pdf("Hello\nThis is a menu, not a statement"))
    with pytest.raises(StatementError, match="readable PDF"):
        parse_pdf(b"not a pdf at all")


def test_card_billing_is_compared_with_captured_alerts(db):
    with db.session_scope() as s:
        st, created = save_statement(s, make_pdf(CARD_TEXT), filename="card.pdf")
        assert created and len(st.lines) == 4
        assert save_statement(s, make_pdf(CARD_TEXT))[1] is False  # same file again: no duplicate
        when = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
        for amt in (1_000_000, 170_000):  # alerts caught 1,170,000 of the 1,300,000 billed to card 4321
            s.add(Transaction(merchant="PRICE SMART", amount=amt, currency="CRC", card_last4="4321", occurred_at=when))
        s.add(Transaction(merchant="APPLE", amount=35.5, currency="USD", card_last4="4321", occurred_at=when))
        s.add(Transaction(merchant="OTHER CARD", amount=999, currency="CRC", card_last4="9999", occurred_at=when))
        s.add(Transaction(merchant="TOO LATE", amount=5, currency="CRC", card_last4="4321",
                          occurred_at=datetime(2026, 9, 30, 18, tzinfo=timezone.utc)))  # after the cut-off
    with db.session_scope() as s:
        data = overview(s, pipeline.Env.load(s, get_settings()))
    [card] = data["statements"]
    lines = {(line["label"], line["currency"]): line for line in card["lines"]}
    crc = lines[("American Express ****1111", "CRC")]
    assert (crc["captured"], crc["captured_count"], crc["coverage"], crc["missing"]) == (1170000.0, 2, 0.9, 130000.0)
    assert lines[("American Express ****1111", "USD")]["coverage"] == 1.0
    assert lines[("Master Card ****5555", "USD")]["coverage"] is None  # nothing billed: nothing to compare


def test_balances_by_month(db):
    with db.session_scope() as s:
        save_statement(s, make_pdf(ACCOUNT_TEXT))
        later = ACCOUNT_TEXT.replace("30/SEP/26", "31/OCT/26").replace("Total 3,500,000.00 CRC", "Total 3,600,000.00 CRC")
        save_statement(s, make_pdf(later))
    with db.session_scope() as s:
        data = overview(s, pipeline.Env.load(s, get_settings()))
    assets = next(b for b in data["balances"] if b["kind"] == "assets" and b["currency"] == "CRC")
    assert [tuple(p) for p in assets["points"]] == [("2026-09-01", 3500000.0), ("2026-10-01", 3600000.0)]


def test_statements_arrive_by_email(db, monkeypatch):
    from email.message import EmailMessage

    from fraudalert.ingest import imap_client

    from .test_imap import FakeIMAP

    def statement_mail(n: int, subject: str, pdf: bytes) -> bytes:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "BAC <estadodecuenta@bank.example>", "me@example.com", subject
        m["Date"], m["Message-ID"] = "Thu, 01 Oct 2026 10:00:00 +0000", f"<stmt-{n}@bank.example>"
        m.set_content("Adjunto su estado de cuenta.")
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"EstadoCta_{n}.pdf")
        return bytes(m)

    searches = []

    class StatementIMAP(FakeIMAP):
        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                searches.append(args[1])
                # alerts: nothing new; statements: the two statement emails
                if "estadodecuenta" not in args[1]:
                    return "OK", [b""]
            return super().uid(cmd, *args)

    FakeIMAP.mailbox = {b"1": statement_mail(1, "Estado de cuenta Tarjeta de Crédito", make_pdf(CARD_TEXT)),
                        b"2": statement_mail(2, "Estado de cuenta de cuenta(s) bancaria(s)", make_pdf(ACCOUNT_TEXT))}
    FakeIMAP.fetched_bodies = []
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", StatementIMAP)
    monkeypatch.setenv("FRAUDALERT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FRAUDALERT_IMAP_PASSWORD", "app-pw")
    monkeypatch.setenv("FRAUDALERT_SENDER_FILTER", "alerts@bank.example")
    monkeypatch.setenv("FRAUDALERT_STATEMENT_SENDER_FILTER", "estadodecuenta@bank.example")
    get_settings.cache_clear()

    r = pipeline.sync_inbox()
    assert (r.statements, r.errors) == (2, []) and "statements 2" in str(r)
    assert any('FROM "estadodecuenta@bank.example"' in q for q in searches)
    with db.session_scope() as s:
        assert sorted(s.scalars(select(BankStatement.kind))) == ["account", "card"]
        assert s.scalar(select(BankStatement.message_id).where(BankStatement.kind == "card")) == "<stmt-1@bank.example>"
    FakeIMAP.fetched_bodies = []
    assert pipeline.sync_inbox().statements == 0 and FakeIMAP.fetched_bodies == []  # known emails aren't re-downloaded
    get_settings.cache_clear()


def test_statements_pages_upload_and_delete(db):
    client = TestClient(create_app(init=False))
    page = client.get("/statements").text
    assert "<h1>Statements</h1>" in page and "FRAUDALERT_STATEMENT_SENDER_FILTER" in page
    assert "No bank statements yet" in client.get("/").text

    r = client.post("/statements/upload", files=[("files", ("card.pdf", make_pdf(CARD_TEXT), "application/pdf")),
                                                 ("files", ("acct.pdf", make_pdf(ACCOUNT_TEXT), "application/pdf"))],
                    follow_redirects=False)
    assert r.status_code == 303 and "Imported" in r.headers["location"]
    r = client.post("/statements/upload", files=[("files", ("menu.pdf", make_pdf("lunch menu"), "application/pdf"))],
                    follow_redirects=False)
    assert "error=" in r.headers["location"]

    data = client.get("/api/statements").json()
    assert sorted(s["kind"] for s in data["statements"]) == ["account", "card"]
    home = client.get("/").text
    assert "Latest statements" in home and "American Express ****1111" in home and "Account ****2233" in home

    sid = next(s["id"] for s in data["statements"] if s["kind"] == "card")
    assert client.post(f"/statements/{sid}/delete", follow_redirects=False).status_code == 303
    with db.session_scope() as s:
        assert s.scalar(select(func.count(BankStatement.id))) == 1
        assert s.scalar(select(func.count(StatementTotal.id))) == 6  # the card statement's totals went with it


def test_statements_turned_on_later_and_kept_apart_from_alerts(db, monkeypatch):
    """Alerts already sync; statements are switched on afterwards. The statement from 4 days ago is still
    found (statements keep their own position), and alert searches never pick statement emails up, even
    with a broad alert filter."""
    import re
    from datetime import timedelta
    from email.message import EmailMessage
    from email.utils import format_datetime

    from fraudalert.ingest import imap_client
    from fraudalert.models import RawEmail

    from .conftest import make_eml

    now = datetime.now(timezone.utc)
    statement = EmailMessage()
    statement["From"], statement["To"] = "BAC <estadodecuenta@bank.example>", "me@example.com"
    statement["Subject"], statement["Message-ID"] = "Estado de cuenta Tarjeta de Crédito", "<stmt-old@bank.example>"
    statement["Date"] = format_datetime(now - timedelta(days=4))
    statement.set_content("Adjunto su estado de cuenta.")
    statement.add_attachment(make_pdf(CARD_TEXT), maintype="application", subtype="pdf", filename="EstadoCta.pdf")
    mailbox = [(now - timedelta(days=4), "estadodecuenta@bank.example", bytes(statement)),
               (now - timedelta(days=1), "alerts@bank.example",
                make_eml("Alert", "You spent $12.00 at DELI.", now - timedelta(days=1), sender="Bank <alerts@bank.example>"))]
    queries = []

    class Server:
        def __init__(self, host, port): pass
        def login(self, user, pw): pass
        def select(self, folder, readonly=False): return "OK", [str(len(mailbox)).encode()]
        def logout(self): pass

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                q = args[1]; queries.append(q)
                since = datetime.strptime(re.search(r"SINCE (\S+)", q).group(1), "%d-%b-%Y").date()
                nots = re.findall(r'NOT FROM "([^"]+)"', q)
                froms = re.findall(r'(?<!NOT )FROM "([^"]+)"', q)
                hits = [str(i + 1).encode() for i, (when, sender, _) in enumerate(mailbox)
                        if when.date() >= since and not any(n in sender for n in nots)
                        and (not froms or any(f in sender for f in froms))]
                return "OK", [b" ".join(hits)]
            uid, what = args
            raw = mailbox[int(uid) - 1][2]
            if "HEADER.FIELDS" in what:
                head = raw.split(b"\n\n", 1)[0]
                keep = b"\r\n".join(l for l in head.splitlines() if l.lower().startswith((b"message-id", b"subject")))
                return "OK", [(b"hdr", keep + b"\r\n")]
            return "OK", [(b"body", raw)]

    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", Server)
    monkeypatch.setenv("FRAUDALERT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FRAUDALERT_IMAP_PASSWORD", "app-pw")
    monkeypatch.setenv("FRAUDALERT_SENDER_FILTER", "alerts@bank.example")
    get_settings.cache_clear()
    r = pipeline.sync_inbox()  # statements not configured yet
    assert (r.parsed, r.statements) == (1, 0)

    monkeypatch.setenv("FRAUDALERT_SENDER_FILTER", "")  # a broad alert filter: everything in the folder
    monkeypatch.setenv("FRAUDALERT_STATEMENT_SENDER_FILTER", "estadodecuenta@bank.example")
    get_settings.cache_clear()
    queries.clear()
    r = pipeline.sync_inbox()
    assert (r.statements, r.errors) == (1, [])  # 4 days old, beyond the alert sync's 2-day overlap
    alert_query, statement_query = queries
    assert 'NOT FROM "estadodecuenta@bank.example"' in alert_query and "NOT FROM" not in statement_query
    with db.session_scope() as s:
        assert s.scalar(select(func.count(RawEmail.id))) == 1  # the statement never became an "unparsed alert"
        assert s.scalar(select(func.count(BankStatement.id))) == 1

    # statements are checked once a day (FRAUDALERT_STATEMENT_SYNC_HOURS), not on every 5-minute alert sync
    queries.clear()
    pipeline.sync_inbox()
    assert len(queries) == 1 and "NOT FROM" in queries[0]  # alerts only
    queries.clear()
    pipeline.sync_inbox(statements_now=True)  # the Statements page's "Check now"
    assert len(queries) == 2
    from fraudalert.models import SyncState
    with db.session_scope() as s:
        s.get(SyncState, "last_statement_sync").value = (now - timedelta(hours=25)).isoformat()
    queries.clear()
    pipeline.sync_inbox()
    assert len(queries) == 2  # a day later: due again
    get_settings.cache_clear()


def test_check_statements_now_button(db, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "sync_inbox", lambda settings=None, statements_now=False: calls.append(statements_now))
    monkeypatch.setenv("FRAUDALERT_STATEMENT_SENDER_FILTER", "estadodecuenta@bank.example")
    get_settings.cache_clear()
    client = TestClient(create_app(init=False))
    page = client.get("/statements").text
    assert "checked every 24 hours" in page and "not checked yet" in page and "Check now" in page
    get_settings.cache_clear()
    r = client.post("/statements/check", follow_redirects=False)
    assert r.status_code == 303 and "Checking" in r.headers["location"] and calls == [True]
