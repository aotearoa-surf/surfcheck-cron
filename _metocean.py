# -*- coding: utf-8 -*-
"""MetOcean marine source for the main cycle (M1, live 9 Aug 2026).

Fetch at the spot's LINEUP GPS, not an offshore pin: MetOcean's nearshore
model resolves coastal transformation, which is the whole reason for the swap
(measured 9 Aug: fleet MAE vs GoodSurfNow 0.078 m over 44 truth spots, vs
0.15-0.22 for the calibrated Stormglass system).

Variables: total height + peak period for display, the above/below-8s band
pairs for the two-tone bar and 2nd-swell row (GoodSurfNow's own convention),
band peak directions. wave.period.peak is the display period: measured 1.21 s
vs GSN against 1.82 s for a dominant-band-peak construction.

M1_FACTORS: per-spot multipliers fitted on the two 9 Aug GSN captures
(gsn_2026-08-09.json + gsn_2026-08-09_batch2.json, lead 0-2 days). Fits inside
0.90-1.10 are dropped to 1.0 so noise is not chased. Spots absent here run
factor 1.0. These are SEPARATE from spots.adjustment_factor on purpose: the
Stormglass factors stay untouched in the DB so MARINE_MODE=stormglass rolls
back cleanly.
"""
import json, math, os, urllib.request
from pathlib import Path

MO_URL = "https://forecast-v2.metoceanapi.com/point/time"

# ── D1 directional shelter curves (Che approved 9 Aug night) ─────────────
# Per-spot, per-45deg-sector multipliers fitted on the two-regime GSN truth
# (fleet MAE 0.296 -> 0.148). Sectors absent from a spot's curve run raw 1.0:
# only data-qualified sectors are ever scaled (no guesswork). Amplifiers
# (>1.0, Kaikoura/Timaru/Oamaru S-swell focus) included, max observed 1.45.
# Full audit trail: _audit/shelter_curves_draft.json + fit_shelter_curves.py
# in the site repo. Applied-spot list in this file's _meta.applied_spots.
D1_CURVES = json.loads((Path(__file__).parent / "_shelter_curves.json")
                       .read_text(encoding="utf-8"))
_SECT = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

