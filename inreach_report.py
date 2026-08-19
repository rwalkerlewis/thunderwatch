#!/usr/bin/env python3
"""Compact trip lightning report formatted for Garmin inReach / Messenger.

One datum per line so arbitrary SMS segment splits stay parseable; 24-hour
colon-free times; zero-probability hour runs compressed. Aim: one app-path
message (~300 chars), two SMS segments worst case.

Usage (from repo root):
    python3 inreach_report.py ROUTE.gpx 2026-08-18T05:30 [--pace-factor F]
        [--utc-offset -6] [--label D2] [--out file]
See GLOSSARY.txt for field definitions and abbreviations.
"""
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thunder_watch as tw
from trip_report import sunset_utc

MI = 0.621371
FT = 3.28084


def clk(dt, ref=None):
    s = f"{dt:%H%M}"
    if ref and dt.date() > ref:
        s += "+1"
    return s


def compress_p(rows):
    """'LTG P=0 all hrs' or 'LTG P=0 exc 16h=.11' or ranged runs."""
    nz = [(r["local_hour"], r["p_lightning"]) for r in rows
          if r["p_lightning"] >= 0.005]
    if not nz:
        return "LTG P=0 all hrs"
    runs = []
    h0, p0 = nz[0]
    h1 = h0
    for h, p in nz[1:]:
        if h == h1 + 1 and abs(p - p0) < 0.05:
            h1 = h
        else:
            runs.append((h0, h1, p0))
            h0, h1, p0 = h, h, p
    runs.append((h0, h1, p0))
    def fp(p):
        return f"{p:.2f}".lstrip("0")
    parts = [f"{a}h={fp(p)}" if a == b else f"{a}-{b}h={fp(p)}"
             for a, b, p in runs]
    if len(nz) <= len(rows) // 2:
        return "LTG P=0 exc " + " ".join(parts)
    return "LTG " + " ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx")
    ap.add_argument("start_local")
    ap.add_argument("--pace-factor", type=float, default=0.7)
    ap.add_argument("--utc-offset", type=float, default=-6.0)
    ap.add_argument("--label", default="RT")
    ap.add_argument("--treeline-m", type=float, default=3500.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--style", choices=["plain", "compact", "oneline"],
                    default="plain",
                    help="plain: readable sentences split into <=160-char "
                         "tagged messages (for reading in the phone app); "
                         "compact: single ultra-terse message")
    ap.add_argument("--waypoints", default=None,
                    help="extra timeline points, km-based: "
                         "'4.3=connector,10.4=Fravert meadows'")
    ap.add_argument("--names", default=None,
                    help="rename auto landmarks, e.g. "
                         "'Ps1=West Maroon,Ps2=Frigid Air'")
    ap.add_argument("--max-chars", type=int, default=160,
                    help="hard per-message budget (inReach Mini SBD = 160; "
                         "Messenger Plus = 1600)")
    a = ap.parse_args()

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
    gain = sum(max(0.0, b[3] - x[3]) for x, b in zip(tl, tl[1:]))
    local = lambda dt: dt + timedelta(hours=a.utc_offset)
    ref = local(start).date()

    try:
        curve = tw.fetch_convective_forecast(clat, clon)
    except Exception:
        try:
            curve = tw.fetch_nws_thunder(clat, clon)
        except Exception:
            curve = {}
    prof = tw.hazard_profile(tl, curve, a.utc_offset,
                             treeline_m=a.treeline_m) if curve else []
    rows = tw.route_briefing(curve, a.utc_offset, tl[0][0]) if curve else []

    L = [f"TW {local(start):%a%b%-d} (iss {local(now):%a %H%M})",
         f"{a.label}: {tl[-1][4]*MI:.1f}mi +{gain*FT:.0f}ft",
         compress_p(rows) if rows else "LTG fcst unavail"]

    # landmarks: start, crest groups, peak hazard, sunset, end
    crest = tw._crest_flags(tl)
    top = max(p[3] for p in tl)
    marks = [(tl[0][0], tl[0][3], "start")]
    group = []
    n_pass = 0
    for i, (t, la, lo, e, d) in enumerate(tl):
        if crest[i] and e >= top - 250 and e >= a.treeline_m - 200:
            group.append((t, e))
        elif group:
            n_pass += 1
            g = max(group, key=lambda x: x[1])
            marks.append((g[0], g[1], f"Ps{n_pass}"))
            group = []
    if group:
        n_pass += 1
        g = max(group, key=lambda x: x[1])
        marks.append((g[0], g[1], f"Ps{n_pass}"))
    if prof:
        pk = max(prof, key=lambda r: r["hazard"])
        marks.append((pk["t"], None, f"pk haz {pk['hazard']:.2f} {pk['band']}"))
    ss = sunset_utc(clat, clon, local(start))
    if ss:
        ssu = ss.replace(tzinfo=timezone.utc)
        km = max((d for (t, _, _, _, d) in tl if t <= ssu), default=None)
        marks.append((ssu, None,
                      f"sunset mi{km*MI:.1f}" if km and ssu < tl[-1][0]
                      else "sunset"))
    marks.append((tl[-1][0], tl[-1][3], f"end mi{tl[-1][4]*MI:.1f}"))
    marks.sort(key=lambda m: m[0])
    for t, e, label in marks:
        trip = curve.get(t.replace(minute=0, second=0, microsecond=0)) \
            if curve else None
        ptag = ""
        if trip is not None and label.startswith("Ps"):
            ptag = f" P{trip['p']:.2f}".replace("P0.00", "P0")
        etag = f" {e*FT:.0f}" if e is not None else ""
        L.append(f"{clk(local(t), ref)} {label}{etag}{ptag}")

    if a.names:
        for pair in a.names.split(","):
            k, _, v = pair.partition("=")
            L = [ln.replace(k.strip(), v.strip()) for ln in L]
            marks = [(t, e, lb.replace(k.strip(), v.strip()))
                     for t, e, lb in marks]

    if a.style == "oneline":
        # single-message brief: wx shape, plan, hard boundary
        day = f"{local(start):%a}".upper()
        nzp = [(r["local_hour"], r["p_lightning"]) for r in rows
               if r["p_lightning"] >= 0.05] if rows else []
        if not nzp:
            wx = "no ltg fcst all day"
        else:
            first_h = nzp[0][0]
            pk_h, pk_p = max(nzp, key=lambda x: x[1])
            pk_hours = [h for h, p in nzp if p >= pk_p - 0.05]
            wx = (f"clr til {first_h:02d}00, pk {pk_p:.2f} "
                  f"{pk_hours[0]:02d}-{pk_hours[-1]+1:02d}h")
        skip = ("start",)
        p_first = next((m for m in marks if not (m[2] in skip
                        or m[2].startswith(("pk haz", "sunset", "end")))), None)
        plan = f"Go {clk(local(start))}"
        if p_first:
            plan += f", pass {clk(local(p_first[0]), ref)}"
        plan += f", camp {clk(local(tl[-1][0]), ref)}"
        bound = ""
        hi = next((h for h, p in ((r["local_hour"], r["p_lightning"])
                   for r in rows) if p >= 0.25), None)
        if hi is not None and p_first:
            bound = f" Off pass by {hi:02d}00."
        msg = f"{day}: {wx}. {plan}.{bound}"
        if len(msg) > a.max_chars:
            msg = msg.replace(f", camp {clk(local(tl[-1][0]), ref)}", "")
        print(msg)
        print(f"[{len(msg)} chars]", file=sys.stderr)
        if a.out:
            open(a.out, "w").write(msg + "\n")
        return

    if a.style == "plain":
        # sentence list, then greedy packing into <=max-chars messages
        sents = []
        # lead sentence uses hiker-following hazard by hour (max in hour)
        hmax = {}
        for r in prof:
            lh = (r["t"] + timedelta(hours=a.utc_offset)).hour
            hmax[lh] = max(hmax.get(lh, 0.0), r["hazard"])
        hz_rows = [{"local_hour": r["local_hour"],
                    "p_lightning": round(hmax.get(r["local_hour"], 0.0), 2)}
                   for r in rows] if rows else []
        nz = [(r["local_hour"], r["p_lightning"]) for r in hz_rows
              if r["p_lightning"] >= 0.005]
        if not rows:
            sents.append("Lightning forecast unavailable.")
        elif not nz:
            sents.append("Hazard to you zero all hours.")
        else:
            runs = []
            h0, p0 = nz[0]; h1 = h0
            for h, p in nz[1:]:
                if h == h1 + 1 and abs(p - p0) < 0.05:
                    h1 = h
                else:
                    runs.append((h0, h1, p0)); h0, h1, p0 = h, h, p
            runs.append((h0, h1, p0))
            parts = [f"{x}00-{y}59 at {p:.2f}" if x != y
                     else f"{x}00 hour at {p:.2f}" for x, y, p in runs]
            if len(nz) <= len(rows) // 2:
                sents.append("Hazard to you zero except "
                             + ", ".join(parts) + ".")
            else:
                sents.append("Hazard to you: " + ", ".join(parts) + ".")
        # corridor P summary sentence (same compression as the hazard lead)
        pnz = [(r["local_hour"], r["p_lightning"]) for r in rows
               if r["p_lightning"] >= 0.005] if rows else []
        if rows:
            if not pnz:
                sents.append("Corridor P zero all hours.")
            else:
                pruns = []
                h0, p0 = pnz[0]; h1 = h0
                for h, p in pnz[1:]:
                    if h == h1 + 1 and abs(p - p0) < 0.05:
                        h1 = h
                    else:
                        pruns.append((h0, h1, p0)); h0, h1, p0 = h, h, p
                pruns.append((h0, h1, p0))
                pparts = [f"{x}00-{y}59 at {p:.2f}" if x != y
                          else f"{x}00 hour at {p:.2f}" for x, y, p in pruns]
                if len(pnz) <= len(rows) // 2:
                    sents.append("Corridor P zero except "
                                 + ", ".join(pparts) + ".")
                else:
                    sents.append("Corridor P: " + ", ".join(pparts) + ".")
        if prof:
            pk = max(prof, key=lambda r: r["hazard"])
            km_pk = max((d for (t, _, _, _, d) in tl if t <= pk["t"]),
                        default=None)
            where = f", mi{km_pk*MI:.1f}" if km_pk is not None else ""
            sents.append(f"Peak hazard {pk['hazard']:.2f} {pk['band']} "
                         f"at {local(pk['t']):%H%M}{where}.")
        pass_marks = [m for m in marks if m[2].startswith("Ps")
                      or (a.names and any(v.partition('=')[2].strip() in m[2]
                          for v in a.names.split(',')))]
        if pass_marks and curve:
            ps = [curve.get(t.replace(minute=0, second=0, microsecond=0))
                  for t, _, _ in pass_marks]
            if all(p is not None and p["p"] < 0.005 for p in ps):
                sents.append("All passes cross at zero hazard.")

        # timeline sentences: merge auto marks with custom waypoints
        tmarks = [m for m in marks if not m[2].startswith("pk haz")]
        if a.waypoints:
            for pair in a.waypoints.split(","):
                kmtxt, _, label = pair.partition("=")
                km = float(kmtxt)
                for (t, la, lo, e, d) in tl:
                    if d >= km:
                        tmarks.append((t, e, label.strip()))
                        break
        tmarks.sort(key=lambda m: m[0])
        for t, e, label in tmarks:
            if label == "start":
                sents.append(f"{clk(local(t), ref)} leave camp.")
            elif label.startswith("sunset"):
                sents.append(f"{clk(local(t), ref)} {label}.")
            elif label.startswith("end"):
                etxt = f" {e*FT:.0f}ft" if e is not None else ""
                sents.append(f"{clk(local(t), ref)} camp "
                             f"{label[4:]}{etxt}.")
            else:
                # hiker-following hazard: corridor P for the hour times
                # terrain exposure at THIS point (passes count as crests)
                trip = curve.get(t.replace(minute=0, second=0,
                                           microsecond=0)) if curve else None
                htxt = ""
                if trip is not None and e is not None:
                    is_pass = (t, e, label) in pass_marks or                         label.startswith("Ps")
                    hval = trip["p"] * tw.exposure_factor(
                        e, is_pass, a.treeline_m)
                    htxt = " H0" if hval < 0.005 else f" H{hval:.2f}"
                    if trip["p"] >= 0.005 and abs(trip["p"] - hval) >= 0.005:
                        htxt += f" P{trip['p']:.2f}"
                etxt = f" {e*FT:.0f}ft" if e is not None else ""
                sents.append(f"{clk(local(t), ref)} {label}{etxt}{htxt}.")
        ss_mark = next((m for m in marks if m[2].startswith("sunset")
                        and "mi" in m[2]), None)
        if ss_mark:
            km_dark = tl[-1][4] - float(ss_mark[2].split("mi")[1]) / MI
            if km_dark > 0.2:
                sents.append(f"Last {km_dark*MI:.1f}mi after dark.")

        sents[0] = f"{local(start):%a %b%-d}: " + sents[0]
        msgs, cur = [], ""
        for sn in sents:
            cand = (cur + " " + sn).strip()
            if len(cand) <= a.max_chars:
                cur = cand
            else:
                if cur:
                    msgs.append(cur)
                cur = sn
        if cur:
            msgs.append(cur)
        for m in msgs:
            flag = "" if len(m) <= a.max_chars else " OVER BUDGET"
            print(m + "\n")
            print(f"[{len(m)} chars{flag}]", file=sys.stderr)
        if a.out:
            with open(a.out, "w") as f:
                f.write("\n\n".join(msgs) + "\n")
        return

    report = "\n".join(L)
    # progressive trimming to fit the per-message budget (Mini = 160):
    # 1. drop elevations on non-pass lines  2. drop climb from route line
    # 3. drop issue stamp  4. drop pass elevations
    import re as _re
    trims = [
        lambda t: "\n".join(
            _re.sub(r"^(\d{4}(?:\+1)? (?:start|end mi[\d.]+)) \d{3,5}$",
                    r"\1", ln) for ln in t.split("\n")),
        lambda t: _re.sub(r" \+\d+ft", "", t),
        lambda t: _re.sub(r" \(iss [^)]*\)", "", t),
        lambda t: "\n".join(
            _re.sub(r"^(\d{4}(?:\+1)? Ps\d) \d{3,5}( P[\d.]+)$",
                    r"\1\2", ln) for ln in t.split("\n")),
    ]
    for trim in trims:
        if len(report) <= a.max_chars:
            break
        report = trim(report)
    print(report)
    over = "" if len(report) <= a.max_chars else         f" OVER BUDGET ({a.max_chars})"
    print(f"\n[{len(report)} chars{over}]", file=sys.stderr)
    if a.out:
        with open(a.out, "w") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
