#!/usr/bin/env python3
"""
thunder_watch.py

Base case: given a coordinate, determine whether any currently tracked
thunderstorm cell will pass over (or near) that point, when, and how
intense it is. Emits probabilistic results plus a WATCH/WARNING tier.

Data source: NEXRAD Level III storm attribute table, aggregated across all
CONUS radars by the Iowa Environmental Mesonet:
    https://mesonet.agron.iastate.edu/geojson/nexrad_attr.geojson
Each feature is one algorithm-identified cell (SCIT) with position, motion
vector (drct/sknt), max reflectivity, VIL, echo top, hail and rotation flags.

Also pulls active NWS convective alerts for the point (api.weather.gov) as
ground truth from the human forecasters.

Method:
  1. Fetch all cells, keep those within --search-radius of the target.
  2. Advect each cell linearly along its reported motion vector.
  3. Compute closest point of approach (CPA) to the target: time, distance.
  4. Assign an effective cell radius from VIL/dBZ (the attribute table has
     no size field; this is a heuristic, flagged for improvement).
  5. Probability of passage = P(|cross-track error| < R_eff) with a
     Gaussian cross-track error that grows with lead time, discounted by
     cell lifecycle decay for ordinary (non-severe) cells at long lead.
  6. Classify intensity, issue NONE / ADVISORY / WATCH / WARNING.

Usage:
    python thunder_watch.py LAT LON [--horizon-min 90] [--search-radius-km 250] [--json]

No dependencies outside the standard library.
"""

import argparse
import concurrent.futures
import json
import math
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    import netCDF4  # required only for GLM lightning; pip install netCDF4
    HAVE_NETCDF4 = True
except ImportError:
    HAVE_NETCDF4 = False

ATTR_URL = "https://mesonet.agron.iastate.edu/geojson/nexrad_attr.geojson"
ALERTS_URL = "https://api.weather.gov/alerts/active?point={lat},{lon}"
USER_AGENT = "thunder-watch/0.1 (hiker storm warning prototype)"

KT_TO_KMH = 1.852
EARTH_R_KM = 6371.0

# Cross-track error model: sigma(t) = SIGMA0 + SIGMA_RATE * t_minutes  [km]
# Rough numbers consistent with SCIT track error growth (~30% position error
# at 30 min lead is typical for linear extrapolation of convective cells).
SIGMA0_KM = 2.0
SIGMA_RATE_KM_PER_MIN = 0.20

# Ordinary-cell lifecycle: single cells live ~30-60 min. A cell projected to
# arrive in 75 min may not exist by then. Severe/organized cells (high VIL,
# meso, TVS) live longer, so decay is relaxed for them.
ORDINARY_HALFLIFE_MIN = 45.0
SEVERE_HALFLIFE_MIN = 120.0

# --- GOES GLM (Geostationary Lightning Mapper) ---
# L2 Lightning Cluster-Filter Algorithm files, one per 20 s, on the NOAA
# open-data S3 buckets (anonymous HTTPS, no credentials).
# GOES-19 = East (75.2 W), GOES-18 = West (137 W). Both see all of CONUS;
# pick the one with the better viewing angle for the target longitude.
GLM_LATENCY_S = 60          # files land on S3 ~30-60 s after valid time
GLM_MAX_WORKERS = 8
# Lightning distance thresholds (km). 15 km is the standard "you can be
# struck by the next flash" radius; 40 km gives ~30 min of lead at typical
# cell speeds and covers anvil/bolt-from-the-blue range.
GLM_WARN_KM = 15.0
GLM_WATCH_KM = 40.0
GLM_INFO_KM = 100.0


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# --- Archive / replay data sources ---
RADAR_SITES_URL = "https://mesonet.agron.iastate.edu/geojson/network/NEXRAD.geojson"
ATTR_ARCHIVE_URL = ("https://mesonet.agron.iastate.edu/cgi-bin/request/gis/"
                    "nexrad_storm_attrs.py?radar={radar}&sts={sts}&ets={ets}&fmt=csv")
SBW_URL = ("https://mesonet.agron.iastate.edu/json/sbw_by_point.py"
           "?lon={lon}&lat={lat}&sdate={sdate}&edate={edate}")
ATTR_LOOKBACK_MIN = 12  # keep the latest scan per cell within this window


def fetch_archive_cells(tgt_lat, tgt_lon, search_radius_km, at):
    """Rebuild the realtime attribute-feed feature list for time `at` from
    the IEM per-radar storm attribute archive."""
    sites = fetch_json(RADAR_SITES_URL)["features"]
    radars = []
    for s in sites:
        lon, lat = s["geometry"]["coordinates"][:2]
        x, y = enu_km(tgt_lat, tgt_lon, lat, lon)
        # a radar can track a cell up to ~230 km from itself; pad accordingly
        if math.hypot(x, y) <= search_radius_km + 230.0:
            radars.append(s["id"])

    sts = (at - timedelta(minutes=ATTR_LOOKBACK_MIN)).strftime("%Y-%m-%dT%H:%MZ")
    ets = at.strftime("%Y-%m-%dT%H:%MZ")

    def one(radar):
        try:
            return _http_get(ATTR_ARCHIVE_URL.format(radar=radar, sts=sts, ets=ets)
                             ).decode()
        except Exception:
            return ""

    latest = {}  # (radar, storm_id) -> row dict
    with concurrent.futures.ThreadPoolExecutor(GLM_MAX_WORKERS) as pool:
        for text in pool.map(one, radars):
            lines = [l for l in text.strip().splitlines() if l]
            if len(lines) < 2:
                continue
            hdr = lines[0].split(",")
            for line in lines[1:]:
                row = dict(zip(hdr, line.split(",")))
                key = (row["NEXRAD"], row["STORM_ID"])
                if key not in latest or row["VALID"] > latest[key]["VALID"]:
                    latest[key] = row

    features = []
    for row in latest.values():
        try:
            valid = datetime.strptime(row["VALID"], "%Y%m%d%H%M")
            features.append({
                "geometry": {"coordinates": [float(row["LON"]), float(row["LAT"])]},
                "properties": {
                    "storm_id": row["STORM_ID"], "nexrad": row["NEXRAD"],
                    "valid": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "drct": float(row["DRCT"]), "sknt": float(row["SKNT"]),
                    "max_dbz": float(row["MAX_DBZ"]), "vil": float(row["VIL"]),
                    "top": float(row["TOP"]),
                    "tvs": row["TVS"], "meso": row["MESO"],
                    "posh": float(row["POSH"]), "max_size": float(row["MAX_SIZE"]),
                }})
        except (KeyError, ValueError):
            continue
    return features


