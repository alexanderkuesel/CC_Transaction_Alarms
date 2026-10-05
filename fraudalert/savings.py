"""The savings loop: monthly saving as a control problem.

    SP   savings target for the month: a fixed amount, or a percentage of the month's income
    PV   projected savings at month end = income - projected spending
    FF   known disturbances, compensated up front (feedforward): recurring income and fixed expenses, so
         the discretionary budget = income - fixed expenses - target
    FB   unknown disturbances (card spending) are corrected by feedback: actual spending is compared with a
         setpoint trajectory (fixed expenses on their days + the budget spread like your usual month)
    OUT  advisory, the person is the final control element: how much can still be spent per day
    D    spending pace over the last 7 days against the plan; a warning signal only, filtered over a week

There is deliberately no integral term: a missed month doesn't raise the next month's target. The status
(OK / HI / HIHI) follows ISA-18.2: HI when projected savings fall more than 15% short of the target, HIHI
when you'd spend more than you earn, each with a deadband so it doesn't chatter as purchases land.
"""

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from fraudalert import spending
from fraudalert.models import RecurringIncome, SyncState

GOAL_KEY = "pref:savings_goal"
STATUS_KEY = "savings_status"
HI_SHORTFALL, HI_CLEAR = 0.15, 0.10  # of the target: alarm above 15% short, clear below 10% (deadband)
HIHI_CLEAR = 0.05  # HIHI (spending more than income) clears once projected savings exceed 5% of the target
PACE_DAYS, PACE_WARN = 7, 1.25  # derivative-like early warning: last week's card spend vs plan
MIN_COVERAGE = 0.5  # never scale card spending up by more than 2x for alerts that were missed


# ---- the setpoint ---------------------------------------------------------------------------------

def get_goal(session: Session) -> dict | None:
    row = session.get(SyncState, GOAL_KEY)
    return json.loads(row.value) if row else None


def set_goal(session: Session, env, data: dict) -> dict:
    """{"mode": "fixed", "amount": 500000, "currency": "CRC"} or {"mode": "percent", "percent": 20}."""
    mode = str(data.get("mode") or "").strip()
    if mode == "fixed":
        amount = _number(data.get("amount"), "amount")
        cur = str(data.get("currency") or env.home_currency).strip().upper()
        if env.fx.rate(cur) is None:
            raise ValueError(f"unknown currency {cur!r}")
        goal = {"mode": "fixed", "amount": amount, "currency": cur}
    elif mode == "percent":
        pct = _number(data.get("percent"), "percent")
        if pct > 100:
            raise ValueError("percent must be between 0 and 100")
        goal = {"mode": "percent", "percent": pct}
    else:
        raise ValueError("mode must be 'fixed' or 'percent'")
    row = session.get(SyncState, GOAL_KEY)
    if row:
        row.value = json.dumps(goal)
    else:
        session.add(SyncState(key=GOAL_KEY, value=json.dumps(goal)))
    return goal


def _number(value, name: str) -> float:
    try:
        v = float(str(value if value is not None else "").replace(",", "").strip())
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if v < 0:
        raise ValueError(f"{name} can't be negative")
    return round(v, 2)


# ---- income: the known disturbance ------------------------------------------------------------------

def _income_fields(session: Session, env, data: dict, current=None) -> dict:
    data = {k: v for k, v in data.items() if k != "category_id"}  # same rules as fixed expenses, no category
    return spending._expense_fields(session, env, data, current)


def create_income(session: Session, env, data: dict) -> RecurringIncome:
    e = RecurringIncome(**_income_fields(session, env, data))
    session.add(e)
    session.flush()
    return e


def update_income(session: Session, env, income_id: int, data: dict) -> RecurringIncome:
    e = session.get(RecurringIncome, income_id)
    if e is None:
        raise LookupError("no such income entry")
    for k, v in _income_fields(session, env, data, e).items():
        setattr(e, k, v)
    session.flush()
    return e


def delete_income(session: Session, income_id: int) -> None:
    e = session.get(RecurringIncome, income_id)
    if e is None:
        raise LookupError("no such income entry")
    session.delete(e)


def list_income(session: Session, env) -> list[dict]:
    rows = session.scalars(select(RecurringIncome).order_by(RecurringIncome.name, RecurringIncome.id)).all()
    return [{"id": e.id, "name": e.name, "amount": float(e.amount), "currency": e.currency,
             "home_amount": env.fx.to_home(float(e.amount), e.currency), "day_of_month": e.day_of_month,
             "start_month": e.start_month.isoformat()[:7],
             "end_month": e.end_month.isoformat()[:7] if e.end_month else None, "note": e.note} for e in rows]


# ---- measurement bias: alerts miss some purchases --------------------------------------------------

def alert_coverage(session: Session, env) -> float | None:
    """Share of the latest card statement's purchases that alert emails captured (home currency), like
    correcting an online analyzer against the last lab sample. None without a card statement."""
    from fraudalert.statements.store import overview

    card = next((s for s in overview(session, env)["statements"] if s["kind"] == "card"), None)
    if card is None:
        return None
    billed = captured = 0.0
    for line in card["lines"]:
        if line["kind"] == "card" and (line.get("debits") or 0) > 0:
            billed += env.fx.to_home(line["debits"], line["currency"])
            captured += env.fx.to_home(line.get("captured") or 0, line["currency"])
    return round(captured / billed, 3) if billed > 0 else None


# ---- the loop --------------------------------------------------------------------------------------

def _cumsum(values):
    out, running = [], 0.0
    for v in values:
        running += v
        out.append(running)
    return out


