#!/usr/bin/env python3
"""Example case: Four Pass Loop day 1 (Maroon Bells), 2024-08-05, start
07:30 MDT.

Runs the complete hiker-warning experiment:
  1. Pre-trip briefing at the trailhead from the archived model forecast
     (hourly P(lightning near route)).
  2. Roll forward through the day in 30-min steps using only data available
     at each step (radar cell archive + last 10 min of GLM lightning).
  3. Compare predictions against the full-day GLM record: when did lightning
     actually first come within 15 km of the hiker, and how much warning did
     each signal provide.

First run downloads ~900 GLM files (~200 MB) to build the day's flash cache
(examples/flashes_2024-08-05.csv); subsequent runs reuse it. Requires
netCDF4 for the cache build. Run from the repo root:

    python3 examples/four_pass_loop_replay.py
"""
import concurrent.futures
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from thunder_watch import (load_gpx, route_timeline, hiker_pos, enu_km,
                           analyze_cell_route, fetch_archive_cells, tier_for,
                           corridor_metrics, outlook_probability, TIER_ORDER,
                           GLM_WARN_KM, GLM_WATCH_KM, list_glm_keys, _http_get,
                           _parse_glm_bytes, glm_bucket_for,
                           fetch_convective_forecast, route_briefing)

HERE = os.path.dirname(os.path.abspath(__file__))
GPX = os.path.join(HERE, "4pl-day-1.gpx")
CACHE = os.path.join(HERE, "flashes_2024-08-05.csv")
DAY = datetime(2024, 8, 5, tzinfo=timezone.utc)
START = DAY.replace(hour=13, minute=30)          # 07:30 MDT
ROLL_END = DAY.replace(hour=22, minute=30)
CACHE_END = DAY.replace(hour=23, minute=30)
UTC_OFF = -6.0
HORIZON_MIN = 240.0


def build_cache(clat, clon):
    bucket = glm_bucket_for(clon, CACHE_END)
    keys = list_glm_keys(bucket, START - timedelta(minutes=10), CACHE_END)[::2]
    print(f"Building GLM cache: {len(keys)} files from {bucket} ...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futs = {pool.submit(_http_get,
                            f"https://{bucket}.s3.amazonaws.com/{k}"): t
                for t, k in keys}
        blobs, fails = [], 0
        for fut in concurrent.futures.as_completed(futs):
            try:
                blobs.append((futs[fut], fut.result()))
            except Exception:
                fails += 1
    n = 0
    with open(CACHE, "w") as f:
        for t, data in sorted(blobs):
            try:
                for la, lo, en in _parse_glm_bytes(data):
                    if abs(la - clat) <= 1.3 and abs(lo - clon) <= 1.7:
                        f.write(f"{t.isoformat()},{la:.4f},{lo:.4f}\n")
                        n += 1
            except Exception:
                fails += 1
    print(f"cached {n} flashes ({fails} failed files)\n", flush=True)


