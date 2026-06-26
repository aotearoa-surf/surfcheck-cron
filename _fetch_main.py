"""Main forecast cycle — Stormglass + Open-Meteo + rating computation.

Runs every 3 hours via cron. Does NOT touch tide data (that's _fetch_tides.py
running daily, since tides barely change between forecast updates).

Updates the following slot_forecast columns:
  wave_m, wind_kt, wind_deg, wind_gust, period_s, swell_deg,
  weather_code, air_c, water_c, precip_pct, uv,
  rating_score, rating_label, rating_wave_type,
  rating_wind_class, rating_wind_strength, rating_reason,
  fetched_at

Leaves untouched (managed by _fetch_tides.py):
  tide_height_m, tide_direction, tide_event_type,
  tide_event_time, tide_event_height_m

Cost per cycle: 48 Stormglass + 244 Open-Meteo. Stormglass quota safe at 3h cadence.
"""
import json, os, sys, io, time, re
from pathlib import Path
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
    "Prefer": "resolution=merge-duplicates,return=minimal",
}
SB_HEADERS_RETURN = {**SB_HEADERS, "Prefer": "return=representation"}

SG_KEY = os.environ["STORMGLASS_KEY"]
OM_KEY = os.environ["OPEN_METEO_KEY"]
NZ_TZ  = timezone(timedelta(hours=12))
SG_PARAMS = "waveHeight,swellHeight,swellPeriod,swellDirection,windSpeed,windDirection"
SOURCE_PREF = ["ecmwf","sg","noaa","dwd","icon","meteo","smhi"]
# Our sources report a MEAN swell period (Tm); surf forecasts (Surfline etc.) show
# PEAK period (Tp). Tp ~ 1.2 * Tm is the textbook ratio, so we convert once at fetch
# to match them (and stop under-rating clean long-period swells). (Che 2026-06-25)
PEAK_PERIOD_FACTOR = 1.2


