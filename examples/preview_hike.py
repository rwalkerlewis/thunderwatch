#!/usr/bin/env python3
"""Pre-trip preview for any GPX route and start time.

Prints route stats (length, gain, elevations in meters, camp ETA), then runs
the full ThunderWatch route analysis: pre-trip briefing from the environment
forecast, current cells and lightning, tier, and the dual-scope map pair.

Usage (from repo root):
    python3 examples/preview_hike.py ROUTE.gpx 2026-08-18T07:30 [--utc-offset -6] [--map out.png]
"""
import argparse, math, os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import thunder_watch as tw

ap = argparse.ArgumentParser()
ap.add_argument("gpx")
ap.add_argument("start_local", help="YYYY-MM-DDTHH:MM local, or HH:MM for today")
ap.add_argument("--utc-offset", type=float, default=-6.0)
ap.add_argument("--pace-factor", type=float, default=0.7)
ap.add_argument("--map", default=None)
a = ap.parse_args()

pts = tw.load_gpx(a.gpx)
d = g = 0.0
for (a1, o1, e1), (a2, o2, e2) in zip(pts, pts[1:]):
    x = (o2 - o1) * 111.195 * math.cos(math.radians(a1))
    y = (a2 - a1) * 111.195
    d += math.hypot(x, y)
    g += max(0.0, e2 - e1)
if "T" in a.start_local:
    start = datetime.strptime(a.start_local, "%Y-%m-%dT%H:%M") \
        .replace(tzinfo=timezone.utc) - timedelta(hours=a.utc_offset)
else:
    hh, mm = map(int, a.start_local.split(":"))
    start = datetime.now(timezone.utc).replace(hour=hh, minute=mm, second=0,
                                               microsecond=0) \
        - timedelta(hours=a.utc_offset)
tl = tw.route_timeline(pts, start, a.pace_factor)
print(f"ROUTE: {os.path.basename(a.gpx)}  {d:.2f} km, +{g:.0f} m; "
      f"{round(pts[0][2])} m -> max {round(max(p[2] for p in pts))} m -> "
      f"{round(pts[-1][2])} m")
print(f"START: {(start + timedelta(hours=a.utc_offset)):%Y-%m-%d %H:%M} local; "
      f"camp ETA {(tl[-1][0] + timedelta(hours=a.utc_offset)):%H:%M} local "
      f"({tl[-1][0]:%H:%M}Z)\n")

argv = ["thunder_watch", "0", "0", "--gpx", a.gpx,
        "--start-local", a.start_local, "--utc-offset", str(a.utc_offset),
        "--pace-factor", str(a.pace_factor)]
if a.map:
    argv += ["--map", a.map]
sys.argv = argv
tw.main()
