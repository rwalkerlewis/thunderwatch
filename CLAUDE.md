# ThunderWatch

Thunderstorm threat nowcasting for a coordinate, built for backcountry hiker
warnings. Given lat/lon, determine whether any tracked thunderstorm cell or
lightning activity will affect that point, when, and how intense, and emit a
NONE / ADVISORY / WATCH / WARNING tier plus per-cell probabilities.

Single file: `thunder_watch.py`. Keep it that way until it genuinely needs
splitting; a flat script that runs anywhere is a feature for field use.

## Run

```
python3 thunder_watch.py LAT LON                      # realtime
python3 thunder_watch.py LAT LON --json               # machine output
python3 thunder_watch.py LAT LON --at 2024-08-05T21:00 --verify-min 60   # replay + score
python3 thunder_watch.py LAT LON --at 2024-08-05T21:00 --map out.png     # GMT map
```

Reference test target: Maroon Bells, 39.0708 -106.9890.

## Dependencies

- Core: Python 3.10+ stdlib only. Do not add hard dependencies to the core path.
- `netCDF4` (pip): required only for GLM lightning; everything else degrades
  gracefully without it (`--no-glm`).
- `gmt` >= 6 (apt: gmt gmt-dcw gmt-gshhg): required only for `--map`.

## Data sources

1. Cells (realtime): IEM aggregate of NEXRAD Level III storm attributes,
   `https://mesonet.agron.iastate.edu/geojson/nexrad_attr.geojson`.
2. Cells (replay): IEM per-radar archive,
   `cgi-bin/request/gis/nexrad_storm_attrs.py?radar=X&sts=..&ets=..&fmt=csv`.
   Radar site list from `geojson/network/NEXRAD.geojson`.
3. Lightning: GOES GLM L2 LCFA netCDF, one file per 20 s, anonymous S3:
   `noaa-goes{16,17,18,19}` buckets, prefix `GLM-L2-LCFA/YYYY/DDD/HH/`.
   Satellite selection is date-aware (see `glm_bucket_for`): East was GOES-16
   until 2025-04-07 then GOES-19; West was GOES-17 until 2023-01-04 then
   GOES-18; no West GLM before 2019-02-12; no GLM at all before 2017-12-18.
4. Alerts: NWS `api.weather.gov/alerts/active?point=` (realtime); IEM VTEC
   `json/sbw_by_point.py` (replay).
5. Map relief: GMT remote `@earth_relief_03s`, cut and cached to `dem/topo.nc`
   on first map render.

## Domain conventions (do not "fix" these)

- NEXRAD storm motion `drct` uses meteorological FROM convention: a cell with
  drct=242 is moving TOWARD 062. Heading = (drct + 180) % 360.
- Attribute-table `TOP` is in kft; converted to km on ingest.
- Elevations in output and docs are always meters.
- Lightning thresholds: 15 km = strike range (WARNING floor), 40 km = lead
  range (WATCH floor), 100 km = situational awareness.
- Lightning tiers can only RAISE the cell-track tier, never lower it.
- Probability model: Gaussian cross-track error sigma(t) = 2 km + 0.20 km/min,
  times a cell-lifecycle survival factor (half-life 45 min ordinary, 120 min
  severe). These constants are hand-tuned placeholders awaiting calibration
  via replay verification; changing them requires re-running the calibration
  set, not eyeballing one case.

## Known gotchas (each cost debugging time)

- netCDF4/HDF5 is NOT thread-safe: download GLM files concurrently, parse
  serially in the main thread. Parsing in ThreadPoolExecutor workers segfaults.
- GMT 6.5 + ghostscript 10: stroked point symbols (`-S+`, `-Sx`) with many
  points trigger a `/limitcheck` PostScript error. Use filled symbols
  (`-Sd`, `-Sc`) for dense layers like flashes.
- The cached DEM (`dem/topo.nc`) is region-specific but keyed only by path;
  changing the target coordinate requires deleting `dem/` or passing `--dem`.
- Everything in the output dict must be JSON-serializable; GLM flash tuples
  contain datetimes and must stay filtered out of `out["lightning_glm"]`.
- S3 listing at hour boundaries: GLM key windows must span all hour prefixes
  covering [start, end].
- IEM archive CSV header typo `MAZ_DBZ_H` is upstream; do not depend on it.

## Verified behavior (regression anchors)

Replay cases that must keep producing these outcomes:

- `--at 2024-08-05T19:00 --verify-min 90` at Maroon Bells: WATCH, HIT
  (first flash <=15 km at +11 min).
- `--at 2024-08-05T21:15 --verify-min 60` at Maroon Bells: WARNING, HIT.
- `--at 2024-08-01T20:30 --verify-min 90` at Maroon Bells: NONE, CORRECT_NULL.

