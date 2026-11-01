"""
Phase 1.5 dashboard backend (FastAPI). Account-level drill-down with period
selection, vs-previous deltas, and computed recommendations.
Run:  python app.py  -> http://localhost:8000
"""
from pathlib import Path

import os
import sys
import base64
import secrets
import threading
import subprocess
from datetime import datetime, timezone, date, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

from config import AUTH_USER, AUTH_PASS, HOST, PORT

import db
import intelligence as intel
import fatigue as fat_mod
import meta_write
import alerts as alert_mod
import budget as budget_mod
import guardrails as guard_mod
import sales_sync
import budget_import
from config import SYNC_HOUR

# default to Safe (dry-run): actions are logged but NOT sent to Meta until flipped
SAFE_MODE = {"on": True}
SYNC_STATE = {"running": False, "last_run": None, "last_result": None}
BASE = Path(__file__).parent
_scheduler = BackgroundScheduler()


def run_pipeline():
    """Full refresh: sync from Meta (subprocess) then scan + deliver alerts."""
    if SYNC_STATE["running"]:
        return
    SYNC_STATE["running"] = True
    try:
        r = subprocess.run([sys.executable, str(BASE / "sync.py")], cwd=str(BASE),
                           capture_output=True, text=True, timeout=1200)
        ok = r.returncode == 0
        con = db.connect()
        res = alert_mod.run(con)
        con.close()
        SYNC_STATE["last_result"] = {"sync_ok": ok, **res}
    except Exception as e:
        SYNC_STATE["last_result"] = {"error": str(e)[:120]}
    finally:
        SYNC_STATE["running"] = False
        SYNC_STATE["last_run"] = datetime.now(timezone.utc).isoformat()


def scan_alerts_only():
    con = db.connect()
    res = alert_mod.run(con)
    con.close()
    return res


def _fatigue_for(con, acct_id, lookback=14):
    rows = [dict(r) for r in con.execute(
        "select cd.*, c.name, c.is_video from creative_daily cd "
        "left join creatives c on c.id=cd.ad_id where cd.account_id=?", (acct_id,)).fetchall()]
    return fat_mod.analyze(rows, {"lookback": lookback})

app = FastAPI(title="Agency Dashboard")
STATIC = Path(__file__).parent / "static"
db.init()  # ensure schema (incl. actions_log, alerts) exists


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """Optional HTTP Basic auth — active only when AUTH_USER/AUTH_PASS are set."""
    if AUTH_USER and AUTH_PASS:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                u, p = base64.b64decode(hdr[6:]).decode("utf-8").split(":", 1)
                ok = (secrets.compare_digest(u, AUTH_USER)
                      and secrets.compare_digest(p, AUTH_PASS))
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Agency Dashboard"'})
    return await call_next(request)


@app.on_event("startup")
def _startup():
    try:
        scan_alerts_only()  # surface alerts immediately from existing data
    except Exception as e:
        print(f"startup alert scan err: {e}")
    _scheduler.add_job(run_pipeline, "cron", hour=SYNC_HOUR, id="daily_sync",
                       replace_existing=True)
    _scheduler.start()
    print(f"Scheduler started — daily auto-sync at {SYNC_HOUR:02d}:00 local")

PALETTE = ["#2563eb", "#0d9488", "#db2777", "#d97706", "#7c3aed", "#0891b2",
           "#dc2626", "#65a30d", "#c026d3", "#ea580c", "#4f46e5", "#059669"]


def _color(s):
    return PALETTE[sum(ord(c) for c in (s or "x")) % len(PALETTE)]


