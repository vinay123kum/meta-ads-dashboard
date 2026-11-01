"""
Budget pacing & runway. Meta has no monthly-budget concept, so the user sets a
monthly budget per account; we measure month-to-date spend against it and project
whether it lasts the month.

Pacing is anchored to the latest data date (avoids server-clock drift).
"""
import calendar
from datetime import date, timedelta


def get_budgets(con):
    return {r["account_id"]: r["monthly_budget"]
            for r in con.execute("select account_id, monthly_budget from account_budgets").fetchall()}


def set_budget(con, account_id, monthly_budget, now_iso):
    con.execute(
        "insert into account_budgets(account_id, monthly_budget, updated_at) values(?,?,?) "
        "on conflict(account_id) do update set monthly_budget=excluded.monthly_budget, "
        "updated_at=excluded.updated_at", (account_id, monthly_budget, now_iso))
    con.commit()


def _as_of(con):
    r = con.execute(
        "select max(date) mx from insights_daily where campaign_id='ACCOUNT'").fetchone()
    return r["mx"]


def pacing(con, account_id, monthly_budget, as_of=None):
    """Return month-to-date pacing + runway for one account."""
    as_of = as_of or _as_of(con)
    if not as_of or not monthly_budget:
        return None
    y, m, d = map(int, as_of.split("-"))
    month_prefix = f"{y:04d}-{m:02d}"

    rows = con.execute(
        "select spend from insights_daily where account_id=? and campaign_id='ACCOUNT' "
        "and date like ?", (account_id, month_prefix + "%")).fetchall()
    mtd = sum(r["spend"] for r in rows)

    dim = calendar.monthrange(y, m)[1]
    days_elapsed = d
    days_remaining = dim - d

    last7 = con.execute(
        "select spend from insights_daily where account_id=? and campaign_id='ACCOUNT' "
        "order by date desc limit 7", (account_id,)).fetchall()
    avg_daily = (sum(r["spend"] for r in last7) / len(last7)) if last7 else 0.0

    projected = mtd + avg_daily * days_remaining
    pct = (mtd / monthly_budget * 100) if monthly_budget else 0
    runway_days = ((monthly_budget - mtd) / avg_daily) if avg_daily > 0 else None
    will_last = (runway_days is None) or (runway_days >= days_remaining)

    runout = None
    if runway_days is not None and runway_days < days_remaining:
        runout = (date(y, m, d) + timedelta(days=int(runway_days))).isoformat()

    return {
        "monthly_budget": round(monthly_budget, 2),
        "mtd_spend": round(mtd, 2),
        "pct": round(pct, 1),
        "avg_daily": round(avg_daily, 2),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "days_in_month": dim,
        "projected": round(projected, 2),
        "projected_pct": round((projected / monthly_budget * 100) if monthly_budget else 0, 1),
        "runway_days": round(runway_days, 1) if runway_days is not None else None,
        "will_last": will_last,
        "runout_date": runout,
        "as_of": as_of,
    }


def pacing_all(con):
    budgets = get_budgets(con)
    as_of = _as_of(con)
    out = {}
    for aid, mb in budgets.items():
        out[aid] = pacing(con, aid, mb, as_of)
    return out
