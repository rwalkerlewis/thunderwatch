#!/usr/bin/env python3
"""Direct HRRR access: fetch CAPE, CIN, and LTNG (hourly-max lightning
threat) for a point from the latest available HRRR cycle on the NOAA open
S3 bucket, using .idx byte-range requests so only the needed fields are
downloaded (~1-2 MB per field instead of ~150 MB per file).

Why: HRRR is a 3 km convection-allowing model refreshed hourly; inside
~18 h lead it is the sharpest guidance for convective timing, and reading
it directly gives us the model's explicit lightning-threat field rather
than a blended CAPE proxy. Requires pygrib.

Exposes fetch_hrrr_curve(lat, lon, hours_ahead) -> curve in the same
{valid_dt_utc: {"p","cape","cin","pop","src"}} shape thunder_watch uses.
"""
import math
import os
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone

HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
USER_AGENT = "thunder-watch/0.2 (HRRR direct)"

# field selectors as they appear in the wrfsfc .idx files
FIELDS = {
    "cape": ":CAPE:surface:",
    "cin": ":CIN:surface:",
    "ltng": ":LTNG:entire atmosphere",
}


def _get(url, rng=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if rng:
        req.add_header("Range", f"bytes={rng[0]}-{rng[1]}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _idx_ranges(idx_text):
    """Parse a GRIB .idx: return list of (start_byte, varkey_line)."""
    lines = [l for l in idx_text.decode().splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        parts = line.split(":")
        start = int(parts[1])
        end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
        out.append((start, end, line))
    return out


def latest_cycle(now=None, max_back=6):
    """Most recent cycle whose f01 idx exists (files land ~50-90 min after
    the cycle hour)."""
    now = now or datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    for _ in range(max_back):
        url = (f"{HRRR_BUCKET}/hrrr.{t:%Y%m%d}/conus/"
               f"hrrr.t{t:%H}z.wrfsfcf01.grib2.idx")
        try:
            _get(url, rng=(0, 128))
            return t
        except Exception:
            t -= timedelta(hours=1)
    return None


def ltng_to_p(ltng):
    """Hourly-max lightning threat (flashes/km^2/5min) -> P(lightning near
    the point that hour). Uncalibrated placeholder ramp: any nonzero LTNG at
    the route gridpoint means the model is making lightning there
    explicitly, which deserves more weight than environmental CAPE."""
    if ltng <= 0.001:
        return 0.0
    if ltng < 0.05:
        return 0.25 + (ltng / 0.05) * 0.15
    if ltng < 0.2:
        return 0.40 + (ltng - 0.05) / 0.15 * 0.25
    return min(0.85, 0.65 + (ltng - 0.2) * 0.5)


def fetch_hrrr_curve(lat, lon, hours_ahead=18, cycle=None,
                     hourly_lightning_prob=None):
    """Return {valid_dt: {"p","cape","cin","pop","src"}} from HRRR direct.
    P = max(environmental ramp on CAPE/CIN, explicit-LTNG ramp)."""
    import pygrib
    if cycle is not None:
        cycles = [cycle]
    else:
        c0 = latest_cycle()
        if c0 is None:
            raise RuntimeError("no recent HRRR cycle found")
        # newest cycle uploads forecast hours progressively; backfill any
        # not-yet-posted hours from the two prior (complete) cycles, and
        # from the most recent long (48 h) cycle for hours beyond 18 h lead
        cycles = [c0, c0 - timedelta(hours=1), c0 - timedelta(hours=2)]
        t = c0
        for _ in range(7):
            if t.hour % 6 == 0:
                if t not in cycles:
                    cycles.append(t)
                break
            t -= timedelta(hours=1)
    curve = {}
    nearest = None  # cached (iy, ix) once grid located
    horizon = (cycles[0] + timedelta(hours=hours_ahead))
    for cyc in cycles:
      max_fh = 48 if cyc.hour in (0, 6, 12, 18) else 18
      for fh in range(1, max_fh + 1):
        valid_t = cyc + timedelta(hours=fh)
        if valid_t > horizon or valid_t in curve:
            continue
        base = (f"{HRRR_BUCKET}/hrrr.{cyc:%Y%m%d}/conus/"
                f"hrrr.t{cyc:%H}z.wrfsfcf{fh:02d}.grib2")
        try:
            ranges = _idx_ranges(_get(base + ".idx"))
        except Exception:
            continue
        blob = b""
        wanted = []
        for start, end, line in ranges:
            for key, sel in FIELDS.items():
                if sel in line:
                    wanted.append((key, start, end))
        vals = {}
        for key, start, end in wanted:
            try:
                data = _get(base, rng=(start, end if end else start + 3_000_000))
            except Exception:
                continue
            with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
                f.write(data)
                path = f.name
            try:
                g = pygrib.open(path)
                grb = g.message(1)
                if nearest is None:
                    lats, lons = grb.latlons()
                    lons = ((lons + 180) % 360) - 180
                    d2 = (lats - lat) ** 2 + ((lons - lon)
                          * math.cos(math.radians(lat))) ** 2
                    import numpy as np
                    nearest = np.unravel_index(np.argmin(d2), d2.shape)
                vals[key] = float(grb.values[nearest])
                g.close()
            except Exception:
                pass
            finally:
                os.unlink(path)
        if not vals:
            continue
        cape = vals.get("cape", 0.0)
        cin = vals.get("cin", 0.0)
        # HRRR CIN is negative J/kg for a cap; our ramp expects that sign
        ltng = vals.get("ltng", 0.0)
        if hourly_lightning_prob is not None:
            p_env = hourly_lightning_prob(cape, cin, 0.0)
        else:
            p_env = 0.0
        p = max(p_env, ltng_to_p(ltng))
        curve[valid_t] = {"p": round(p, 2), "cape": cape, "cin": cin,
                        "pop": None, "ltng": round(ltng, 3),
                        "src": f"hrrr.{cyc:%Y%m%d}t{cyc:%H}z"}
    if not curve:
        raise RuntimeError("HRRR fetch produced no data")
    return curve
