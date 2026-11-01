"""
Layer 0 "hello world": authenticate with Meta and list every ad account the
token can reach, with yesterday's spend. If this prints a clean table, the data
pipeline foundation works and Phase 1 (dashboard) is mostly query-writing.

Run:
    python test_connection.py
"""
import sys
from datetime import datetime, timedelta

try:
    from facebook_business.api import FacebookAdsApi
    from facebook_business.adobjects.user import User
except ImportError:
    sys.exit("ERROR: run  pip install -r requirements.txt  first.")

from config import APP_ID, APP_SECRET, ACCESS_TOKEN, ACCOUNT_ALLOWLIST

# account_status codes -> human label (Meta Marketing API)
STATUS = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
    101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}


def init_api():
    if not ACCESS_TOKEN:
        sys.exit("ERROR: META_ACCESS_TOKEN not set. Copy .env.example to .env and fill it in.")
    FacebookAdsApi.init(APP_ID, APP_SECRET, ACCESS_TOKEN)


def get_accounts():
    fields = ["account_id", "name", "account_status", "currency", "amount_spent"]
    accounts = list(User(fbid="me").get_ad_accounts(fields=fields))
    if ACCOUNT_ALLOWLIST:
        accounts = [a for a in accounts if a.get("id") in ACCOUNT_ALLOWLIST
                    or f"act_{a.get('account_id')}" in ACCOUNT_ALLOWLIST]
    return accounts


def yesterday_spend(account):
    yest = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "time_range": {"since": yest, "until": yest},
        "fields": ["spend", "impressions", "clicks"],
        "level": "account",
    }
    try:
        rows = list(account.get_insights(params=params))
        if rows:
            r = rows[0]
            return r.get("spend", "0"), r.get("impressions", "0"), r.get("clicks", "0")
        return "0.00", "0", "0"
    except Exception as e:  # one bad account must not kill the whole run
        return f"ERR {str(e)[:40]}", "-", "-"


def main():
    init_api()
    print("Connecting to Meta Marketing API...\n")
    accounts = get_accounts()
    if not accounts:
        sys.exit("No ad accounts visible to this token. Check permissions / partner access.")

    print(f"{'Account':<34} {'ID':<16} {'Status':<18} {'Cur':<4} "
          f"{'Spend(y)':>10} {'Impr':>9} {'Clicks':>8}")
    print("-" * 104)

    total = 0.0
    active = 0
    for acct in accounts:
        spend, impr, clicks = yesterday_spend(acct)
        status = STATUS.get(acct.get("account_status"), str(acct.get("account_status")))
        if status == "ACTIVE":
            active += 1
        try:
            total += float(spend)
        except (TypeError, ValueError):
            pass
        name = (acct.get("name") or "")[:32]
        print(f"{name:<34} {acct.get('account_id',''):<16} {status:<18} "
              f"{acct.get('currency',''):<4} {str(spend):>10} {str(impr):>9} {str(clicks):>8}")

    print("-" * 104)
    print(f"\n{len(accounts)} accounts reachable | {active} active | "
          f"yesterday spend total ~{total:,.2f} (mixed currencies if applicable)")
    print("\nLayer 0 OK. Next: Phase 1 — pull into Postgres and build the read dashboard.")


if __name__ == "__main__":
    main()
