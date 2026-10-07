"""The anomaly lens: what the model "sees", in your units instead of feature vectors.

The detector scores 11 numbers per purchase (see features.py), several of them derived (hour as sin/cos,
amount as a log and as two z-scores). This module lets the web page ask "what if" questions in plain
terms: *this purchase, but at 3 am* / *for 10x the amount* / *at a merchant seen 20 times before*. Each
"knob" below maps one human quantity onto the features it drives, so a change stays self-consistent
(moving the amount also moves both z-scores against the same history the purchase was scored with).

* `slice_` scores a 2-D grid over two knobs with everything else held at a purchase's values (a
  partial-dependence slice through that purchase), plus a 1-D curve per knob (individual conditional
  expectation), plus the existing explanation of the purchase's own score.
* `points` returns every scored purchase in knob units, to plot on top of the landscape.

Scores are the detector's own: percentiles of your history for the Isolation Forest (0.98 = odder than
98% of your purchases), the baseline's squashed score otherwise.
"""

import math
import statistics
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from fraudalert.anomaly.explain import GROUPS, NEUTRAL, WEEKDAYS, explain
from fraudalert.anomaly.features import MIN_LOG_SCALE, merchant_key

TAU = 2 * math.pi
POINTS_LIMIT = 3000


def _log(v: float) -> float:
    return math.log1p(max(v, 0.0))


def _geom(lo: float, hi: float, n: int) -> list[float]:
    return [lo * (hi / lo) ** (i / (n - 1)) for i in range(n)]


@dataclass
class Knob:
    key: str
    label: str
    unit: str
    log: bool = False  # draw on a log axis
    discrete: bool = False


KNOBS = [
    Knob("hour", "Time of day", "h"),
    Knob("amount", "Amount", "home", log=True),
    Knob("repeats", "Earlier purchases at this merchant", "purchases", log=True, discrete=True),
    Knob("weekday", "Day of week", "", discrete=True),
    Knob("gap", "Hours since your previous purchase", "h", log=True),
    Knob("burst", "Other purchases in the past 24 h", "purchases", discrete=True),
    Knob("foreign", "Abroad or unusual currency", "", discrete=True),
]
KNOB = {k.key: k for k in KNOBS}
# Which explanation group (explain.GROUPS) each knob drives, so the page can line them up.
KNOB_GROUP = {"hour": "time", "amount": "amount", "repeats": "merchant", "weekday": "weekday", "gap": "gap",
              "burst": "burst", "foreign": "foreign"}


@dataclass
class _Stats:
    """Median and spread of log amounts, as features._robust_z computes them (None: under 3 values)."""
    med: float | None = None
    scale: float | None = None

    @classmethod
    def of(cls, values: list[float]) -> "_Stats":
        if len(values) < 3:
            return cls()
        med = statistics.median(values)
        mad = statistics.median(abs(v - med) for v in values)
        return cls(med, max(1.4826 * mad, MIN_LOG_SCALE))

    def z(self, log_amount: float) -> float:
        if self.med is None:
            return 0.0
        return max(-10.0, min(10.0, (log_amount - self.med) / self.scale))


@dataclass
class Base:
    """A purchase as the model saw it: its features, plus the history statistics needed to move the amount."""
    features: dict
    global_stats: _Stats
    merchant_stats: _Stats
    merchant_count: int
    txn: dict | None = None  # the purchase this is, for the page (None = a typical purchase)
    extra: dict = field(default_factory=dict)


def human(f: dict) -> dict:
    """Knob values for a feature dict."""
    return {
        "hour": round((math.atan2(f.get("hour_sin", 0.0), f.get("hour_cos", 1.0)) / TAU * 24) % 24, 4),
        "amount": round(math.expm1(f.get("log_amount", 0.0)), 4),
        "repeats": round(math.expm1(f.get("merchant_seen_log", 0.0))),
        "weekday": round((math.atan2(f.get("dow_sin", 0.0), f.get("dow_cos", 1.0)) % TAU) / TAU * 7) % 7,
        "gap": round(math.expm1(f.get("hours_since_prev_log", 0.0)), 4),
        "burst": int(f.get("txns_last_24h", 0.0)),
        "foreign": int(f.get("is_foreign", 0.0) >= 0.5),
    }


