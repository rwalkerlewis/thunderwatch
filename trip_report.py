#!/usr/bin/env python3
"""Generate a plain-text, copy-paste trip lightning report for a GPX route
and start time: dated header, hourly P table, landmark timeline with pass
crossings and sunset, hazard summary, current conditions.

Usage (from repo root):
    python3 trip_report.py ROUTE.gpx 2026-08-18T05:30 [--pace-factor 0.29]
        [--utc-offset -6] [--units mi|km] [--label "Day 2, full leg"]
        [--out report.txt]
"""
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_watch as tw

MI = 0.621371
FT = 3.28084


def sunset_utc(lat, lon, date):
    """NOAA-style approximation, good to a few minutes."""
    n = date.timetuple().tm_yday
    g = math.radians(0.98565 * (n - 2))
    decl = -math.asin(0.39779 * math.cos(math.radians(0.98565 * (n + 10))
                                        + 0.03342 * math.sin(g)))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl))
              - math.tan(la) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    b = 2 * math.pi * (n - 81) / 364.0
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    minutes = 720 - 4 * lon - eot + 4 * ha
    return date.replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(minutes=minutes)


def fmt_clock(dt_local, ref_date=None):
    s = f"{dt_local:%H%M}"
    if ref_date and dt_local.date() > ref_date:
        s += "+1"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx")
    ap.add_argument("start_local", help="YYYY-MM-DDTHH:MM local, or HH:MM today")
    ap.add_argument("--pace-factor", type=float, default=0.7)
    ap.add_argument("--utc-offset", type=float, default=-6.0)
    ap.add_argument("--units", choices=["mi", "km"], default="mi")
    ap.add_argument("--label", default=None)
    ap.add_argument("--names", default=None,
                    help="rename auto landmarks: 'Ps1=West Maroon,...' "
                         "(passes are numbered in route order)")
    ap.add_argument("--waypoints", default=None,
                    help="extra timeline points, km-based: "
                         "'4.3=connector,10.4=Fravert meadows'")
    ap.add_argument("--treeline-m", type=float, default=3500.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    def d_u(km):
        return (km * MI, "mi") if a.units == "mi" else (km, "km")

    def e_u(m):
        return (m * FT, "ft") if a.units == "mi" else (m, "m")

    now = datetime.now(timezone.utc)
    if "T" in a.start_local:
        start = datetime.strptime(a.start_local, "%Y-%m-%dT%H:%M") \
            .replace(tzinfo=timezone.utc) - timedelta(hours=a.utc_offset)
    else:
        hh, mm = map(int, a.start_local.split(":"))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0) \
            - timedelta(hours=a.utc_offset)

    pts = tw.load_gpx(a.gpx)
    tl = tw.route_timeline(pts, start, a.pace_factor)
    clat = sum(p[1] for p in tl) / len(tl)
    clon = sum(p[2] for p in tl) / len(tl)
    length_km = tl[-1][4]
    gain = sum(max(0.0, b[3] - x[3]) for x, b in zip(tl, tl[1:]))

    try:
        _ha = max(18, int((tl[-1][0] + __import__("datetime").timedelta(hours=9)
                           - now).total_seconds() // 3600) + 1)
        curve = tw.fetch_convective_forecast(clat, clon, hours_ahead=_ha)
    except Exception:
        try:
            curve = tw.fetch_nws_thunder(clat, clon)
        except Exception:
            curve = {}
    prof = tw.hazard_profile(tl, curve, a.utc_offset,
                             treeline_m=a.treeline_m) if curve else []

    # current conditions
    n_cells = None
    try:
        feats = tw.fetch_json(tw.ATTR_URL).get("features", [])
        n_cells = sum(1 for f in feats if math.hypot(
            *tw.enu_km(clat, clon, f["geometry"]["coordinates"][1],
                       f["geometry"]["coordinates"][0])) <= 250.0)
    except Exception:
        pass
    glm = {}
    try:
        end = now - timedelta(seconds=tw.GLM_LATENCY_S)
        glm = tw.fetch_glm_lightning(clat, clon, end - timedelta(minutes=10), end)
    except Exception:
        pass

    L = []
    local = lambda dt: dt + timedelta(hours=a.utc_offset)
    hike_date = local(start)
    L.append(f"THUNDERWATCH FORECAST - {hike_date:%A, %B %-d, %Y} "
             f"(issued {local(now):%a %b %-d, %H%M})")
    L.append("")
    dv, du = d_u(length_km)
    gv, gu = e_u(gain)
    e0, _ = e_u(tl[0][3])
    emax, _ = e_u(max(p[3] for p in tl))
    e1, eu = e_u(tl[-1][3])
    lab = a.label or os.path.basename(a.gpx)
    L.append(f"ROUTE: {lab} - {dv:.1f} {du}, +{gv:,.0f} {gu}")
    L.append(f"Start {e0:,.0f} {eu} -> high point {emax:,.0f} {eu} "
             f"-> end {e1:,.0f} {eu}")
    L.append("")
    if curve:
        rows = tw.route_briefing(curve, a.utc_offset, tl[0][0])
        L.append("LIGHTNING FORECAST, P by hour "
                 f"({'MDT' if a.utc_offset == -6 else 'local'}):")
        L.append("  hour " + " ".join(f"{r['local_hour']:>4}" for r in rows))
        L.append("  P    " + " ".join(f"{r['p_lightning']:4.2f}" for r in rows))
        if prof:
            hmax = {}
            for r in prof:
                lh = (r["t"] + timedelta(hours=a.utc_offset)).hour
                hmax[lh] = max(hmax.get(lh, 0.0), r["hazard"])
            L.append("  H    " + " ".join(f"{hmax.get(r['local_hour'], 0):4.2f}"
                                          for r in rows)
                     + "   <- follows the hiker")
    else:
        L.append("LIGHTNING FORECAST: unavailable (no forecast source reachable)")
    L.append("")

    # landmark timeline: start, crest groups, day's peak hazard, sunset, end
    L.append(f"TIMELINE ({fmt_clock(local(start))} start, "
             f"pace factor {a.pace_factor}):")
    crest = tw._crest_flags(tl)
    marks = [(tl[0][0], tl[0][3], 0.0, "leave camp/trailhead")]
    top = max(p[3] for p in tl)
    group = []
    n_pass = 0
    for i, (t, la, lo, e, d) in enumerate(tl):
        if crest[i] and e >= top - 250 and e >= a.treeline_m - 200:
            group.append((t, e, d))
        elif group:
            n_pass += 1
            g = max(group, key=lambda x: x[1])
            marks.append((g[0], g[1], g[2], f"Ps{n_pass}"))
            group = []
    if group:
        n_pass += 1
        g = max(group, key=lambda x: x[1])
        marks.append((g[0], g[1], g[2], f"Ps{n_pass}"))
    if a.waypoints:
        for pair in a.waypoints.split(","):
            kmtxt, _, wlabel = pair.partition("=")
            km = float(kmtxt)
            for (t, la, lo, e, d) in tl:
                if d >= km:
                    marks.append((t, e, d, wlabel.strip()))
                    break
    names = {}
    if a.names:
        for pair in a.names.split(","):
            k, _, v = pair.partition("=")
            names[k.strip()] = v.strip()
    if prof:
        pk = max(prof, key=lambda r: r["hazard"])
        if pk["hazard"] > 0:
            i = min(range(len(tl)),
                    key=lambda k: abs((tl[k][0] - pk["t"]).total_seconds()))
            marks.append((pk["t"], tl[i][3], tl[i][4],
                          f"day's peak hazard H={pk['hazard']:.2f}"))
    ss = sunset_utc(clat, clon, local(start))
    if ss:
        ss_utc = ss.replace(tzinfo=timezone.utc)
        if tl[0][0] <= ss_utc <= tl[-1][0]:
            p = tw.hiker_pos(tl, ss_utc)
            km = max((d for (t, _, _, _, d) in tl if t <= ss_utc), default=0)
            marks.append((ss_utc, p[2], km, "sunset"))
        else:
            marks.append((ss_utc, None, None, "sunset"))
    marks.append((tl[-1][0], tl[-1][3], tl[-1][4], "end of route"))
    marks.sort(key=lambda m: m[0])
    pass_labels = {m[3] for m in marks if m[3].startswith("Ps")}
    for t, e, d, label in marks:
        is_pass = label in pass_labels
        label = names.get(label, "pass/crest" if is_pass else label)
        p_here = ""
        trip = curve.get(t.replace(minute=0, second=0, microsecond=0)) \
            if curve else None
        if trip is not None and e is not None and label not in ("sunset",):
            hval = trip["p"] * tw.exposure_factor(e, is_pass, a.treeline_m)
            p_here = f"   H={hval:.2f}"
            if is_pass:
                p_here += f" (P={trip['p']:.2f})"
        if e is None:
            L.append(f"  {fmt_clock(local(t), hike_date.date()):>9}  {label}")
        else:
            ev, eu2 = e_u(e)
            dv2, du2 = d_u(d)
            L.append(f"  {fmt_clock(local(t), hike_date.date()):>9}  {label:<28} "
                     f"{ev:8,.0f} {eu2}   {du2} {dv2:.1f}{p_here}")
    L.append("")
    if prof:
        pk = max(prof, key=lambda r: r["hazard"])
        L.append(f"HAZARD (P x terrain exposure): peak {pk['hazard']:.2f} "
                 f"({pk['band']}) at {pk['t_local'] if 't_local' in pk else fmt_clock(local(pk['t']))}"
                 if isinstance(pk.get('t_local'), str) else
                 f"HAZARD (P x terrain exposure): peak {pk['hazard']:.2f} "
                 f"({pk['band']}) at {fmt_clock(local(pk['t']))}")
    cur = []
    if n_cells is not None:
        rv, ru = d_u(250)
        cur.append(f"{n_cells} storm cells within {rv:.0f} {ru}")
    if glm.get("available"):
        rv, ru = d_u(100)
        cur.append(f"{glm['flashes_within_100km']} lightning flashes "
                   f"within {rv:.0f} {ru} (last 10 min)")
    if cur:
        L.append(f"CURRENT ({fmt_clock(local(now))}): " + ", ".join(cur))

    report = "\n".join(L)
    print(report)
    if a.out:
        with open(a.out, "w") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
