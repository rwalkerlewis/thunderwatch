# ThunderWatch

Thunderstorm threat nowcasting for a coordinate, built toward backcountry
hiker warnings. Combines NEXRAD storm-cell tracking, GOES GLM lightning, and
NWS alerts into per-cell passage probabilities and a hurricane-style
NONE / ADVISORY / WATCH / WARNING tier. Includes an archive replay mode with
outcome scoring and GMT map rendering on shaded topographic relief.

```
python3 thunder_watch.py 39.0708 -106.9890
python3 thunder_watch.py 39.0708 -106.9890 --at 2024-08-05T21:00 --verify-min 60
python3 thunder_watch.py 39.0708 -106.9890 --at 2024-08-05T21:00 --map map.png
```

Core is stdlib-only. `pip install netCDF4` for GLM lightning; install `gmt`
(>= 6) for maps. See CLAUDE.md for architecture, data sources, and roadmap.