def apply(base: Base, knob: str, value: float) -> dict:
    """`base.features` with one knob set to `value` (human units), keeping derived features consistent."""
    f = dict(base.features)
    if knob == "hour":
        f["hour_sin"], f["hour_cos"] = math.sin(TAU * value / 24), math.cos(TAU * value / 24)
    elif knob == "weekday":
        f["dow_sin"], f["dow_cos"] = math.sin(TAU * value / 7), math.cos(TAU * value / 7)
    elif knob == "amount":
        la = _log(value)
        f["log_amount"] = la
        f["amount_z_global"] = base.global_stats.z(la)
        if base.merchant_count >= 3:
            f["amount_z_merchant"] = base.merchant_stats.z(la)
    elif knob == "repeats":
        n = max(int(round(value)), 0)
        f["merchant_seen_log"] = _log(n)
        if n < 3:
            f["amount_z_merchant"] = 0.0  # under 3 earlier purchases there is no merchant price to compare with
        elif base.merchant_count < 3:
            f["amount_z_merchant"] = 0.0  # imagined history: assume you paid what you usually pay there
    elif knob == "gap":
        f["hours_since_prev_log"] = _log(value)
    elif knob == "burst":
        f["txns_last_24h"] = float(max(int(round(value)), 0))
    elif knob == "foreign":
        f["is_foreign"] = 1.0 if value >= 0.5 else 0.0
    else:
        raise ValueError(f"unknown knob {knob!r}")
    return f


def apply_many(base: Base, values: dict) -> dict:
    f = base.features
    for knob, value in values.items():
        f = apply(Base(f, base.global_stats, base.merchant_stats, base.merchant_count), knob, value)
    return f


# ---- the detector, as a batch scorer ----

class Scorer:
    def __init__(self, detector):
        self.name = detector.name
        model = getattr(detector, "model", None)
        self.model = model
        self.typical = (getattr(model, "medians", None) or NEUTRAL) if model is not None else NEUTRAL
        self._one = detector.score
        self._explain = detector.explain

    @property
    def isolation_forest(self) -> bool:
        return self.model is not None

    def score(self, rows: list[dict]) -> list[float | None]:
        if self.model is not None:
            return self.model.score_many(rows)
        return [self._one(r) for r in rows]

    def explain(self, features: dict) -> list[dict]:
        return self._explain(features) or []


# ---- loading purchases ----

def _home(env, t) -> float:
    return env.fx.to_home(float(t.amount), t.currency)


def load_base(session: Session, env, scorer: Scorer, txn_id: int | None) -> Base:
    """The purchase `txn_id` with the history it was scored against; None = a typical purchase (the model's
    medians, against all of your history)."""
    from fraudalert.models import Transaction
    from fraudalert.pipeline import HISTORY_LIMIT

    if txn_id is None:
        amounts = [_log(_home(env, t)) for t in session.scalars(
            select(Transaction).order_by(Transaction.occurred_at.desc()).limit(HISTORY_LIMIT))]
        typical = {**NEUTRAL, **scorer.typical, "history_size_log": _log(len(amounts))}
        return Base(typical, _Stats.of(amounts), _Stats(), round(math.expm1(typical.get("merchant_seen_log", 0.0))))
    t = session.get(Transaction, txn_id)
    if t is None or not t.features:
        raise LookupError(f"no scored purchase {txn_id}")
    history = session.scalars(select(Transaction).where(Transaction.occurred_at < t.occurred_at, Transaction.id != t.id)
                              .order_by(Transaction.occurred_at.desc()).limit(HISTORY_LIMIT)).all()
    key = merchant_key(t.merchant or "")
    same = [_log(_home(env, h)) for h in history if merchant_key(h.merchant or "") == key]
    return Base(dict(t.features), _Stats.of([_log(_home(env, h)) for h in history]), _Stats.of(same), len(same),
                txn=_txn_json(env, t))