Site truth worth remembering: the Elk Mountains sit in a radar gap (KGJX beam
overshoots at ~100 km; KPUX/KFTG blocked). On 2024-08-05 the radar tracker
produced maxP=0.28 all day while GLM drove every tier change. GLM is the
primary sensor at mountain sites; the cell tracker earns its keep on plains.

## Roadmap (current; done items retained for context)

Done: environment forecast layer (Open-Meteo CAPE/CIN/PoP briefing + rolling
outlook; originally "HRRR flag"), moving-target route support (GPX, Tobler
timeline, corridor lightning), replay + verification, GMT maps (dual scope),
packaged example with regression numbers.

1. Calibration harness (top priority; everything below it is tuning-blind
   until this exists): batch replay over many days/sites, tally
   HIT/MISS/FALSE_ALARM/CORRECT_NULL, fit sigma/half-life constants, the
   hourly_lightning_prob ramp/CIN/PoP constants, and tier + outlook
   thresholds; reliability diagram for all emitted probabilities.
2. Test suite: pytest wrapping the regression anchors with recorded HTTP
   fixtures so tests do not hammer IEM/AWS/Open-Meteo; shares fixture
   machinery with the calibration harness.
3. Lightning-cluster tracking: cluster GLM flashes spatially, give clusters
   motion vectors and ETAs (radar-independent approach warning; the missing
   0-60 min layer at radar-gap sites).
4. Multi-point environment sampling along long routes (centroid sampling is
   only valid for short routes like the 8 km example).
5. GLM parallax correction (few km at 37-39 N; grows poleward).
6. MRMS reflectivity polygons for true cell footprints instead of the
   effective-radius heuristic.
7. Daemon mode: poll every N minutes, emit on tier change; NWS SBW polygons
   as automatic WARNING override.

## Style

- Plain natural English in comments and docs; no startup slang.
- Lead with what a function does and why the nonobvious parts are the way
  they are, especially anything in the gotcha list.

## Route mode (added)