def sector_name(deg):
    return _SECT[int(((deg + 22.5) % 360) // 45)]

# ── D2 smooth directional shelter (Che approved 12 Aug 2026) ─────────────
# Heavily sheltered spots cannot be described by 45deg bins: an unobserved
# sector defaults to 1.0, so the factor cliffs (Te Arai read 0.7/0.4/0.7 on
# 15 Aug with nothing changing in the ocean), and one factor per slot jumps
# whenever the two bands swap dominance.
#
#   shelter(theta) = lo + (hi - lo) * ((1 + cos(theta - open)) / 2) ** p
#
# applied to EACH band by its own direction, recombined by energy. Continuous
# in direction, and independent of which band happens to be larger, so
# neither cliff can occur. Fitted per spot on the 4/9/12 Aug captures and
# validated leave-one-capture-out: out-of-sample error 0.210 m vs 0.295 for
# the bins and 0.325 raw. Applied ONLY where it beat the bins out-of-sample;
# open-coast spots keep D1_CURVES, where a 4-parameter fit only adds noise.
D2_SMOOTH = {
    "takapuna": {"open_deg": 351, "lo": 0.06, "hi": 0.60, "p": 4.0},
    # Orewa response scaled x1.40 (Che 15 Aug: reading small). The scale is
    # the all-four-regime optimum, not a fit to this week alone: it lowers
    # error on 4/12/15 Aug together (0.094 -> 0.082). lo/hi = 0.23/0.50 x1.40.
    "orewa":    {"open_deg": 21,  "lo": 0.32, "hi": 0.70, "p": 6.5},
    "omaha":    {"open_deg": 81,  "lo": 0.00, "hi": 0.55, "p": 0.5},
}


def smooth_shelter(deg, s):
    """Directional response in [lo, hi]; 1.0 when the direction is unknown."""
    if deg is None:
        return 1.0
    c = (1.0 + math.cos(math.radians(deg - s["open_deg"]))) / 2.0
    return s["lo"] + (s["hi"] - s["lo"]) * (c ** s["p"])

MO_VARS = ["wave.height", "wave.period.peak",
           "wave.height.above-8s", "wave.period.above-8s.peak", "wave.direction.above-8s.peak",
           "wave.height.below-8s", "wave.period.below-8s.peak", "wave.direction.below-8s.peak"]

# Che, 9 Aug 2026 evening: "remove all adjustment factors for now and we
# reassess.. I dont want adjustment factors on any MetOcean spot data."
# MetOcean runs RAW at every spot. Reason: the fitted shadow factors were
# direction-blind and crushed real swell events (sweep found Sumner showing
# 0.33-0.58 m against GSN's 1.3-1.6 m Wed-Sat, Mahia north 0.54 vs 1.8).
# The fitted values are preserved below for the reassessment; do NOT
# reactivate without a full recommendation and Che's approval.
M1_FACTORS = {}

_FITTED_9AUG_FOR_REASSESSMENT = {
    "takapuna": 0.40, "omaha": 0.40, "okiwi-gbi": 0.45, "pauanui": 0.75,
    "sumner-scarborough": 0.45, "mahia-north-coast": 0.45,
    "hotwater-beach": 0.85, "waihi-beach": 0.85, "waipu-cove": 0.85, "matata": 1.15,
    "aramoana": 0.70, "cray-bay": 0.65, "gizzy-pipe": 1.20, "mahia-open-coast": 0.85,
    "meatworks": 1.20, "ocean-beach-hb": 0.75, "otama": 0.75,
    "raglan-tirohanga-indies": 0.85, "riverton": 0.65, "tauroa-shippies": 1.15,
    "te-awanga": 0.50, "the-cut-blenheim": 0.55, "waimarama": 0.40,
    "wainui-beach": 1.30, "whareakeake-murdering-bay": 0.45,
}


def fetch_metocean(lat, lng, from_utc, n_slots, timeout=60):
    """One spot, all 8 variables, n_slots six-hourly steps. Values whose
    noData code is non-GOOD come back as None."""
    body = json.dumps({"points": [{"lon": lng, "lat": lat}], "variables": MO_VARS,
                       "time": {"from": from_utc, "interval": "6h",
                                "repeat": n_slots - 1}}).encode()
    req = urllib.request.Request(MO_URL, data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": os.environ["METOCEAN_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    out = {}
    for v in MO_VARS:
        var = d["variables"].get(v, {})
        data, nod = var.get("data"), var.get("noData")
        out[v] = [x if (nod is None or nod[i] == 0) else None
                  for i, x in enumerate(data or [])]
    return out


def metocean_slot_fields(mo, j, curve, smooth=None):
    """Map MetOcean arrays at slot index j onto our slot_forecast fields.
    Returns None when the slot has no total height (caller keeps stale row).
    prim = the groundswell row, period_s = overall spectrum peak (GSN's number).

    Shelter: `smooth` (a D2_SMOOTH entry) wins when present and is applied
    per band; otherwise `curve` (the spot's D1 sector dict, {} for none) is
    applied per slot from the dominant band's direction, the convention those
    were fitted under."""
    tot = mo["wave.height"][j] if j < len(mo["wave.height"]) else None
    if tot is None:
        return None
    a_h, b_h = mo["wave.height.above-8s"][j], mo["wave.height.below-8s"][j]
    a_p, b_p = mo["wave.period.above-8s.peak"][j], mo["wave.period.below-8s.peak"][j]
    a_d, b_d = mo["wave.direction.above-8s.peak"][j], mo["wave.direction.below-8s.peak"][j]
    # FIXED band rows (Che 10 Aug): groundswell (above-8s) is ALWAYS the
    # primary row, windsea always secondary, GSN's own convention. The old
    # bigger-band-first sort flipped rows on a 2 cm difference, which churned
    # the displayed direction AND the rating's swell-window score for single
    # slots (Sumner Tue 11 12pm). Only exception: no groundswell at all
    # (band under 5 cm) puts the windsea in the primary row so the swell row
    # never shows a phantom.
    if (a_h or 0) >= 0.05:
        prim_h, prim_p, prim_d, sec_h, sec_p, sec_d = a_h, a_p, a_d, b_h, b_p, b_d
    else:
        prim_h, prim_p, prim_d, sec_h, sec_p, sec_d = b_h, b_p, b_d, a_h, a_p, a_d
    if smooth:
        # D2: attenuate each band by ITS OWN direction, recombine by energy.
        # No slot-level choice is made, so a band swap cannot move the number.
        ad = a_d if a_d is not None else b_d
        bd = b_d if b_d is not None else a_d
        fa = smooth_shelter(ad, smooth)
        fb = smooth_shelter(bd, smooth)
        ah, bh = (a_h or 0.0), (b_h or 0.0)
        tot_s = math.sqrt((ah * fa) ** 2 + (bh * fb) ** 2)
        # keep the published total consistent with the bands we publish
        tot, factor = tot_s, 1.0
        prim_f, sec_f = (fa, fb) if (a_h or 0) >= 0.05 else (fb, fa)
    else:
        factor = 1.0
        if curve:
            # D1 keeps its FITTED convention: the factor follows the DOMINANT
            # band's direction (that is how the curves were derived), with the
            # other band as fallback when the direction is null.
            a_dominant = (a_h or 0) >= (b_h or 0)
            dom_d = a_d if a_dominant else b_d
            other_d = b_d if a_dominant else a_d
            d = dom_d if dom_d is not None else other_d
            if d is not None:
                factor = curve.get(sector_name(d), 1.0)
        prim_f = sec_f = factor
    pk = mo["wave.period.peak"][j]
    return {
        "wave_m": round(tot * factor, 2),
        "period_s": round(pk, 1) if pk is not None else (
            round(prim_p, 1) if prim_p is not None else None),
        "swell_deg": round(prim_d) if prim_d is not None else None,
        "prim_swell_h": round(prim_h * prim_f, 2) if prim_h is not None else None,
        "sec_swell_h": round(sec_h * sec_f, 2) if sec_h is not None else None,
        "sec_swell_period": round(sec_p, 1) if sec_p is not None else None,
        "sec_swell_deg": round(sec_d) if sec_d is not None else None,
        "windwave_h": None,   # absorbed into the below-8s band, GSN convention
    }
