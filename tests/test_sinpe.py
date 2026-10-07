"""SINPE transfers (Costa Rica): BAC's "Notificación de Transferencia Local" emails become expenses paid to
the person named after "Estimado(a)". All names, accounts and references here are made up."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from fraudalert import expenses, pipeline, report
from fraudalert.config import Settings, get_settings
from fraudalert.ingest.message import EmailMessage
from fraudalert.ingest.parsers import SINPE, parse_email
from fraudalert.models import RawEmail, Transaction
from fraudalert.web.app import create_app

from .conftest import make_eml

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SENDER = "BAC Credomatic <alerta@baccredomatic.com>"


def sinpe_body(payee="JUAN CARLOS PEREZ MORA", payer="MARIA FERNANDA LOPEZ SOLIS", amount="60.000,00 CRC",
               when="07-10-2026 a las 12:22:51", note="Sin Descripcion", ref="2026100700000000000000001"):
    return f"""Notificación
de Transferencia Local

Estimado(a) {payee} :

BAC Credomatic le comunica que {payer} realizó una transferencia electrónica a su cuenta N° *****1234.

La transferencia se realizó el día {when} horas; por un monto de {amount} , por concepto de:

{note}

El número de referencia es {ref}

Muchas Gracias."""


def write(tmp_path, name, body, when=None, subject="Notificación de Transferencia Local"):
    p = tmp_path / name
    p.write_bytes(make_eml(subject, body, when or NOW - timedelta(hours=1), sender=SENDER))
    return p


def test_parse_bac_sinpe_notification():
    msg = EmailMessage("<a@b>", SENDER, "Notificación de Transferencia Local", NOW, sinpe_body())
    p, name = parse_email(msg, "USD")
    assert name == "bac-sinpe" and p.source == SINPE
    assert (p.merchant, p.payer) == ("JUAN CARLOS PEREZ MORA", "MARIA FERNANDA LOPEZ SOLIS")
    assert (str(p.amount), p.currency) == ("60000.00", "CRC")  # 60.000,00 = sixty thousand colones
    assert p.occurred_at == datetime(2026, 10, 7, 12, 22, 51)
    assert p.reference == "2026100700000000000000001" and p.note is None and p.card_last4 is None

    # HTML-flattened variant: "Estimado" without "(a)", description on the same line, USD
    body = sinpe_body(amount="125.50 USD", note="Alquiler octubre").replace("Estimado(a)", "Estimada")
    p, _ = parse_email(EmailMessage("<c@d>", SENDER, "", NOW, body.replace("por concepto de:\n\n", "por concepto de: ")),
                       "USD")
    assert (str(p.amount), p.currency, p.note) == ("125.50", "USD", "Alquiler octubre")


def test_account_holder_matching():
    s = Settings(_env_file=None, account_holder="maria lopez, M. F. LOPEZ")
    assert s.is_account_holder("MARIA FERNANDA LOPEZ SOLIS")
    assert s.is_account_holder("María Fernanda López Solís")  # accents and case don't matter
    assert not s.is_account_holder("JUAN CARLOS PEREZ MORA")
    assert not Settings(_env_file=None).is_account_holder("MARIA FERNANDA LOPEZ SOLIS")


def test_transfer_out_is_an_expense_and_transfer_in_is_ignored(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FRAUDALERT_ACCOUNT_HOLDER", "MARIA LOPEZ")
    get_settings.cache_clear()
    r = pipeline.import_eml_files([
        write(tmp_path, "out.eml", sinpe_body(note="Alquiler octubre")),
        write(tmp_path, "in.eml", sinpe_body(payee="MARIA FERNANDA LOPEZ SOLIS", payer="JUAN CARLOS PEREZ MORA",
                                             ref="2026100700000000000000002")),
        write(tmp_path, "small.eml", sinpe_body(amount="500,00 CRC", payee="ANA RUIZ", ref="2026100700000000000000003")),
    ])
    assert (r.parsed, r.failed) == (2, 0)
    with db.session_scope() as s:
        out = s.scalar(select(Transaction).where(Transaction.merchant == "JUAN CARLOS PEREZ MORA"))
        assert out.source == SINPE and out.card_last4 is None and out.comment == "Alquiler octubre"
        assert out.reference == "2026100700000000000000001"
        ignored = s.scalar(select(RawEmail).where(RawEmail.parse_status == "ignored"))
        assert ignored.parse_error == pipeline.TRANSFER_IN
        small = s.scalar(select(Transaction).where(Transaction.merchant == "ANA RUIZ"))
        # 500 colones is under the card-test amount, but a transfer is no card test
        assert not any("Card test" in a.reason for a in small.alerts)
    get_settings.cache_clear()


def test_setting_the_account_holder_later_drops_transfers_in(db, tmp_path, monkeypatch):
    pipeline.import_eml_files([write(tmp_path, "in.eml", sinpe_body(payee="MARIA FERNANDA LOPEZ SOLIS"))])
    with db.session_scope() as s:
        txn = s.scalar(select(Transaction))
        txn.comment = "my note"
    monkeypatch.setenv("FRAUDALERT_ACCOUNT_HOLDER", "MARIA LOPEZ")
    get_settings.cache_clear()
    pipeline.reevaluate_all(reparse="all")
    with db.session_scope() as s:
        assert s.scalar(select(Transaction.id)) is None
        assert s.scalar(select(RawEmail.parse_status)) == "ignored"
    get_settings.cache_clear()


def test_channel_rule_field(db, tmp_path):
    from fraudalert.models import Rule

    with db.session_scope() as s:
        s.add(Rule(name="Big transfer", match="all", severity="high", conditions=[
            {"field": "channel", "op": "eq", "value": "sinpe"}, {"field": "amount", "op": "gt", "value": 50}]))
    pipeline.import_eml_files([
        write(tmp_path, "t.eml", sinpe_body()),
        write(tmp_path, "c.eml", "You spent $90.00 at BIG STORE.", subject="Purchase alert"),
    ])
    with db.session_scope() as s:
        hits = {t.merchant: [a.reason.split(":")[0] for a in t.alerts] for t in s.scalars(select(Transaction))}
    assert "Big transfer" in hits["JUAN CARLOS PEREZ MORA"] and "Big transfer" not in hits["BIG STORE"]


def test_views_show_transfers_as_sinpe(db, tmp_path):
    pipeline.import_eml_files([
        write(tmp_path, "t.eml", sinpe_body()),
        write(tmp_path, "c.eml", "You spent $90.00 at BIG STORE.", subject="Purchase alert"),
    ])
    settings = get_settings()
    with db.session_scope() as s:
        env = pipeline.Env.load(s, settings)
        only = expenses.rows(s, env, expenses.Filter(source="sinpe"))
        cards = expenses.rows(s, env, expenses.Filter(source="card"))
        page = expenses.page(s, env, expenses.Filter())
        from fraudalert.network import build_network

        graph = build_network(s, env, None)
        rep = report.build_report(s, settings, NOW - timedelta(days=1), NOW, report.get_prefs(s, settings))
    assert [r["merchant"] for r in only] == ["JUAN CARLOS PEREZ MORA"] and only[0]["source"] == SINPE
    assert [r["merchant"] for r in cards] == ["BIG STORE"]
    assert page["by_source"][SINPE] > 0 and page["by_source"]["card"] == 90.0
    assert any(n["label"] == "SINPE transfers" for n in graph["nodes"] if n["kind"] == "card")
    assert "JUAN CARLOS PEREZ MORA  SINPE transfer" in rep.text and "MORA  card" not in rep.text

    client = TestClient(create_app(init=False))
    assert '<option value="sinpe">SINPE transfers</option>' in client.get("/expenses").text
    assert client.get("/api/expenses?source=sinpe").json()["count"] == 1
