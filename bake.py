"""Hong Kong's road network from the Transport Department's Intelligent Road Network, 2nd generation (data.gov.hk
hk-td-tis_15-road-network-v2): the whole file geodatabase RdNet_IRNP.gdb.zip (~17 MB; the KMZ copies on the same page hold the
same layers at 7x the size), every layer, every field.
Curved geometries (centrelines, speed limits, bus lanes, no-stopping restrictions are stored as MULTICURVE) are turned into
lines by GDAL as Fiona reads them; coordinates go from HK1980 Grid (EPSG:2326) to WGS84.
Writes, in data/out/:
  - roads.pmtiles: vector tiles z8-16, one tile layer per geometry layer (lower-case names: centerline, speed_limit, ...),
    each from its own zoom (LAYERS below), tiles gzipped inside (tippecanoe)
  - <layer>.parquet for all 17 layers: GeoParquet (WKB, EPSG:4326) for the 14 with geometry and for the on-street parking
    details (GISP_ON_STREET_PARKING: meter, capacity, vehicle type, hours), placed at their own X_COOR/Y_COOR (the same spots as
    ONSTREETPARK, whose points carry only an id: 36,956 of 37,279 coincide within 0.1 m, 2026-10-10); plain Parquet for the two
    toll tables
  - index.json: counts per layer, sizes, source
Usage: python3 bake.py"""
import datetime, hashlib, json, os, shutil, subprocess, urllib.request, zipfile
import duckdb, fiona
from pyproj import Transformer
import sources

URL = 'https://static.data.gov.hk/td/road-network-v2/RdNet_IRNP.gdb.zip'
TO_LL = Transformer.from_crs(2326, 4326, always_xy=True)
# tile layers: the zoom each starts at (dense layers later); table layers have no geometry and go to Parquet only
LAYERS = {'CENTERLINE': 8, 'SPEED_LIMIT': 12, 'BUS_ONLY_LANE': 12, 'PEDESTRIAN_ZONE': 12, 'ROUNDABOUT': 12, 'NSR': 14, 'TURN': 14,
          'INTERSECTION': 14, 'TRAFFIC_FEATURES': 14, 'RUN_IN_OUT': 14, 'PERMIT': 14, 'PROHIBITION': 14, 'VEHICLE_RESTRICTION': 14,
          'ONSTREETPARK': 15, 'GISP_ON_STREET_PARKING': 15}

def fetch():
    """the geodatabase, unzipped into the cache; a new download replaces it only when its bytes changed"""
    os.makedirs(sources.CACHE, exist_ok=True)
    z = os.path.join(sources.CACHE, 'RdNet_IRNP.gdb.zip')
    for attempt in range(4):
        try: body = urllib.request.urlopen(URL, timeout=300).read(); break
        except Exception:
            if attempt == 3: raise
    sha = hashlib.sha256(body).hexdigest()[:16]; gdb = os.path.join(sources.CACHE, sha, 'RdNet_IRNP.gdb')
    if not os.path.isdir(gdb):
        open(z, 'wb').write(body)
        with zipfile.ZipFile(z) as f: f.extractall(os.path.dirname(gdb))
    return gdb, sha

def ll(g):
    """GeoJSON geometry dict, HK1980 -> lon/lat (7 decimals, ~1 cm)"""
    t = g['type']; c = g['coordinates']
    pt = lambda p: [round(v, 7) for v in TO_LL.transform(p[0], p[1])]
    f = {'Point': pt, 'MultiPoint': lambda cs: [pt(p) for p in cs], 'LineString': lambda cs: [pt(p) for p in cs],
         'MultiLineString': lambda ls: [[pt(p) for p in l] for l in ls]}[t]
    return {'type': t, 'coordinates': f(c)}

def main():
    gdb, sha = fetch()
    os.makedirs(sources.OUT, exist_ok=True); tmp = os.path.join(sources.CACHE, 'geojsonl'); shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    c = duckdb.connect(); c.execute('INSTALL spatial; LOAD spatial')
    counts, tiles_in, layers = {}, [], fiona.listlayers(gdb)
    for L in sorted(layers):
        name = L.lower(); src = os.path.join(tmp, f'{name}.geojsonl'); n = 0
        with fiona.open(gdb, layer=L) as s, open(src, 'w') as out:
            has_geom = s.schema['geometry'] not in (None, 'None')
            xy = not has_geom and {'X_COOR', 'Y_COOR'} <= set(s.schema['properties'])   # a table with HK1980 coordinates: points from them
            for f in s:
                p = {k: v for k, v in dict(f.properties).items() if v is not None}
                g = ll(dict(f.geometry)) if has_geom and f.geometry else ll({'type': 'Point', 'coordinates': [float(p['X_COOR']), float(p['Y_COOR'])]}) if xy and 'X_COOR' in p and 'Y_COOR' in p else None
                if has_geom and not g: continue                        # a feature without geometry in a geometry layer
                row = {'type': 'Feature', 'properties': p, 'geometry': g}
                if L in LAYERS: row['tippecanoe'] = {'minzoom': LAYERS[L]}
                out.write(json.dumps(row, ensure_ascii=False, default=str) + '\n'); n += 1
        counts[name] = n; has_geom = has_geom or xy
        if has_geom and L in LAYERS: tiles_in += ['-L', json.dumps({'file': src, 'layer': name})]
        # Parquet: every property as text (the geodatabase's own types vary by layer; text keeps every value as published)
        pq = os.path.join(sources.OUT, f'{name}.parquet')
        geom = ", ST_GeomFromGeoJSON(geometry::VARCHAR) AS geometry" if has_geom else ''
        keys = [k for k in s.schema['properties']]
        cols = ', '.join(f"properties->>'{k}' AS \"{k}\"" for k in keys)
        c.execute(f"""COPY (SELECT {cols}{geom} FROM read_json('{src}', format = 'newline_delimited', columns = {{properties: 'JSON', geometry: 'JSON'}}))
                      TO '{pq}.part' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 16384)""")
        os.replace(pq + '.part', pq)
    missing = set(LAYERS) - set(layers)
    assert not missing, f'layers gone from the geodatabase: {missing}'
    tiles = os.path.join(sources.OUT, 'roads.pmtiles')
    subprocess.run(['tippecanoe', '-o', tiles + '.part.pmtiles', '-Z8', '-z16', '--drop-densest-as-needed', '--extend-zooms-if-still-dropping',
                    '--force', '--quiet', *tiles_in], check=True)
    os.replace(tiles + '.part.pmtiles', tiles)
    idx = {'built': datetime.datetime.now().isoformat(timespec='seconds'), 'counts': counts,
           'pmtiles': {'file': 'roads.pmtiles', 'layers': {L.lower(): {'minzoom': z} for L, z in LAYERS.items()}, 'minzoom': 8, 'maxzoom': 16,
                       'MB': round(os.path.getsize(tiles) / 1e6, 1)},
           'parquet': {f'{k}.parquet': round(os.path.getsize(os.path.join(sources.OUT, f'{k}.parquet')) / 1e6, 2) for k in counts},
           'source': f'Transport Department, Road Network (2nd Generation) {URL}, snapshot {sha}, tippecanoe 2.79.0'}
    json.dump(idx, open(os.path.join(sources.OUT, 'index.json'), 'w'), indent=1)
    print(json.dumps(idx))

if __name__ == '__main__':
    main()
