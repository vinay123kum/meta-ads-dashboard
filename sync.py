"""
Phase 1.5 data pipeline: pull accounts -> campaigns -> 90d account daily +
30d campaign daily + 30d geo daily into SQLite, with conversions parsed from
Meta's actions field.

    python sync.py
"""
import sys
from datetime import datetime, timezone

try:
    from facebook_business.api import FacebookAdsApi
    from facebook_business.adobjects.user import User
except ImportError:
    sys.exit("ERROR: run  pip install -r requirements.txt  first.")

from config import APP_ID, APP_SECRET, ACCESS_TOKEN, ACCOUNT_ALLOWLIST
import db

STATUS = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
    101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}
NOW = datetime.now(timezone.utc).isoformat()

# action_types that count as a meaningful conversion (not engagement noise)
CONV_TYPES = {
    "lead", "offsite_conversion.fb_pixel_lead", "onsite_conversion.lead_grouped",
    "purchase", "offsite_conversion.fb_pixel_purchase", "omni_purchase",
    "complete_registration", "offsite_conversion.fb_pixel_complete_registration",
    "schedule", "contact", "submit_application",
    "onsite_conversion.messaging_conversation_started_7d",
}


def init_api():
    if not ACCESS_TOKEN:
        sys.exit("ERROR: META_ACCESS_TOKEN not set in .env")
    FacebookAdsApi.init(APP_ID, APP_SECRET, ACCESS_TOKEN)


def _sum_actions(row, field, types):
    out = 0.0
    for a in (row.get(field) or []):
        if a.get("action_type") in types:
            try:
                out += float(a.get("value", 0) or 0)
            except (TypeError, ValueError):
                pass
    return out


def _sum_all(row, field):
    """Sum all values in a list-of-actions field regardless of action_type."""
    out = 0.0
    for a in (row.get(field) or []):
        try:
            out += float(a.get("value", 0) or 0)
        except (TypeError, ValueError):
            pass
    return out


def get_accounts():
    fields = ["account_id", "name", "account_status", "currency"]
    accts = list(User(fbid="me").get_ad_accounts(fields=fields))
    if ACCOUNT_ALLOWLIST:
        accts = [a for a in accts if a.get("id") in ACCOUNT_ALLOWLIST]
    return accts


def sync_insights(con, acct, level, preset, campaign_id_const=None):
    fields = ["spend", "impressions", "clicks", "actions", "action_values"]
    if level == "campaign":
        fields += ["campaign_id"]
    params = {"level": level, "time_increment": 1, "date_preset": preset}
    try:
        for r in acct.get_insights(fields=fields, params=params):
            conv = _sum_actions(r, "actions", CONV_TYPES)
            cval = _sum_actions(r, "action_values", CONV_TYPES)
            cid = campaign_id_const or r.get("campaign_id") or "UNKNOWN"
            db.upsert(con, "insights_daily", {
                "account_id": acct["id"], "campaign_id": cid,
                "date": r.get("date_start"),
                "spend": float(r.get("spend", 0) or 0),
                "impressions": int(r.get("impressions", 0) or 0),
                "clicks": int(r.get("clicks", 0) or 0),
                "conversions": conv, "conv_value": cval,
            })
    except Exception as e:
        print(f"   {level} insights err {acct.get('name','')[:20]}: {str(e)[:60]}")


def sync_campaigns(con, acct):
    try:
        for c in acct.get_campaigns(fields=["name", "objective", "status", "daily_budget",
                                            "smart_promotion_type"]):
            db.upsert(con, "campaigns", {
                "id": c["id"], "account_id": acct["id"], "name": c.get("name"),
                "objective": c.get("objective"), "status": c.get("status"),
                "daily_budget": float(c.get("daily_budget", 0) or 0) / 100.0,
                "smart_promotion_type": c.get("smart_promotion_type"),
                "updated_at": NOW,
            })
    except Exception as e:
        print(f"   campaign err {acct.get('name','')[:20]}: {str(e)[:60]}")


def _extract_url(creative):
    """Dig the destination URL out of a creative's various shapes."""
    if not creative:
        return None
    oss = creative.get("object_story_spec") or {}
    ld = oss.get("link_data") or {}
    if ld.get("link"):
        return ld["link"]
    vd = oss.get("video_data") or {}
    cta = (vd.get("call_to_action") or {}).get("value") or {}
    if cta.get("link"):
        return cta["link"]
    afs = creative.get("asset_feed_spec") or {}
    for lu in (afs.get("link_urls") or []):
        if lu.get("website_url"):
            return lu["website_url"]
    return creative.get("link_url")


def sync_ad_urls(con, acct):
    """Pull each ad's destination URL + effective status (for the URL guardrail)."""
    fields = ["name", "effective_status", "campaign_id",
              "creative{object_story_spec,asset_feed_spec,link_url}"]
    try:
        for ad in acct.get_ads(fields=fields, params={"limit": 200}):
            aid = ad["id"]
            url = _extract_url(ad.get("creative") or {})
            con.execute(
                "insert or ignore into creatives(id,account_id,campaign_id,name,is_video,updated_at) "
                "values(?,?,?,?,0,?)",
                (aid, acct["id"], ad.get("campaign_id"), ad.get("name"), NOW))
            con.execute(
                "update creatives set final_url=?, status=?, name=coalesce(name,?), "
                "campaign_id=coalesce(campaign_id,?), updated_at=? where id=?",
                (url, ad.get("effective_status"), ad.get("name"), ad.get("campaign_id"), NOW, aid))
    except Exception as e:
        print(f"   ad-url err {acct.get('name','')[:20]}: {str(e)[:60]}")


