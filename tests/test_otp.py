"""OTP requests: a one-time code to confirm a purchase you didn't make is a High alarm, not a transaction."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from fraudalert import pipeline, report
from fraudalert.config import Settings, get_settings
from fraudalert.models import OtpRequest, RawEmail, Transaction
from fraudalert.web.app import create_app, unack_by_priority

from .conftest import make_eml

NOW = datetime.now(timezone.utc).replace(microsecond=0)
OTP_BODY = ("Su código de verificación es 482913. Compra en AMAZON MKTPLACE por USD 249.99 "
            "con su tarjeta terminada en 4321. No comparta este código.")


def write(tmp_path, name, subject, body, when, sender="Bank Alerts <alerts@bank.example>"):
    p = tmp_path / name
    p.write_bytes(make_eml(subject, body, when, sender=sender))
    return p


def otp_on(monkeypatch, subjects="código de verificación", senders=""):
    monkeypatch.setenv("FRAUDALERT_OTP_SUBJECT_FILTER", subjects)
    monkeypatch.setenv("FRAUDALERT_OTP_SENDER_FILTER", senders)
    get_settings.cache_clear()


def test_otp_details_spanish_and_english():
    from fraudalert.ingest.parsers import otp_details

    assert otp_details(OTP_BODY, "Código de verificación", "USD") == {
        "merchant": "AMAZON MKTPLACE", "amount": Decimal("249.99"), "currency": "USD", "card_last4": "4321"}
    en = otp_details("Your code is 123456 for a purchase at NETFLIX.COM for $15.49 on your card ending in 9999.",
                     "Your OTP", "USD")
    assert (en["merchant"], en["card_last4"]) == ("NETFLIX.COM", "9999")
    assert otp_details("Su clave dinámica es 445566", "Clave dinámica", "CRC") == dict.fromkeys(
        ("merchant", "amount", "currency", "card_last4"))  # the code itself is never read as an amount


BAC_OTP = """Logo BAC
Estimado/a Cliente
Para completar la compra con su tarjeta BAC terminada en 5678, verifique la siguiente información y si es correcta ingrese el código de confirmación

Comercio:

elecrow

Monto:

USD 127.43

Código de Confirmación:

111222


Por seguridad no comparta la información del código, verifique el nombre del comercio y monto de su compra.
Si desconoce esta solicitud, comuníquese con el banco inmediatamente.
Este comunicado es válido para Costa Rica
¿Necesitás asistencia? ¡Contactanos!

Vía Whatsapp
(506) 8000-0000
Digite Compra Segura"""


def test_bac_otp_email(db, tmp_path, monkeypatch):
    """BAC Costa Rica's real layout (card digits, code and phone replaced): labels on their own lines."""
    from fraudalert.ingest.parsers import otp_details

    assert otp_details(BAC_OTP, "", "CRC") == {
        "merchant": "elecrow", "amount": Decimal("127.43"), "currency": "USD", "card_last4": "5678"}
    assert otp_details("Comercio: ELECROW LTD\nMonto: USD 127.43", "", "CRC")["merchant"] == "ELECROW LTD"

    otp_on(monkeypatch, subjects="", senders="notificacionesotp_cri@baccredomatic.com")
    monkeypatch.setattr(pipeline, "notify_otp", lambda settings, otp: True)
    r = pipeline.import_eml_files([write(tmp_path, "bac.eml", "Compra Segura BAC", BAC_OTP, NOW - timedelta(minutes=2),
                                         sender="BAC Credomatic <notificacionesotp_cri@baccredomatic.com>")])
    assert (r.otp, r.parsed, r.failed) == (1, 0, 0)
    with db.session_scope() as s:
        assert s.scalar(select(Transaction.id)) is None  # BAC's Spanish parser never sees it as a purchase
        otp = s.scalar(select(OtpRequest))
        assert (otp.merchant, str(otp.amount), otp.currency, otp.card_last4) == ("elecrow", "127.43", "USD", "5678")
    get_settings.cache_clear()