def _txn_json(env, t) -> dict:
    from fraudalert.ingest.parsers import SINPE
    from fraudalert.web.filters import txn_priority, txn_state

    return {"id": t.id, "merchant": t.merchant or "", "when": t.occurred_at.isoformat(), "amount": float(t.amount),
            "currency": t.currency, "home": round(_home(env, t), 2), "score": t.anomaly_score, "state": txn_state(t),
            "priority": txn_priority(t), "sinpe": t.source == SINPE, "card": t.card_last4,
            "reasons": t.anomaly_reasons or []}


def points(session: Session, env, limit: int = POINTS_LIMIT) -> list[dict]:
    """Recent scored purchases in knob units: the dots on the landscape."""
    from fraudalert.models import Transaction
    from sqlalchemy.orm import selectinload

    rows = session.scalars(select(Transaction).options(selectinload(Transaction.alerts))
                           .where(Transaction.features.is_not(None))
                           .order_by(Transaction.occurred_at.desc()).limit(limit)).all()
    return [{**_txn_json(env, t), "k": human(t.features)} for t in rows]


# ---- the slice ----

def axis_values(knob: str, base: Base, pts: list[dict], n: int) -> list[float]:
    """Sample positions along a knob, wide enough to cover your purchases and the purchase being looked at."""
    if knob == "hour":
        return [24 * i / n for i in range(n + 1)]
    if knob == "weekday":
        return list(range(7))
    if knob == "foreign":
        return [0, 1]
    if knob == "burst":
        top = max([p["k"]["burst"] for p in pts] + [human(base.features)["burst"], 8])
        return list(range(0, min(top + 3, 40) + 1))
    if knob == "repeats":
        top = max([p["k"]["repeats"] for p in pts] + [human(base.features)["repeats"], 10])
        return sorted({0, 1, 2, 3, 4, 5} | {round(v) for v in _geom(1, top * 1.5, n)})
    if knob == "amount":
        seen = [p["k"]["amount"] for p in pts if p["k"]["amount"] > 0] + [max(human(base.features)["amount"], 0.01)]
        lo, hi = max(min(seen) / 3, 0.1), max(seen) * 4
        return [round(v, 2) for v in _geom(lo, hi, n)]
    if knob == "gap":
        return [round(v, 3) for v in _geom(0.05, 24 * 60, n)]
    raise ValueError(f"unknown knob {knob!r}")


def slice_(session: Session, env, scorer: Scorer, txn_id: int | None, x: str, y: str, n: int = 40) -> dict:
    if x not in KNOB or y not in KNOB or x == y:
        raise ValueError("x and y must be two different knobs: " + ", ".join(KNOB))
    base = load_base(session, env, scorer, txn_id)
    pts = points(session, env)
    xs, ys = axis_values(x, base, pts, n), axis_values(y, base, pts, n)
    rows = [apply_many(base, {x: xv, y: yv}) for yv in ys for xv in xs]
    curves = {}
    for k in KNOBS:
        vals = axis_values(k.key, base, pts, 60)
        curves[k.key] = {"xs": vals, "scores": None}
        rows += [apply(base, k.key, v) for v in vals]
    scores = scorer.score([base.features] + rows)
    own, grid_flat, rest = scores[0], scores[1:1 + len(xs) * len(ys)], scores[1 + len(xs) * len(ys):]
    for k in KNOBS:
        m = len(curves[k.key]["xs"])
        curves[k.key]["scores"], rest = rest[:m], rest[m:]
    reasons = scorer.explain(base.features)
    return {
        "x": x, "y": y, "xs": xs, "ys": ys,
        "grid": [grid_flat[i * len(xs):(i + 1) * len(xs)] for i in range(len(ys))],
        "base": {"k": human(base.features), "score": own, "txn": base.txn,
                 "typical": human({**NEUTRAL, **scorer.typical})},
        "curves": curves,
        "reasons": [{**r, "knob": next((kk for kk, g in KNOB_GROUP.items() if g == r["key"]), None)} for r in reasons],
    }


def knobs_json() -> list[dict]:
    return [{"key": k.key, "label": k.label, "unit": k.unit, "log": k.log, "discrete": k.discrete} for k in KNOBS]


__all__ = ["KNOBS", "Scorer", "slice_", "points", "knobs_json", "human", "apply", "WEEKDAYS", "GROUPS"]