def main():
    pts = load_gpx(GPX)
    tl = route_timeline(pts, START, pace_factor=0.7)
    clat = sum(p[1] for p in tl) / len(tl)
    clon = sum(p[2] for p in tl) / len(tl)
    print(f"Route {tl[-1][4]:.2f} km, {round(tl[0][3])} m -> "
          f"{round(max(p[3] for p in tl))} m; start "
          f"{START:%H:%M}Z (07:30 MDT), camp ETA {tl[-1][0]:%H:%M}Z "
          f"({(tl[-1][0] + timedelta(hours=UTC_OFF)):%H:%M} MDT)\n")

    curve = fetch_convective_forecast(clat, clon, date_utc=START)
    b = route_briefing(curve, UTC_OFF, start)
    print("PRE-TRIP BRIEFING (issued 07:30 MDT), P(lightning near route):")
    print("  hour " + " ".join(f"{r['local_hour']:>4}" for r in b))
    print("  P    " + " ".join(f"{r['p_lightning']:4.2f}" for r in b) + "\n")

    if not os.path.exists(CACHE):
        build_cache(clat, clon)
    flashes = []
    for line in open(CACHE):
        ts, la, lo = line.strip().split(",")
        flashes.append((datetime.fromisoformat(ts), float(la), float(lo)))
    flashes.sort()

    first15 = first40 = None
    for t, la, lo in flashes:
        h = hiker_pos(tl, t)
        d = math.hypot(*enu_km(h[0], h[1], la, lo))
        if d <= GLM_WATCH_KM and first40 is None:
            first40 = (t, d)
        if d <= GLM_WARN_KM and first15 is None:
            first15 = (t, d)
            break

    print(f"{'UTC':>5} {'MDT':>5} {'pos km':>6} {'ele':>5} | {'cells':>5} "
          f"{'maxP':>5} | {'fl->hiker':>9} {'fl->route':>9} {'outlook':>7} | tier")
    rows = []
    T = START
    while T <= ROLL_END:
        h = hiker_pos(tl, T)
        feats = fetch_archive_cells(h[0], h[1], 250.0, T)
        threats = [r for f in feats
                   if (r := analyze_cell_route(f, tl, T, HORIZON_MIN))]
        tier = tier_for(threats)
        win = [(t, 0, 0, la, lo) for t, la, lo in flashes
               if T - timedelta(minutes=10) < t <= T]
        d_now, d_corr = corridor_metrics(tl, T, win)
        p_out = outlook_probability(T, UTC_OFF, d_corr, curve)
        if d_now is not None and d_now <= GLM_WARN_KM:
            tier = "WARNING"
        elif ((d_now is not None and d_now <= GLM_WATCH_KM)
              or (d_corr is not None and d_corr <= GLM_WATCH_KM)):
            tier = max(tier, "WATCH", key=lambda x: TIER_ORDER[x])
        maxP = max((t_["probability"] for t_ in threats), default=0.0)
        dist_km = max((d for (t_, a, o, e, d) in tl if t_ <= T), default=0.0)
        rows.append((T, tier, p_out))
        print(f"{T:%H:%M} {(T + timedelta(hours=UTC_OFF)):%H:%M} {dist_km:6.2f} "
              f"{h[2]:5.0f} | {len(feats):>5} {maxP:5.2f} | "
              f"{('%7.1f' % d_now) if d_now is not None else '      -'}   "
              f"{('%7.1f' % d_corr) if d_corr is not None else '      -'}   "
              f"{p_out:7.2f} | {tier}")
        T += timedelta(minutes=30)

    print()
    if first40:
        print(f"ACTUAL: first flash <=40 km of hiker {first40[0]:%H:%M}Z "
              f"({(first40[0] + timedelta(hours=UTC_OFF)):%H:%M} MDT), "
              f"{first40[1]:.1f} km")
    if not first15:
        print("ACTUAL: no flash within 15 km of the hiker all day")
        return
    ev = first15[0]
    print(f"ACTUAL: first flash <=15 km of hiker {ev:%H:%M}Z "
          f"({(ev + timedelta(hours=UTC_OFF)):%H:%M} MDT), {first15[1]:.1f} km")
    peak = max(b, key=lambda r: r["p_lightning"])
    lead_b = (ev - START).total_seconds() / 60.0
    print(f"  briefing (07:30)  peak P={peak['p_lightning']} at "
          f"{peak['local_hour']}:00 local -> issued {lead_b:.0f} min ahead")
    for name, cond in [("outlook >= 0.60", lambda r: r[2] >= 0.60),
                       ("outlook >= 0.50", lambda r: r[2] >= 0.50),
                       ("tier >= WATCH", lambda r: TIER_ORDER[r[1]] >= 2),
                       ("tier == WARNING", lambda r: r[1] == "WARNING")]:
        t_first = next((r[0] for r in rows if cond(r) and r[0] <= ev), None)
        if t_first:
            lead = (ev - t_first).total_seconds() / 60.0
            print(f"  {name:<17} first at {t_first:%H:%M}Z -> lead {lead:.0f} min"
                  f"{'  (>= 3 h)' if lead >= 180 else ''}")
        else:
            print(f"  {name:<17} never fired before the event")


if __name__ == "__main__":
    main()