def sync_adsets(con, acct):
    """Ad sets with Advantage+ Audience flag (AI-targeting detection)."""
    try:
        for s in acct.get_ad_sets(fields=["name", "effective_status", "campaign_id", "targeting"],
                                  params={"limit": 200}):
            t = s.get("targeting") or {}
            aa = (t.get("targeting_automation") or {}).get("advantage_audience")
            db.upsert(con, "ad_sets", {
                "id": s["id"], "account_id": acct["id"], "campaign_id": s.get("campaign_id"),
                "name": s.get("name"), "status": s.get("effective_status"),
                "advantage_audience": int(aa or 0), "updated_at": NOW})
    except Exception as e:
        print(f"   adset err {acct.get('name','')[:20]}: {str(e)[:60]}")


def sync_ads(con, acct, preset="last_30d"):
    """One level=ad call per account returns daily-per-ad rows (efficient).
    Captures 3-sec views (hook), thruplay (hold), frequency, cpm, ctr."""
    fields = ["ad_id", "ad_name", "campaign_id", "spend", "impressions", "clicks",
              "frequency", "cpm", "ctr", "actions",
              "video_thruplay_watched_actions", "video_play_actions"]
    params = {"level": "ad", "time_increment": 1, "date_preset": preset}
    seen = {}
    try:
        for r in acct.get_insights(fields=fields, params=params):
            aid = r.get("ad_id")
            if not aid:
                continue
            v3 = _sum_actions(r, "actions", {"video_view"})       # 3-sec views
            lc = _sum_actions(r, "actions", {"link_click"})
            tp = _sum_all(r, "video_thruplay_watched_actions")
            vp = _sum_all(r, "video_play_actions")
            db.upsert(con, "creative_daily", {
                "ad_id": aid, "account_id": acct["id"], "date": r.get("date_start"),
                "spend": float(r.get("spend", 0) or 0),
                "impressions": int(r.get("impressions", 0) or 0),
                "clicks": int(r.get("clicks", 0) or 0),
                "link_clicks": int(lc), "video_3s": int(v3), "thruplay": int(tp),
                "frequency": float(r.get("frequency", 0) or 0),
                "cpm": float(r.get("cpm", 0) or 0),
                "ctr": float(r.get("ctr", 0) or 0),
            })
            m = seen.setdefault(aid, {"name": r.get("ad_name"),
                                      "campaign_id": r.get("campaign_id"), "is_video": 0})
            if vp > 0 or tp > 0:
                m["is_video"] = 1
        for aid, m in seen.items():
            db.upsert(con, "creatives", {
                "id": aid, "account_id": acct["id"], "campaign_id": m["campaign_id"],
                "name": m["name"], "is_video": m["is_video"], "updated_at": NOW})
    except Exception as e:
        print(f"   ads err {acct.get('name','')[:20]}: {str(e)[:60]}")


def sync_geo(con, acct):
    params = {"level": "account", "time_increment": 1, "date_preset": "last_30d",
              "breakdowns": ["country"]}
    try:
        for r in acct.get_insights(fields=["spend", "impressions", "clicks", "actions"],
                                   params=params):
            db.upsert(con, "geo_daily", {
                "account_id": acct["id"], "date": r.get("date_start"),
                "country": r.get("country", "??"),
                "spend": float(r.get("spend", 0) or 0),
                "impressions": int(r.get("impressions", 0) or 0),
                "clicks": int(r.get("clicks", 0) or 0),
                "conversions": _sum_actions(r, "actions", CONV_TYPES),
            })
    except Exception as e:
        print(f"   geo err {acct.get('name','')[:20]}: {str(e)[:60]}")


def main():
    init_api()
    db.init()
    con = db.connect()
    accts = get_accounts()
    print(f"Syncing {len(accts)} accounts (90d account / 30d campaign+geo)...\n")
    for i, acct in enumerate(accts, 1):
        db.upsert(con, "accounts", {
            "id": acct["id"], "account_id": acct.get("account_id"),
            "name": acct.get("name"), "currency": acct.get("currency"),
            "status": STATUS.get(acct.get("account_status"), str(acct.get("account_status"))),
            "updated_at": NOW,
        })
        sync_insights(con, acct, "account", "last_90d", campaign_id_const="ACCOUNT")
        sync_insights(con, acct, "campaign", "last_30d")
        sync_campaigns(con, acct)
        sync_ads(con, acct)
        sync_ad_urls(con, acct)
        sync_adsets(con, acct)
        sync_geo(con, acct)
        con.commit()
        print(f"[{i:>2}/{len(accts)}] {acct.get('name','')[:34]}")

    c = con.cursor()
    stats = {t: c.execute(f"select count(*) from {t}").fetchone()[0]
             for t in ("accounts", "campaigns", "insights_daily", "geo_daily",
                       "creatives", "creative_daily", "ad_sets")}
    con.close()
    print(f"\nDone. {stats}")
    print("Now run:  python app.py   and open http://localhost:8000")


if __name__ == "__main__":
    main()