def test_is_otp_matching():
    off = Settings(_env_file=None)
    assert not off.otp_enabled and not off.is_otp("a@bank", "Código de verificación")
    by_subject = Settings(_env_file=None, otp_subject_filter="código de verificación, OTP")
    assert by_subject.is_otp("anyone@x", "Su CÓDIGO DE VERIFICACIÓN") and by_subject.is_otp("x", "Your OTP")
    assert not by_subject.is_otp("x", "Purchase alert")
    both = Settings(_env_file=None, otp_subject_filter="otp", otp_sender_filter="otp@bank.example")
    assert both.is_otp("Bank <otp@bank.example>", "Your OTP") and not both.is_otp("alerts@bank.example", "Your OTP")
    by_sender = Settings(_env_file=None, otp_sender_filter="otp@bank.example")
    assert by_sender.is_otp("otp@bank.example", "anything")
    assert not Settings(_env_file=None, otp_sender_filter="otp@bank.example").for_statements().otp_enabled


def test_otp_email_is_a_high_alarm_not_a_transaction(db, tmp_path, monkeypatch):
    otp_on(monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline, "notify_otp", lambda settings, otp: calls.append(otp.merchant) or True)
    r = pipeline.import_eml_files([
        write(tmp_path, "otp.eml", "Código de verificación", OTP_BODY, NOW - timedelta(minutes=5)),
        write(tmp_path, "old.eml", "Código de verificación", OTP_BODY, NOW - timedelta(days=40)),
        write(tmp_path, "buy.eml", "Purchase alert", "You spent $12.00 at DELI.", NOW - timedelta(hours=1)),
    ])
    assert (r.fetched, r.parsed, r.otp) == (3, 1, 1)
    assert len(calls) == 1  # only the fresh one is notified
    with db.session_scope() as s:
        assert [t.merchant for t in s.scalars(select(Transaction))] == ["DELI"]  # never counted as spending
        fresh, old = s.scalars(select(OtpRequest).order_by(OtpRequest.received_at.desc())).all()
        assert (fresh.label_fraud, fresh.notified, fresh.card_last4) == (None, True, "4321")
        assert (fresh.merchant, str(fresh.amount), fresh.currency) == ("AMAZON MKTPLACE", "249.99", "USD")
        assert old.label_fraud is False and old.comment == pipeline.OTP_HISTORY_NOTE  # history: auto-acknowledged
        assert {e.parse_status for e in s.scalars(select(RawEmail))} == {"otp", "parsed"}
        assert unack_by_priority(s) == {0: 1, 1: 0, 2: 0, 3: 0}  # Critical, not High


def test_turning_otp_on_converts_stored_emails_and_off_removes_them(db, tmp_path, monkeypatch):
    # Before OTP detection is configured, the OTP email reads as a USD 249.99 purchase.
    pipeline.import_eml_files([write(tmp_path, "otp.eml", "Código de verificación", OTP_BODY, NOW - timedelta(days=3))])
    with db.session_scope() as s:
        assert s.scalar(select(Transaction.merchant)) is not None
    otp_on(monkeypatch)
    pipeline.reevaluate_all(reparse="all")
    with db.session_scope() as s:
        assert s.scalar(select(Transaction.id)) is None
        otp = s.scalar(select(OtpRequest))
        assert otp.label_fraud is False  # 3 days old when first seen: acknowledged as history
        otp.label_fraud = True  # your acknowledgement survives a re-parse
    pipeline.reevaluate_all(reparse="all")
    with db.session_scope() as s:
        assert s.scalar(select(OtpRequest)).label_fraud is True
    otp_on(monkeypatch, subjects="")
    pipeline.reevaluate_all(reparse="all")
    with db.session_scope() as s:
        assert s.scalar(select(OtpRequest.id)) is None
        assert s.scalar(select(Transaction.id)) is not None
    get_settings.cache_clear()


def test_alarms_page_report_and_acknowledge(db, tmp_path, monkeypatch):
    otp_on(monkeypatch)
    pipeline.import_eml_files([write(tmp_path, "otp.eml", "Código de verificación", OTP_BODY, NOW - timedelta(minutes=5))])
    client = TestClient(create_app(init=False))
    html = client.get("/alarms").text
    assert "OTP requests · 1 unacknowledged" in html and "AMAZON MKTPLACE" in html and "otp-panel has-unack" in html
    assert 'title="Priority 0 · Critical"' in html and "top-p0" in html and 'nav-count p0' in html
    assert "AMAZON MKTPLACE" in client.get("/").text  # also on the overview's alarm card
    assert "482913" not in html  # the code itself is never shown
    with db.session_scope() as s:
        rep = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW + timedelta(minutes=1),
                                  report.get_prefs(s, get_settings()))
        otp_id = s.scalar(select(OtpRequest.id))
    assert rep.unacknowledged == 1 and "(1 Critical)" in rep.subject and "[P0 Critical]" in rep.text
    assert "OTP request" in rep.text and "OTP request" in rep.html and "482913" not in rep.text + rep.html

    client.post(f"/otp/{otp_id}/label", data={"label": "legit"})
    html = client.get("/alarms").text
    assert "MINE" in html and "otp-panel has-unack" not in html
    with db.session_scope() as s:
        assert unack_by_priority(s)[0] == 0
    get_settings.cache_clear()