def http_get(url, headers=None, timeout=25, retries=2, backoff=2):
    """GET -> JSON with a SHORT per-attempt timeout + one light retry.

    Open-Meteo / Stormglass normally answer in 1-3 s, so a 25 s ceiling lets a
    genuinely-stuck spot fail fast (instead of burning 60 s each and blowing the
    whole-run budget when the API has a slow spell). One retry recovers the
    common transient blip. Genuine 4xx client errors fail immediately."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers or {}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt < retries - 1:
                time.sleep(backoff)   # brief pause before the single retry
    raise last


def sb_select(table, params=""):
    r = requests.get(f"{URL}/rest/v1/{table}?select=*{params}", headers=SB_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_insert(table, rows, return_data=False):
    headers = SB_HEADERS_RETURN if return_data else SB_HEADERS
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


# ── Rating engine (same as _fetch_cycle.py) ────────────────────────────────
COMPASS_TO_DEG = {"N":0,"NE":45,"E":90,"SE":135,"S":180,"SW":225,"W":270,"NW":315}

def compass_to_deg(s):
    if not s: return None
    parts = re.split(r"[-/]", s.upper().replace(" ", ""))
    degs = [COMPASS_TO_DEG[p] for p in parts if p in COMPASS_TO_DEG]
    if not degs: return None
    if len(degs) == 1: return degs[0]
    a, b = degs[0], degs[1]
    if abs(a - b) > 180:
        if a < b: a += 360
        else: b += 360
    return ((a + b) / 2) % 360

def angle_delta(d1, d2):
    if d1 is None or d2 is None: return None
    diff = abs(((d1 - d2) % 360 + 360) % 360)
    return 360 - diff if diff > 180 else diff

def parse_size(s):
    if not s: return {"min":0.3,"max":2.5,"opt":1.2,"soft":False}
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(\+?)\s*m?", s, re.I)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return {"min":lo,"max":hi,"opt":(lo+hi)/2,"soft":m.group(3)=="+"}
    return {"min":0.3,"max":2.5,"opt":1.2,"soft":False}

def wind_score(off_deg, kt, wd):
    """Wind score (0-10). 40% of the overall spot rating.

    Direction (absolute degrees off the spot's offshore direction):
        offshore         0-30
        cross-offshore  30-60
        cross-shore     60-120
        cross-onshore  120-150
        onshore        150-180

    Speed buckets: <8 / 8-12 / 12-20 / 20+ kt.

    Peak = 9 (light <8kt offshore). 10 deliberately unreachable — stronger
    offshore wind brings spray, paddle drag, and wind ripple, so it isn't
    actually "perfect" at any wind speed. The genuinely best wind is near-
    glassy + just enough offshore to clean the face.
    """
    if kt is None: return 5
    kmh = kt * 1.852
    band = 0 if kmh < 5 else 1 if kmh < 12 else 2 if kmh < 25 else 3 if kmh < 35 else 4
    if off_deg is None or wd is None:
        return [7, 6, 4, 3, 0][band]   # unknown offshore: treat as cross-shore (neutral)
    d = angle_delta(wd, off_deg)
    if   d <= 30:  cat = "off"
    elif d <= 60:  cat = "cross_off"
    elif d <= 120: cat = "cross"
    elif d <= 150: cat = "cross_on"
    else:          cat = "on"
    table = {
        "off":       [9, 8, 7, 6, 3],
        "cross_off": [9, 7, 7, 5, 2],
        "cross":     [7, 6, 4, 3, 0],
        "cross_on":  [6, 5, 2, 1, 0],
        "on":        [6, 4, 1, 0, 0],
    }
    return table[cat][band]

def size_score(w, sz):
    if w is None: return 5
    mn, mx, opt, soft = sz["min"], sz["max"], sz["opt"], sz["soft"]
    if w < mn: return max(0, (w/mn) * 4)
    if w <= opt:
        t = (w - mn) / max(0.01, opt - mn)
        return 6 + 3*t
    if w <= mx:
        t = (w - opt) / max(0.01, mx - opt)
        return 9 - 2*t
    over = (w - mx) / max(0.5, mx)
    if soft: return max(5, 7 - 1.5*over)
    return max(2, 7 - 4*over)

def period_score(p):
    """Smooth (interpolated) period quality so a sub-second change can't flip a tier.
    Was stepped 3/5/7/9/10 at 6/8/10/13s, which jumped under whole-second display
    rounding (a 9.7s and a 10.0s both show '10s' but scored 7 vs 9). Anchored to the
    old band centres, so the overall distribution is ~unchanged (2026-06-27)."""
    if p is None: return 5
    pts = ((5, 3), (7, 5), (9, 7), (11.5, 9), (14, 10))
    if p <= pts[0][0]: return 3.0
    if p >= pts[-1][0]: return 10.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if p <= x1:
            return round(y0 + (y1 - y0) * (p - x0) / (x1 - x0), 2)
    return 10.0

def swell_dir_score(sd, wd):
    """Smooth (interpolated) swell-window alignment so a few degrees can't flip a tier.
    Was stepped 9/6/3 at 30/60deg; a 28deg and 32deg swell both show e.g. 'E' but scored
    9 vs 6. Anchored to the old band centres, ~distribution-neutral (2026-06-27)."""
    if wd is None or sd is None: return 7
    d = angle_delta(sd, wd)
    pts = ((15, 9), (45, 6), (75, 3))
    if d <= pts[0][0]: return 9.0
    if d >= pts[-1][0]: return 3.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if d <= x1:
            return round(y0 + (y1 - y0) * (d - x0) / (x1 - x0), 2)
    return 3.0

def classify_wind_dir(off, wd):
    """Five-category classification matching wind_score's bins."""
    if off is None or wd is None: return None
    d = angle_delta(wd, off)
    if d <= 30:  return "Offshore"
    if d <= 60:  return "Cross-offshore"
    if d <= 120: return "Cross-shore"
    if d <= 150: return "Cross-onshore"
    return "Onshore"

def classify_wind_strength(kt):
    """Speed buckets aligned with wind_score: <5 Calm, <12 Light, <20 Moderate, 20+ Strong."""
    if kt is None: return None
    kmh = kt * 1.852
    if kmh < 5:  return "Calm"
    if kmh < 12: return "Light"
    if kmh < 25: return "Moderate"
    if kmh < 35: return "Fresh"
    return "Strong"

def classify_wave_type(kt, cls):
    """Wave-surface label for the rating-reason string."""
    if kt is None: return None
    if kt < 5: return "Glassy"
    if kt > 20: return "Blown Out"
    # Offshore + cross-offshore: clean unless wind builds
    if cls in ("Offshore", "Cross-offshore"):
        return "Clean" if kt < 12 else "Bumpy"
    # Cross-shore: bumpy at lower winds, messy at higher
    if cls == "Cross-shore":
        return "Bumpy" if kt < 12 else "Messy"
    # Cross-onshore + onshore: always messy at any rideable wind
    return "Messy"

def score_to_label(s):
    if s < 2:   return "Flat"
    if s < 4:   return "Grim"
    if s < 5:   return "Poor"
    if s < 6:   return "Fair"
    if s < 7.3: return "Good"
    if s < 8.3: return "Mint"
    return "Epic"

DIR_CHARS = ["N","NE","E","SE","S","SW","W","NW"]
def deg_to_compass(d):
    if d is None: return ""
    return DIR_CHARS[round(((d % 360) + 360) % 360 / 45) % 8]

def compute_rating(slot, spot):
    off = compass_to_deg(spot.get("offshore_wind"))
    win = compass_to_deg(spot.get("swell_window"))
    sz = parse_size(spot.get("best_size"))
    w  = wind_score(off, slot.get("wind_kt"), slot.get("wind_deg"))
    sc = size_score(slot.get("wave_m"), sz)
    p  = period_score(slot.get("period_s"))
    sd = swell_dir_score(slot.get("swell_deg"), win)
    # Original additive model (reverted 2026-06-09 at Che's request).
    score = 0.40*w + 0.30*sc + 0.15*p + 0.15*sd
    # Rideability gate (Che 2026-06-25): a wave of 0.2m or less is nothing to ride,
    # so force Flat (score 0) no matter how good the wind / period / swell window are.
    # Without this, a glassy light-offshore day on a tiny swell floats up to Fair/Good.
    if slot.get("wave_m") is not None and slot["wave_m"] <= 0.2:
        score = 0.0
    wd_cls = classify_wind_dir(off, slot.get("wind_deg"))
    wd_str = classify_wind_strength(slot.get("wind_kt"))
    wt = classify_wave_type(slot.get("wind_kt"), wd_cls)
    parts = []
    if wt: parts.append(wt)
    if wd_cls and wd_str:
        parts.append(f"{wd_str.lower()} {wd_cls.lower()} {deg_to_compass(slot.get('wind_deg'))}".strip())
    if slot.get("wave_m") is not None and slot.get("period_s") is not None:
        parts.append(f"{slot['wave_m']:.1f}m @ {round(slot['period_s'])}s")
    return {
        "rating_score": round(score, 1),
        "rating_label": score_to_label(score),
        "rating_wave_type": wt,
        "rating_wind_class": wd_cls,
        "rating_wind_strength": wd_str,
        "rating_reason": " · ".join(x for x in parts if x),
    }


# ── API fetchers ──────────────────────────────────────────────────────────
def pick_sg(v):
    if not isinstance(v, dict): return v
    for src in SOURCE_PREF:
        val = v.get(src)
        if isinstance(val, (int, float)): return val
    return None

def fetch_stormglass(lat, lng):
    now = int(time.time())
    qs = f"lat={lat}&lng={lng}&params={SG_PARAMS}&start={now}&end={now + 7*86400}"
    return http_get(f"https://api.stormglass.io/v2/weather/point?{qs}",
                    headers={"Authorization": SG_KEY})

def fetch_open_meteo_forecast(lat, lng):
    h = "wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,uv_index,temperature_2m,precipitation_probability"
    return http_get(f"https://customer-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}"
                    f"&hourly={h}&wind_speed_unit=kn&timezone=Pacific%2FAuckland&forecast_days=7&apikey={OM_KEY}")

def fetch_open_meteo_marine(lat, lng):
    h = "wave_height,wave_direction,swell_wave_period,sea_surface_temperature,sea_level_height_msl"
    return http_get(f"https://customer-marine-api.open-meteo.com/v1/marine?latitude={lat}&longitude={lng}"
                    f"&hourly={h}&timezone=Pacific%2FAuckland&forecast_days=7&apikey={OM_KEY}")


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]

