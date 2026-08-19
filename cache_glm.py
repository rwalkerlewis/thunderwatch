#!/usr/bin/env python3
"""Cache GLM flashes near the route for a time range into a CSV.
Usage: cache_glm.py START_ISO END_ISO OUT.csv  (UTC, e.g. 2024-08-05T13:20)
Samples every 2nd 20-s file (40-s effective resolution)."""
import sys, math, concurrent.futures
from datetime import datetime, timezone
sys.path.insert(0, ".")
from thunder_watch import (list_glm_keys, _http_get, _parse_glm_bytes,
                           glm_bucket_for, load_gpx)

start = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=timezone.utc)
end = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
out = sys.argv[3]

pts = load_gpx("4pl-day-1.gpx")
clat = sum(p[0] for p in pts) / len(pts)
clon = sum(p[1] for p in pts) / len(pts)

bucket = glm_bucket_for(clon, end)
keys = list_glm_keys(bucket, start, end)[::2]
print(f"{bucket}: {len(keys)} files {start:%H:%M}-{end:%H:%M}Z", flush=True)

with concurrent.futures.ThreadPoolExecutor(8) as pool:
    futs = {pool.submit(_http_get, f"https://{bucket}.s3.amazonaws.com/{k}"): t
            for t, k in keys}
    blobs, fails = [], 0
    for fut in concurrent.futures.as_completed(futs):
        try:
            blobs.append((futs[fut], fut.result()))
        except Exception:
            fails += 1

n = 0
with open(out, "w") as f:
    for t, data in sorted(blobs):
        try:
            for la, lo, en in _parse_glm_bytes(data):
                if abs(la - clat) <= 1.3 and abs(lo - clon) <= 1.7:
                    f.write(f"{t.isoformat()},{la:.4f},{lo:.4f}\n")
                    n += 1
        except Exception:
            fails += 1
print(f"wrote {n} flashes, {fails} failed files -> {out}", flush=True)
