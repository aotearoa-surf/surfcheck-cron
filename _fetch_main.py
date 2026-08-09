"""Main forecast cycle — Stormglass + Open-Meteo + rating computation.

Runs every 3 hours via cron. Does NOT touch tide data (that's _fetch_tides.py
running daily, since tides barely change between forecast updates).

Updates the following slot_forecast columns:
  wave_m, wind_kt, wind_deg, wind_gust, period_s, swell_deg,
  prim_swell_h, sec_swell_h, sec_swell_period, sec_swell_deg, windwave_h,
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
SG_PARAMS = ("waveHeight,swellHeight,swellPeriod,swellDirection,"
            "secondarySwellHeight,secondarySwellPeriod,secondarySwellDirection,"
            "windWaveHeight,windSpeed,windDirection")
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

def compass_dirs(s):
    """Every direction listed in a label, NOT their midpoint.

    compass_to_deg() averages "S / SW" to 202.5, which is what the wind score
    wants (how groomed is the face). The offshore-chop trim below wants a
    different question answered: is the wind near ANY of the listed offshore
    directions. Nearest-of-listed scored better than the midpoint on the ground
    truth (over-reads 3.9% vs 4.6%), so the two must not share a helper.
    """
    if not s: return []
    return [COMPASS_TO_DEG[p] for p in re.split(r"[-/,]", s.upper().replace(" ", ""))
            if p in COMPASS_TO_DEG]

# ── Offshore-chop trim (Che 2026-08-05, approved fleet-wide) ───────────────
# wave_m is TOTAL sea state at an offshore pin, so it includes wind sea raised
# over open-water fetch. When that wind is blowing OFF the land at the actual
# lineup, none of it reaches the beach, yet we were still publishing it as surf.
#
# Mt Maunganui Tay on 5 Aug: swell 0.20-0.47 m (which matches GoodSurfNow's
# 0.2-0.4 exactly), plus 0.27-0.46 m of chop under a 6-9 kt S/SW wind, which is
# Tay's own offshore direction. We published 0.56-0.89 m. Same signature at Te
# Arai and at Sumner before it moved to Open-Meteo.
#
# Measured over 2,370 ground-truth slots: mean error 0.233 -> 0.224 m and slots
# over GoodSurfNow by >0.4 m fall 7.3% -> 3.9%. It costs under-reads, 8.9% ->
# 10.8%, and it damages 15 already-accurate spots (Taieri Mouth, New Brighton,
# Waihau Bay, Robin Hood Bay). Che chose fleet-wide over a per-spot list on
# 5 Aug knowing that cost, on the same asymmetric-cost reasoning as the
# groundswell gate: an under-read annoys someone, an over-read sends them
# driving to the coast.
#
# NOTE this is the one fix here that no adjustment_factor could deliver. Tay is
# +0.5 m over today and -0.7 m under on Monday's northerly; a constant that
# fixes one breaks the other. Only a conditional rule can separate them.
OFFSHORE_ARC   = 45     # degrees either side of ANY listed offshore direction
OFFSHORE_RATIO = 0.7    # fires only when chop is at least this multiple of swell
OFFSHORE_KEEP  = 0.25   # fraction of chop ENERGY kept, i.e. half its height

def offshore_chop_trim(wave_m, prim, sec, chop, wind_deg, offshore_label):
    """-> (wave_m, chop). Unchanged unless the trigger fires."""
    dirs = compass_dirs(offshore_label)
    if (wave_m is None or chop is None or wind_deg is None or not dirs
            or prim is None):
        return wave_m, chop
    if min(angle_delta(wind_deg, d) for d in dirs) > OFFSHORE_ARC:
        return wave_m, chop
    swell = (prim * prim + (sec or 0) ** 2) ** 0.5
    if swell <= 0.01 or chop < OFFSHORE_RATIO * swell:
        return wave_m, chop
    trimmed = (swell * swell + OFFSHORE_KEEP * chop * chop) ** 0.5
    if trimmed >= wave_m:
        return wave_m, chop          # only ever a trim, never a lift
    # Keep the drawn bar honest: the chop block must shrink with the total.
    return round(trimmed, 2), round(chop * (OFFSHORE_KEEP ** 0.5), 2)

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
    if s < 5.5: return "Poor"
    if s < 6.5: return "Fair"
    if s < 7.8: return "Good"
    if s < 8.5: return "Mint"
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
    # GROUNDSWELL GATE (Che 2026-08-05, from user error report #18 on Tay Street).
    # The gate above tests wave_m, which is TOTAL sea state and includes wind
    # chop, so on a flat-but-choppy day it never fires. A user reported Tay St as
    # "flat, hardly ankle height" while we published 0.82m and "Fair": the total
    # was 0.82m but the actual groundswell underneath was 0.24m at 5.3s.
    #
    # Checked against the 85-spot GoodSurfNow ground truth: 206 slots where they
    # had a spot flat (<=0.30m) and we rated it Fair or better. A gate on SWELL
    # HEIGHT catches 156 of them while wrongly downgrading only 45 of 1054
    # correct calls, which is the best precision of anything tested. Gating on
    # period fails, because the worst cases are long-period (Te Arai was rated
    # MINT at 1.61m on a 0.21m swell at 12.9s). Gating on swell-as-a-share-of-
    # total also fails (precision ~45%).
    #
    # Cap at Poor rather than forcing Flat: there IS water moving, it just is not
    # rideable. The costs are asymmetric - calling a rideable day Poor annoys
    # someone, calling a flat day Fair sends them driving to the coast.
    #
    # REWRITTEN 9 Aug 2026 (Che: "we should be gating rating labels on the wave
    # size"): the gate now tests the TRIMMED WAVE SIZE, not the swell partitions.
    #
    # Why the partition gate had to go: during the 9 Aug northerly windswell,
    # Stormglass filed the rideable 7-8s sea under windWaveHeight, so Forestry
    # read combined swell 0.21 under a 1.6m rideable sea and got capped at Poor
    # while GoodSurfNow showed 1.1m of real surf. At midnight the SAME wave
    # train was reclassified as 1.41m of primary swell. The gate was trusting a
    # label that flips mid-event.
    #
    # Why gating on wave size is safe NOW when it failed at Tay Street in June:
    # wave_m reaching this function is already through the offshore-chop trim,
    # so chop an offshore wind cannot deliver to the beach has been removed.
    # Post-trim wave size approximates surf that actually arrives, which is the
    # quantity GoodSurfNow publishes.
    #
    # Measured over 2,598 matched slots across BOTH regimes (S captures 4-7 Aug
    # plus the 9 Aug northerly, lead 0-4 days):
    #
    #   rule                       catches flat  false alarms  precision
    #   combined swell <= 0.30       579/717        86/1529       87%
    #   wave_m <= 0.50               575/717        39/1529       94%
    #   wave_m <= 0.45  <- shipped   547/717        26/1529       95%
    #   wave_m <= 0.40               507/717        18/1529       97%
    #
    # On the northerly capture alone the old gate false-fired 25 times (the
    # Forestry class); wave <= 0.45 fires 8. 0.45 also caps only 39% of
    # genuinely knee-high (GSN 0.4-0.5) days at Poor, which is a fair label.
    #
    # KEEP THIS AS A SINGLE `if`, NOT a nested one. The `elif` below chains to
    # it on purpose: gate fires -> cap at Poor and skip the ceilings; gate does
    # not fire -> the size ceilings apply. On 5 Aug a rewrite into a nested if
    # silently rebound that `elif` and disabled the ceilings fleet-wide. It
    # shipped and put Te Arai on screen as "Mint" at 0.38 m.
    if slot.get("wave_m") is not None and slot["wave_m"] <= 0.45:
        score = min(score, 5.4)          # 5.4 = top of the Poor band
    # Wave-size ceilings (Che 2026-07-17): a top label must be backed by real size.
    # Perfect-but-small days cap at the band the wave qualifies for; the score is
    # capped (not just the label) so the number always sits inside its band on
    # every surface. <0.5m tops out at Fair 6.4; <0.7m at Good 7.7; <1.0m at
    # Mint 8.4; Epic (8.5+) requires 1.0m+ of swell.
    # Long-period lift (Che 2026-07-18): 12s+ groundswell punches above its
    # height (validated at Te Arai on a 0.4-0.6m 14s day that surfed Good),
    # so each ceiling lifts ONE band when period_s >= 12. The Epic floor is
    # NOT liftable: under 1.0m still caps at Mint 8.4 whatever the period.
    elif slot.get("wave_m") is not None:
        wm = slot["wave_m"]
        long_p = slot.get("period_s") is not None and slot["period_s"] >= 12
        if   wm < 0.5: score = min(score, 7.7 if long_p else 6.4)
        elif wm < 0.7: score = min(score, 8.4 if long_p else 7.7)
        elif wm < 1.0: score = min(score, 8.4)
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

# Display-only swell breakdown (groundswell / 2nd swell / wind-wave). These are
# DISPLAY fields — wave_m/period_s/swell_deg keep their calibrated per-field sourcing.
# Pull the whole breakdown from ONE model so the parts are internally consistent
# (otherwise e.g. groundswell comes from a different model than wind-wave and the
# two-tone split is meaningless). Prefer a source carrying the full partition set.
PARTITION_SRC_PREF = ["sg", "noaa", "dwd", "icon", "meteo", "ecmwf", "smhi"]
_PARTITION_FIELDS = ("swellHeight", "secondarySwellHeight", "secondarySwellPeriod",
                     "secondarySwellDirection", "windWaveHeight")

def pick_partition_source(hour):
    best = None
    for src in PARTITION_SRC_PREF:
        vals = {}
        for f in _PARTITION_FIELDS:
            v = hour.get(f)
            if isinstance(v, dict) and isinstance(v.get(src), (int, float)):
                vals[f] = v[src]
        if "swellHeight" in vals:
            if "secondarySwellHeight" in vals:
                return vals            # full set from one model — use it
            if best is None:
                best = vals            # fallback: has swell but no 2nd
    return best or {}

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
    h = "wave_height,wave_direction,swell_wave_period,sea_surface_temperature"
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
    # Swell partitions added 2026-08-04 for marine_source="open-meteo" spots.
    # Same request count, slightly larger payload. Open-Meteo partitions into a
    # primary and a secondary swell train plus wind wave, the same shape
    # Stormglass gives, so the two-tone bar and the 2nd-swell row work either way.
    h = ("wave_height,wave_direction,swell_wave_period,sea_surface_temperature,"
         "swell_wave_height,swell_wave_direction,"
         "secondary_swell_wave_height,secondary_swell_wave_period,"
         "secondary_swell_wave_direction,wind_wave_height")
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

        # Stormglass mostly down (quota 402s etc.) -> abort BEFORE any writes so
        # the previous cycle's Stormglass data survives in the DB. Mirrors the
        # Open-Meteo half-down guard below. (Che 2026-07-18: stale Stormglass
        # beats fresh Open-Meteo marine, always.)
        sg_failed = sum(1 for v in sg_by_pin.values() if v is None)
        if pins and sg_failed > len(pins) // 2:
            raise RuntimeError(f"Stormglass mostly down: {sg_failed}/{len(pins)} pins failed - aborting cycle, keeping previous data")

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

                # Marine source. Default is Stormglass off the shared pin.
                # marine_source="open-meteo" opts a spot out, for breaks whose
                # shelter Stormglass cannot see. Banks Peninsula is the proven
                # case: Stormglass returns an IDENTICAL swell height 30 km apart
                # across the peninsula, so Sumner reads like open Pegasus Bay
                # on a S swell when Surfline and GoodSurfNow both have it flat.
                # Verified against both before switching, see
                # _MARINE_SOURCE_POLICY.md. (Che approved 2026-08-04.)
                marine_src = (s.get("marine_source") or "stormglass").strip().lower()
                use_om_marine = marine_src.startswith("open")

                # Stormglass — pin-only, no lineup fallback
                sg = None
                sg_expected = bool(s["calibrated"] and s["pin_id"]) and not use_om_marine
                if sg_expected and sg_by_pin.get(s["pin_id"]):
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

                # The slot currently in progress (e.g. the 12pm slot at 3pm).
                # Stormglass only returns hours from NOW forward, so once a
                # slot's start hour has passed it has no exact SG index and
                # (since the SG-only guard) would be skipped, freezing the
                # "Right now" banner at up to 6h old. For the CURRENT slot
                # only, fall back to the first SG hour of this response
                # (conditions right now), so every cycle re-forecasts the
                # in-progress session and "Updated Xh ago" tracks the cycle.
                _now_nz = nz_now()
                cur_slot = slot_key(_now_nz.replace(hour=(_now_nz.hour // 6) * 6,
                                                    minute=0, second=0, microsecond=0))

                for key in slot_keys:
                    fi = fc_idx.get(key); mi = mar_idx.get(key); si = sg_idx.get(key)
                    if si is None and key == cur_slot and sg and sg.get("hours"):
                        si = 0
                    wave_m = period_s = swell_deg = None
                    prim_swell_h = sec_swell_h = sec_swell_period = sec_swell_deg = windwave_h = None
                    if use_om_marine and mi is not None:
                        # Open-Meteo marine at the spot's own lineup. Its grid
                        # resolves coastal sheltering that Stormglass does not.
                        # Same field set and same factor/Tp handling as the
                        # Stormglass branch so everything downstream is identical.
                        omh = om_mar["hourly"]
                        def _om(k):
                            v = omh.get(k)
                            return v[mi] if v else None
                        raw = _om("wave_height")
                        if raw is not None: wave_m = raw * factor
                        period_s  = _om("swell_wave_period")
                        swell_deg = _om("swell_wave_direction")
                        prim_swell_h = _om("swell_wave_height")
                        if prim_swell_h is not None: prim_swell_h = round(prim_swell_h * factor, 2)
                        sec_swell_h = _om("secondary_swell_wave_height")
                        if sec_swell_h is not None: sec_swell_h = round(sec_swell_h * factor, 2)
                        sec_p = _om("secondary_swell_wave_period")
                        if sec_p is not None: sec_swell_period = round(sec_p * PEAK_PERIOD_FACTOR, 1)
                        sec_swell_deg = _om("secondary_swell_wave_direction")
                        windwave_h = _om("wind_wave_height")
                        if windwave_h is not None: windwave_h = round(windwave_h * factor, 2)
                    elif sg and si is not None:
                        raw = pick_sg(sg["hours"][si].get("waveHeight"))
                        if raw is not None: wave_m = raw * factor
                        period_s = pick_sg(sg["hours"][si].get("swellPeriod"))
                        swell_deg = pick_sg(sg["hours"][si].get("swellDirection"))
                        # Swell breakdown for the two-tone bar (ground vs wind) + 2nd-swell row.
                        # DISPLAY-ONLY: single-sourced from one model so the parts reconcile.
                        # Heights scaled by the spot's adjustment_factor like wave_m; secondary
                        # period gets the same Tm->Tp x1.2. swellHeight = combined swell (ground).
                        part = pick_partition_source(sg["hours"][si])
                        prim_swell_h = part.get("swellHeight")
                        if prim_swell_h is not None: prim_swell_h = round(prim_swell_h * factor, 2)
                        sec_swell_h = part.get("secondarySwellHeight")
                        if sec_swell_h is not None: sec_swell_h = round(sec_swell_h * factor, 2)
                        sec_p = part.get("secondarySwellPeriod")
                        if sec_p is not None: sec_swell_period = round(sec_p * PEAK_PERIOD_FACTOR, 1)
                        sec_swell_deg = part.get("secondarySwellDirection")
                        windwave_h = part.get("windWaveHeight")
                        if windwave_h is not None: windwave_h = round(windwave_h * factor, 2)
                    # Calibrated spots are Stormglass-ONLY for marine fields: no
                    # Open-Meteo fallback for wave/period/direction/partitions.
                    # Missing Stormglass -> skip the row entirely so the previous
                    # cycle's Stormglass data stays in the DB. (Che 2026-07-18:
                    # "I never want to use open meteo for this data - I would
                    # rather have stale SG data in the database".)
                    if sg_expected and wave_m is None:
                        continue
                    if wave_m is None and mi is not None:
                        wave_m = om_mar["hourly"]["wave_height"][mi] * factor
                    if period_s is None and not sg_expected and mi is not None:
                        wp = om_mar["hourly"].get("swell_wave_period")
                        period_s = wp[mi] if wp else None
                    # Mean swell period -> peak period (Tp ~ 1.2 * Tm) so our numbers match
                    # peak-period surf forecasts (Surfline etc.). Both sources give a mean
                    # period, so convert once here regardless of which one supplied it.
                    if period_s is not None:
                        period_s = round(period_s * PEAK_PERIOD_FACTOR, 1)
                    # Energy-weighted dominant swell (Che 2026-07-17): Stormglass ranks
                    # partitions by HEIGHT, so a short local wind-slop can out-rank the
                    # groundswell (Te Arai showing "3s S" while Surfline shows "16s E").
                    # Promote by energy (h^2 x period) instead, like Surfline/LOTUS.
                    # When the secondary wins, swap the partitions so the table's Period
                    # row, 2nd-swell row, ratings and SurfGuru all stay consistent.
                    # Flips ~5% of slots; both periods are already Tp-scaled here.
                    _ranked = (prim_swell_h is not None and sec_swell_h is not None
                               and sec_swell_period is not None and period_s is not None)
                    if (_ranked and sec_swell_h * sec_swell_h * sec_swell_period
                            > prim_swell_h * prim_swell_h * period_s):
                        period_s, sec_swell_period = sec_swell_period, period_s
                        swell_deg, sec_swell_deg = sec_swell_deg, swell_deg
                        prim_swell_h, sec_swell_h = sec_swell_h, prim_swell_h
                    # Groundswell override (Che 2026-08-05). Energy alone is not
                    # enough: h^2 * T lets a fat short wind sea beat a real
                    # groundswell, so the displayed period flip-flops between two
                    # different waves. Mt Maunganui Tay on 5 Aug went 4s, 5s, 4s,
                    # 4s, 14s, 13s, 19s, 19s while GoodSurfNow held a steady 11-17s.
                    # Wed 12am there was 0.46m @ 4s against 0.09m @ 10s: energy
                    # 0.95 vs 0.09, so the wind sea won.
                    #
                    # If the winner is wind sea (under 8s) and a genuine
                    # groundswell sits behind it, show the groundswell.
                    #
                    # Measured on 476 slots across 17 spots against GoodSurfNow
                    # period data (_audit/gsn_periods_2026-08-05.json):
                    #
                    #   share  mean    out by 5s+  bias    spots made worse
                    #   none   3.68s   32.1%       -2.41s  -
                    #   25%    3.00s   20.0%       -0.64s  3
                    #   30%    2.96s   19.3%       -0.92s  0   <- shipped
                    #   40%    3.00s   20.0%       -1.39s  0
                    #
                    # 30% is the only setting that takes the best mean AND the best
                    # gross-error rate while making no spot worse. We were running
                    # 2.4s SHORT fleet-wide because wind sea kept winning; this
                    # closes most of that.
                    #
                    # This runs AFTER the energy swap and re-tests the result,
                    # which is how it was measured. Folding it into the energy
                    # condition would change its meaning.
                    if (_ranked and period_s < 8 and sec_swell_period >= 9
                            and sec_swell_h >= 0.30 * prim_swell_h
                            and sec_swell_h >= 0.05):
                        period_s, sec_swell_period = sec_swell_period, period_s
                        swell_deg, sec_swell_deg = sec_swell_deg, swell_deg
                        prim_swell_h, sec_swell_h = sec_swell_h, prim_swell_h
                    if swell_deg is None and not sg_expected and mi is not None:
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

                    # Drop wind sea an offshore wind cannot deliver to the beach.
                    # Runs AFTER the energy-weighted partition swap so prim/sec
                    # are settled, and BEFORE the rating so the size score and
                    # every ceiling downstream see the trimmed number.
                    wave_m, windwave_h = offshore_chop_trim(
                        wave_m, prim_swell_h, sec_swell_h, windwave_h,
                        wind_deg, s.get("offshore_wind"))

                    # sec_swell_h is required here: the groundswell gate tests
                    # COMBINED swell, and without this key it silently degrades
                    # to primary-only and the widening does nothing.
                    slot_data = {"wave_m":wave_m, "period_s":period_s, "swell_deg":swell_deg,
                                 "wind_kt":wind_kt, "wind_deg":wind_deg,
                                 "prim_swell_h":prim_swell_h, "sec_swell_h":sec_swell_h}
                    rating = compute_rating(slot_data, s)

                    all_rows.append({
                        "spot_id": s["id"], "slot_key": key,
                        "slot_time": slot_dt.isoformat(),
                        "day_offset": day_offset,
                        "wave_m": wave_m, "wind_kt": wind_kt, "wind_deg": wind_deg,
                        "wind_gust": wind_gust, "period_s": period_s, "swell_deg": swell_deg,
                        "prim_swell_h": prim_swell_h, "sec_swell_h": sec_swell_h,
                        "sec_swell_period": sec_swell_period, "sec_swell_deg": sec_swell_deg,
                        "windwave_h": windwave_h,
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
