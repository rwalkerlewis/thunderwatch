#!/usr/bin/env python3
"""Render per-step maps for the Four Pass Loop 2024-08-05 roll-forward."""
import math, sys, types
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from thunder_watch import (load_gpx, route_timeline, hiker_pos, enu_km,
                           analyze_cell_route, fetch_archive_cells, tier_for,
                           corridor_metrics, outlook_probability, TIER_ORDER,
                           GLM_WARN_KM, GLM_WATCH_KM, make_map,
                           fetch_convective_forecast)

UTC_OFF=-6.0
pts=load_gpx("4pl-day-1.gpx")
start=datetime(2024,8,5,13,30,tzinfo=timezone.utc)
tl=route_timeline(pts,start,pace_factor=0.7)
clat=sum(p[1] for p in tl)/len(tl); clon=sum(p[2] for p in tl)/len(tl)
curve=fetch_convective_forecast(clat,clon,date_utc=start)
flashes=[]
for line in open("flashes_day.csv"):
    ts,la,lo=line.strip().split(",")
    flashes.append((datetime.fromisoformat(ts),float(la),float(lo)))
flashes.sort()

T=start
while T<=datetime(2024,8,5,22,30,tzinfo=timezone.utc):
    h=hiker_pos(tl,T)
    feats=fetch_archive_cells(clat,clon,250.0,T)
    threats=[]; map_cells=[]
    for f in feats:
        lon,lat=f["geometry"]["coordinates"]
        r=analyze_cell_route(f,tl,T,240.0)
        if r: threats.append(r)
        p=f["properties"]
        map_cells.append({"lat":lat,"lon":lon,"dbz":p["max_dbz"],
                          "hdg":(p["drct"]+180.0)%360.0,
                          "spd_kmh":p["sknt"]*1.852,"threat":bool(r)})
    tier=tier_for(threats)
    win=[(t,0,0,la,lo) for t,la,lo in flashes if T-timedelta(minutes=10)<t<=T]
    d_now,d_corr=corridor_metrics(tl,T,win)
    if d_now is not None and d_now<=GLM_WARN_KM: tier="WARNING"
    elif ((d_now is not None and d_now<=GLM_WATCH_KM) or
          (d_corr is not None and d_corr<=GLM_WATCH_KM)):
        tier=max(tier,"WATCH",key=lambda x:TIER_ORDER[x])
    glm={"available":True,"flashes":win,
         "flashes_within_100km":len(win),"window_min":10.0}
    args=types.SimpleNamespace(lat=clat,lon=clon,
                               dem="dem/topo.nc",dem_shade="dem/shade.nc")
    lats=[p[1] for p in tl]; lons=[p[2] for p in tl]
    span=math.hypot((max(lats)-min(lats))*111.0,
                    (max(lons)-min(lons))*111.0*math.cos(math.radians(clat)))
    fit_r=max(span/2.0+7.0,12.0)
    make_map(f"route_maps/wide_{T:%H%M}Z.png",args,T,tier,map_cells,glm,True,route_tl=tl)
    make_map(f"route_maps/fit_{T:%H%M}Z.png",args,T,tier,map_cells,glm,True,route_tl=tl,radius_km=fit_r)
    print(T.strftime("%H%MZ"),tier,flush=True)
    T+=timedelta(minutes=30)
