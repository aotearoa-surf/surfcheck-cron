"""Tide-only cycle — NIWA tides, run once per 24 hours.

Tides barely change between refreshes (the table is essentially deterministic
for any given lat/lng), so daily-cadence is fine. Keeping this script separate
from _fetch_main.py keeps Stormglass cycles (every 3h) fast and respects
NIWA's free-tier quota (5,000 calls/month — at ~95 clusters/day = 2,850/month,
well under).

Updates the following slot_forecast columns:
  tide_height_m, tide_direction,
  tide_event_type, tide_event_time, tide_event_height_m

Assumes slot_forecast rows already exist (created by _fetch_main.py). This
script ONLY refreshes the tide columns on those existing rows.
"""
import json, os, sys, io, time, re
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
SB_HEADERS = {
    "apikey": KEY, "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}
SB_HEADERS_RETURN = {**SB_HEADERS, "Prefer": "return=representation"}

NIWA_KEY = os.environ["NIWA_KEY"]
NZ_TZ    = timezone(timedelta(hours=12))


def sb_select(table, params=""):
    r = requests.get(f"{URL}/rest/v1/{table}?select=*{params}", headers=SB_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_insert(table, rows, return_data=False):
    headers = SB_HEADERS_RETURN if return_data else {**SB_HEADERS, "Prefer": "return=minimal"}
    r = requests.post(f"{URL}/rest/v1/{table}", headers=headers, json=rows, timeout=60)
    if r.status_code >= 400:
        print(f"  ERROR {r.status_code}: {r.text}", flush=True)
        r.raise_for_status()
    return r.json() if return_data else None


def sb_update(table, row_id, updates):
    r = requests.patch(f"{URL}/rest/v1/{table}?id=eq.{row_id}",
                       headers=SB_HEADERS, json=updates, timeout=30)
    if r.status_code >= 400:
        print(f"  ERROR {r.status_code}: {r.text}", flush=True)
        r.raise_for_status()


def sb_upsert_chunk(table, rows, on_conflict):
    headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(f"{URL}/rest/v1/{table}?on_conflict={on_conflict}",
                      headers=headers, json=rows, timeout=120)
    if r.status_code >= 400:
        print(f"  ERROR {r.status_code}: {r.text[:500]}", flush=True)
        r.raise_for_status()


def fetch_niwa_tide(lat, lng, max_retries=3):
    # NIWA's free tier allows up to 30 days per call. Pulling the full window
    # lets us store long-range tide events (which are deterministic) and
    # gives us a buffer against future API failures.
    url = (f"https://api.niwa.co.nz/tides/data?lat={lat}&long={lng}"
           f"&numberOfDays=30&datum=MSL&interval=30&apikey={NIWA_KEY}")
    for attempt in range(max_retries):
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            backoff = 5 * (attempt + 1)
            print(f"    NIWA 429 — backing off {backoff}s (attempt {attempt+1}/{max_retries})", flush=True)
            time.sleep(backoff)
            continue
        r.raise_for_status()
    raise RuntimeError(f"NIWA rate-limited after {max_retries} retries")


def tide_cluster_key(lat, lng):
    return (round(lat / 0.2) * 0.2, round(lng / 0.2) * 0.2)


def niwa_extremes(niwa):
    """Plateau-aware extrema with parabolic interpolation for sub-30-min precision."""
    if not niwa or not isinstance(niwa.get("values"), list): return []
    v = niwa["values"]
    events = []
    for i in range(1, len(v)-1):
        a, b, c = v[i-1]["value"], v[i]["value"], v[i+1]["value"]
        if a is None or b is None or c is None: continue
        kind = None; offset_min = 0
        if b > a and b > c: kind = "H"
        elif b < a and b < c: kind = "L"
        elif b == c and i+2 < len(v) and v[i+2]["value"] is not None:
            d = v[i+2]["value"]
            if b > a and b > d: kind = "H"; offset_min = 15
            elif b < a and b < d: kind = "L"; offset_min = 15
        if not kind: continue
        if offset_min == 0:
            denom = a - 2*b + c
            if denom != 0:
                off = 0.5 * (a - c) / denom
                offset_min = max(-15, min(15, off * 30))
        t_utc = datetime.fromisoformat(v[i]["time"].replace("Z","+00:00"))
        t_utc = t_utc + timedelta(minutes=offset_min)
        events.append({"time_utc": t_utc, "type": kind, "height": b})
    return events


def tide_event_for_slot(events, slot_dt_nz):
    """NZ time-of-day bucketing: same calendar day, 6h buckets."""
    target_date = slot_dt_nz.date()
    bucket = 0 if slot_dt_nz.hour < 6 else 6 if slot_dt_nz.hour < 12 else 12 if slot_dt_nz.hour < 18 else 18
    for ev in events:
        nz = ev["time_utc"].astimezone(NZ_TZ)
        if nz.date() != target_date: continue
        nzh = nz.hour
        eb = 0 if nzh < 6 else 6 if nzh < 12 else 12 if nzh < 18 else 18
        if eb == bucket:
            local_time = nz.strftime("%I:%M%p").lower().lstrip("0")
            return {"type": ev["type"], "time": local_time, "height": ev["height"]}
    return None


def fetch_om_marine_for_sealevel(lat, lng):
    """Open-Meteo Marine gives a 30-min sea_level_height_msl array we use for the
    rising/falling direction on non-event slots."""
    h = "sea_level_height_msl"
    return requests.get(
        f"https://marine-api.open-meteo.com/v1/marine?latitude={lat}&longitude={lng}"
        f"&hourly={h}&timezone=Pacific%2FAuckland&forecast_days=7", timeout=30
    ).json()


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════
def main():
    log = sb_insert("fetch_log", [{"status":"running", "notes":"tides"}], return_data=True)
    log_id = log[0]["id"]
    started = time.time()
    errors = 0

    try:
        spots = sb_select("spots")
        print(f"[tides] Loaded {len(spots)} spots\n", flush=True)

        # ── Load existing tide coverage so we NEVER overwrite known-good data ──
        # Tide events are astronomical predictions and don't change once computed.
        # Once a (spot_id, slot_key) row has any tide data, it stays locked —
        # giving us resilience against future NIWA outages and a growing buffer
        # of stored predictions.
        print("[init] Loading existing tide coverage from DB…", flush=True)
        existing_pages = []
        offset = 0
        while True:
            r = requests.get(
                f"{URL}/rest/v1/slot_forecast?select=spot_id,slot_key,tide_event_type,tide_height_m"
                f"&offset={offset}&limit=1000",
                headers=SB_HEADERS, timeout=60,
            )
            r.raise_for_status()
            chunk = r.json()
            existing_pages.extend(chunk)
            if len(chunk) < 1000: break
            offset += 1000
        locked_keys = set(
            (row["spot_id"], row["slot_key"])
            for row in existing_pages
            if row.get("tide_event_type") is not None or row.get("tide_height_m") is not None
        )
        print(f"[init] {len(locked_keys)} slots already have tide data — will skip those\n", flush=True)

        # Cluster-cache NIWA fetches
        niwa_cache = {}        # cluster -> events list
        seal_cache = {}        # cluster -> sea_level array + time array
        all_rows = []
        cycle_fetched_at = datetime.now(timezone.utc).isoformat()

        for idx, s in enumerate(spots, 1):
            try:
                ck = tide_cluster_key(s["lineup_lat"], s["lineup_lng"])
                if ck not in niwa_cache:
                    try:
                        niwa = fetch_niwa_tide(*ck)
                        niwa_cache[ck] = niwa_extremes(niwa)
                        time.sleep(1.1)
                    except Exception as e:
                        print(f"    NIWA failed for cluster {ck}: {e}", flush=True)
                        niwa_cache[ck] = []
                tide_events = niwa_cache[ck]

                if ck not in seal_cache:
                    try:
                        om = fetch_om_marine_for_sealevel(*ck)
                        seal_cache[ck] = {
                            "times": om["hourly"]["time"],
                            "values": om["hourly"]["sea_level_height_msl"],
                        }
                    except Exception:
                        seal_cache[ck] = {"times": [], "values": []}

                # Build slot keys for this spot's 7-day window starting today
                today_nz = datetime.now(NZ_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                seal = seal_cache[ck]
                # Index sea_level by NZ hour-key for height + direction
                seal_idx = {}
                for i, t in enumerate(seal["times"]):
                    dt = datetime.fromisoformat(t).replace(tzinfo=NZ_TZ)
                    seal_idx[dt.strftime("%Y-%m-%dT%H")] = i

                # 30-day window. Days 1-7 get Open-Meteo continuous height +
                # NIWA events; days 8-30 get NIWA events only (Marine API caps
                # at 7 days). Slots with NO tide data at all are skipped — no
                # point inserting empty placeholder rows.
                for d in range(30):
                    base = today_nz + timedelta(days=d)
                    for h in (0, 6, 12, 18):
                        slot_dt = base.replace(hour=h)
                        slot_key = slot_dt.strftime("%Y-%m-%dT%H")
                        # Skip locked rows — existing tide data is final, don't
                        # overwrite. This protects against NIWA returning bad
                        # values on a future run.
                        if (s["id"], slot_key) in locked_keys:
                            continue
                        si = seal_idx.get(slot_key)
                        tide_height = seal["values"][si] if si is not None and si < len(seal["values"]) else None
                        prev_height = seal["values"][si-1] if si is not None and 0 < si < len(seal["values"]) else None
                        tide_direction = None
                        if tide_height is not None and prev_height is not None:
                            tide_direction = "rising" if tide_height > prev_height else "falling"
                        ev = tide_event_for_slot(tide_events, slot_dt) if tide_events else None
                        # Skip slots with nothing useful to write
                        if tide_height is None and not ev:
                            continue
                        all_rows.append({
                            "spot_id": s["id"],
                            "slot_key": slot_key,
                            "tide_height_m": tide_height,
                            "tide_direction": tide_direction,
                            "tide_event_type": ev["type"] if ev else None,
                            "tide_event_time": ev["time"] if ev else None,
                            "tide_event_height_m": ev["height"] if ev else None,
                            "fetched_at": cycle_fetched_at,
                        })
                if idx % 20 == 0:
                    print(f"  {idx}/{len(spots)} spots · {len(niwa_cache)} NIWA clusters cached", flush=True)
            except Exception as e:
                print(f"  ✗ {s['name']}: {e}", flush=True)
                errors += 1

        # Upsert
        print(f"\n[done] Writing {len(all_rows)} tide rows to Supabase…", flush=True)
        CHUNK = 200
        rows_written = 0
        for i in range(0, len(all_rows), CHUNK):
            sb_upsert_chunk("slot_forecast", all_rows[i:i+CHUNK], on_conflict="spot_id,slot_key")
            rows_written += min(CHUNK, len(all_rows) - i)
            print(f"  Wrote {rows_written}/{len(all_rows)}", flush=True)

        sb_update("fetch_log", log_id, {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "success" if errors == 0 else "partial",
            "spots_updated": len(spots) - errors,
            "errors_count": errors,
            "notes": f"tides: wrote {rows_written} tide updates in {time.time()-started:.0f}s",
        })
        print(f"\n✅ Tides cycle complete in {time.time()-started:.0f}s · {rows_written} rows · {errors} errors", flush=True)
    except Exception as e:
        sb_update("fetch_log", log_id, {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed", "notes": f"tides: {e}",
        })
        raise

if __name__ == "__main__":
    main()