def fetch_open_meteo_forecast_batch(coords, chunk=12):
    """Forecast for many coords per request (one-per-spot -> a few requests).
    Returns a list of per-location hourly dicts, aligned with coords order.
    Chunk kept modest so each 7-day multi-location payload returns in time."""
    h = "wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,uv_index,temperature_2m,precipitation_probability"
    out = []
    for ch in _chunks(coords, chunk):
        lats = ",".join(f"{c[0]}" for c in ch)
        lngs = ",".join(f"{c[1]}" for c in ch)
        try:
            data = http_get(f"https://customer-api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lngs}"
                            f"&hourly={h}&wind_speed_unit=kn&timezone=Pacific%2FAuckland&forecast_days=7&apikey={OM_KEY}", timeout=90)
            out.extend(data if isinstance(data, list) else [data])
        except Exception as e:
            # One slow/failed chunk must not kill the cycle (2026-06-12, run #12:
            # a single read-timeout aborted everything). Skipped spots keep their
            # previous forecast in the DB and refresh next cycle.
            print(f"  WARN forecast chunk failed ({len(ch)} spots): {str(e)[:120]}", flush=True)
            out.extend([None] * len(ch))
    return out

def fetch_open_meteo_marine_batch(coords, chunk=12):
    """Marine for many coords per request (same idea)."""
    h = "wave_height,wave_direction,swell_wave_period,sea_surface_temperature,sea_level_height_msl"
    out = []
    for ch in _chunks(coords, chunk):
        lats = ",".join(f"{c[0]}" for c in ch)
        lngs = ",".join(f"{c[1]}" for c in ch)
        try:
            data = http_get(f"https://customer-marine-api.open-meteo.com/v1/marine?latitude={lats}&longitude={lngs}"
                            f"&hourly={h}&timezone=Pacific%2FAuckland&forecast_days=7&apikey={OM_KEY}", timeout=90)
            out.extend(data if isinstance(data, list) else [data])
        except Exception as e:
            print(f"  WARN marine chunk failed ({len(ch)} spots): {str(e)[:120]}", flush=True)
            out.extend([None] * len(ch))
    return out


