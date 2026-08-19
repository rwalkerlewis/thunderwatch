#!/usr/bin/env python3
"""Generate the full human-readable report AND the inReach message set in
one run, from the same inputs. This is the standard daily product.

Usage (from repo root):
    python3 daily_report.py ROUTE.gpx 2026-08-18T05:30 --pace-factor 0.29 \
        --label "Day 2" --names "Ps1=W Maroon Pass,Ps2=Frigid Air Pass" \
        --waypoints "4.3=connector,10.4=Fravert meadows" [--out-dir DIR]
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument("gpx")
ap.add_argument("start_local")
ap.add_argument("--pace-factor", default="0.7")
ap.add_argument("--utc-offset", default="-6")
ap.add_argument("--label", default=None)
ap.add_argument("--names", default=None)
ap.add_argument("--waypoints", default=None)
ap.add_argument("--units", default="mi")
ap.add_argument("--treeline-m", default="3500")
ap.add_argument("--out-dir", default=None)
a = ap.parse_args()

common = [a.gpx, a.start_local, "--pace-factor", a.pace_factor,
          "--utc-offset", a.utc_offset, "--treeline-m", a.treeline_m]
opt = []
if a.names:
    opt += ["--names", a.names]
if a.waypoints:
    opt += ["--waypoints", a.waypoints]

full_cmd = [sys.executable, os.path.join(HERE, "trip_report.py")] + common \
    + ["--units", a.units] + opt + (["--label", a.label] if a.label else [])
ir_cmd = [sys.executable, os.path.join(HERE, "inreach_report.py")] + common + opt
ol_cmd = ir_cmd + ["--style", "oneline"]

if a.out_dir:
    os.makedirs(a.out_dir, exist_ok=True)
    full_cmd += ["--out", os.path.join(a.out_dir, "report_full.txt")]
    ir_cmd += ["--out", os.path.join(a.out_dir, "report_inreach.txt")]
    ol_cmd += ["--out", os.path.join(a.out_dir, "report_oneline.txt")]

r1 = subprocess.run(full_cmd, capture_output=True, text=True)
r2 = subprocess.run(ir_cmd, capture_output=True, text=True)
r3 = subprocess.run(ol_cmd, capture_output=True, text=True)
print(r1.stdout.rstrip())
print("\n" + "=" * 34 + " INREACH " + "=" * 34 + "\n")
print(r2.stdout.rstrip())
print("\n" + "=" * 34 + " ONELINE " + "=" * 34 + "\n")
print(r3.stdout.rstrip())
for r in (r2, r3):
    if r.stderr.strip():
        print("\n" + r.stderr.strip(), file=sys.stderr)
