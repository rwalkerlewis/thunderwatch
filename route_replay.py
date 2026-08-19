#!/usr/bin/env python3
"""Roll-forward test: GPX route, start 07:30 MDT 2024-08-05, step 30 min.
At each step, use only data available at that time (cells from the radar
archive, flashes from the last 10 min of the GLM cache) to predict whether
a thunder cell / lightning will cross the hiker's path, then compare against
what the full-day flash record shows actually happened."""
import math, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from thunder_watch import (load_gpx, route_timeline, hiker_pos, enu_km,
                           analyze_cell_route, fetch_archive_cells, tier_for,
                           corridor_metrics, outlook_probability, TIER_ORDER,
                           GLM_WARN_KM, GLM_WATCH_KM,
                           fetch_convective_forecast, route_briefing)

UTC_OFF = -6.0
HORIZON_MIN = 240.0

pts = load_gpx("4pl-day-1.gpx")
start = datetime(2024, 8, 5, 13, 30, tzinfo=timezone.utc)  # 07:30 MDT
tl = route_timeline(pts, start, pace_factor=0.7)
print(f"Route {tl[-1][4]:.2f} km, start 13:30Z, camp ETA {tl[-1][0]:%H:%M}Z "
      f"({(tl[-1][0]+timedelta(hours=UTC_OFF)):%H:%M} MDT)")

c_lat = sum(p[1] for p in tl) / len(tl); c_lon = sum(p[2] for p in tl) / len(tl)
curve = fetch_convective_forecast(c_lat, c_lon, date_utc=start)
print("PRE-TRIP BRIEFING at 07:30 MDT, P(lightning near route) by local hour:")
b = route_briefing(curve, UTC_OFF, start)
print("  hour " + " ".join(f"{r['local_hour']:>4}" for r in b))
print("  P    " + " ".join(f"{r['p_lightning']:4.2f}" for r in b))
print()

flashes = []  # (t, lat, lon)
for line in open("flashes_day.csv"):
    ts, la, lo = line.strip().split(",")
    flashes.append((datetime.fromisoformat(ts), float(la), float(lo)))
flashes.sort()

# --- Truth: distance from each flash to the hiker's position at flash time
first15 = first40 = None
for t, la, lo in flashes:
    h = hiker_pos(tl, t)
    d = math.hypot(*enu_km(h[0], h[1], la, lo))
    if d <= GLM_WATCH_KM and first40 is None:
        first40 = (t, d)
    if d <= GLM_WARN_KM and first15 is None:
        first15 = (t, d)
        break

# --- Roll forward
print(f"{'UTC':>5} {'MDT':>5} {'pos km':>6} {'ele':>5} | {'cells':>5} "
      f"{'maxP':>5} {'ETA':>5} | {'fl->hiker':>9} {'fl->route':>9} "
      f"{'outlook':>7} | tier")
rows = []
T = start
while T <= datetime(2024, 8, 5, 22, 30, tzinfo=timezone.utc):
    h = hiker_pos(tl, T)
    feats = fetch_archive_cells(h[0], h[1], 250.0, T)
    threats = []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"]
        if math.hypot(*enu_km(h[0], h[1], lat, lon)) > 250.0:
            continue
        r = analyze_cell_route(f, tl, T, HORIZON_MIN)
        if r:
            threats.append(r)
    tier = tier_for(threats)

    win = [(t, la, lo) for t, la, lo in flashes
           if T - timedelta(minutes=10) < t <= T]
    win5 = [(t, 0, 0, la, lo) for t, la, lo in win]  # corridor_metrics format
    d_now, d_corr = corridor_metrics(tl, T, win5)
    p_out = outlook_probability(T, UTC_OFF, d_corr, curve)

    if d_now is not None and d_now <= GLM_WARN_KM:
        tier = "WARNING"
    elif d_now is not None and d_now <= GLM_WATCH_KM:
        tier = max(tier, "WATCH", key=lambda x: TIER_ORDER[x])
    if d_corr is not None and d_corr <= GLM_WATCH_KM:
        tier = max(tier, "WATCH", key=lambda x: TIER_ORDER[x])

    maxP = max((t_["probability"] for t_ in threats), default=0.0)
    eta = min((t_["eta_min"] for t_ in threats
               if t_["probability"] == maxP), default=None) if threats else None
    dist_km = 0.0
    for (t_, a, o, e, d) in tl:
        if t_ <= T:
            dist_km = d
    rows.append((T, tier, p_out))
    print(f"{T:%H:%M} {(T+timedelta(hours=UTC_OFF)):%H:%M} {dist_km:6.2f} "
          f"{h[2]:5.0f} | {len(feats):>5} {maxP:5.2f} "
          f"{('%4.0fm' % eta) if eta is not None else '   -'} | "
          f"{('%7.1f' % d_now) if d_now is not None else '      -'}   "
          f"{('%7.1f' % d_corr) if d_corr is not None else '      -'}   "
          f"{p_out:7.2f} | {tier}")
    T += timedelta(minutes=30)

# --- Score
print()
if first40:
    print(f"ACTUAL: first flash <=40 km of hiker at {first40[0]:%H:%M}Z "
          f"({(first40[0]+timedelta(hours=UTC_OFF)):%H:%M} MDT), {first40[1]:.1f} km")
if first15:
    print(f"ACTUAL: first flash <=15 km of hiker at {first15[0]:%H:%M}Z "
          f"({(first15[0]+timedelta(hours=UTC_OFF)):%H:%M} MDT), {first15[1]:.1f} km")
    ev = first15[0]
    for name, cond in [("outlook >= 0.60", lambda r: r[2] >= 0.60),
                       ("tier >= WATCH", lambda r: TIER_ORDER[r[1]] >= 2),
                       ("tier == WARNING", lambda r: r[1] == "WARNING")]:
        t_first = next((r[0] for r in rows if cond(r) and r[0] <= ev), None)
        if t_first:
            lead = (ev - t_first).total_seconds() / 60.0
            print(f"  {name:<16} first at {t_first:%H:%M}Z -> lead {lead:.0f} min"
                  f"  {'(>= 3 h goal MET)' if lead >= 180 else '(< 3 h)'}")
        else:
            print(f"  {name:<16} never fired before the event")
else:
    print("ACTUAL: no flash within 15 km of the hiker all day")
