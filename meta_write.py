"""
Meta write operations for the control layer. Thin, generic wrappers over the
Graph API node-update call so we can pause/resume any object (campaign/adset/ad)
and set budgets without juggling per-type SDK classes.

All calls require a token with ads_management AND an advertiser/admin role on
the account. View-only partner access will fail (handled by the caller).
"""
from facebook_business.api import FacebookAdsApi
from facebook_business.exceptions import FacebookRequestError

from config import APP_ID, APP_SECRET, ACCESS_TOKEN

_inited = False


def init():
    global _inited
    if not _inited:
        FacebookAdsApi.init(APP_ID, APP_SECRET, ACCESS_TOKEN)
        _inited = True
    return FacebookAdsApi.get_default_api()


def _post(object_id, params):
    api = init()
    resp = api.call("POST", (object_id,), params=params)
    try:
        return True, resp.json()
    except Exception:
        return True, {}


def set_status(object_id, status):
    """status: 'ACTIVE' or 'PAUSED'. Works on campaign / adset / ad ids."""
    assert status in ("ACTIVE", "PAUSED")
    try:
        return _post(object_id, {"status": status})
    except FacebookRequestError as e:
        return False, {"error": e.api_error_message()}
    except Exception as e:
        return False, {"error": str(e)}


def set_daily_budget(object_id, dollars):
    """Set daily_budget (campaign CBO or ad set). Meta wants minor units (cents)."""
    cents = int(round(float(dollars) * 100))
    try:
        return _post(object_id, {"daily_budget": cents})
    except FacebookRequestError as e:
        return False, {"error": e.api_error_message()}
    except Exception as e:
        return False, {"error": str(e)}
