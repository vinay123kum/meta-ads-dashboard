"""
Guardrails: (1) landing-page/URL drift — flag active ads that point somewhere
different from the account's dominant destination (or a manual expected pattern);
(2) AI-targeting on — Advantage+ Audience ad sets and Advantage+ shopping campaigns.
"""
from urllib.parse import urlparse
from collections import Counter


def norm_url(u):
    """Reduce a URL to host + first path segment, e.g. booking.example.com/brand."""
    if not u:
        return None
    try:
        p = urlparse(u if "://" in u else "https://" + u)
        host = p.netloc.lower().replace("www.", "")
        seg = [s for s in p.path.split("/") if s][:1]
        return host + ("/" + seg[0].lower() if seg else "")
    except Exception:
        return None


def account_guard(con, account_id):
    ads = con.execute(
        "select id,name,final_url,status from creatives "
        "where account_id=? and final_url is not null and final_url!=''", (account_id,)).fetchall()

    counts = Counter()
    for a in ads:
        n = norm_url(a["final_url"])
        if n:
            counts[n] += 1
    dominant = counts.most_common(1)[0][0] if counts else None

    row = con.execute("select expected_url from account_settings where account_id=?",
                      (account_id,)).fetchone()
    override = row["expected_url"] if row and row["expected_url"] else None
    expected = override or dominant

    mismatches = []
    for a in ads:
        if a["status"] != "ACTIVE":       # only flag ads actually serving
            continue
        n = norm_url(a["final_url"])
        if not expected or not n:
            continue
        bad = (expected.lower() not in a["final_url"].lower()) if override else (n != dominant)
        if bad:
            mismatches.append({"name": a["name"], "url": a["final_url"], "pattern": n})

    adsets = con.execute("select name,status,advantage_audience from ad_sets where account_id=?",
                         (account_id,)).fetchall()
    ai_adsets = [s for s in adsets if s["advantage_audience"] and s["status"] == "ACTIVE"]
    asc = con.execute(
        "select count(*) n from campaigns where account_id=? and status='ACTIVE' "
        "and smart_promotion_type is not null and smart_promotion_type!=''", (account_id,)).fetchone()["n"]

    return {
        "expected": expected, "dominant": dominant, "override": override,
        "patterns": counts.most_common(6),
        "mismatches": mismatches,
        "ai_adsets": len(ai_adsets), "ai_adset_names": [s["name"] for s in ai_adsets[:6]],
        "advantage_shopping": asc,
    }
