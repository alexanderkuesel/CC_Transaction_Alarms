from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fraudalert import pipeline, savings, spending
from fraudalert.config import get_settings
from fraudalert.models import Transaction
from fraudalert.web.app import create_app

NOW = datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)  # day 15 of a 30-day month (New York time)


def env(s):
    return pipeline.Env.load(s, get_settings())


def on(month, day):
    return datetime(2026, month, day, 16, tzinfo=timezone.utc)


def setup_month(db, goal=None):
    """Income 4,000 and rent 1,000 on the 1st; usual card spending 300 on the 1st and 300 on the 20th
    (March-May); this month 300 on June 2."""
    with db.session_scope() as s:
        for m in (3, 4, 5):
            s.add(Transaction(merchant="AUTOMERCADO", amount=300, currency="USD", card_last4="4321", occurred_at=on(m, 1)))
            s.add(Transaction(merchant="AUTOMERCADO", amount=300, currency="USD", card_last4="4321", occurred_at=on(m, 20)))
        s.add(Transaction(merchant="AUTOMERCADO", amount=300, currency="USD", card_last4="4321", occurred_at=on(6, 2)))
        spending.create_expense(s, env(s), {"name": "Rent", "amount": 1000, "day_of_month": 1, "start_month": "2026-01"})
        savings.create_income(s, env(s), {"name": "Salary", "amount": 4000, "day_of_month": 1, "start_month": "2026-01"})
        savings.set_goal(s, env(s), goal or {"mode": "fixed", "amount": 1000})


def test_goal_and_income_validation(db):
    with db.session_scope() as s:
        assert savings.set_goal(s, env(s), {"mode": "fixed", "amount": "500,000", "currency": "usd"}) == \
            {"mode": "fixed", "amount": 500000.0, "currency": "USD"}
        assert savings.get_goal(s)["amount"] == 500000.0
        assert savings.set_goal(s, env(s), {"mode": "percent", "percent": "20"}) == {"mode": "percent", "percent": 20.0}
        for bad, msg in [({"mode": "x"}, "mode"), ({"mode": "percent", "percent": 120}, "between"),
                         ({"mode": "fixed", "amount": "lots"}, "number"), ({"mode": "fixed", "amount": -1}, "negative"),
                         ({"mode": "fixed", "amount": 1, "currency": "XYZ"}, "currency")]:
            with pytest.raises(ValueError, match=msg):
                savings.set_goal(s, env(s), bad)
        e = savings.create_income(s, env(s), {"name": "Salary", "amount": "2,600,000", "currency": "CRC", "day_of_month": 15,
                                              "start_month": "2026-01", "category_id": 99})  # no category for income
        assert (e.name, float(e.amount), e.currency, e.day_of_month) == ("Salary", 2600000.0, "CRC", 15)
        with pytest.raises(ValueError, match="more than zero"):
            savings.create_income(s, env(s), {"name": "x", "amount": 0})
        savings.update_income(s, env(s), e.id, {"end_month": "2026-12"})
        assert savings.list_income(s, env(s))[0]["end_month"] == "2026-12"
        savings.delete_income(s, e.id)
        assert savings.list_income(s, env(s)) == []


def test_the_loop(db):
    setup_month(db)
    with db.session_scope() as s:
        r = savings.compute(s, env(s), now=NOW)
    assert (r["income"], r["target"], r["fixed"], r["budget"]) == (4000, 1000, 1000, 2000)  # feedforward
    assert r["plan"][14] == 2000  # rent + half the budget: by the 15th a usual month has spent half
    assert r["plan"][-1] == 3000  # ends at income - target
    assert (r["spent"], r["card_to_date"], r["error"]) == (1300, 300, 700)  # P: 700 under plan
    assert r["allowance"] == round((2000 - 300) / 16, 2) and r["days_left"] == 16  # OUT
    assert (r["projected_spending"], r["projected_savings"], r["status"]) == (1600, 2400, "ok")  # PV
    assert r["limits"] == {"hi": 3150, "hihi": 4000}
    assert r["forecast"][0] == 1300 and r["forecast"][-1] == 1600


def test_status_hi_hihi_and_deadband(db):
    setup_month(db)

    def status_with_extra(amount):
        with db.session_scope() as s:
            extra = s.scalar(select(Transaction).where(Transaction.merchant == "EXTRA"))
            if extra is None:
                s.add(Transaction(merchant="EXTRA", amount=amount, currency="USD", card_last4="4321", occurred_at=on(6, 10)))
            else:
                extra.amount = amount
        with db.session_scope() as s:
            r = savings.compute(s, env(s), now=NOW)
            return r["status"], r["projected_savings"]

    assert status_with_extra(2350) == ("hi", 50)  # 95% short of the 1,000 target
    assert status_with_extra(2550) == ("hihi", -150)  # spending more than income
    assert status_with_extra(1520) == ("hi", 880)  # HIHI cleared, but 12% short: HI holds (clears below 10%)
    assert status_with_extra(1480) == ("ok", 920)  # 8% short: back to normal
    assert status_with_extra(1520) == ("ok", 880)  # 12% short from OK: inside the deadband, no new alarm


def test_percent_goal_setup_states_and_bias_correction(db, monkeypatch):
    with db.session_scope() as s:
        assert savings.compute(s, env(s), now=NOW)["status"] == "setup"  # nothing configured
    setup_month(db, goal={"mode": "percent", "percent": 20})
    with db.session_scope() as s:
        r = savings.compute(s, env(s), now=NOW)
    assert (r["target"], r["budget"]) == (800, 2200)
    monkeypatch.setattr(savings, "alert_coverage", lambda session, env: 0.8)  # alerts caught 80% last statement
    with db.session_scope() as s:
        r = savings.compute(s, env(s), now=NOW)
    assert (r["correction"], r["card_to_date"], r["card_to_date_captured"]) == (1.25, 375, 300)


def test_savings_api_page_and_overview(db):
    client = TestClient(create_app(init=False))
    assert "<h1>Savings</h1>" in client.get("/savings").text
    assert client.put("/api/savings/goal", json={"mode": "nope"}).status_code == 422
    assert client.put("/api/savings/goal", json={"mode": "percent", "percent": 25}).json() == {"mode": "percent", "percent": 25.0}
    r = client.post("/api/savings/income", json={"name": "Salary", "amount": "5000", "day_of_month": "1"})
    assert r.status_code == 201
    iid = r.json()["id"]
    assert client.post("/api/savings/income", json={"name": "x", "amount": "abc"}).status_code == 422
    assert client.patch(f"/api/savings/income/{iid}", json={"amount": "5200"}).status_code == 200
    data = client.get("/api/savings").json()
    assert data["income"] == 5200 and data["target"] == 1300 and data["income_entries"][0]["name"] == "Salary"
    assert "Savings this month" in client.get("/").text
    assert client.delete(f"/api/savings/income/{iid}").status_code == 204
    assert client.delete(f"/api/savings/income/{iid}").status_code == 404
    assert client.get("/api/savings").json()["status"] == "setup"
