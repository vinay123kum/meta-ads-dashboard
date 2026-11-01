"""
Import Meta (+Instagram) monthly budgets from the free-form media-plan sheet
into account_budgets. Parses each location tab's latest month, maps the tab to
the matching ad account, and sets the budget. Values are a DRAFT to verify in
the Budgets screen (the source sheet is inconsistent).
"""
import io
import re
import urllib.request

import openpyxl

import db
from config import BUDGET_SHEET_ID

PLAT = ("meta", "instagram", "google", "seo")
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def _pmoney(s):
    s = str(s or "").strip()
    m = re.fullmatch(r"\$?\s*([\d,]+(?:\.\d+)?)\s*\$?", s)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v if 0 < v < 100000 else None


def _month_of(t):
    t = t.lower()
    for i, mn in enumerate(MONTHS, 1):
        if mn in t and "2026" in t:
            return i
    m = re.search(r"\d{2}/(\d{2})/2026", t)
    return int(m.group(1)) if m else None


def parse_tab(ws):
    """Return list of (month, meta_ig_budget) blocks for one location tab."""
    rows = [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
    hdrs = [i for i, r in enumerate(rows)
            if any("budget" in c.lower() for c in r) and any("platform" in c.lower() for c in r)]
    out = []
    for k, hi in enumerate(hdrs):
        end = hdrs[k + 1] if k + 1 < len(hdrs) else len(rows)
        hdr = [c.lower() for c in rows[hi]]
        pcol = next((j for j, c in enumerate(hdr) if "platform" in c), 0)
        bcol = next((j for j, c in enumerate(hdr) if "budget" in c), pcol + 2)
        mon = None
        for up in range(hi, max(-1, hi - 6), -1):
            mon = _month_of(" ".join(rows[up]))
            if mon:
                break
        cur, mig = None, 0.0
        for r in rows[hi + 1:end]:
            pv = r[pcol].lower() if pcol < len(r) else ""
            p = next((x for x in PLAT if pv.startswith(x)), None)
            if p:
                cur = p
            b = _pmoney(r[bcol]) if bcol < len(r) else None
            if b and cur in ("meta", "instagram"):
                mig += b
        out.append((mon, mig))
    return out


def latest_meta_budget(ws):
    """Pick the newest month with a Meta+IG budget; return (month, amount)."""
    blocks = [b for b in parse_tab(ws) if b[0]]
    if not blocks:
        return None, 0.0
    target = max(b[0] for b in blocks)
    for mon, amt in sorted(blocks, key=lambda b: -b[0]):
        if mon == target and amt > 0:
            return mon, amt
    for mon, amt in sorted(blocks, key=lambda b: -b[0]):   # fallback: latest non-zero
        if amt > 0:
            return mon, amt
    return target, 0.0


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def map_accounts(con, tab_budgets):
    """Map {tab: (month, amount)} -> account rows, picking highest-spend match on ties."""
    accts = con.execute("select id, name from accounts").fetchall()
    spend = {r["account_id"]: r["s"] for r in con.execute(
        "select account_id, sum(spend) s from insights_daily where campaign_id='ACCOUNT' "
        "group by account_id").fetchall()}
    results = []
    for tab, (mon, amt) in tab_budgets.items():
        tok = _norm(tab)
        cands = [a for a in accts if tok and tok in _norm(a["name"])]
        acct = max(cands, key=lambda a: spend.get(a["id"], 0)) if cands else None
        results.append({"tab": tab, "month": mon, "amount": amt,
                        "account_id": acct["id"] if acct else None,
                        "account_name": acct["name"] if acct else None})
    return results


def _load_wb():
    """Fetch the live sheet (public export) — falls back handled by caller."""
    url = f"https://docs.google.com/spreadsheets/d/{BUDGET_SHEET_ID}/export?format=xlsx"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=45).read()
    if data[:2] != b"PK":
        raise RuntimeError("budget sheet not publicly accessible")
    return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)


def run(con, wb=None):
    if wb is None:
        wb = _load_wb()
    tab_budgets = {ws.title.strip(): latest_meta_budget(ws) for ws in wb.worksheets}
    mapping = map_accounts(con, tab_budgets)
    import budget as budget_mod
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    for m in mapping:
        if m["account_id"] and m["amount"] > 0:
            budget_mod.set_budget(con, m["account_id"], m["amount"], now)
            applied += 1
    return {"applied": applied, "mapping": mapping}
