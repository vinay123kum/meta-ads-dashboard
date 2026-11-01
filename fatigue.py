"""
Creative fatigue monitor. Splits a rolling lookback into week-1 (older) vs
week-2 (recent), computes trends for hook rate, CTR, frequency, CPM, and
classifies each creative HIGH / MEDIUM / LOW risk. Detection only (no briefs).

Hook rate = 3-sec views / impressions (video) or link-clicks / impressions (static).
"""
from collections import defaultdict

DEFAULTS = {
    "lookback": 14,
    "hook_rate_floor": 0.25,      # fraction (25%)
    "ctr_decline_threshold": 0.15,  # 15% WoW drop
    "frequency_cap": 4.0,
    "cpm_spike_threshold": 0.30,  # 30% WoW rise
    "min_spend": 100.0,           # window spend gate
}


def _avg(days, fn):
    vals = [fn(d) for d in days if d["impressions"] > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _hook(d, is_video):
    if not d["impressions"]:
        return 0.0
    num = d["video_3s"] if is_video else d["link_clicks"]
    return num / d["impressions"]


def _ctr(d):
    return (d["clicks"] / d["impressions"]) if d["impressions"] else 0.0


def _cpm(d):
    return (d["spend"] / d["impressions"] * 1000) if d["impressions"] else 0.0


def _pct(a, b):
    return round((b - a) / a * 100, 1) if a else None


def analyze(rows, p=None):
    """rows: creative_daily joined with creatives (name, is_video). Returns
    {summary:{high,med,low}, items:[...]} sorted by risk."""
    o = {**DEFAULTS, **(p or {})}
    by_ad = defaultdict(list)
    for r in rows:
        by_ad[r["ad_id"]].append(r)

    items = []
    for aid, days in by_ad.items():
        days.sort(key=lambda d: d["date"])
        days = days[-o["lookback"]:]
        spend = sum(d["spend"] for d in days)
        if len(days) < 7 or spend < o["min_spend"]:
            continue
        is_video = bool(days[0].get("is_video"))
        name = days[0].get("name") or aid
        w2 = days[-7:]                     # recent week
        w1 = days[-14:-7] or days[:-7]     # prior week (best effort)

        hook_w2 = _avg(w2, lambda d: _hook(d, is_video))
        hook_w1 = _avg(w1, lambda d: _hook(d, is_video))
        ctr_w2, ctr_w1 = _avg(w2, _ctr), _avg(w1, _ctr)
        cpm_w2, cpm_w1 = _avg(w2, _cpm), _avg(w1, _cpm)
        freq = _avg(w2, lambda d: d["frequency"]) or max((d["frequency"] for d in w2), default=0)

        ctr_decline = ((ctr_w1 - ctr_w2) / ctr_w1) if ctr_w1 else 0.0
        cpm_rise = ((cpm_w2 - cpm_w1) / cpm_w1) if cpm_w1 else 0.0

        reasons = []
        if is_video and hook_w2 < o["hook_rate_floor"]:
            reasons.append(f"hook rate {hook_w2:.0%} below {o['hook_rate_floor']:.0%} floor")
        if ctr_decline > o["ctr_decline_threshold"]:
            reasons.append(f"CTR down {ctr_decline:.0%} WoW")
        if freq > o["frequency_cap"]:
            reasons.append(f"frequency {freq:.1f} over cap")
        if cpm_rise > o["cpm_spike_threshold"]:
            reasons.append(f"CPM up {cpm_rise:.0%} WoW")

        if reasons:
            risk = "HIGH"
        elif ((is_video and hook_w2 < o["hook_rate_floor"] * 1.2)
              or (0.10 <= ctr_decline <= o["ctr_decline_threshold"])
              or (3.0 <= freq <= o["frequency_cap"])):
            risk = "MEDIUM"
            if is_video and hook_w2 < o["hook_rate_floor"] * 1.2:
                reasons.append("hook rate nearing floor")
            if 0.10 <= ctr_decline <= o["ctr_decline_threshold"]:
                reasons.append(f"CTR softening ({ctr_decline:.0%})")
            if 3.0 <= freq <= o["frequency_cap"]:
                reasons.append(f"frequency building ({freq:.1f})")
        else:
            risk = "LOW"

        action = ("Refresh / rotate creative" if risk == "HIGH"
                  else "Queue a replacement variant" if risk == "MEDIUM"
                  else "Stable — keep running")

        items.append({
            "ad_id": aid, "name": name, "is_video": is_video, "risk": risk,
            "spend": round(spend, 2),
            "hook_rate": round(hook_w2 * 100, 1), "hook_trend": _pct(hook_w1, hook_w2),
            "ctr": round(ctr_w2 * 100, 2), "ctr_trend": _pct(ctr_w1, ctr_w2),
            "frequency": round(freq, 1),
            "cpm": round(cpm_w2, 2), "cpm_trend": _pct(cpm_w1, cpm_w2),
            "reasons": reasons, "action": action,
        })

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda x: (order[x["risk"]], -x["spend"]))
    summary = {"high": sum(1 for i in items if i["risk"] == "HIGH"),
               "med": sum(1 for i in items if i["risk"] == "MEDIUM"),
               "low": sum(1 for i in items if i["risk"] == "LOW")}
    return {"summary": summary, "items": items}