def test_imap_fetches_otp_senders_and_subjects(db, monkeypatch):
    from fraudalert.ingest import imap_client

    from .test_imap import FakeIMAP

    FakeIMAP.mailbox = {
        b"1": make_eml("Purchase alert", "You spent $12.00 at DELI.", NOW - timedelta(hours=2)),
        b"2": make_eml("Your one-time code", OTP_BODY, NOW - timedelta(minutes=5), sender="Codes <otp@bank.example>"),
        b"3": make_eml("Newsletter", "Save $5.00 today", NOW),
    }
    FakeIMAP.fetched_bodies, FakeIMAP.queries = [], []
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(pipeline, "notify_otp", lambda settings, otp: True)
    monkeypatch.setenv("FRAUDALERT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FRAUDALERT_IMAP_PASSWORD", "app-pw")
    monkeypatch.setenv("FRAUDALERT_SENDER_FILTER", "alerts@bank.example")
    monkeypatch.setenv("FRAUDALERT_SUBJECT_FILTER", "purchase")
    otp_on(monkeypatch, subjects="", senders="otp@bank.example")

    r = pipeline.sync_inbox()
    assert 'OR FROM "alerts@bank.example" FROM "otp@bank.example"' in FakeIMAP.queries[0]
    assert sorted(FakeIMAP.fetched_bodies) == [b"1", b"2"]  # the OTP passes the alert subject filter
    assert (r.parsed, r.otp, r.errors) == (1, 1, [])
    get_settings.cache_clear()


def test_critical_priority_for_rules(db, tmp_path):
    """Critical (P0) is a full priority: rules can use it, it sorts and filters above High, and rank 0 is never
    mistaken for "no alarm"."""
    from fraudalert.models import Rule
    from fraudalert.network import build_network

    with db.session_scope() as s:
        s.add(Rule(name="Jeweler", match="all", severity="critical",
                   conditions=[{"field": "merchant", "op": "contains", "value": "jewel"}]))
    pipeline.import_eml_files([
        write(tmp_path, "a.eml", "Alert", "You spent $0.00 at CARD TESTER.", NOW - timedelta(hours=3)),
        write(tmp_path, "b.eml", "Alert", "You spent $50.00 at JEWELER.", NOW - timedelta(hours=4)),
    ])
    client = TestClient(create_app(init=False))
    html = client.get("/alarms").text
    assert html.index("JEWELER") < html.index("CARD TESTER")  # Critical before High, though older
    assert 'title="Priority 0 · Critical"' in html and "pri-row-0" in html
    only = client.get("/alarms?pri=0").text
    assert "JEWELER" in only and "CARD TESTER" not in only
    assert "JEWELER" not in client.get("/alarms?pri=none").text
    assert '<option value="critical"' in client.get("/rules").text
    with db.session_scope() as s:
        assert unack_by_priority(s) == {0: 1, 1: 1, 2: 0, 3: 0}
        rep = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW + timedelta(minutes=1),
                                  report.get_prefs(s, get_settings()))
    assert "2 unacknowledged (1 Critical, 1 High)" in rep.subject
    assert rep.text.index("JEWELER") < rep.text.index("CARD TESTER")
    with db.session_scope() as s:
        graph = build_network(s, pipeline.Env.load(s, get_settings()), None)
    assert {n["label"]: n["state"] for n in graph["nodes"] if n.get("label") in ("JEWELER", "CARD TESTER")} == {
        "JEWELER": "p0", "CARD TESTER": "p1"}

def test_report_hides_critical_when_there_is_none(db, tmp_path):
    pipeline.import_eml_files([write(tmp_path, "a.eml", "Alert", "You spent $0.00 at CARD TESTER.", NOW - timedelta(hours=3))])
    with db.session_scope() as s:
        rep = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW + timedelta(minutes=1),
                                  report.get_prefs(s, get_settings()))
    assert "New alarms: 1 High, 0 Medium, 0 Low." in rep.text and "Critical" not in rep.text
