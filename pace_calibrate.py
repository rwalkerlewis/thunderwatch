#!/usr/bin/env python3
"""Calibrate the hiker's pace factor from observed position fixes (inReach
tracking points or messaged positions).

Each fix is matched to the nearest point on the route; the pace factor for
each segment between fixes is (modeled time at factor 1.0) / (observed
time). Reports per-segment factors split by terrain character plus the
distance-weighted overall factor to use in the next daily_report run.

Usage (from repo root):
    python3 pace_calibrate.py ROUTE.gpx 2026-08-18 \
        --fixes "0530=39.049626,-106.980335; 0958=39.0553,-107.0079; 1430=39.0802,-107.0233" \
        [--utc-offset -6]
Times are local HHMM (24-h, no colon); a +1 suffix means next day.
"""
import argparse
import math
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_watch as tw

MI = 0.621371


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx")
    ap.add_argument("date", help="local hike date YYYY-MM-DD")
    ap.add_argument("--fixes", required=True,
                    help="'HHMM=lat,lon; HHMM=lat,lon; ...' in route order")
    ap.add_argument("--utc-offset", type=float, default=-6.0)
    a = ap.parse_args()

    pts = tw.load_gpx(a.gpx)
    base = datetime.strptime(a.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # reference timeline at factor 1.0 (pure Tobler + break inflation)
    ref = tw.route_timeline(pts, base, pace_factor=1.0)
    t0 = ref[0][0]

    def model_hours_at(km):
        for (t, la, lo, e, d) in ref:
            if d >= km:
                return (t - t0).total_seconds() / 3600.0, e
        return (ref[-1][0] - t0).total_seconds() / 3600.0, ref[-1][3]

    fixes = []
    for chunk in a.fixes.split(";"):
        clock, _, coord = chunk.strip().partition("=")
        plus = clock.endswith("+1")
        clock = clock.replace("+1", "")
        hh, mm = int(clock[:2]), int(clock[2:])
        t_local = base + timedelta(days=1 if plus else 0, hours=hh, minutes=mm)
        t_utc = t_local - timedelta(hours=a.utc_offset)
        lat, lon = map(float, coord.split(","))
        # nearest route point
        best = min(ref, key=lambda p: math.hypot(
            (p[2] - lon) * 111.195 * math.cos(math.radians(lat)),
            (p[1] - lat) * 111.195))
        off = math.hypot((best[2] - lon) * 111.195 * math.cos(math.radians(lat)),
                         (best[1] - lat) * 111.195)
        fixes.append({"t": t_utc, "local": t_local, "km": best[4],
                      "ele": best[3], "off_m": off * 1000.0})

    fixes.sort(key=lambda f: f["t"])
    print(f"{len(fixes)} fixes on {os.path.basename(a.gpx)}:")
    for f in fixes:
        print(f"  {f['local']:%H%M}  mi {f['km']*MI:5.2f}  "
              f"{f['ele']*3.28084:6.0f} ft  ({f['off_m']:.0f} m off-track)")
    if len(fixes) < 2:
        print("Need at least two fixes to calibrate.")
        return

    print("\nSegment factors (modeled@1.0 / observed):")
    total_w = total_wf = 0.0
    for f1, f2 in zip(fixes, fixes[1:]):
        obs_h = (f2["t"] - f1["t"]).total_seconds() / 3600.0
        m1, e1 = model_hours_at(f1["km"])
        m2, e2 = model_hours_at(f2["km"])
        mod_h = m2 - m1
        dk = f2["km"] - f1["km"]
        if obs_h <= 0 or dk <= 0.05:
            continue
        fac = mod_h / obs_h
        de = e2 - e1
        kind = "climb" if de > 60 else "descent" if de < -60 else "rolling"
        print(f"  mi {f1['km']*MI:5.2f}->{f2['km']*MI:5.2f} "
              f"({kind:>7}, {de:+5.0f} m): observed {obs_h:4.1f} h, "
              f"factor {fac:.2f}  [{dk/obs_h:.2f} km/h]")
        total_w += dk
        total_wf += dk * fac
    if total_w > 0:
        overall = total_wf / total_w
        print(f"\nOverall (distance-weighted) pace factor: {overall:.2f}")
        print(f"Use: daily_report.py ... --pace-factor {overall:.2f}")


if __name__ == "__main__":
    main()
