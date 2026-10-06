from datetime import datetime, timezone

from fastapi.testclient import TestClient

from fraudalert import expenses, pipeline, spending
from fraudalert.config import get_settings
from fraudalert.models import Transaction
from fraudalert.web.app import create_app


def on(month, day, hour=16):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


def seed(db):
    with db.session_scope() as s:
        env = pipeline.Env.load(s, get_settings())
        spending.seed_categories(s)
        s.add(Transaction(merchant="UBER EATS", amount=12.5, currency="USD", card_last4="4321", occurred_at=on(9, 3)))
        s.add(Transaction(merchant="AUTOMERCADO", amount=50000, currency="CRC", card_last4="1111", occurred_at=on(9, 10)))
        s.add(Transaction(merchant="BIG TV", amount=900, currency="USD", card_last4="4321", occurred_at=on(9, 12),
                          flagged=True, label_fraud=True, comment="not me"))
        s.add(Transaction(merchant="NETFLIX.COM", amount=15.49, currency="USD", card_last4="4321", occurred_at=on(8, 20)))
        spending.create_expense(s, env, {"name": "Rent", "amount": 1000, "day_of_month": 1, "start_month": "2026-08",
                                         "end_month": "2026-09"})


def test_rows_filters_and_totals(db):
    seed(db)
    with db.session_scope() as s:
        env = pipeline.Env.load(s, get_settings())
        sept = expenses.Filter(start=datetime(2026, 9, 1).date(), end=datetime(2026, 9, 30).date())
        page = expenses.page(s, env, sept)
        assert page["count"] == 4  # 3 card purchases + rent on Sep 1
        assert [r["merchant"] for r in page["rows"]] == ["BIG TV", "AUTOMERCADO", "UBER EATS", "Rent"]  # newest first
        crc = next(r for r in page["rows"] if r["merchant"] == "AUTOMERCADO")
        assert (crc["category"], crc["source"], crc["home"]) == ("Groceries", "card", env.fx.to_home(50000, "CRC"))
        assert page["fraud_excluded"] == 1 and page["total"] == round(12.5 + crc["home"] + 1000, 2)  # BIG TV not counted
        assert page["by_source"]["fixed"] == 1000

        def merchants(**kw):
            f = expenses.Filter(start=sept.start, end=sept.end, **kw)
            return [r["merchant"] for r in expenses.rows(s, env, f)]

        assert merchants(card="4321") == ["BIG TV", "UBER EATS"]  # fixed expenses have no card
        assert merchants(source="fixed") == ["Rent"]
        assert merchants(q="not me") == ["BIG TV"]  # comments are searchable
        # amounts filter in the home currency (50,000 CRC = 100 USD); rows marked as fraud stay listed
        assert merchants(amin=100, sort="amount", desc=False) == ["AUTOMERCADO", "BIG TV", "Rent"]
        dining = next(cid for cid, in s.execute(spending.select(spending.Category.id).where(spending.Category.name == "Dining")))
        assert merchants(category=str(dining)) == ["UBER EATS"]
        assert merchants(sort="merchant", desc=False)[0] == "AUTOMERCADO"
        assert len(expenses.rows(s, env, expenses.Filter())) == 6  # everything: + Netflix and August's rent
        csv = expenses.to_csv(expenses.rows(s, env, sept), "USD")
        assert csv.splitlines()[0].startswith("date,merchant,category,amount,currency,amount_usd") and "BIG TV" in csv


def test_expenses_page_api_and_csv(db):
    seed(db)
    client = TestClient(create_app(init=False))
    page = client.get("/expenses").text
    assert "<h1>Expenses</h1>" in page and "expenses.js" in page and "…4321" in page
    d = client.get("/api/expenses?from=2026-09-01&to=2026-09-30&sort=amount&dir=desc&limit=2").json()
    assert d["count"] == 4 and len(d["rows"]) == 2 and d["rows"][0]["merchant"] == "Rent"
    assert client.get("/api/expenses?from=nope").status_code == 422
    assert client.get("/api/expenses?sort=sideways").status_code == 422
    assert client.get("/api/expenses?category=abc").status_code == 422
    r = client.get("/expenses.csv?from=2026-09-01&to=2026-09-30&source=card")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Rent" not in r.text and "UBER EATS" in r.text
