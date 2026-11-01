"""
Recommendation engine: turns period metrics into a health score, a
what's-working / what's-not list, and prioritized recommendations.
Explainable rules — every output traces to a concrete signal.
"""

BENCHMARK_CTR = 1.0          # % — below this = weak creative
DEAD_SPEND = 15.0            # $ spent in period with 0 conv = waste flag
WINNER_MIN_CONV = 3         # min conv in period to call a campaign a winner


def _pct(cur, prev):
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _label(score):
    if score >= 80:
        return "EXCELLENT"
    if score >= 65:
        return "GOOD"
    if score >= 45:
        return "NEEDS WORK"
    return "AT RISK"


def health(account, cur, prev, campaigns):
    """Return {score, label, sub:{Delivery,Efficiency,Creative,Tracking}}."""
    spend, conv, ctr = cur["spend"], cur["conversions"], cur["ctr"]
    status = account["status"]

    # Tracking — is conversion data flowing relative to spend?
    if spend > 0 and conv == 0:
        tracking = 20
    elif conv > 0:
        tracking = 95
    else:
        tracking = 60  # no spend, can't tell

    # Delivery — active and actually spending across campaigns?
    delivering = sum(1 for c in campaigns if c["spend"] > 0)
    total_c = len(campaigns) or 1
    if status != "ACTIVE":
        delivery = 30
    elif spend == 0:
        delivery = 40
    else:
        delivery = min(95, 55 + int(delivering / total_c * 40))

    # Creative — CTR vs benchmark, penalise decline
    if ctr >= 3:
        creative = 90
    elif ctr >= 2:
        creative = 75
    elif ctr >= BENCHMARK_CTR:
        creative = 60
    else:
        creative = 40
    ctr_delta = _pct(cur["ctr"], prev["ctr"]) if prev else None
    if ctr_delta is not None and ctr_delta < -20:
        creative = max(25, creative - 15)

    # Efficiency — CPA direction + presence of conversions
    efficiency = 55
    cpa_delta = _pct(cur["cpa"], prev["cpa"]) if prev and prev["cpa"] else None
    if conv == 0 and spend > 0:
        efficiency = 30
    elif cpa_delta is not None:
        efficiency = 70 - min(25, max(-25, int(cpa_delta / 2)))  # rising CPA lowers it
    elif conv > 0:
        efficiency = 70

    score = round(delivery * .25 + efficiency * .3 + creative * .25 + tracking * .2)
    return {"score": score, "label": _label(score),
            "sub": {"Delivery": delivery, "Efficiency": efficiency,
                    "Creative": creative, "Tracking": tracking}}


def analyze(account, cur, prev, campaigns, geo):
    """Return {working, not_working, recommendations}."""
    working, not_working, recs = [], [], []
    spend, conv = cur["spend"], cur["conversions"]
    camps = sorted(campaigns, key=lambda c: c["spend"], reverse=True)

    # ---- account-level signals ----
    cpa_delta = _pct(cur["cpa"], prev["cpa"]) if prev and prev["cpa"] else None
    conv_delta = _pct(cur["conversions"], prev["conversions"]) if prev else None

    if spend > 0 and conv == 0:
        not_working.append(f"${spend:,.0f} spent, 0 conversions — tracking or offer issue")
        recs.append({"priority": "HIGH",
                     "title": "Verify conversion tracking / pixel",
                     "detail": f"${spend:,.0f} spent this period with zero recorded conversions. "
                               "Check pixel + event setup before spending more.",
                     "impact": "unblocks all optimization"})
    if cpa_delta is not None and cpa_delta <= -10:
        working.append(f"CPA improving — down {abs(cpa_delta):.0f}% vs previous period")
    if cpa_delta is not None and cpa_delta >= 15:
        not_working.append(f"CPA rising — up {cpa_delta:.0f}% vs previous period")
        recs.append({"priority": "MED", "title": "CPA climbing — review bids & targeting",
                     "detail": f"Cost per result up {cpa_delta:.0f}%. Check audience overlap, "
                               "frequency, and bid strategy.",
                     "impact": "protect efficiency"})
    if conv_delta is not None and conv_delta >= 15:
        working.append(f"Conversions up {conv_delta:.0f}% vs previous period")

    # ---- campaign-level signals ----
    for c in camps:
        if c["spend"] >= DEAD_SPEND and c["conversions"] == 0 and c["status"] == "ACTIVE":
            not_working.append(f'"{c["name"]}" — ${c["spend"]:,.0f} spent, 0 conv')
            recs.append({"priority": "HIGH", "title": f'Pause "{c["name"]}"',
                         "detail": f'${c["spend"]:,.0f} spent, 0 conversions, '
                                   f'CTR {c["ctr"]:.1f}%. Strong kill candidate.',
                         "impact": f'save ~${c["spend"]/_days_guard(cur):.0f}/day'})
        elif c["conversions"] >= WINNER_MIN_CONV and c["ctr"] >= BENCHMARK_CTR * 2 \
                and c["cpa"] and c["cpa"] <= (cur["cpa"] or 1e9):
            working.append(f'"{c["name"]}" winning — CTR {c["ctr"]:.1f}%, CPA ${c["cpa"]:.1f}')
            recs.append({"priority": "MED", "title": f'Scale "{c["name"]}" +20%',
                         "detail": f'CTR {c["ctr"]:.1f}%, CPA ${c["cpa"]:.1f} beats account avg. '
                                   "Room to scale budget.",
                         "impact": "more results at current efficiency"})

    # weak creative across spenders
    weak = [c for c in camps[:5] if c["spend"] > 0 and c["ctr"] < BENCHMARK_CTR]
    if len(weak) >= 2:
        not_working.append(f"{len(weak)} active campaigns below {BENCHMARK_CTR}% CTR")
        recs.append({"priority": "MED", "title": "Refresh fatigued / weak creative",
                     "detail": f'{len(weak)} top-spending campaigns under {BENCHMARK_CTR}% CTR '
                               "— rotate in fresh creative.",
                     "impact": "lift CTR, lower CPC"})

    # spend concentration
    if camps and spend > 0:
        top_share = camps[0]["spend"] / spend * 100
        if top_share >= 60:
            not_working.append(f'{top_share:.0f}% of spend in one campaign ("{camps[0]["name"]}")')
            recs.append({"priority": "LOW", "title": "Reduce concentration risk",
                         "detail": f'{top_share:.0f}% of budget rides on one campaign. '
                                   "Diversify winning structures.",
                         "impact": "stability"})

    # geo concentration (informational win/risk)
    if geo:
        gtot = sum(g["spend"] for g in geo) or 1
        top = max(geo, key=lambda g: g["spend"])
        if top["spend"] / gtot >= 0.7 and top["conversions"] > 0:
            working.append(f'{top["country"]} driving {top["spend"]/gtot*100:.0f}% of spend with results')

    if not working:
        working.append("No standout wins yet this period")
    if not not_working:
        not_working.append("No blocking issues detected 🎉")

    order = {"HIGH": 0, "MED": 1, "LOW": 2}
    recs.sort(key=lambda r: order[r["priority"]])
    return {"working": working[:6], "not_working": not_working[:6], "recommendations": recs[:6]}


def _days_guard(cur):
    return max(1, cur.get("days", 7))