# ── Slot key builder ──────────────────────────────────────────────────────
def nz_now(): return datetime.now(NZ_TZ)
def slot_key(dt_nz): return dt_nz.strftime("%Y-%m-%dT%H")

def build_slot_keys():
    today = nz_now().replace(hour=0, minute=0, second=0, microsecond=0)
    keys = []
    for d in range(7):
        base = today + timedelta(days=d)
        for h in (0, 6, 12, 18):
            keys.append(slot_key(base.replace(hour=h)))
    return keys


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════
def main():
    log = sb_insert("fetch_log", [{"status":"running", "notes":"main"}], return_data=True)
    log_id = log[0]["id"]
    started = time.time()
    errors = 0
    rows_written = 0
    cycle_fetched_at = datetime.now(timezone.utc).isoformat()

    try:
        pins  = sb_select("pins")
        spots = sb_select("spots")
        print(f"[main] Loaded {len(pins)} pins, {len(spots)} spots\n", flush=True)

        # 1. Stormglass per pin
        print("[1/3] Fetching Stormglass for offshore pins…", flush=True)
        sg_by_pin = {}
        for p in pins:
            try:
                sg_by_pin[p["id"]] = fetch_stormglass(p["lat"], p["lng"])
                print(f"  ✓ {p['id']}", flush=True)
                time.sleep(0.4)
            except Exception as e:
                print(f"  ✗ {p['id']}: {e}", flush=True)
                errors += 1
                sg_by_pin[p["id"]] = None

        slot_keys = build_slot_keys()
        today_nz  = nz_now().replace(hour=0, minute=0, second=0, microsecond=0)

        # 2. Open-Meteo - BATCHED (all spots in a few requests, not one-per-spot,
        # which dodges Open-Meteo throttling of cloud / GitHub-runner IPs).
        print(f"\n[2/3] Batch-fetching Open-Meteo for {len(spots)} spots…", flush=True)
        coords = [(s["lineup_lat"], s["lineup_lng"]) for s in spots]
        om_fc_list  = fetch_open_meteo_forecast_batch(coords)
        om_mar_list = fetch_open_meteo_marine_batch(coords)
        if len(om_fc_list) != len(spots) or len(om_mar_list) != len(spots):
            raise RuntimeError(f"Open-Meteo batch count mismatch fc={len(om_fc_list)} mar={len(om_mar_list)} spots={len(spots)}")
        om_fc_by_id  = {spots[i]["id"]: om_fc_list[i]  for i in range(len(spots))}
        om_mar_by_id = {spots[i]["id"]: om_mar_list[i] for i in range(len(spots))}
        _missing = 0
        for _i, _s in enumerate(spots):  # CORRECTNESS GUARD: result order must match spot order
            _r = om_fc_list[_i]
            if _r is None or om_mar_list[_i] is None:
                _missing += 1
                continue                 # chunk failed upstream; spot skips this cycle
            _rlat = _r.get("latitude"); _rlng = _r.get("longitude")
            if _rlat is None or abs(_rlat - _s["lineup_lat"]) > 0.5 or abs(_rlng - _s["lineup_lng"]) > 0.5:
                raise RuntimeError(f"Open-Meteo batch MISALIGNED at {_s['id']}: got ({_rlat},{_rlng}) vs spot (~{_s['lineup_lat']},{_s['lineup_lng']})")
        if _missing > len(spots) // 2:
            raise RuntimeError(f"Open-Meteo mostly down: {_missing}/{len(spots)} spots missing - aborting cycle")
        if _missing:
            print(f"  WARN {_missing} spot(s) skipped this cycle (chunk failures); they keep previous data", flush=True)
        print(f"  ok batched {len(om_fc_list)} forecast + {len(om_mar_list)} marine locations", flush=True)
        all_rows = []
        for idx, s in enumerate(spots, 1):
            try:
                om_fc  = om_fc_by_id[s["id"]]
                om_mar = om_mar_by_id[s["id"]]
                if om_fc is None or om_mar is None:
                    continue             # chunk failed; previous rows stay current

                # Stormglass — pin-only, no lineup fallback
                sg = None
                if s["calibrated"] and s["pin_id"] and sg_by_pin.get(s["pin_id"]):
                    sg = sg_by_pin[s["pin_id"]]
                factor = s.get("adjustment_factor") or 1.0

                fc_idx = {}
                for i, t in enumerate(om_fc["hourly"]["time"]):
                    fc_idx[slot_key(datetime.fromisoformat(t))] = i
                mar_idx = {}
                for i, t in enumerate(om_mar["hourly"]["time"]):
                    mar_idx[slot_key(datetime.fromisoformat(t))] = i
                sg_idx = {}
                if sg and "hours" in sg:
                    for i, h in enumerate(sg["hours"]):
                        utc = datetime.fromisoformat(h["time"].replace("Z","+00:00"))
                        sg_idx[slot_key(utc.astimezone(NZ_TZ))] = i

                for key in slot_keys:
                    fi = fc_idx.get(key); mi = mar_idx.get(key); si = sg_idx.get(key)
                    wave_m = period_s = swell_deg = None
                    if sg and si is not None:
                        raw = pick_sg(sg["hours"][si].get("waveHeight"))
                        if raw is not None: wave_m = raw * factor
                        period_s = pick_sg(sg["hours"][si].get("swellPeriod"))
                        swell_deg = pick_sg(sg["hours"][si].get("swellDirection"))
                    if wave_m is None and mi is not None:
                        wave_m = om_mar["hourly"]["wave_height"][mi]
                    if period_s is None and mi is not None:
                        wp = om_mar["hourly"].get("swell_wave_period")
                        period_s = wp[mi] if wp else None
                    # Mean swell period -> peak period (Tp ~ 1.2 * Tm) so our numbers match
                    # peak-period surf forecasts (Surfline etc.). Both sources give a mean
                    # period, so convert once here regardless of which one supplied it.
                    if period_s is not None:
                        period_s = round(period_s * PEAK_PERIOD_FACTOR, 1)
                    if swell_deg is None and mi is not None:
                        wd = om_mar["hourly"].get("wave_direction")
                        swell_deg = wd[mi] if wd else None

                    wind_kt   = om_fc["hourly"]["wind_speed_10m"][fi]    if fi is not None else None
                    wind_deg  = om_fc["hourly"]["wind_direction_10m"][fi] if fi is not None else None
                    wind_gust = om_fc["hourly"]["wind_gusts_10m"][fi]    if fi is not None else None
                    wcode     = om_fc["hourly"]["weather_code"][fi]      if fi is not None else None
                    uv        = om_fc["hourly"]["uv_index"][fi]          if fi is not None else None
                    air_c     = om_fc["hourly"]["temperature_2m"][fi]    if fi is not None else None
                    precip    = om_fc["hourly"]["precipitation_probability"][fi] if fi is not None else None
                    water_c   = om_mar["hourly"]["sea_surface_temperature"][mi]  if mi is not None else None

                    date_part, hh = key.split("T")
                    slot_dt = datetime.strptime(date_part, "%Y-%m-%d").replace(
                        hour=int(hh), tzinfo=NZ_TZ)
                    day_offset = (slot_dt.date() - today_nz.date()).days

                    slot_data = {"wave_m":wave_m, "period_s":period_s, "swell_deg":swell_deg,
                                 "wind_kt":wind_kt, "wind_deg":wind_deg}
                    rating = compute_rating(slot_data, s)

                    all_rows.append({
                        "spot_id": s["id"], "slot_key": key,
                        "slot_time": slot_dt.isoformat(),
                        "day_offset": day_offset,
                        "wave_m": wave_m, "wind_kt": wind_kt, "wind_deg": wind_deg,
                        "wind_gust": wind_gust, "period_s": period_s, "swell_deg": swell_deg,
                        "weather_code": wcode, "air_c": air_c, "water_c": water_c,
                        "precip_pct": precip, "uv": uv,
                        "fetched_at": cycle_fetched_at,
                        **rating,
                    })
                if idx % 10 == 0:
                    print(f"  {idx}/{len(spots)} spots done", flush=True)
            except Exception as e:
                print(f"  ✗ {s['name']}: {e}", flush=True)
                errors += 1

        # 3. Upsert
        print(f"\n[3/3] Writing {len(all_rows)} slot rows to Supabase…", flush=True)
        CHUNK = 200
        for i in range(0, len(all_rows), CHUNK):
            sb_upsert_chunk("slot_forecast", all_rows[i:i+CHUNK], on_conflict="spot_id,slot_key")
            rows_written += min(CHUNK, len(all_rows) - i)
            print(f"  Wrote {rows_written}/{len(all_rows)}", flush=True)

        # 3b. Keep the table lean: prune past slots older than 7 days (never queried).
        try:
            from _prune_slots import prune_old_slots
            prune_old_slots(verbose=True)
        except Exception as e:
            print(f"  prune skipped: {e}", flush=True)

        # 3c. Refresh the spot_now aggregate (1 row/spot: current slot + 7-day best)
        #     so the list pages read 122 rows in one request instead of paginating.
        try:
            # Re-query the DB for today-onward slots instead of using this
            # cycle's (future-only) rows, so days[] includes today's already-
            # past sessions (forecast page shows the full day, now highlighted).
            from _build_spot_now import backfill
            backfill()
        except Exception as e:
            print(f"  spot_now skipped: {e}", flush=True)

        sb_update("fetch_log", log_id, {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "success" if errors == 0 else "partial",
            "spots_updated": len(spots) - errors,
            "errors_count": errors,
            "notes": f"main: wrote {rows_written} slot rows in {time.time()-started:.0f}s",
        })
        print(f"\n✅ Main cycle complete in {time.time()-started:.0f}s · {rows_written} rows · {errors} errors", flush=True)
    except Exception as e:
        sb_update("fetch_log", log_id, {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed", "notes": f"main: {e}",
        })
        raise

if __name__ == "__main__":
    main()