def _initials(name):
    parts = [p for p in (name or "?").split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def _agg(rows):
    sp = sum(r["spend"] for r in rows)
    im = sum(r["impressions"] for r in rows)
    ck = sum(r["clicks"] for r in rows)
    cv = sum(r["conversions"] for r in rows)
    return {"spend": sp, "impressions": im, "clicks": ck, "conversions": cv,
            "ctr": (ck / im * 100) if im else 0,
            "cpc": (sp / ck) if ck else 0,
            "cpa": (sp / cv) if cv else 0,
            "days": len({r["date"] for r in rows})}


def _delta(cur, prev, key):
    if not prev or not prev.get(key):
        return None
    return round((cur[key] - prev[key]) / prev[key] * 100, 1)


@app.get("/api/accounts")
def accounts():
    con = db.connect()
    accts = con.execute("select * from accounts order by name").fetchall()
    out = []
    for a in accts:
        rows = con.execute(
            "select * from insights_daily where account_id=? and campaign_id='ACCOUNT'",
            (a["id"],)).fetchall()
        spend30 = sum(r["spend"] for r in rows)
        cur = _agg(rows)
        camps = _campaign_rows(con, a["id"], {r["date"] for r in rows})
        h = intel.health(a, cur, None, camps)
        fat = _fatigue_for(con, a["id"])
        issues = sum(1 for c in camps if c["spend"] >= intel.DEAD_SPEND and c["conversions"] == 0)
        if a["status"] != "ACTIVE":
            issues += 1
        issues += fat["summary"]["high"]
        out.append({"id": a["id"], "name": a["name"], "status": a["status"],
                    "currency": a["currency"], "color": _color(a["id"]),
                    "initials": _initials(a["name"]), "spend30": round(spend30, 2),
                    "health": h["score"], "issues": issues,
                    "fatigue_high": fat["summary"]["high"], "fatigue_med": fat["summary"]["med"]})
    con.close()
    return JSONResponse(out)


def _campaign_rows(con, acct_id, date_set):
    if not date_set:
        return []
    placeholders = ",".join("?" for _ in date_set)
    rows = con.execute(
        f"select i.campaign_id, sum(i.spend) spend, sum(i.impressions) impressions, "
        f"sum(i.clicks) clicks, sum(i.conversions) conversions "
        f"from insights_daily i where i.account_id=? and i.campaign_id!='ACCOUNT' "
        f"and i.date in ({placeholders}) group by i.campaign_id",
        [acct_id, *date_set]).fetchall()
    camp_meta = {c["id"]: c for c in con.execute(
        "select id,name,status,daily_budget from campaigns where account_id=?", (acct_id,)).fetchall()}
    out = []
    for r in rows:
        meta = camp_meta.get(r["campaign_id"])
        name = meta["name"] if meta else r["campaign_id"]
        status = meta["status"] if meta else "?"
        ctr = (r["clicks"] / r["impressions"] * 100) if r["impressions"] else 0
        cpa = (r["spend"] / r["conversions"]) if r["conversions"] else 0
        out.append({"id": r["campaign_id"], "name": name, "status": status,
                    "daily_budget": meta["daily_budget"] if meta else 0,
                    "spend": r["spend"], "impressions": r["impressions"],
                    "clicks": r["clicks"], "conversions": r["conversions"],
                    "ctr": ctr, "cpa": cpa})
    return sorted(out, key=lambda c: c["spend"], reverse=True)


@app.get("/api/account/{acct_id}")
def account(acct_id: str, period: int = 7, start: str = None, end: str = None):
    con = db.connect()
    a = con.execute("select * from accounts where id=?", (acct_id,)).fetchone()
    if not a:
        con.close()
        return JSONResponse({"error": "not found"}, status_code=404)

    acct_rows = con.execute(
        "select * from insights_daily where account_id=? and campaign_id='ACCOUNT' order by date",
        (acct_id,)).fetchall()
    dates = sorted({r["date"] for r in acct_rows})
    if start and end:
        # explicit calendar range; previous = equal-length window just before start
        cur_dates = {d for d in dates if start <= d <= end}
        before = [d for d in dates if d < start]
        prev_dates = set(before[-len(cur_dates):]) if cur_dates else set()
        window_label = f"{start} → {end}"
    else:
        # calendar window anchored to the latest data date across ALL accounts,
        # so "last N days" means real calendar days. An account with no delivery
        # in that window correctly shows 0 instead of reaching back to old rows.
        anchor = con.execute(
            "select max(date) mx from insights_daily where campaign_id='ACCOUNT'"
        ).fetchone()["mx"]
        if anchor:
            ad = date.fromisoformat(anchor)
            cur_dates = {(ad - timedelta(days=i)).isoformat() for i in range(period)}
            prev_dates = {(ad - timedelta(days=i)).isoformat()
                          for i in range(period, 2 * period)}
        else:
            cur_dates, prev_dates = set(), set()
        window_label = f"last {period} days"

    cur_rows = [r for r in acct_rows if r["date"] in cur_dates]
    prev_rows = [r for r in acct_rows if r["date"] in prev_dates]
    cur = _agg(cur_rows)
    prev = _agg(prev_rows) if prev_rows else None

    campaigns = _campaign_rows(con, acct_id, cur_dates)

    # geo for current period
    if cur_dates:
        ph = ",".join("?" for _ in cur_dates)
        geo = [dict(g) for g in con.execute(
            f"select country, round(sum(spend),2) spend, sum(impressions) impressions, "
            f"sum(clicks) clicks, sum(conversions) conversions from geo_daily "
            f"where account_id=? and date in ({ph}) group by country "
            f"order by spend desc limit 12", [acct_id, *cur_dates]).fetchall()]
    else:
        geo = []

    h = intel.health(a, cur, prev, campaigns)
    insight = intel.analyze(a, cur, prev, campaigns, geo)

    budgets = budget_mod.get_budgets(con)
    pacing = budget_mod.pacing(con, acct_id, budgets[acct_id]) if acct_id in budgets else None
    guard = guard_mod.account_guard(con, acct_id)

    # ---- creative fatigue (rolling 14d, independent of selected period) ----
    fat = _fatigue_for(con, acct_id)
    high = fat["summary"]["high"]
    if high:
        pen = min(35, high * 12)
        h["sub"]["Creative"] = max(20, h["sub"]["Creative"] - pen)
        h["score"] = max(10, h["score"] - round(pen * 0.25))
        h["label"] = intel._label(h["score"])
        top = next(i for i in fat["items"] if i["risk"] == "HIGH")
        insight["recommendations"].insert(0, {
            "priority": "HIGH",
            "title": f'Refresh {high} fatiguing creative' + ("s" if high > 1 else ""),
            "detail": f'Worst: "{top["name"][:50]}" — {", ".join(top["reasons"][:2])}.',
            "impact": "restore CTR / lower CPM"})
        for it in fat["items"]:
            if it["risk"] == "HIGH":
                insight["not_working"].append(
                    f'Creative "{it["name"][:38]}" fatiguing — {", ".join(it["reasons"][:2])}')
        insight["not_working"] = insight["not_working"][:8]
        insight["recommendations"] = insight["recommendations"][:8]

    kpi_cfg = [("spend", "Spend", "$", False), ("conversions", "Conversions", "", True),
               ("cpa", "Cost / Result", "$", True), ("ctr", "CTR", "%", True),
               ("cpc", "CPC", "$", True), ("clicks", "Clicks", "", True),
               ("impressions", "Impressions", "", True)]
    # for cpa/cpc, "good" direction is DOWN, so up_good=False
    good_down = {"cpa", "cpc", "spend"}
    kpis = []
    for key, label, unit, _ in kpi_cfg:
        kpis.append({"key": key, "label": label, "unit": unit,
                     "value": round(cur[key], 2),
                     "delta": _delta(cur, prev, key),
                     "up_good": key not in good_down})

    trend = [{"date": r["date"], "spend": round(r["spend"], 2),
              "conv": round(r["conversions"], 1)} for r in cur_rows]
    con.close()

    return JSONResponse({
        "account": {"id": a["id"], "name": a["name"], "status": a["status"],
                    "currency": a["currency"], "color": _color(a["id"]),
                    "initials": _initials(a["name"])},
        "period": period, "days": len(cur_dates), "window_label": window_label,
        "health": h, "kpis": kpis, "trend": trend,
        "working": insight["working"], "not_working": insight["not_working"],
        "recommendations": insight["recommendations"],
        "campaigns": [{**c, "spend": round(c["spend"], 2), "ctr": round(c["ctr"], 2),
                       "cpa": round(c["cpa"], 2)} for c in campaigns[:15]],
        "geo": geo,
        "fatigue": fat,
        "pacing": pacing,
        "guard": guard,
    })


@app.post("/api/sync")
def trigger_sync():
    if SYNC_STATE["running"]:
        return {"started": False, "reason": "already running"}
    threading.Thread(target=run_pipeline, daemon=True).start()
    return {"started": True}


@app.get("/api/sync/status")
def sync_status():
    con = db.connect()
    row = con.execute("select max(updated_at) mx from accounts").fetchone()
    con.close()
    return {"running": SYNC_STATE["running"], "last_sync": row["mx"],
            "last_run": SYNC_STATE["last_run"], "last_result": SYNC_STATE["last_result"]}


@app.get("/api/sales")
def sales():
    con = db.connect()
    rows = con.execute(
        "select location, count(*) sales, min(date) first, max(date) last "
        "from sales group by location order by sales desc").fetchall()
    total = con.execute("select count(*) n from sales").fetchone()["n"]
    paid = con.execute(
        "select count(*) n from sales where utm_campaign is not null and utm_campaign!=''").fetchone()["n"]
    meta = {r["k"]: r["v"] for r in con.execute(
        "select k,v from app_meta where k in ('sales_last_sync','sales_stats')").fetchall()}
    con.close()
    import json as _json
    return JSONResponse({
        "locations": [dict(r) for r in rows], "total": total, "attributed": paid,
        "last_sync": meta.get("sales_last_sync"),
        "stats": _json.loads(meta.get("sales_stats", "{}")),
    })


@app.post("/api/sales/sync")
def sales_sync_now():
    con = db.connect()
    res = sales_sync.sync(con)
    con.close()
    return JSONResponse(res)


@app.get("/api/budgets")
def list_budgets():
    con = db.connect()
    accts = con.execute("select id, name, currency from accounts order by name").fetchall()
    budgets = budget_mod.get_budgets(con)
    out = []
    for a in accts:
        mb = budgets.get(a["id"])
        pace = budget_mod.pacing(con, a["id"], mb) if mb else None
        out.append({"id": a["id"], "name": a["name"], "currency": a["currency"],
                    "monthly_budget": mb, "pacing": pace})
    con.close()
    return JSONResponse(out)


@app.post("/api/budgets/sync")
def budgets_sync_from_sheet():
    con = db.connect()
    try:
        res = budget_import.run(con)
        alert_mod.run(con)
    except Exception as e:
        con.close()
        return JSONResponse({"ok": False, "reason": str(e)[:160]})
    con.close()
    return JSONResponse({"ok": True, "applied": res["applied"], "mapping": res["mapping"]})


@app.post("/api/budgets")
async def set_budget(request: Request):
    d = await request.json()
    con = db.connect()
    budget_mod.set_budget(con, d["account_id"], float(d["monthly_budget"]),
                          datetime.now(timezone.utc).isoformat())
    alert_mod.run(con)   # re-scan so budget alerts update immediately
    con.close()
    return {"ok": True}


@app.post("/api/guard/expected")
async def set_expected_url(request: Request):
    d = await request.json()
    con = db.connect()
    con.execute(
        "insert into account_settings(account_id, expected_url, updated_at) values(?,?,?) "
        "on conflict(account_id) do update set expected_url=excluded.expected_url, "
        "updated_at=excluded.updated_at",
        (d["account_id"], d.get("expected_url") or None, datetime.now(timezone.utc).isoformat()))
    alert_mod.run(con)
    con.close()
    return {"ok": True}


@app.get("/api/alerts")
def list_alerts(status: str = "open"):
    con = db.connect()
    rows = con.execute(
        "select * from alerts where status=? order by "
        "case severity when 'critical' then 0 when 'warn' then 1 else 2 end, account_name",
        (status,)).fetchall()
    con.close()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/alerts/{alert_id}/{new_status}")
def update_alert(alert_id: int, new_status: str):
    if new_status not in ("open", "ack", "resolved"):
        return JSONResponse({"error": "bad status"}, status_code=400)
    con = db.connect()
    con.execute("update alerts set status=?, updated_at=? where id=?",
                (new_status, datetime.now(timezone.utc).isoformat(), alert_id))
    con.commit()
    con.close()
    return {"id": alert_id, "status": new_status}


@app.get("/api/mode")
def get_mode():
    return {"safe": SAFE_MODE["on"]}


@app.post("/api/mode")
async def set_mode(request: Request):
    d = await request.json()
    SAFE_MODE["on"] = bool(d.get("safe", True))
    return {"safe": SAFE_MODE["on"]}


def _log(con, **kw):
    con.execute(
        "insert into actions_log(ts,account_id,account_name,level,object_id,object_name,"
        "action,before_state,after_state,status,detail) values(?,?,?,?,?,?,?,?,?,?,?)",
        (kw["ts"], kw["account_id"], kw["account_name"], kw["level"], kw["object_id"],
         kw["object_name"], kw["action"], kw["before"], kw["after"], kw["status"], kw["detail"]))
    con.commit()
    return con.execute("select last_insert_rowid()").fetchone()[0]


def _apply(action, object_id, value, before):
    """Execute the write against Meta. Returns (status, after, detail)."""
    if action in ("pause", "resume"):
        newst = "PAUSED" if action == "pause" else "ACTIVE"
        ok, resp = meta_write.set_status(object_id, newst)
        return ("success" if ok else "failed", newst if ok else before,
                "applied" if ok else resp.get("error", "error"))
    if action == "set_budget":
        ok, resp = meta_write.set_daily_budget(object_id, value)
        return ("success" if ok else "failed", str(value) if ok else before,
                "applied" if ok else resp.get("error", "error"))
    return "failed", before, "unknown action"


@app.post("/api/action")
async def do_action(request: Request):
    d = await request.json()
    action = d["action"]                       # pause / resume / set_budget
    level = d.get("level", "campaign")         # campaign / ad
    oid = str(d["object_id"])
    value = d.get("value")
    before = str(d.get("before", ""))
    ts = datetime.now(timezone.utc).isoformat()

    if SAFE_MODE["on"]:
        after = (str(value) if action == "set_budget"
                 else "PAUSED" if action == "pause" else "ACTIVE")
        status, detail = "dryrun", "dry-run (Safe mode — not sent to Meta)"
    else:
        status, after, detail = _apply(action, oid, value, before)

    con = db.connect()
    log_id = _log(con, ts=ts, account_id=d.get("account_id", ""),
                  account_name=d.get("account_name", ""), level=level, object_id=oid,
                  object_name=d.get("object_name", ""), action=action,
                  before=before, after=str(after), status=status, detail=detail)
    # reflect campaign state locally ONLY on a real success (dry-run must not desync)
    if status == "success" and level == "campaign":
        if action in ("pause", "resume"):
            con.execute("update campaigns set status=? where id=?", (after, oid))
        elif action == "set_budget":
            con.execute("update campaigns set daily_budget=? where id=?", (value, oid))
        con.commit()
    con.close()
    return {"status": status, "after": after, "detail": detail, "id": log_id}


@app.get("/api/actions")
def actions(account_id: str = None, limit: int = 25):
    con = db.connect()
    if account_id:
        rows = con.execute("select * from actions_log where account_id=? order by id desc limit ?",
                           (account_id, limit)).fetchall()
    else:
        rows = con.execute("select * from actions_log order by id desc limit ?", (limit,)).fetchall()
    con.close()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/action/{log_id}/undo")
async def undo(log_id: int):
    con = db.connect()
    row = con.execute("select * from actions_log where id=?", (log_id,)).fetchone()
    if not row:
        con.close()
        return JSONResponse({"error": "not found"}, status_code=404)
    rev = {"pause": "resume", "resume": "pause"}.get(row["action"])
    ts = datetime.now(timezone.utc).isoformat()
    if row["action"] == "set_budget":
        value = row["before_state"]
        if SAFE_MODE["on"]:
            status, after, detail = "dryrun", value, "undo dry-run"
        else:
            status, after, detail = _apply("set_budget", row["object_id"], value, row["after_state"])
        act = "set_budget"
    elif rev:
        if SAFE_MODE["on"]:
            status, after, detail = "dryrun", ("PAUSED" if rev == "pause" else "ACTIVE"), "undo dry-run"
        else:
            status, after, detail = _apply(rev, row["object_id"], None, row["after_state"])
        act = rev
    else:
        con.close()
        return JSONResponse({"error": "not undoable"}, status_code=400)
    log_id2 = _log(con, ts=ts, account_id=row["account_id"], account_name=row["account_name"],
                   level=row["level"], object_id=row["object_id"],
                   object_name=row["object_name"], action=act, before=row["after_state"],
                   after=str(after), status=status, detail=f"undo of #{log_id}: {detail}")
    if status == "success" and row["level"] == "campaign":
        if act in ("pause", "resume"):
            con.execute("update campaigns set status=? where id=?", (after, row["object_id"]))
        elif act == "set_budget":
            con.execute("update campaigns set daily_budget=? where id=?", (after, row["object_id"]))
        con.commit()
    con.close()
    return {"status": status, "after": after, "id": log_id2}


@app.get("/api/meta")
def meta():
    con = db.connect()
    row = con.execute(
        "select min(date) mn, max(date) mx from insights_daily where campaign_id='ACCOUNT'"
    ).fetchone()
    con.close()
    return JSONResponse({"min_date": row["mn"], "max_date": row["mx"]})


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