def fetch_sbw_archive(lat, lon, start, end):
    """Storm-based warning polygons covering the point, from the IEM VTEC
    archive, filtered to those overlapping [start, end]."""
    url = SBW_URL.format(lon=lon, lat=lat,
                         sdate=(start - timedelta(days=1)).strftime("%Y-%m-%d"),
                         edate=(end + timedelta(days=1)).strftime("%Y-%m-%d"))
    out = []
    for w in fetch_json(url).get("sbws", []):
        try:
            iss = datetime.strptime(w["issue"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            exp = datetime.strptime(w["expire"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if iss <= end and exp >= start:
            out.append({"event": w.get("name", w.get("phenomena")),
                        "headline": f"{w.get('wfo')} {w.get('phenomena')}."
                                    f"{w.get('eventid')} {w['issue']}-{w['expire']}",
                        "issue": iss, "expires": w["expire"]})
    return out


def enu_km(lat0, lon0, lat, lon):
    """Equirectangular local tangent plane, fine to a few hundred km."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_R_KM
    y = math.radians(lat - lat0) * EARTH_R_KM
    return x, y


def effective_radius_km(p):
    """Heuristic cell footprint radius. The Level III attribute table carries
    no size metric, so scale a base radius by intensity proxies."""
    r = 4.0
    if p["max_dbz"] >= 55:
        r += 2.0
    if p["vil"] >= 30:
        r += 2.0
    if p["vil"] >= 50:
        r += 2.0
    return r


def classify_intensity(p):
    dbz, vil = p["max_dbz"], p["vil"]
    severe_flags = []
    if p.get("tvs", "NONE") != "NONE":
        severe_flags.append("TVS (tornado vortex signature)")
    if p.get("meso", "NONE") != "NONE":
        severe_flags.append("mesocyclone")
    if p.get("posh", 0) >= 50:
        severe_flags.append(f"severe hail POSH {p['posh']}% (max size {p['max_size']:.2f} in)")

    if dbz >= 60 or vil >= 50 or severe_flags:
        level = "EXTREME" if (dbz >= 65 or vil >= 60 or severe_flags) else "SEVERE"
    elif dbz >= 55 or vil >= 30:
        level = "SEVERE"
    elif dbz >= 50 or vil >= 15:
        level = "STRONG"
    elif dbz >= 40:
        level = "MODERATE"
    else:
        level = "WEAK"
    return level, severe_flags


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --- Route (moving hiker) support ---

def load_gpx(path):
    """Return [(lat, lon, ele_m)] from trkpt or rtept elements."""
    import xml.etree.ElementTree as ET2
    root = ET2.parse(path).getroot()
    ns = root.tag.split("}")[0] + "}"
    pts = []
    for tag in ("trkpt", "rtept"):
        for p in root.iter(ns + tag):
            pts.append((float(p.get("lat")), float(p.get("lon")),
                        float(p.findtext(ns + "ele") or 0.0)))
        if pts:
            break
    if len(pts) < 2:
        raise ValueError("no track/route points in GPX")
    return pts


def route_timeline(pts, start_utc, pace_factor=0.7, break_factor=1.125):
    """Assign an arrival time to every route point using Tobler's hiking
    function scaled by pace_factor (0.7 ~ loaded backpacker), inflated by
    break_factor for rest stops. Returns [(t_utc, lat, lon, ele, dist_km)]."""
    out = [(start_utc, pts[0][0], pts[0][1], pts[0][2], 0.0)]
    t, dist = start_utc, 0.0
    for (a1, o1, e1), (a2, o2, e2) in zip(pts, pts[1:]):
        x = (o2 - o1) * 111.195 * math.cos(math.radians(a1))
        y = (a2 - a1) * 111.195
        dd = math.hypot(x, y)
        if dd > 0:
            slope = (e2 - e1) / 1000.0 / dd
            v = 6.0 * math.exp(-3.5 * abs(slope + 0.05)) * pace_factor
            t = t + timedelta(hours=dd / max(v, 0.5) * break_factor)
            dist += dd
        out.append((t, a2, o2, e2, dist))
    return out


def hiker_pos(timeline, when):
    """Hiker position at `when`: trailhead before start, camp (endpoint)
    after arrival, linear interpolation between route points otherwise."""
    if when <= timeline[0][0]:
        return timeline[0][1:4]
    if when >= timeline[-1][0]:
        return timeline[-1][1:4]
    for (t1, a1, o1, e1, _), (t2, a2, o2, e2, _) in zip(timeline, timeline[1:]):
        if t1 <= when <= t2:
            if t2 == t1:
                return (a1, o1, e1)
            f = (when - t1).total_seconds() / (t2 - t1).total_seconds()
            return (a1 + f * (a2 - a1), o1 + f * (o2 - o1), e1 + f * (e2 - e1))
    return timeline[-1][1:4]


def analyze_cell_route(feature, timeline, now, horizon_min, step_min=5.0):
    """Moving-target version of analyze_cell: minimize distance between the
    linearly advected cell and the hiker's future positions."""
    p = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"]
    spd_kmh = p["sknt"] * KT_TO_KMH
    hdg = (p["drct"] + 180.0) % 360.0
    valid = datetime.strptime(p["valid"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_h = max(0.0, (now - valid).total_seconds() / 3600.0)
    r_eff = effective_radius_km(p)
    level, flags = classify_intensity(p)
    severe = bool(flags) or level in ("SEVERE", "EXTREME")

    best = None  # (dist, tau_min)
    tau = 0.0
    while tau <= horizon_min:
        h_lat, h_lon, _ = hiker_pos(timeline, now + timedelta(minutes=tau))
        cx, cy = enu_km(h_lat, h_lon, lat, lon)
        run = spd_kmh * (age_h + tau / 60.0)
        cx += run * math.sin(math.radians(hdg))
        cy += run * math.cos(math.radians(hdg))
        d = math.hypot(cx, cy)
        if best is None or d < best[0]:
            best = (d, tau)
        tau += step_min
    d_min, tau_min = best
    if spd_kmh < 2.0 and d_min > r_eff:
        return None

    sigma = SIGMA0_KM + SIGMA_RATE_KM_PER_MIN * tau_min
    p_track = (norm_cdf((r_eff - d_min) / sigma)
               - norm_cdf((-r_eff - d_min) / sigma))
    halflife = SEVERE_HALFLIFE_MIN if severe else ORDINARY_HALFLIFE_MIN
    prob = p_track * (0.5 ** (tau_min / halflife))
    if prob < 0.02 and d_min > 3 * r_eff:
        return None
    return {"storm_id": f"{p['nexrad']}-{p['storm_id']}",
            "lat": lat, "lon": lon, "eta_min": tau_min, "cpa_km": d_min,
            "probability": prob, "intensity": level, "max_dbz": p["max_dbz"],
            "vil_kg_m2": p["vil"], "severe_signatures": flags,
            "eff_radius_km": r_eff, "speed_kmh": spd_kmh,
            "heading_deg": hdg if spd_kmh >= 2.0 else None,
            "echo_top_km": p["top"] * 0.3048,
            "duration_min": None, "data_age_min": age_h * 60.0}


def corridor_metrics(timeline, now, flashes):
    """Lightning relative to the hiker: distance to current position and
    minimum distance to any not-yet-hiked route point (the corridor ahead)."""
    h_lat, h_lon, _ = hiker_pos(timeline, now)
    ahead = [(a, o) for (t, a, o, e, d) in timeline if t >= now] or \
            [timeline[-1][1:3]]
    d_now = d_corr = None
    for fl in flashes:
        la, lo = fl[3], fl[4]
        x, y = enu_km(h_lat, h_lon, la, lo)
        d = math.hypot(x, y)
        d_now = d if d_now is None else min(d_now, d)
        dc = min(math.hypot(*enu_km(a, o, la, lo)) for a, o in ahead[::4])
        d_corr = dc if d_corr is None else min(d_corr, dc)
    return d_now, d_corr


def _fetch_openmeteo_curve(lat, lon, date_utc=None):
    """Open-Meteo blend (live or archived) -> curve dict."""
    hourly = "cape,convective_inhibition,precipitation_probability"
    if date_utc is not None:
        d = date_utc.strftime("%Y-%m-%d")
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}&hourly={hourly}"
               f"&start_date={d}&end_date={d}&timezone=UTC")
    else:
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}&hourly={hourly}"
               "&forecast_days=5&timezone=UTC")
    import time as _time
    h = None
    for attempt in range(3):
        try:
            h = fetch_json(url)["hourly"]
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                _time.sleep(20)
                continue
            raise
    curve = {}
    for i, t in enumerate(h["time"]):
        cape = h["cape"][i] or 0.0
        cin = h["convective_inhibition"][i] or 0.0
        pop = (h["precipitation_probability"][i] or 0) / 100.0
        dt = datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        curve[dt] = {"p": hourly_lightning_prob(cape, cin, pop),
                     "cape": cape, "cin": cin, "pop": pop, "src": "open-meteo"}
    return curve


def fetch_convective_forecast(lat, lon, date_utc=None, hours_ahead=18):
    """Hourly convective forecast curve, hardened per-hour fallback chain.
    Live: HRRR direct first (3 km, explicit LTNG); any hours HRRR fails to
    deliver (progressive uploads, per-field fetch dropouts) are filled from
    the Open-Meteo blend, then NWS probabilityOfThunder, then linear
    interpolation across isolated single-hour gaps (src tagged "interp").
    Replay dates: Open-Meteo archive only. Raises only if every source
    fails for every hour; callers treat that as curve=None."""
    if date_utc is not None:
        return _fetch_openmeteo_curve(lat, lon, date_utc)

    now_h = datetime.now(timezone.utc).replace(minute=0, second=0,
                                               microsecond=0)
    need = [now_h + timedelta(hours=k) for k in range(hours_ahead + 1)]
    curve = {}
    try:
        from hrrr_direct import fetch_hrrr_curve
        curve.update(fetch_hrrr_curve(
            lat, lon, hours_ahead=hours_ahead,
            hourly_lightning_prob=hourly_lightning_prob))
    except Exception:
        pass
    missing = [t for t in need if t not in curve]
    if missing:
        try:
            blend = _fetch_openmeteo_curve(lat, lon)
            for t in missing:
                if t in blend:
                    curve[t] = blend[t]
        except Exception:
            pass
        missing = [t for t in need if t not in curve]
    if missing:
        try:
            nws = fetch_nws_thunder(lat, lon)
            for t in missing:
                if t in nws:
                    curve[t] = nws[t]
        except Exception:
            pass
        missing = [t for t in need if t not in curve]
    # isolated single-hour gaps: interpolate neighbors rather than report a
    # false zero in the middle of an active afternoon
    for t in list(missing):
        lo, hi = curve.get(t - timedelta(hours=1)), curve.get(t + timedelta(hours=1))
        if lo and hi:
            curve[t] = {"p": round((lo["p"] + hi["p"]) / 2, 2),
                        "cape": None, "cin": None, "pop": None,
                        "src": "interp"}
    if not curve:
        raise RuntimeError("all forecast sources failed")
    return curve


def fetch_nws_thunder(lat, lon):
    """Fallback/alternate environment source: NWS gridpoint
    probabilityOfThunder (live only, no archive). Returns the same curve
    structure with p taken directly from the NWS product."""
    meta = fetch_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    grid = fetch_json(meta["properties"]["forecastGridData"])
    curve = {}
    for v in grid["properties"].get("probabilityOfThunder", {}).get("values", []):
        span, _, dur = v["validTime"].partition("/")
        t0 = datetime.fromisoformat(span).astimezone(timezone.utc)
        hours = 1
        m = None
        import re as _re
        m = _re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?", dur or "PT1H")
        if m:
            hours = (int(m.group(1) or 0)) * 24 + int(m.group(2) or 0) or 1
        p = (v["value"] or 0) / 100.0
        for k in range(hours):
            curve[t0.replace(minute=0, second=0, microsecond=0)
                  + timedelta(hours=k)] = {"p": round(p, 2), "cape": None,
                                           "cin": None, "pop": None,
                                           "src": "nws-thunder"}
    return curve


def hourly_lightning_prob(cape, cin, pop):
    """P(lightning within ~15 km of the route during one hour) from the
    forecast environment. The CAPE ramp is deliberately aggressive at low
    values because high terrain initiates convection at 300-600 J/kg that
    would stay capped over the plains. Placeholder calibration; the replay
    harness is the tool for fitting these numbers properly."""
    ramp = [(100, 0.10), (300, 0.35), (600, 0.55), (1000, 0.65), (2000, 0.80)]
    if cape < ramp[0][0]:
        p = 0.0
    else:
        p = ramp[-1][1]
        for (c1, p1), (c2, p2) in zip(ramp, ramp[1:]):
            if cape <= c2:
                p = p1 + (p2 - p1) * (cape - c1) / (c2 - c1)
                break
    if cin <= -100:
        p *= 0.25
    elif cin <= -50:
        p *= 0.6
    if cape >= 100:
        p = max(p, 0.5 * pop)
    return min(p, 0.85)


def forecast_window_prob(curve, now, hours=3.0):
    """P(lightning near route within the next `hours`) by compounding the
    hourly forecast probabilities: 1 - prod(1 - p_h)."""
    if not curve:
        return None
    q = 1.0
    t = now
    end = now + timedelta(hours=hours)
    while t < end:
        trip = curve.get(t.replace(minute=0, second=0, microsecond=0))
        if trip is not None:
            frac = min(1.0, (end - t).total_seconds() / 3600.0)
            p_h = trip["p"]
            q *= (1.0 - p_h) ** frac
        t = (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return min(1.0 - q, 0.90)


def route_briefing(curve, utc_offset_h, start_utc, hours=15):
    """Pre-trip table anchored to the hike start: local hour ->
    P(lightning near route that hour), taken from the correct absolute
    forecast hour (no hour-of-day aliasing across days)."""
    rows = []
    t0 = start_utc.replace(minute=0, second=0, microsecond=0)
    for k in range(hours):
        dt = t0 + timedelta(hours=k)
        if dt in curve:
            trip = curve[dt]
            local = dt + timedelta(hours=utc_offset_h)
            rows.append({"local_hour": local.hour,
                         "p_lightning": trip["p"], "cape": trip["cape"],
                         "cin": trip["cin"], "pop": trip["pop"],
                         "src": trip["src"]})
    return rows


TREELINE_M = 3500.0  # Elk Mountains; override per range with --treeline-m


def exposure_factor(ele_m, crest=False, treeline_m=TREELINE_M):
    """Terrain exposure multiplier for lightning hazard at the hiker's
    position. 1.0 above treeline, ramping down through the treeline band to
    0.35 below it (forest reduces but does not remove risk: ground current,
    tall trees). Crest/pass points get a bonus because they are the local
    strike maxima and offer no retreat."""
    if ele_m >= treeline_m:
        e = 1.0
    elif ele_m >= treeline_m - 200.0:
        e = 0.5 + 0.5 * (ele_m - (treeline_m - 200.0)) / 200.0
    else:
        e = 0.35
    if crest and ele_m >= treeline_m - 300.0:
        e = min(1.0, e + 0.15)
    return e


def hazard_band(h):
    if h >= 0.60:
        return "EXTREME"
    if h >= 0.35:
        return "HIGH"
    if h >= 0.15:
        return "MODERATE"
    return "LOW"


def _crest_flags(timeline, window_km=1.2, tol_m=40.0):
    """Mark route points that are local elevation maxima (passes, crests)
    within +-window_km along-route."""
    flags = []
    for i, (_, _, _, e, d) in enumerate(timeline):
        lo, hi = d - window_km, d + window_km
        emax = max(p[3] for p in timeline if lo <= p[4] <= hi)
        flags.append(e >= emax - tol_m)
    return flags


def hazard_profile(timeline, curve, utc_offset_h, step_min=15.0,
                   camp_hours=9.0, treeline_m=TREELINE_M):
    """Exposure-weighted lightning hazard along the hiker's projected track:
    H(t) = P(lightning near route, hour of t) x exposure(position at t).
    Covers hike start through arrival plus camp_hours at camp. This is the
    metric that distinguishes "storm while below treeline" from "storm while
    on the pass" -- the corridor P alone cannot."""
    if not curve:
        return []
    crest = _crest_flags(timeline)
    out = []
    t = timeline[0][0]
    end = timeline[-1][0] + timedelta(hours=camp_hours)
    while t <= end:
        lat, lon, ele = hiker_pos(timeline, t)
        trip = curve.get(t.replace(minute=0, second=0, microsecond=0))
        p_h = trip["p"] if trip else 0.0
        on_crest = False
        if t <= timeline[-1][0]:
            i = min(range(len(timeline)),
                    key=lambda k: abs((timeline[k][0] - t).total_seconds()))
            on_crest = crest[i]
        e = exposure_factor(ele, on_crest, treeline_m)
        h = p_h * e
        out.append({"t": t, "ele_m": round(ele), "crest": on_crest,
                    "p_hour": round(p_h, 2), "exposure": round(e, 2),
                    "hazard": round(h, 2), "band": hazard_band(h)})
        t += timedelta(minutes=step_min)
    return out


def outlook_probability(now, utc_offset_h, d_corr_km, curve=None):
    """P(lightning within 15 km of hiker in the next ~3 h): forecast-driven
    when a convective forecast curve is available, diurnal climatology
    fallback otherwise, raised by observed lightning near the corridor."""
    p = forecast_window_prob(curve, now) if curve else None
    if p is None:
        local_h = (now + timedelta(hours=utc_offset_h)).hour + \
                  (now + timedelta(hours=utc_offset_h)).minute / 60.0
        if local_h < 9:
            p = 0.10
        elif local_h < 11:
            p = 0.30
        elif local_h < 18:
            p = 0.45
        else:
            p = 0.20
    if d_corr_km is not None:
        if d_corr_km <= 15:
            p = max(p, 0.95)
        elif d_corr_km <= 40:
            p = max(p, 0.75)
        elif d_corr_km <= 75:
            p = max(p, 0.60)
    return p


def analyze_cell(feature, tgt_lat, tgt_lon, horizon_min, now):
    p = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"]

    # Cell position relative to target
    x0, y0 = enu_km(tgt_lat, tgt_lon, lat, lon)
    dist_now = math.hypot(x0, y0)

    spd_kmh = p["sknt"] * KT_TO_KMH
    # NEXRAD storm-attribute movement uses meteorological "from" convention:
    # drct 242 means the cell is coming FROM 242 deg, i.e. heading toward 062.
    hdg = (p["drct"] + 180.0) % 360.0
    vx = spd_kmh * math.sin(math.radians(hdg))  # east
    vy = spd_kmh * math.cos(math.radians(hdg))  # north

    # Data latency: attribute records are only regenerated per volume scan;
    # advance the cell to "now" before extrapolating.
    valid = datetime.strptime(p["valid"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_h = max(0.0, (now - valid).total_seconds() / 3600.0)
    x0 += vx * age_h
    y0 += vy * age_h

    r_eff = effective_radius_km(p)
    level, flags = classify_intensity(p)
    severe = bool(flags) or level in ("SEVERE", "EXTREME")

    result = {
        "storm_id": f"{p['nexrad']}-{p['storm_id']}",
        "lat": lat, "lon": lon,
        "distance_now_km": dist_now,
        "speed_kmh": spd_kmh,
        "heading_deg": hdg if spd_kmh > 0 else None,
        "max_dbz": p["max_dbz"],
        "vil_kg_m2": p["vil"],
        "echo_top_km": p["top"] * 0.3048,  # feed reports kft
        "eff_radius_km": r_eff,
        "intensity": level,
        "severe_signatures": flags,
        "data_age_min": age_h * 60.0,
    }

    if spd_kmh < 2.0:
        # Quasi-stationary cell: threat only if it is already on top of us.
        if dist_now <= r_eff:
            result.update(overhead_now=True, eta_min=0.0, cpa_km=dist_now,
                          probability=0.9, duration_min=None)
            return result
        result.update(overhead_now=False, probability=0.0)
        return None

    # Closest point of approach: minimize |r + v t|
    v2 = vx * vx + vy * vy
    t_cpa_h = -(x0 * vx + y0 * vy) / v2
    if t_cpa_h < 0:
        return None  # moving away
    t_cpa_min = t_cpa_h * 60.0
    if t_cpa_min > horizon_min:
        return None

    cx = x0 + vx * t_cpa_h
    cy = y0 + vy * t_cpa_h
    d_cpa = math.hypot(cx, cy)

    # Probability that the true track passes within r_eff, Gaussian
    # cross-track error growing with lead time.
    sigma = SIGMA0_KM + SIGMA_RATE_KM_PER_MIN * t_cpa_min
    p_track = norm_cdf((r_eff - d_cpa) / sigma) - norm_cdf((-r_eff - d_cpa) / sigma)

    # Lifecycle decay: will the cell still exist on arrival?
    halflife = SEVERE_HALFLIFE_MIN if severe else ORDINARY_HALFLIFE_MIN
    p_alive = 0.5 ** (t_cpa_min / halflife)

    prob = p_track * p_alive
    if prob < 0.02 and d_cpa > 3 * r_eff:
        return None

    # Duration of passage through the effective footprint (deterministic track)
    duration_min = None
    if d_cpa < r_eff:
        half_chord = math.sqrt(r_eff ** 2 - d_cpa ** 2)
        duration_min = 2.0 * half_chord / spd_kmh * 60.0
        eta_min = max(0.0, t_cpa_min - half_chord / spd_kmh * 60.0)
    else:
        eta_min = t_cpa_min

    result.update(overhead_now=dist_now <= r_eff, eta_min=eta_min,
                  t_cpa_min=t_cpa_min, cpa_km=d_cpa, probability=prob,
                  p_track=p_track, p_alive=p_alive, duration_min=duration_min)
    return result


def glm_bucket_for(lon, at):
    """Date-aware satellite selection. Operational GLM history:
      East: GOES-16 (Dec 2017 - Apr 2025), then GOES-19.
      West: GOES-17 (Feb 2019 - Jan 2023), then GOES-18.
    Before Feb 2019 the West slot has no GLM; fall back to East, which still
    sees all of CONUS at a shallower angle. Before Dec 2017: no GLM at all."""
    if at < datetime(2017, 12, 18, tzinfo=timezone.utc):
        return None
    east = "noaa-goes16" if at < datetime(2025, 4, 7, tzinfo=timezone.utc) else "noaa-goes19"
    if lon >= -106.0:
        return east
    if at < datetime(2019, 2, 12, tzinfo=timezone.utc):
        return east
    return "noaa-goes17" if at < datetime(2023, 1, 4, tzinfo=timezone.utc) else "noaa-goes18"


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def list_glm_keys(bucket, start, end):
    """List GLM-L2-LCFA object keys whose file start time falls in [start, end].
    Keys look like: GLM-L2-LCFA/2026/228/13/OR_GLM-L2-LCFA_G19_s20262281336000_...nc
    """
    keys = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        prefix = f"GLM-L2-LCFA/{hour:%Y}/{hour:%j}/{hour:%H}/"
        url = (f"https://{bucket}.s3.amazonaws.com/?list-type=2"
               f"&prefix={prefix}&max-keys=1000")
        try:
            root = ET.fromstring(_http_get(url))
        except Exception:
            hour += timedelta(hours=1)
            continue
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for k in root.iterfind(".//s3:Contents/s3:Key", ns):
            key = k.text
            try:
                stamp = key.split("_s")[1][:13]  # YYYYDDDHHMMSS
                t = datetime.strptime(stamp, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
            except (IndexError, ValueError):
                continue
            if start <= t <= end:
                keys.append((t, key))
        hour += timedelta(hours=1)
    return sorted(keys)


def _parse_glm_bytes(data):
    """Return list of (lat, lon, energy_J) good-quality flashes in one file.
    NOTE: netCDF4/HDF5 is not thread-safe; call this only from one thread."""
    ds = netCDF4.Dataset("inmem.nc", memory=data)
    try:
        lats = ds.variables["flash_lat"][:]
        lons = ds.variables["flash_lon"][:]
        energy = ds.variables["flash_energy"][:]
        if "flash_quality_flag" in ds.variables:
            q = ds.variables["flash_quality_flag"][:]
            good = q == 0
        else:
            good = [True] * len(lats)
        return [(float(la), float(lo), float(en))
                for la, lo, en, g in zip(lats, lons, energy, good) if g]
    finally:
        ds.close()


def fetch_glm_lightning(tgt_lat, tgt_lon, start, end, return_flashes=False):
    """Fetch GLM flashes in [start, end] and summarize lightning activity
    relative to the target point."""
    if not HAVE_NETCDF4:
        return {"available": False,
                "error": "netCDF4 not installed (pip install netCDF4)"}

    bucket = glm_bucket_for(tgt_lon, end)
    if bucket is None:
        return {"available": False, "error": "no GLM before Dec 2017"}
    window_min = (end - start).total_seconds() / 60.0
    keys = list_glm_keys(bucket, start, end)
    if not keys:
        return {"available": False, "error": f"no GLM files listed on {bucket}"}

    # Download concurrently (network-bound), parse serially (HDF5 is not
    # thread-safe and segfaults if netCDF4 runs in worker threads).
    flashes = []  # (t, dist_km, energy)
    n_failed = 0
    url = f"https://{bucket}.s3.amazonaws.com/{{}}"
    with concurrent.futures.ThreadPoolExecutor(GLM_MAX_WORKERS) as pool:
        futs = {pool.submit(_http_get, url.format(key)): t for t, key in keys}
        blobs = []
        for fut in concurrent.futures.as_completed(futs):
            try:
                blobs.append((futs[fut], fut.result()))
            except Exception:
                n_failed += 1
    for t, data in blobs:
        try:
            for la, lo, en in _parse_glm_bytes(data):
                # cheap prefilter before trig
                if abs(la - tgt_lat) > 1.5 or abs(lo - tgt_lon) > 2.0:
                    continue
                x, y = enu_km(tgt_lat, tgt_lon, la, lo)
                d = math.hypot(x, y)
                if d <= GLM_INFO_KM:
                    flashes.append((t, d, en, la, lo))
        except Exception:
            n_failed += 1

    flashes.sort(key=lambda f: f[1])
    within = lambda r: [f for f in flashes if f[1] <= r]
    n15, n40, n100 = len(within(GLM_WARN_KM)), len(within(GLM_WATCH_KM)), len(flashes)

    # Flash-rate trend inside 50 km: second half of window vs first half.
    mid = start + (end - start) / 2
    near = [f for f in flashes if f[1] <= 50.0]
    first = sum(1 for f in near if f[0] < mid)
    second = len(near) - first
    if second > first * 1.5 and second >= 5:
        trend = "INCREASING"
    elif first > second * 1.5 and first >= 5:
        trend = "DECREASING"
    else:
        trend = "STEADY"

    summary = {
        "available": True,
        "satellite": {"noaa-goes16": "GOES-16 (East)", "noaa-goes17": "GOES-17 (West)",
                      "noaa-goes18": "GOES-18 (West)", "noaa-goes19": "GOES-19 (East)"}[bucket],
        "window_min": window_min,
        "files_read": len(keys) - n_failed,
        "files_failed": n_failed,
        "nearest_flash_km": round(flashes[0][1], 1) if flashes else None,
        "flashes_within_15km": n15,
        "flashes_within_40km": n40,
        "flashes_within_100km": n100,
        "rate_40km_per_min": round(n40 / window_min, 2),
        "trend_50km": trend,
    }
    if return_flashes:
        summary["flashes"] = flashes  # (time, dist_km, energy), sorted by dist
    return summary


def lightning_tier(glm):
    """Tier floor implied by observed lightning alone."""
    if not glm.get("available"):
        return "NONE"
    if glm["flashes_within_15km"] > 0:
        return "WARNING"
    if glm["flashes_within_40km"] > 0:
        return "WATCH"
    if glm["flashes_within_100km"] > 0:
        return "ADVISORY"
    return "NONE"


TIER_ORDER = {"NONE": 0, "ADVISORY": 1, "WATCH": 2, "WARNING": 3}
TIER_COLOR = {"NONE": "70/130/70", "ADVISORY": "230/180/40",
              "WATCH": "235/125/30", "WARNING": "200/30/30"}

TOPO_CPT = """1300 106/154/94 1800 141/176/112
1800 141/176/112 2300 190/200/141
2300 190/200/141 2800 222/206/160
2800 222/206/160 3300 203/171/132
3300 203/171/132 3700 180/151/120
3700 180/151/120 4100 232/232/232
4100 232/232/232 4600 255/255/255
"""


SKADI_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}{lat:02d}/{ns}{lat:02d}{ew}{lon:03d}.hgt.gz"


def ensure_dem(dem, shade, w, e, s_, n):
    """Guarantee a topo grid covering the region. If the cached grid is
    missing or too small, download the needed 1-degree SRTM1 tiles from the
    AWS open terrain bucket (GMT's own remote server can be deactivated) and
    blend them at 2 arcsec. Returns the dem path or None on failure."""
    import subprocess, gzip, os
    pad = 0.02
    if os.path.exists(dem):
        r = subprocess.run(["gmt", "grdinfo", "-C", dem],
                           capture_output=True, text=True)
        try:
            f = r.stdout.split()
            gw, ge, gs, gn = map(float, f[1:5])
            if gw <= w + pad and ge >= e - pad and gs <= s_ + pad and gn >= n - pad:
                if shade and not os.path.exists(shade):
                    subprocess.run(["gmt", "grdgradient", dem, "-A315",
                                    "-Ne0.6", f"-G{shade}"], capture_output=True)
                return dem
        except (ValueError, IndexError):
            pass
    tiledir = os.path.dirname(os.path.abspath(dem)) or "."
    os.makedirs(tiledir, exist_ok=True)
    tiles = []
    for la in range(math.floor(s_), math.ceil(n)):
        for lo in range(math.floor(w), math.ceil(e)):
            ns, ew = ("N", "W") if la >= 0 else ("S", "W")
            alat, alon = abs(la), abs(lo) if lo < 0 else lo
            name = f"{ns}{alat:02d}{'W' if lo < 0 else 'E'}{alon:03d}.hgt"
            path = os.path.join(tiledir, name)
            if not os.path.exists(path):
                url = SKADI_URL.format(ns=ns, lat=alat,
                                       ew="W" if lo < 0 else "E", lon=alon)
                try:
                    gz = _http_get(url)
                    with open(path, "wb") as fh:
                        fh.write(gzip.decompress(gz))
                except Exception:
                    continue
            tiles.append(path)
    if not tiles:
        return None
    reg = f"-R{w:.4f}/{e:.4f}/{s_:.4f}/{n:.4f}"
    tmp = dem + ".blend.nc"
    r = subprocess.run(["gmt", "grdblend"] + tiles +
                       [reg, "-I1s", f"-G{tmp}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    subprocess.run(["gmt", "grdsample", tmp, "-I2s", f"-G{dem}"],
                   capture_output=True)
    os.remove(tmp)
    subprocess.run(["gmt", "grdgradient", dem, "-A315", "-Ne0.6",
                    f"-G{shade}"], capture_output=True)
    return dem if os.path.exists(dem) else None


def make_map(png_path, args, now, tier, map_cells, glm, replay,
             route_tl=None, radius_km=None):
    """Render a GMT map: topo relief, range rings, cells with motion vectors,
    GLM flashes, target, tier. Needs `gmt` on PATH and a local DEM grid
    (--dem); falls back to a plain basemap without one. If route_tl (a
    route timeline) is given, draws the route, the hiker's current position,
    and the camp endpoint."""
    import subprocess, tempfile, os, shutil

    lat0, lon0 = args.lat, args.lon
    radius = radius_km or getattr(args, "map_radius_km", None) or 115.0
    dlat = radius / 111.0
    dlon = radius / (111.0 * math.cos(math.radians(lat0)))
    region = f"{lon0-dlon:.4f}/{lon0+dlon:.4f}/{lat0-dlat:.4f}/{lat0+dlat:.4f}"

    wd = tempfile.mkdtemp(prefix="twmap_")

    def resolve(p):
        if not p:
            return None
        if os.path.isabs(p) or os.path.exists(p):
            return os.path.abspath(p)
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
        return alt if os.path.exists(alt) else os.path.abspath(p)

    dem = resolve(args.dem)
    dem_shade = resolve(args.dem_shade)
    dem = ensure_dem(dem, dem_shade, lon0 - dlon, lon0 + dlon,
                     lat0 - dlat, lat0 + dlat) if dem else None
    if dem_shade and not os.path.exists(dem_shade):
        dem_shade = None

    def circle_txt(radii):
        seg = []
        for r in radii:
            seg.append(f"> ring {r}")
            for a in range(0, 361, 2):
                x = r * math.sin(math.radians(a)); y = r * math.cos(math.radians(a))
                seg.append(f"{lon0 + x/(111.0*math.cos(math.radians(lat0)))} {lat0 + y/111.0}")
        return "\n".join(seg)

    with open(f"{wd}/rings.txt", "w") as f:
        f.write(circle_txt([GLM_WARN_KM, GLM_WATCH_KM, GLM_INFO_KM]))
    with open(f"{wd}/topo.cpt", "w") as f:
        f.write(TOPO_CPT)

    cells = [c for c in map_cells
             if abs(c["lat"]-lat0) <= dlat and abs(c["lon"]-lon0) <= dlon]
    if cells:
        with open(f"{wd}/cells.txt", "w") as f:
            for c in cells:
                f.write(f"{c['lon']} {c['lat']} {c['dbz']}\n")
        with open(f"{wd}/threat_cells.txt", "w") as f:
            for c in cells:
                if c["threat"]:
                    f.write(f"{c['lon']} {c['lat']} {c['dbz']}\n")
        with open(f"{wd}/vectors.txt", "w") as f:
            for c in cells:
                if c["spd_kmh"] > 2.0:
                    f.write(f"{c['lon']} {c['lat']} {c['hdg']} {c['spd_kmh']*0.5}\n")

    flashes = glm.get("flashes", []) if glm.get("available") else []
    flashes = [fl for fl in flashes
               if abs(fl[3]-lat0) <= dlat and abs(fl[4]-lon0) <= dlon]
    if flashes:
        with open(f"{wd}/flashes.txt", "w") as f:
            for fl in flashes:
                f.write(f"{fl[4]} {fl[3]}\n")

    if route_tl is not None:
        with open(f"{wd}/route.txt", "w") as f:
            for (_, a, o, _, _) in route_tl:
                f.write(f"{o} {a}\n")
        hk = hiker_pos(route_tl, now)
        with open(f"{wd}/hiker.txt", "w") as f:
            f.write(f"{hk[1]} {hk[0]}\n")
        with open(f"{wd}/camp.txt", "w") as f:
            f.write(f"{route_tl[-1][2]} {route_tl[-1][1]}\n")

    label = now.strftime("%Y-%m-%d %H:%MZ") + ("  [REPLAY]" if replay else "")
    nfl = glm.get("flashes_within_100km", "n/a") if glm.get("available") else "n/a"

    s = ["gmt begin fig png",
         f"gmt set FONT_TITLE 13p,Helvetica-Bold MAP_FRAME_TYPE plain GMT_GRAPHICS_DPU 110"]
    if dem and os.path.exists(dem):
        shade = f" -I{dem_shade}" if dem_shade and os.path.exists(dem_shade) else " -I+d"
        s.append(f"gmt grdimage {dem} -R{region} -JM16c -Ctopo.cpt{shade}")
        s.append(f"gmt basemap -Baf -B+t'{label}   STATUS: {tier}   cells: {len(cells)}  flashes(100km/{glm.get('window_min', 0):.0f}min): {nfl}'")
    else:
        s.append(f"gmt basemap -R{region} -JM16c -Baf -B+t'{label}   STATUS: {tier}'")
    s.append("gmt plot rings.txt -W1p,white,4_4")
    if cells:
        s.append("gmt makecpt -Cjet -T20/70/5 -H > dbz.cpt")
        if os.path.getsize(f"{wd}/vectors.txt") > 0:
            s.append("gmt plot vectors.txt -S=0.32c+e+h0.5 -W1.2p,40/40/40 -G40/40/40")
        s.append("gmt plot cells.txt -Sc0.30c -Cdbz.cpt -W0.5p,black")
        if os.path.getsize(f"{wd}/threat_cells.txt") > 0:
            s.append("gmt plot threat_cells.txt -Sc0.42c -W1.6p,magenta")
        s.append("gmt colorbar -Cdbz.cpt -DjBR+w4.5c/0.35c+o0.6c/0.6c+h -Baf+l'max dBZ' -F+gwhite+p0.5p")
    if flashes:
        # NB: stroked symbols (-S+ / -Sx) trigger a gs-10 /limitcheck with
        # multiple points under GMT 6.5; filled symbols are safe.
        s.append("gmt plot flashes.txt -Sd0.15c -Gred -W0.3p,black")
    if route_tl is not None:
        s.append("gmt plot route.txt -W2.4p,white")
        s.append("gmt plot route.txt -W1.2p,blue")
        s.append("gmt plot camp.txt -St0.4c -Gblue -W0.8p,white")
        s.append("gmt plot hiker.txt -Sc0.32c -Gorange -W1p,black")
    if route_tl is None:
        s.append(f"echo {lon0} {lat0} | gmt plot -Sa0.55c -Gyellow -W1p,black")
    s.append(f"echo {lon0-dlon+0.03} {lat0-dlat+0.02} STATUS: {tier} | "
             f"gmt text -F+f12p,Helvetica-Bold,white+jBL -G{TIER_COLOR[tier]} -W0.5p -C30%")
    s.append("gmt end")

    env = dict(os.environ, GMT_SESSION_NAME=os.path.basename(wd))
    r = subprocess.run(["bash", "-ec", "\n".join(s)], cwd=wd,
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"gmt failed: {r.stderr.strip()[-400:]}")
    os.makedirs(os.path.dirname(os.path.abspath(png_path)), exist_ok=True)
    shutil.move(f"{wd}/fig.png", png_path)
    shutil.rmtree(wd, ignore_errors=True)


def tier_for(threats):
    """Hurricane-style tiers.
    WARNING: passage likely (P>=0.5) within 30 min, STRONG or worse, or any
             severe-signature cell with P>=0.3 within 45 min.
    WATCH:   P>=0.25 within the horizon, or any cell CPA<15 km within 60 min.
    ADVISORY: convection tracked in the area but low direct-hit probability.
    """
    tier = "NONE"
    for t in threats:
        p, eta = t["probability"], t["eta_min"]
        strong = t["intensity"] in ("STRONG", "SEVERE", "EXTREME")
        sev = bool(t["severe_signatures"]) or t["intensity"] in ("SEVERE", "EXTREME")
        if t.get("overhead_now") or (p >= 0.5 and eta <= 30 and strong) or (sev and p >= 0.3 and eta <= 45):
            return "WARNING"
        if p >= 0.25 or (t["cpa_km"] < 15 and eta <= 60):
            tier = "WATCH"
        elif tier == "NONE":
            tier = "ADVISORY"
    return tier


def main():
    ap = argparse.ArgumentParser(description="Thunderstorm cell threat for a coordinate")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--horizon-min", type=float, default=90.0)
    ap.add_argument("--search-radius-km", type=float, default=250.0)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--glm-window-min", type=float, default=10.0,
                    help="lightning lookback window (minutes)")
    ap.add_argument("--no-glm", action="store_true", help="skip GLM lightning fetch")
    ap.add_argument("--at", type=str, default=None,
                    help="replay mode: analyze as of this UTC time, "
                         "e.g. 2024-08-01T21:30")
    ap.add_argument("--verify-min", type=float, default=0.0,
                    help="replay only: check what actually happened in the "
                         "N minutes after --at and score the nowcast")
    ap.add_argument("--map", type=str, default=None,
                    help="write a GMT map of the situation to this PNG path; "
                         "in route mode writes a _wide and a _route pair")
    ap.add_argument("--map-radius-km", type=float, default=None,
                    help="map half-width in km (default 115; route-fit map "
                         "is auto-sized regardless)")
    ap.add_argument("--dem", type=str, default="dem/topo.nc",
                    help="local relief grid for the map background")
    ap.add_argument("--dem-shade", type=str, default="dem/shade.nc",
                    help="precomputed hillshade grid (optional)")
    ap.add_argument("--gpx", type=str, default=None,
                    help="route mode: GPX track/route of the planned hike")
    ap.add_argument("--start-local", type=str, default="07:30",
                    help="route mode: hike start, local time. 'HH:MM' "
                         "(today) or 'YYYY-MM-DDTHH:MM'")
    ap.add_argument("--utc-offset", type=float, default=-6.0,
                    help="route mode: local UTC offset in hours (MDT=-6)")
    ap.add_argument("--pace-factor", type=float, default=0.7,
                    help="route mode: Tobler speed scale (0.7 ~ loaded pack)")
    ap.add_argument("--treeline-m", type=float, default=3500.0,
                    help="treeline elevation (m) for the exposure model")
    args = ap.parse_args()

    replay = args.at is not None
    if replay:
        at = args.at.rstrip("Z")
        fmt = "%Y-%m-%dT%H:%M:%S" if at.count(":") == 2 else "%Y-%m-%dT%H:%M"
        now = datetime.strptime(at, fmt).replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        if args.verify_min:
            ap.error("--verify-min requires --at (cannot verify the future)")

    timeline = None
    if args.gpx:
        pts = load_gpx(args.gpx)
        if "T" in args.start_local:
            local = datetime.strptime(args.start_local, "%Y-%m-%dT%H:%M")
            start_utc = local.replace(tzinfo=timezone.utc) \
                - timedelta(hours=args.utc_offset)
        else:
            hh, mm = map(int, args.start_local.split(":"))
            start_utc = now.replace(hour=hh, minute=mm, second=0,
                                    microsecond=0) \
                - timedelta(hours=args.utc_offset)
        timeline = route_timeline(pts, start_utc, args.pace_factor)
        start_local_hour = (start_utc + timedelta(hours=args.utc_offset)).hour
        args.lat, args.lon, _ = hiker_pos(timeline, now)

    if replay:
        features = fetch_archive_cells(args.lat, args.lon,
                                       args.search_radius_km, now)
    else:
        features = fetch_json(ATTR_URL).get("features", [])

    threats = []
    map_cells = []  # every in-range cell, for --map
    n_in_range = 0
    for f in features:
        lon, lat = f["geometry"]["coordinates"]
        x, y = enu_km(args.lat, args.lon, lat, lon)
        if math.hypot(x, y) > args.search_radius_km:
            continue
        n_in_range += 1
        if timeline is not None:
            r = analyze_cell_route(f, timeline, now, args.horizon_min)
        else:
            r = analyze_cell(f, args.lat, args.lon, args.horizon_min, now)
        if r:
            threats.append(r)
        p = f["properties"]
        map_cells.append({"lat": lat, "lon": lon, "dbz": p["max_dbz"],
                          "hdg": (p["drct"] + 180.0) % 360.0,
                          "spd_kmh": p["sknt"] * KT_TO_KMH,
                          "threat": bool(r)})

    threats.sort(key=lambda t: t["eta_min"])
    tier = tier_for(threats)

    # GLM lightning: observed flashes override model-based tiers upward only.
    if args.no_glm:
        glm = {"available": False, "error": "disabled (--no-glm)"}
    else:
        try:
            end = now if replay else now - timedelta(seconds=GLM_LATENCY_S)
            glm = fetch_glm_lightning(args.lat, args.lon,
                                      end - timedelta(minutes=args.glm_window_min),
                                      end,
                                      return_flashes=bool(args.map or timeline))
        except Exception as e:
            glm = {"available": False, "error": str(e)}
    ltier = lightning_tier(glm)
    if TIER_ORDER[ltier] > TIER_ORDER[tier]:
        tier = ltier

    # Active alerts: NWS API in realtime, IEM VTEC archive in replay.
    alerts = []
    try:
        if replay:
            for a in fetch_sbw_archive(args.lat, args.lon, now, now):
                alerts.append({"event": a["event"], "headline": a["headline"],
                               "expires": a["expires"]})
        else:
            adata = fetch_json(ALERTS_URL.format(lat=args.lat, lon=args.lon))
            for a in adata.get("features", []):
                ev = a["properties"].get("event", "")
                if any(k in ev for k in ("Thunderstorm", "Tornado", "Flash Flood", "Special Weather")):
                    alerts.append({"event": ev,
                                   "headline": a["properties"].get("headline"),
                                   "expires": a["properties"].get("expires")})
    except Exception as e:
        alerts = [{"event": "ALERT_FETCH_FAILED", "headline": str(e), "expires": None}]

    # Verification: what actually happened at the point after `at`.
    verify = None
    if replay and args.verify_min > 0:
        vend = now + timedelta(minutes=args.verify_min)
        try:
            vg = fetch_glm_lightning(args.lat, args.lon, now, vend,
                                     return_flashes=True)
        except Exception as e:
            vg = {"available": False, "error": str(e)}
        first15 = first40 = None
        if vg.get("available"):
            for t, d, *_ in sorted(vg.get("flashes", []), key=lambda f: f[0]):
                dt_min = (t - now).total_seconds() / 60.0
                if d <= GLM_WATCH_KM and first40 is None:
                    first40 = dt_min
                if d <= GLM_WARN_KM and first15 is None:
                    first15 = dt_min
                    break
        try:
            sbws = fetch_sbw_archive(args.lat, args.lon, now, vend)
        except Exception:
            sbws = []
        hit = first15 is not None
        predicted = TIER_ORDER[tier] >= TIER_ORDER["WATCH"]
        verify = {
            "window_min": args.verify_min,
            "first_flash_15km_min": round(first15, 1) if first15 is not None else None,
            "first_flash_40km_min": round(first40, 1) if first40 is not None else None,
            "flashes_15km": vg.get("flashes_within_15km"),
            "flashes_40km": vg.get("flashes_within_40km"),
            "sbw_polygons_over_point": [f"{s['event']} {s['issue']:%H:%MZ}" for s in sbws],
            "outcome": ("HIT" if predicted and hit else
                        "MISS" if not predicted and hit else
                        "FALSE_ALARM" if predicted and not hit else
                        "CORRECT_NULL"),
        }

    route = None
    if timeline is not None:
        h_lat, h_lon, h_ele = hiker_pos(timeline, now)
        c_lat = sum(p[1] for p in timeline) / len(timeline)
        c_lon = sum(p[2] for p in timeline) / len(timeline)
        try:
            ha = max(18, int((timeline[-1][0] + timedelta(hours=9)
                              - now).total_seconds() // 3600) + 1)
            curve = fetch_convective_forecast(
                c_lat, c_lon, date_utc=now if replay else None,
                hours_ahead=ha)
        except Exception:
            curve = None
        if curve is None and not replay:
            try:
                curve = fetch_nws_thunder(c_lat, c_lon)
            except Exception:
                curve = None
        done_km = 0.0
        for (t, a, o, e, d) in timeline:
            if t <= now:
                done_km = d
        d_now, d_corr = corridor_metrics(timeline, now,
                                         glm.get("flashes", []) if glm.get("available") else [])
        p_out = outlook_probability(now, args.utc_offset, d_corr, curve)
        route = {
            "start_utc": timeline[0][0].strftime("%H:%MZ"),
            "arrival_utc": timeline[-1][0].strftime("%H:%MZ"),
            "length_km": round(timeline[-1][4], 2),
            "hiker": {"lat": round(h_lat, 5), "lon": round(h_lon, 5),
                      "ele_m": round(h_ele), "km_done": round(done_km, 2),
                      "status": ("pre-start" if now < timeline[0][0] else
                                 "at camp" if now >= timeline[-1][0] else "hiking")},
            "nearest_flash_to_hiker_km": round(d_now, 1) if d_now is not None else None,
            "nearest_flash_to_corridor_km": round(d_corr, 1) if d_corr is not None else None,
            "outlook_p_15km_3h": round(p_out, 2),
            "briefing": (route_briefing(curve, args.utc_offset,
                                        timeline[0][0]) if curve else None),
        }
        prof = hazard_profile(timeline, curve, args.utc_offset,
                              treeline_m=args.treeline_m) if curve else []
        if prof:
            # instantaneous hazard at the hiker now: observed lightning
            # overrides the forecast hour probability
            trip = curve.get(now.replace(minute=0, second=0, microsecond=0))
            p_inst = trip["p"] if trip else 0.0
            if d_now is not None:
                if d_now <= GLM_WARN_KM:
                    p_inst = max(p_inst, 0.95)
                elif d_now <= GLM_WATCH_KM:
                    p_inst = max(p_inst, 0.70)
            i = min(range(len(timeline)),
                    key=lambda k: abs((timeline[k][0] - now).total_seconds()))
            e_now = exposure_factor(h_ele, _crest_flags(timeline)[i]
                                    if now <= timeline[-1][0] else False,
                                    treeline_m=args.treeline_m)
            h_now = p_inst * e_now
            peak = max(prof, key=lambda r: r["hazard"])
            route.update({
                "hazard_now": {"hazard": round(h_now, 2),
                               "band": hazard_band(h_now),
                               "exposure": round(e_now, 2)},
                "hazard_peak": {"t_local": (peak["t"] + timedelta(
                                    hours=args.utc_offset)).strftime("%H:%M"),
                                "hazard": peak["hazard"], "band": peak["band"],
                                "ele_m": peak["ele_m"], "crest": peak["crest"]},
                "hazard_profile": [
                    {"t_local": (r["t"] + timedelta(hours=args.utc_offset)
                                 ).strftime("%H:%M"), **{k: v for k, v in
                                 r.items() if k != "t"}} for r in prof],
            })
        if d_corr is not None and d_corr <= GLM_WATCH_KM \
                and TIER_ORDER[tier] < TIER_ORDER["WATCH"]:
            tier = "WATCH"  # lightning ahead on the route

    out = {
        "target": {"lat": args.lat, "lon": args.lon},
        "mode": "replay" if replay else "realtime",
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cells_within_search_radius": n_in_range,
        "tier": tier,
        "route": route,
        "threats": threats,
        "lightning_glm": {k: v for k, v in glm.items() if k != "flashes"},
        "alerts": alerts,
        "verification": verify,
    }

    if args.map:
        try:
            if timeline is not None:
                lats = [p[1] for p in timeline]
                lons = [p[2] for p in timeline]
                span = math.hypot((max(lats) - min(lats)) * 111.0,
                                  (max(lons) - min(lons)) * 111.0
                                  * math.cos(math.radians(args.lat)))
                fit_r = max(span / 2.0 + 7.0, 12.0)
                base = args.map[:-4] if args.map.endswith(".png") else args.map
                make_map(base + "_wide.png", args, now, tier, map_cells, glm,
                         replay, route_tl=timeline)
                make_map(base + "_route.png", args, now, tier, map_cells, glm,
                         replay, route_tl=timeline, radius_km=fit_r)
                out["map"] = [base + "_wide.png", base + "_route.png"]
            else:
                make_map(args.map, args, now, tier, map_cells, glm, replay)
                out["map"] = args.map
        except Exception as e:
            out["map_error"] = str(e)
            print(f"map generation failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"Target {args.lat:.4f}, {args.lon:.4f}   {out['generated_utc']}"
          f"{'   [REPLAY]' if replay else ''}")
    print(f"Cells within {args.search_radius_km:.0f} km: {n_in_range}")
    print(f"STATUS: {tier}")
    if route:
        h = route["hiker"]
        print(f"Hiker: {h['status']}, km {h['km_done']}/{route['length_km']}, "
              f"{h['ele_m']} m ({h['lat']}, {h['lon']}); "
              f"start {route['start_utc']}, camp ETA {route['arrival_utc']}")
        print(f"  corridor lightning: nearest to hiker "
              f"{route['nearest_flash_to_hiker_km'] or '---'} km, "
              f"nearest to route ahead {route['nearest_flash_to_corridor_km'] or '---'} km; "
              f"outlook P(<=15 km, 3 h) = {route['outlook_p_15km_3h']}")
        if route.get("briefing"):
            print("  Trip forecast, P(lightning near route) by local hour:")
            b = route["briefing"]
            hours = " ".join(f"{r['local_hour']:>4}" for r in b)
            probs = " ".join(f"{r['p_lightning']:4.2f}" for r in b)
            print(f"    hour {hours}")
            print(f"    P    {probs}")
        if route.get("hazard_now"):
            hn, hp = route["hazard_now"], route["hazard_peak"]
            print(f"  Hazard now: {hn['hazard']} ({hn['band']}, exposure "
                  f"{hn['exposure']}); peak along hike: {hp['hazard']} "
                  f"({hp['band']}) at {hp['t_local']} local, {hp['ele_m']} m"
                  f"{', on crest/pass' if hp['crest'] else ''}")
            print("  Hazard profile H = P(hour) x exposure(projected position),"
                  " half-hourly:")
            prof = route["hazard_profile"][::2]
            print("    time " + " ".join(r["t_local"][:5].replace(":", "")
                                         for r in prof))
            print("    ele  " + " ".join(f"{r['ele_m']:>4}" for r in prof))
            print("    H    " + " ".join(f"{r['hazard']:4.2f}" for r in prof))
    if not threats:
        print("No tracked cell projected over this point within "
              f"{args.horizon_min:.0f} min.")
    for t in threats:
        line = (f"  {t['storm_id']:>8}  P={t['probability']:.2f}  "
                f"ETA {t['eta_min']:5.1f} min  CPA {t['cpa_km']:5.1f} km  "
                f"{t['intensity']:<8} {t['max_dbz']} dBZ  VIL {t['vil_kg_m2']}  "
                f"top {t['echo_top_km']:.1f} km  "
                f"{t['speed_kmh']:.0f} km/h hdg {t['heading_deg'] and round(t['heading_deg']) or '---'}")
        if t.get("duration_min"):
            line += f"  passage ~{t['duration_min']:.0f} min"
        if t["severe_signatures"]:
            line += "  [" + "; ".join(t["severe_signatures"]) + "]"
        print(line)
    if glm.get("available"):
        nf = glm["nearest_flash_km"]
        print(f"Lightning (GLM, {glm['satellite']}, last {glm['window_min']:.0f} min): "
              f"{glm['flashes_within_100km']} flashes <=100 km, "
              f"{glm['flashes_within_40km']} <=40 km, "
              f"{glm['flashes_within_15km']} <=15 km; "
              f"nearest {nf if nf is not None else '---'} km; "
              f"rate {glm['rate_40km_per_min']}/min (40 km), trend {glm['trend_50km']}")
    else:
        print(f"Lightning (GLM): unavailable ({glm.get('error')})")
    for a in alerts:
        print(f"  NWS: {a['event']} (expires {a['expires']})")
    if verify:
        print(f"VERIFICATION (+{verify['window_min']:.0f} min): {verify['outcome']}")
        f15, f40 = verify["first_flash_15km_min"], verify["first_flash_40km_min"]
        print(f"  first flash <=15 km: "
              f"{'+%.1f min' % f15 if f15 is not None else 'none'};  "
              f"<=40 km: {'+%.1f min' % f40 if f40 is not None else 'none'};  "
              f"totals {verify['flashes_15km']}/{verify['flashes_40km']} (15/40 km)")
        for s in verify["sbw_polygons_over_point"]:
            print(f"  SBW over point during window: {s}")


if __name__ == "__main__":
    main()