def compute(session: Session, env, now: datetime | None = None, persist: bool = True) -> dict:
    """One evaluation of the loop for the current month. `persist` stores the alarm state (for the deadband)."""
    inp = spending.month_inputs(session, env, now)
    month, n, today = inp["month"], inp["days"], inp["today"]
    last = month.replace(day=n)

    income_daily = [0.0] * n
    for e in session.scalars(select(RecurringIncome)):
        for d in spending._occurrences(e, month, last):
            income_daily[d.day - 1] += env.fx.to_home(float(e.amount), e.currency)
    income = round(sum(income_daily), 2)

    goal = get_goal(session)
    target = None
    if goal and goal["mode"] == "fixed":
        target = env.fx.to_home(goal["amount"], goal["currency"])
    elif goal and goal["mode"] == "percent":
        target = round(income * goal["percent"] / 100, 2)

    fixed_daily, card_daily = inp["fixed_daily"], inp["card_daily"]
    fixed_month = round(sum(fixed_daily), 2)
    coverage = alert_coverage(session, env)
    correction = 1 / max(coverage, MIN_COVERAGE) if coverage is not None and coverage < 0.98 else 1.0
    card_corr = [v * correction for v in card_daily]
    card_to_date = sum(card_corr[:today])

    # setpoint trajectory: fixed expenses on their days + the discretionary budget spread like a usual month
    usual = inp["usual_card"]
    shape = ([u / usual[-1] for u in usual] if usual and usual[-1] > 0 else [(d + 1) / n for d in range(n)])
    budget = round(income - fixed_month - target, 2) if target is not None else None
    cum_fixed = _cumsum(fixed_daily)
    plan = [round(cf + max(budget or 0, 0) * sh, 2) for cf, sh in zip(cum_fixed, shape)] if budget is not None else None
    actual = [round(v, 2) for v in _cumsum([c + f for c, f in zip(card_corr, fixed_daily)])[:today]]
    spent = actual[-1] if actual else 0.0

    # month-end projection (spending's forecast, card part bias-corrected) and its path for the trend
    raw = inp["projected_total"]
    projected = round((raw - fixed_month) * correction + fixed_month, 2) if raw is not None else None
    forecast = None
    if projected is not None and today < n:
        card_left = projected - spent - (cum_fixed[-1] - cum_fixed[today - 1])
        done = shape[today - 1]
        forecast = [round(spent + (cum_fixed[d] - cum_fixed[today - 1]) +
                          card_left * ((shape[d] - done) / (1 - done) if done < 1 else (d - today + 1) / (n - today)), 2)
                    for d in range(today - 1, n)]

    projected_savings = round(income - projected, 2) if projected is not None else None
    error = round(plan[today - 1] - spent, 2) if plan else None  # P: positive = below plan, room to spare
    days_left = n - today + 1
    allowance = round(max(budget - card_to_date, 0) / days_left, 2) if budget is not None else None
    remaining = round(budget - card_to_date, 2) if budget is not None else None

    # D: last week's card spending against what the plan allowed for that week
    lo = max(today - PACE_DAYS, 0)
    week_actual = sum(card_corr[lo:today])
    week_plan = max(budget or 0, 0) * (shape[today - 1] - (shape[lo - 1] if lo > 0 else 0))
    pace = round(week_actual / week_plan, 2) if budget is not None and week_plan > 0 else None

    status = _status(session, month, target, income, projected_savings, persist)
    return {
        "month": month.isoformat(), "days": n, "today": today, "days_left": days_left, "currency": env.home_currency,
        "goal": goal, "target": target, "income": income, "income_to_date": round(sum(income_daily[:today]), 2),
        "fixed": fixed_month, "budget": budget, "card_to_date": round(card_to_date, 2),
        "card_to_date_captured": round(sum(card_daily[:today]), 2), "coverage": coverage,
        "correction": round(correction, 3), "spent": spent, "plan": plan, "actual": actual, "forecast": forecast,
        "projected_spending": projected, "projected_savings": projected_savings, "error": error,
        "remaining": remaining, "allowance": allowance, "pace": pace, "pace_warning": bool(pace and pace >= PACE_WARN),
        "status": status, "basis": inp["basis"],
        "limits": {"hi": round(income - target * (1 - HI_SHORTFALL), 2), "hihi": income} if target is not None else None,
    }


def _status(session: Session, month: date, target: float | None, income: float, projected: float | None,
            persist: bool) -> str:
    """'setup' (no target or income yet), 'early' (no projection yet), else 'ok' / 'hi' / 'hihi' with deadband."""
    if target is None or income <= 0:
        return "setup"
    if projected is None:
        return "early"
    row = session.get(SyncState, STATUS_KEY)
    prev = json.loads(row.value) if row else {}
    prev_status = prev.get("status") if prev.get("month") == month.isoformat() else "ok"  # each month starts clean
    shortfall = target - projected
    if projected < 0 or (prev_status == "hihi" and projected <= HIHI_CLEAR * target):
        status = "hihi"
    elif shortfall > HI_SHORTFALL * target or (prev_status in ("hi", "hihi") and shortfall >= HI_CLEAR * target):
        status = "hi"
    else:
        status = "ok"
    if persist and (status != prev.get("status") or prev.get("month") != month.isoformat()):
        value = json.dumps({"month": month.isoformat(), "status": status,
                            "since": datetime.now(timezone.utc).isoformat()})
        if row:
            row.value = value
        else:
            session.add(SyncState(key=STATUS_KEY, value=value))
    return status
