"""Populate the `spot_now` aggregate (1 row per spot) from slot_forecast.
Each row = the spot's CURRENT 6-hour slot + a `days` JSONB array of the best
slot per day (up to 7). The four list pages read this instead of paginating
~3,900 raw slots.

- build_rows(slots): pure transform, reused by _fetch_main each cycle.
- run as a script: BACKFILL now from the DB (no Stormglass/Open-Meteo calls).
"""
import os, sys, io, json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv

NZ = timezone(timedelta(hours=12))

def current_slot_key(nz_now=None):
    nz_now = nz_now or datetime.now(NZ)
    bucket = (nz_now.hour // 6) * 6
    return f"{nz_now.strftime('%Y-%m-%d')}T{bucket:02d}"

def _fields(r):
    if not r: return None
    return {
        "slot_key": r["slot_key"], "slot_time": r.get("slot_time"),
        "rating_score": r["rating_score"], "rating_label": r["rating_label"],
        "rating_reason": r.get("rating_reason"),
        "wave_m": r["wave_m"], "wind_kt": r["wind_kt"], "wind_deg": r.get("wind_deg"),
        "period_s": r["period_s"],
    }

def build_rows(slots, cur=None):
    """slots: iterable of slot_forecast dicts. Returns list of spot_now rows.

    now      = the current slot (>= cur), T00 included — live is live.
    days[]   = per day: am (T06), pm (best of T12/T18), and the day's `best`
               (the higher of am/pm). T00 is EXCLUDED from these "best of"
               representations (nobody surfs at midnight). Today's already-past
               slots are skipped.
    """
    cur = cur or current_slot_key()
    today = cur.split("T")[0]
    by_spot = defaultdict(list)
    for r in slots:
        if r.get("rating_score") is None:
            continue
        by_spot[r["spot_id"]].append(r)
    out = []
    for sid, rs in by_spot.items():
        rs.sort(key=lambda r: r["slot_key"])
        now = next((r for r in rs if r["slot_key"] >= cur), rs[0])
        by_date = defaultdict(dict)   # date -> {"00":row,"06":row,"12":row,"18":row}
        for r in rs:
            sk = r.get("slot_key") or ""
            if "T" not in sk:
                continue
            d, hh = sk.split("T", 1)
            if d == today and sk < cur:
                continue                       # skip today's already-past slots
            by_date[d][hh] = r
        days = []
        for d in sorted(by_date):
            hh = by_date[d]
            t00, t06, t12, t18 = hh.get("00"), hh.get("06"), hh.get("12"), hh.get("18")
            surfable = [x for x in (t06, t12, t18) if x]   # "best of" EXCLUDES T00
            if not surfable:
                continue
            best = max(surfable, key=lambda r: r["rating_score"] or 0)
            entry = _fields(best)          # inline = the day's best (spots chip strip, no T00)
            entry["date"] = d
            entry["t00"] = _fields(t00)    # all 4 sessions for the forecast detail grid
            entry["t06"] = _fields(t06)
            entry["t12"] = _fields(t12)
            entry["t18"] = _fields(t18)
            days.append(entry)
        days = days[:7]
        out.append({
            "spot_id": sid,
            "slot_key": now["slot_key"], "slot_time": now.get("slot_time"),
            "wave_m": now["wave_m"], "wind_kt": now["wind_kt"], "wind_deg": now.get("wind_deg"),
            "period_s": now["period_s"], "rating_score": now["rating_score"],
            "rating_label": now["rating_label"], "rating_reason": now.get("rating_reason"),
            "days": days,
            "fetched_at": now.get("fetched_at"),
        })
    return out

def upsert_spot_now(rows, URL, H):
    for i in range(0, len(rows), 200):
        r = requests.post(f"{URL}/rest/v1/spot_now?on_conflict=spot_id",
                          headers={**H, "Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates,return=minimal"},
                          data=json.dumps(rows[i:i+200]), timeout=60)
        if r.status_code >= 400:
            print("  ERROR", r.status_code, r.text[:400]); r.raise_for_status()

def backfill():
    load_dotenv()
    URL = os.environ["SUPABASE_URL"].rstrip("/")
    KEY = os.environ["SUPABASE_SERVICE_KEY"]
    H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    today = datetime.now(NZ).strftime("%Y-%m-%d")
    slots, off = [], 0
    while True:
        c = requests.get(f"{URL}/rest/v1/slot_forecast", headers=H, params={
            "slot_key": f"gte.{today}", "rating_score": "not.is.null",
            "select": "spot_id,slot_key,slot_time,wave_m,wind_kt,wind_deg,period_s,rating_score,rating_label,rating_reason,fetched_at",
            "order": "slot_key.asc", "offset": off, "limit": 1000}, timeout=90).json()
        slots += c
        if len(c) < 1000:
            break
        off += 1000
    rows = build_rows(slots)
    upsert_spot_now(rows, URL, H)
    print(f"spot_now backfilled: {len(rows)} spots from {len(slots)} slots (cur slot {current_slot_key()})")

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    backfill()