`--gpx FILE --start-local 07:30 --utc-offset -6 --pace-factor 0.7` builds a
hiker timeline (Tobler's hiking function x pace_factor, x1.125 for breaks),
substitutes the hiker's time-dependent position as the target, evaluates
cells against the moving trajectory (`analyze_cell_route`), and reports
corridor lightning (nearest flash to the route ahead) plus a long-lead
outlook probability. Experiment scripts: `cache_glm.py` (day flash cache),
`route_replay.py` (roll-forward prediction-vs-actual).

Roll-forward result, Four Pass Loop day 1 (examples/4pl-day-1.gpx),
2024-08-05, start 07:30 MDT: camp ETA 11:15 MDT; first flash <=15 km of
hiker 14:14 MDT. WATCH fired 12:30 MDT (105 min lead). First flash anywhere
near the corridor was 12:00 MDT, so the maximum achievable observation-based
lead was 134 min: a 3-hour warning on this day was physically impossible
from radar+lightning observations alone. Three-hour leads require an
environment forecast (HRRR CAPE/CIN, roadmap item 2), which upgrades the
climatology prior in `outlook_probability` from hand-set constants to a
forecast-driven probability. The radar tracker peaked at P=0.19 all day;
GLM again did the work.

## Forecast-driven outlook (added)

`fetch_convective_forecast` pulls hourly CAPE/CIN/PoP for the route centroid
from Open-Meteo (live forecast in realtime; archived model forecasts via
historical-forecast-api for replay dates). `hourly_lightning_prob` maps the
environment to P(lightning near route per hour) with a terrain-aggressive
CAPE ramp and CIN gating; `route_briefing` renders the pre-trip table and
`forecast_window_prob` compounds hours into the rolling 3-h outlook, which
observed corridor lightning can only raise.

2024-08-05 result: the 07:30 MDT briefing showed P=0.00 through 11:00,
0.39 at 13:00, 0.64 at 14:00 (actual first flash <=15 km: 14:14) — the
correct go-early signal issued 6.7 h ahead. The rolling 3-h outlook crossed
0.60 at 11:30 (165 min lead, vs 105 min observation-only); it crossed 0.50
at 11:00 (194 min). Threshold choice is a lead-vs-false-alarm tradeoff and
must come from the calibration harness over many days, not from tuning to
this one case. All constants in hourly_lightning_prob are uncalibrated
placeholders; calibrating them is roadmap item 1.

## Packaged example

`examples/four_pass_loop_replay.py` is the end-to-end regression case:
route + briefing + roll-forward + scoring for 2024-08-05. It builds its GLM
flash cache on first run (~900 files) and reuses
`examples/flashes_2024-08-05.csv` afterward. Expected outcomes: briefing
peak P=0.64 at 14:00 local (event 14:14), outlook>=0.50 lead 195 min,
outlook>=0.60 lead 165 min, WATCH lead 105 min. Material changes to the
probability model should be checked against these numbers.

## Route maps

`examples/render_route_maps.py` renders a per-step map for the roll-forward
(route in blue, hiker orange, camp triangle, cells, flashes, 15/40 km rings)
at `map_radius_km=45`. make_map accepts `route_tl=` and an optional
`args.map_radius_km`. Gotcha: GMT remote data (`@earth_relief_*`) can be
deactivated in some environments ("Remote download is currently
deactivated"); keep a local DEM and `gmt grdcut` subregions from it instead
of relying on the auto-fetch.

## Example: any-route preview

`examples/preview_hike.py ROUTE.gpx YYYY-MM-DDTHH:MM [--map out.png]` prints
route stats and runs the full pre-trip analysis (briefing, current
conditions, dual-scope maps) for any GPX and start time. Day-2 trace of the
Four Pass Loop is in examples/4pl-day-2.gpx (17.44 km, +1050 m, two passes,
max 3808 m). Live forecast fetch spans 3 days so day-after-tomorrow starts
have full briefing coverage.

## Hazard metric (exposure-weighted, hiker-following)

H(t) = P(lightning near route, hour of t) x exposure(hiker's projected
position at t). Exposure: 1.0 above treeline (--treeline-m, default 3500 m
for the Elks), ramping to 0.35 below it, +0.15 crest bonus for local
elevation maxima (passes) along the route. Bands: LOW <0.15, MODERATE,
HIGH >=0.35, EXTREME >=0.60. Route output includes hazard_now
(observed-lightning-aware), hazard_peak, and the half-hourly profile. This
is the metric that separates "storm while below treeline" from "storm while
on the pass"; the corridor P alone cannot. Exposure constants are physical
guesses, calibratable in principle against lightning-casualty literature
rather than the replay harness (GLM cannot verify exposure weighting).

## Trip report generator

`trip_report.py ROUTE.gpx YYYY-MM-DDTHH:MM [--pace-factor F] [--units mi|km]
[--label ...] [--out file]` emits the plain-text copy-paste report: dated
header, hourly P table, landmark timeline (auto-detected passes with P at
crossing time, day's peak hazard, sunset with the hiker's position, next-day
(+1) markers), hazard summary, current cells/GLM. Sunset is an NOAA-style
approximation (few minutes accuracy). Falls back NWS thunder -> nothing if
forecast sources are unreachable.

## inReach report + glossary

`inreach_report.py` emits the compact Garmin inReach/Messenger format (one
datum per line, 24-h colon-free times, compressed P line, +1 next-day
marker, char count to stderr); generate it alongside trip_report.py for
every trip report. GLOSSARY.txt is the recipient field guide for both
formats. All times in all outputs: 24-hour, no colons.

Recipient device is an inReach Mini (Iridium SBD): hard 160-char budget per
inbound message, every fragment of an oversize message costs a plan credit.
inreach_report.py enforces --max-chars 160 by progressive trimming and
warns on stderr if still over. GLOSSARY_INREACH.txt chunks are all <160.

## Daily report product

`daily_report.py` is the standard daily run: generates the full
human-readable report (trip_report.py, now with --names/--waypoints and a
hiker-following H column, passes also show corridor P) AND the inReach
message set (inreach_report.py) from the same inputs in one command.
Always produce both. --out-dir writes report_full.txt and
report_inreach.txt.

Report conventions (current): inReach reports lead with hiker-following
hazard by hour, then a corridor-P summary sentence; timeline points carry
H, plus P wherever it differs from H. Full report table has P and H rows.
Both glossaries match these semantics. Message packer emits as many
<=160-char messages as content needs (currently 4/day at full detail).

## Pace calibration

`pace_calibrate.py ROUTE.gpx DATE --fixes "HHMM=lat,lon; ..."` converts
observed inReach position fixes into per-segment pace factors (labeled
climb/descent/rolling) and a distance-weighted overall factor for the next
daily_report run. Times local, 24-h, no colon, +1 = next day. Validated
against day-1 truth (0530 start, 1430 arrival -> 0.29).

Standard daily send is now ONE message: inreach_report.py --style oneline
(wx shape, start/pass/camp times, "Off pass by HHMM" boundary at the first
P>=0.25 hour). Multi-message plain style reserved for material changes or
when the recipient asks for detail.

## Live trip state (Four Pass Loop, Aug 2026)

Party of two; the slower hiker sets the pace. Calibrated pace factor 0.50
(0.54 fresh climb, 0.48 back 9 mi; three-fix calibration on
examples/4pl-day-2-geneva-south.gpx, Aug 18). The 0.29 day-1 figure was a
camp-morning artifact, retired. Day-2 actual: 0610 start, southern (west
loop) route to Geneva Lake camp 8, arrived by 1928 fix at 39.09831
-107.07351, 12.6 mi. Gaia traces in examples/: Geneva_Lake (west loop),
to_geneva_cutover (short eastern climb), Geneva_camp___8 (shore to site 8).
Day-3 route from camp: examples/4pl-day-3-from-geneva8.gpx.

Recipient reads on the phone via Garmin Messenger paired to an original
inReach Mini (160-char SBD; each inbound fragment costs a plan credit) and
dislikes message volume. Protocol: ONE oneline report per evening;
unscheduled sends only on tier change, boundary shift >= 1 h, or her
question. The "Off pass by HHMM" boundary is the one line never to drop.
Her Garmin account stamps Pacific; route ops are MDT; tag times MDT.

Convective timing at 12-36 h lead has swung the Wed peak hour three times
in one day; the trustworthy send is the one generated closest to departure
(0500 same-day cycle).

daily_report.py now emits three sections every run: FULL, INREACH
(multi-message plain), and ONELINE (the standard daily send). --out-dir
writes report_full.txt, report_inreach.txt, report_oneline.txt.

Wed Aug 19 pre-dawn state: party at Geneva camp 8; plan sent and stable
(go 0530, Trail Rider ~0904, off-pass boundary she carries: 1030). Four
forecast cycles on Tue oscillated the Wed peak (0.44-0.50, 1200-1500)
around a stable core: morning clean, midday peak ~0.4-0.5. Plan is
insensitive to the wobble. Morning task: daily_report.py on
examples/4pl-day-3-from-geneva8.gpx at 0.50 against the 0500 cycle; send
the ONELINE. Day 4 (Thu) route: examples/4pl-day-4.gpx from Snowmass Lake
over Buckskin Pass to Maroon Lake; build a from-actual-camp splice once
Wed's camp fix arrives, and recalibrate pace with any Wed fixes first.

## HRRR direct (added Wed 0330 MDT)

hrrr_direct.py reads HRRR (3 km, hourly cycles) straight from the
noaa-hrrr-bdp-pds bucket via .idx byte-range requests: CAPE, CIN, and the
explicit LTNG hourly-max lightning-threat field at the nearest gridpoint.
P = max(CAPE/CIN environmental ramp, LTNG ramp in ltng_to_p; both
uncalibrated). fetch_convective_forecast now tries HRRR first for live
runs, falling back to the Open-Meteo blend, then NWS thunder; replay
still uses the Open-Meteo archive (HRRR AWS archive replay is a future
upgrade). Requires pygrib (optional dep, like netCDF4/gmt).
Gotcha: the newest cycle posts forecast hours progressively (~2 min apart,
complete ~1 h 45 m after cycle time); fetch_hrrr_curve therefore backfills
missing valid hours from the two prior cycles and tags each hour's src.

Forecast fallback chain (hardened): fetch_convective_forecast fills the
next-18h curve per hour: HRRR direct -> Open-Meteo blend -> NWS
probabilityOfThunder -> linear interpolation across isolated single-hour
gaps (src "interp"). Raises only if every source fails for every hour;
callers treat that as curve=None and degrade to climatology/"unavailable".
Every hour carries a src tag so any report can be audited for provenance.

## Live verification log (Wed Aug 19)

Day 3 executed and verified: 0627 start, Trail Rider by 0930 (her report),
camp fix 1115 at 39.117861 -107.030160 (bench NW of Snowmass Lake; day-4
route spliced from it in examples/4pl-day-4-from-camp.gpx). Three-fix
calibration: climb 0.46, descent 0.58+, overall 0.51 - third consecutive
day at 0.50 +/- 0.01. Afternoon: morning-assimilated cycles moved the peak
to 1200-1300; first flashes within 15 mi of camp arrived ~1315 (model
verified within ~20 min). Live tier ran NONE -> WATCH (1324, flash 20.6 km)
-> WARNING (1329, flash 14.9 km) -> WATCH (1351, receding SE as vectors
said). She was in camp 2 h before onset.

Thursday watch item: long-cycle guidance sharpened the morning-loaded
shape (onset 0900 at 0.37, peak 0.50 midday); from her actual camp a 0530
start puts Buckskin at the onset hour, 0500 buys it back to ~0830 clear.
Evening cycle decides the go time in tonight's oneline; final check 0430.

Monitoring command (the future daemon loop, roadmap 7):
python3 thunder_watch.py 0 0 --gpx examples/4pl-day-4-from-camp.gpx
  --start-local 2026-08-20T05:30 --pace-factor 0.50
