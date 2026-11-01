"""
Alert engine: scans the synced data, produces de-duplicated alerts, stores them,
auto-resolves stale ones, and (optionally) pushes new ones to Slack.

Alert identity = account_id:type (one open alert per account per type).
"""
import json
import urllib.request
from datetime import datetime, timezone

import fatigue as fat_mod
import budget as budget_mod
import guardrails as guard_mod
from config import SLACK_WEBHOOK_URL

DEAD_SPEND = 15.0
NOW = lambda: datetime.now(timezone.utc).isoformat()

SEV_EMOJI = {"critical": "🔴", "warn": "🟠", "info": "🔵"}


def _mk(a, severity, typ, message):
    return {"key": f'{a["id"]}:{typ}', "account_id": a["id"],
            "account_name": a["name"], "severity": severity,
            "type": typ, "message": message}


def scan(con):
    out = []
    budgets = budget_mod.get_budgets(con)
    for a in con.execute("select * from accounts").fetchall():
        rows = con.execute(
            "select date,spend,conversions from insights_daily "
            "where account_id=? and campaign_id='ACCOUNT' order by date", (a["id"],)).fetchall()
        last7, prev7 = rows[-7:], rows[-14:-7]
        spend7 = sum(r["spend"] for r in last7)
        conv7 = sum(r["conversions"] for r in last7)
        spendp = sum(r["spend"] for r in prev7)
        convp = sum(r["conversions"] for r in prev7)

        if a["status"] in ("CLOSED", "DISABLED"):
            out.append(_mk(a, "critical", "account_status", f'Account {a["status"]}'))
        elif a["status"] in ("UNSETTLED", "PENDING_RISK_REVIEW", "IN_GRACE_PERIOD"):
            out.append(_mk(a, "warn", "account_status", f'Account {a["status"]} — billing/review'))

        if a["status"] == "ACTIVE" and spend7 == 0 and spendp > 0:
            out.append(_mk(a, "warn", "stalled", "Spend stopped — active but $0 in last 7d"))
        if spend7 > 0 and conv7 == 0:
            out.append(_mk(a, "critical", "no_conv",
                           f"${spend7:,.0f} spent, 0 conversions (7d) — check tracking"))
        if conv7 > 0 and convp > 0 and spendp > 0:
            cpa, cpap = spend7 / conv7, spendp / convp
            if cpap > 0 and (cpa - cpap) / cpap > 0.40:
                out.append(_mk(a, "warn", "cpa_spike",
                               f"CPA up {(cpa - cpap) / cpap * 100:.0f}% WoW (now ${cpa:.1f})"))

        # dead-spend campaigns in last 7 dates
        dates = [r["date"] for r in rows[-7:]]
        if dates:
            ph = ",".join("?" for _ in dates)
            dead = con.execute(
                f"select count(*) n from (select campaign_id, sum(spend) s, sum(conversions) c "
                f"from insights_daily where account_id=? and campaign_id!='ACCOUNT' "
                f"and date in ({ph}) group by campaign_id having s>=? and c=0)",
                [a["id"], *dates, DEAD_SPEND]).fetchone()["n"]
            if dead:
                out.append(_mk(a, "warn", "dead_spend",
                               f"{dead} campaign(s) spending with 0 conversions (7d)"))

        # high creative fatigue
        crows = [dict(r) for r in con.execute(
            "select cd.*, c.name, c.is_video from creative_daily cd "
            "left join creatives c on c.id=cd.ad_id where cd.account_id=?", (a["id"],)).fetchall()]
        high = fat_mod.analyze(crows)["summary"]["high"]
        if high:
            out.append(_mk(a, "warn", "fatigue", f"{high} creative(s) at HIGH fatigue"))

        # budget pacing (only if a monthly budget is set)
        mb = budgets.get(a["id"])
        if mb:
            p = budget_mod.pacing(con, a["id"], mb)
            if p:
                pct = p["pct"]
                if pct >= 100:
                    out.append(_mk(a, "critical", "budget_pace",
                                   f"Budget {pct:.0f}% spent — overspent by ${p['mtd_spend'] - mb:,.0f}"))
                elif pct >= 80:
                    out.append(_mk(a, "warn", "budget_pace",
                                   f"Budget {pct:.0f}% spent (${p['mtd_spend']:,.0f}/${mb:,.0f})"))
                elif pct >= 60:
                    out.append(_mk(a, "info", "budget_pace",
                                   f"Budget {pct:.0f}% spent (${p['mtd_spend']:,.0f}/${mb:,.0f})"))
                if not p["will_last"] and p["runout_date"]:
                    out.append(_mk(a, "warn", "budget_runway",
                                   f"Budget won't last the month — runs out ~{p['runout_date']} "
                                   f"(pace: ${p['projected']:,.0f} vs ${mb:,.0f})"))

        # guardrails: URL drift + AI targeting
        g = guard_mod.account_guard(con, a["id"])
        if g["mismatches"]:
            m = g["mismatches"][0]
            out.append(_mk(a, "warn", "url_mismatch",
                           f'{len(g["mismatches"])} active ad(s) link off the account norm '
                           f'({g["expected"]}) — e.g. "{m["name"]}" → {m["pattern"]}'))
        if g["ai_adsets"]:
            out.append(_mk(a, "warn", "ai_targeting",
                           f'Advantage+ Audience ON in {g["ai_adsets"]} active ad set(s) '
                           "— audience may expand beyond target"))
        if g["advantage_shopping"]:
            out.append(_mk(a, "info", "ai_campaign",
                           f'{g["advantage_shopping"]} Advantage+ shopping campaign(s) active'))
    return out


def store(con, found):
    """Upsert found alerts, auto-resolve ones no longer present."""
    keys = set()
    for al in found:
        keys.add(al["key"])
        con.execute(
            "insert into alerts(key,account_id,account_name,severity,type,message,"
            "status,delivered,created_at,updated_at) "
            "values(?,?,?,?,?,?, 'open', 0, ?, ?) "
            "on conflict(key) do update set message=excluded.message, "
            "severity=excluded.severity, updated_at=excluded.updated_at, "
            "status=case when alerts.status='resolved' then 'open' else alerts.status end",
            (al["key"], al["account_id"], al["account_name"], al["severity"],
             al["type"], al["message"], NOW(), NOW()))
    # resolve stale
    open_rows = con.execute("select key from alerts where status!='resolved'").fetchall()
    for r in open_rows:
        if r["key"] not in keys:
            con.execute("update alerts set status='resolved', updated_at=? where key=?",
                        (NOW(), r["key"]))
    con.commit()


def deliver(con):
    """Push undelivered open alerts to Slack (if configured). Returns count sent."""
    rows = con.execute(
        "select * from alerts where delivered=0 and status='open' "
        "order by case severity when 'critical' then 0 when 'warn' then 1 else 2 end").fetchall()
    if not rows:
        return 0
    if SLACK_WEBHOOK_URL:
        lines = [f"*Agency Dashboard — {len(rows)} new alert(s)*"]
        for r in rows[:30]:
            lines.append(f'{SEV_EMOJI.get(r["severity"],"•")} *{r["account_name"]}* — {r["message"]}')
        try:
            req = urllib.request.Request(
                SLACK_WEBHOOK_URL, data=json.dumps({"text": "\n".join(lines)}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"   slack deliver err: {str(e)[:80]}")
            return 0
    for r in rows:
        con.execute("update alerts set delivered=1 where id=?", (r["id"],))
    con.commit()
    return len(rows)


def run(con):
    found = scan(con)
    store(con, found)
    sent = deliver(con)
    return {"found": len(found), "delivered": sent}
