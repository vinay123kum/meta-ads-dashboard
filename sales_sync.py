"""
Sales sync from the bookings sheet. Reads every location tab, filters junk/test
rows, dedupes, and stores ONLY non-identifying fields + a one-way hash (no
names/emails/phones ever land in the dashboard DB).

Sources, in priority order:
  1. Service account (GOOGLE_SA_JSON)     — private, recommended
  2. Public xlsx export URL               — only works if the sheet is link-viewable
  3. load_from_xlsx(path)                 — local file (used for the initial load)
"""
import io
import re
import hashlib
import json
import urllib.request
from datetime import datetime, timezone

import openpyxl

import db
from config import SALES_SHEET_ID, GOOGLE_SA_JSON


def _n(x):
    return str(x).strip() if x is not None else ""


def _digits(x):
    return re.sub(r"\D", "", str(x or ""))


def _process_sheet(title, rows):
    """rows: list of row tuples for one tab -> (clean_rows, stats)."""
    if not rows:
        return [], {"raw": 0, "junk": 0, "test": 0, "dup": 0}
    hi = next((i for i, r in enumerate(rows) if r and any(_n(c) == "Timestamp" for c in r)), 0)
    hdr = [_n(c).lower() for c in rows[hi]]

    def col(name):
        return next((i for i, h in enumerate(hdr) if name in h), None)

    ci = {k: col(k) for k in ["first name", "email", "phone", "product name",
                              "utm source", "utm medium", "utm campaign", "timestamp"]}
    out, seen = [], set()
    stats = {"raw": 0, "junk": 0, "test": 0, "dup": 0}
    for r in rows[hi + 1:]:
        if not r or all(_n(c) == "" for c in r):
            continue
        stats["raw"] += 1
        g = lambda k: _n(r[ci[k]]) if ci[k] is not None and ci[k] < len(r) else ""
        fn, em, ph, prod, ts = g("first name"), g("email"), g("phone"), g("product name"), g("timestamp")
        blob = (prod + " " + g("utm source") + " " + g("utm campaign") + " " + fn + " " + em).lower()
        if "utmtest" in blob or "utm test" in blob or em.lower() == "tarun@gmail.com":
            stats["test"] += 1
            continue
        has_id = fn not in ("", "undefined") or em not in ("", "undefined")
        if not (has_id and prod not in ("", "undefined")):
            stats["junk"] += 1
            continue
        date = ts[:10]
        raw_key = f"{em.lower() or _digits(ph)}|{prod.lower()}|{date}"
        if raw_key in seen:
            stats["dup"] += 1
            continue
        seen.add(raw_key)
        out.append({
            "dedup_key": hashlib.sha1(raw_key.encode()).hexdigest(),
            "location": title, "date": date, "ts": ts, "product": prod,
            "utm_source": g("utm source"), "utm_medium": g("utm medium"),
            "utm_campaign": g("utm campaign"),
        })
    return out, stats


def _ingest_wb(con, wb):
    agg = {"raw": 0, "junk": 0, "test": 0, "dup": 0, "new": 0}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        out, stats = _process_sheet(ws.title, rows)
        for row in out:
            cur = con.execute(
                "insert or ignore into sales(dedup_key,location,date,ts,product,"
                "utm_source,utm_medium,utm_campaign) values(?,?,?,?,?,?,?,?)",
                (row["dedup_key"], row["location"], row["date"], row["ts"], row["product"],
                 row["utm_source"], row["utm_medium"], row["utm_campaign"]))
            agg["new"] += cur.rowcount
        for k in ("raw", "junk", "test", "dup"):
            agg[k] += stats[k]
    con.execute("insert or replace into app_meta(k,v) values('sales_last_sync',?)",
                (datetime.now(timezone.utc).isoformat(),))
    con.execute("insert or replace into app_meta(k,v) values('sales_stats',?)", (json.dumps(agg),))
    con.commit()
    return agg


def load_from_xlsx(con, path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return _ingest_wb(con, wb)


def _fetch_export_xlsx():
    url = f"https://docs.google.com/spreadsheets/d/{SALES_SHEET_ID}/export?format=xlsx"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=45).read()
    if data[:2] != b"PK":   # xlsx is a zip; HTML login page means no access
        raise RuntimeError("sheet not publicly accessible")
    return data


def sync(con):
    """Live sync via service account or public export URL."""
    if GOOGLE_SA_JSON:
        try:
            import gspread
            gc = gspread.service_account(filename=GOOGLE_SA_JSON)
            sh = gc.open_by_key(SALES_SHEET_ID)
            # build an in-memory workbook-like via openpyxl from CSV export isn't needed;
            # gspread gives us values directly
            agg = {"raw": 0, "junk": 0, "test": 0, "dup": 0, "new": 0}
            for ws in sh.worksheets():
                rows = ws.get_all_values()
                out, stats = _process_sheet(ws.title, rows)
                for row in out:
                    cur = con.execute(
                        "insert or ignore into sales(dedup_key,location,date,ts,product,"
                        "utm_source,utm_medium,utm_campaign) values(?,?,?,?,?,?,?,?)",
                        (row["dedup_key"], row["location"], row["date"], row["ts"], row["product"],
                         row["utm_source"], row["utm_medium"], row["utm_campaign"]))
                    agg["new"] += cur.rowcount
                for k in ("raw", "junk", "test", "dup"):
                    agg[k] += stats[k]
            con.execute("insert or replace into app_meta(k,v) values('sales_last_sync',?)",
                        (datetime.now(timezone.utc).isoformat(),))
            con.execute("insert or replace into app_meta(k,v) values('sales_stats',?)",
                        (json.dumps(agg),))
            con.commit()
            return {"ok": True, "via": "service_account", **agg}
        except Exception as e:
            return {"ok": False, "reason": f"service account error: {str(e)[:120]}"}
    # fallback: public export URL
    try:
        data = _fetch_export_xlsx()
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        agg = _ingest_wb(con, wb)
        return {"ok": True, "via": "public_url", **agg}
    except Exception as e:
        return {"ok": False, "reason": (
            "Can't reach the sheet automatically. Either set the sheet to "
            "'Anyone with link → Viewer', or add a Google service account "
            f"(GOOGLE_SA_JSON) and share the sheet with it. ({str(e)[:60]})")}
