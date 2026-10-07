"""The anomaly lens: what-if scoring in human units, consistent with how purchases are actually scored."""

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fraudalert import pipeline
from fraudalert.anomaly import get_detector
from fraudalert.anomaly.lens import KNOBS, Scorer, apply, human, load_base, slice_
from fraudalert.config import get_settings
from fraudalert.models import Transaction
from fraudalert.web.app import create_app

from .conftest import make_eml

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def history(db, tmp_path):
    files = []
    for i in range(40):  # a coffee habit around 2 pm, plus a few groceries
        when = (NOW - timedelta(days=40 - i)).replace(hour=14, minute=5)
        p = tmp_path / f"c{i}.eml"
        p.write_bytes(make_eml("Alert", f"You spent ${4 + i % 3}.50 at CAFE CENTRAL.", when))
        files.append(p)
    for i in range(8):
        p = tmp_path / f"g{i}.eml"
        p.write_bytes(make_eml("Alert", f"You spent ${60 + i}.00 at GROCER.", NOW - timedelta(days=5 * i + 1, hours=3)))
        files.append(p)
    p = tmp_path / "odd.eml"
    p.write_bytes(make_eml("Alert", "You spent $900.00 at NEW GADGETS.", (NOW - timedelta(hours=2))))
    files.append(p)
    pipeline.import_eml_files(files)
    return db


def _base(s, txn_id):
    env = pipeline.Env.load(s, get_settings())
    return env, Scorer(get_detector("baseline")), load_base(s, pipeline.Env.load(s, get_settings()),
                                                             Scorer(get_detector("baseline")), txn_id)


def test_knobs_reproduce_the_stored_features(history):
    """Setting a purchase's knobs to its own values must give back exactly the features it was scored with."""
    with history.session_scope() as s:
        t = s.scalar(select(Transaction).where(Transaction.merchant == "CAFE CENTRAL").order_by(Transaction.id.desc()))
        _, _, base = _base(s, t.id)
        k = human(t.features)
        assert k["repeats"] == 39 and abs(k["amount"] - float(t.amount)) < 0.01
        for knob in ("hour", "weekday", "amount", "gap", "burst", "foreign"):
            again = apply(base, knob, k[knob])
            for name, v in t.features.items():
                assert again[name] == pytest.approx(v, abs=1e-3), (knob, name)  # knob values are rounded for display


def test_amount_knob_moves_both_z_scores(history):
    with history.session_scope() as s:
        t = s.scalar(select(Transaction).where(Transaction.merchant == "CAFE CENTRAL").order_by(Transaction.id.desc()))
        _, _, base = _base(s, t.id)
        big = apply(base, "amount", 400.0)
        assert big["log_amount"] == pytest.approx(math.log1p(400))
        assert big["amount_z_merchant"] == 10.0 and big["amount_z_global"] > 2  # far above the usual coffee
        first = apply(base, "repeats", 0)
        assert first["merchant_seen_log"] == 0 and first["amount_z_merchant"] == 0  # no merchant price to compare


def test_slice_scores_grid_curves_and_reasons(history):
    with history.session_scope() as s:
        env = pipeline.Env.load(s, get_settings())
        scorer = Scorer(get_detector("baseline"))
        odd = s.scalar(select(Transaction.id).where(Transaction.merchant == "NEW GADGETS"))
        d = slice_(s, env, scorer, odd, "hour", "amount", n=12)
        assert len(d["grid"]) == len(d["ys"]) and len(d["grid"][0]) == len(d["xs"]) == 13
        assert d["base"]["txn"]["merchant"] == "NEW GADGETS" and d["base"]["score"] is not None
        assert set(d["curves"]) == {k.key for k in KNOBS}
        amt = d["curves"]["amount"]
        assert amt["scores"][-1] > amt["scores"][0]  # a bigger amount is more unusual
        assert any(r["knob"] == "amount" for r in d["reasons"])
        typical = slice_(s, env, scorer, None, "repeats", "gap", n=8)
        assert typical["base"]["txn"] is None and typical["base"]["score"] is not None
        with pytest.raises(ValueError):
            slice_(s, env, scorer, None, "hour", "hour")


def test_lens_page_and_api(history):
    client = TestClient(create_app(init=False))
    page = client.get("/network").text
    assert "Anomaly lens" in page and "lens.js" in page and '"key": "hour"' in page
    pts = client.get("/api/lens/points").json()
    assert len(pts["points"]) == 49 and pts["limit"] == 0.97 and "hour" in pts["points"][0]["k"]
    odd = next(p for p in pts["points"] if p["merchant"] == "NEW GADGETS")
    d = client.get(f"/api/lens/slice?x=amount&y=repeats&txn={odd['id']}").json()
    assert d["x"] == "amount" and d["base"]["txn"]["id"] == odd["id"]
    assert client.get("/api/lens/slice?x=hour&y=hour").status_code == 422
    assert client.get("/api/lens/slice?txn=999999").status_code == 404
    assert client.get("/static/lens.js").status_code == 200
