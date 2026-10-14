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
  - LandsD's Road Centreline (CSDI landsd_rcd_1637310758814_80061; Helen 2026-10-14: LandsD 1:1000 alignment matters), its own
    files because its terms are CSDI's, not DATA.GOV.HK's: landsd_road_centreline_hk1980.parquet (GeoParquet in HK1980 Grid,
    EPSG:2326, the service's coordinates untouched, for aligning with iB1000/iRG1000 and other LandsD 1:1000 data),
    landsd_road_centreline.parquet (WGS84) and landsd-centreline.pmtiles (z12-16). The REST service returns coordinates on a
    1/16 m grid; the full-precision file downloads on CSDI refuse scripted requests (403, 2026-10-14)
  - index.json: counts per layer, sizes, source
Usage: python3 bake.py"""
import datetime, hashlib, json, os, shutil, subprocess, urllib.request, zipfile
import duckdb, fiona
from pyproj import Transformer
import sources

URL = 'https://static.data.gov.hk/td/road-network-v2/RdNet_IRNP.gdb.zip'
LANDSD = 'https://portal.csdi.gov.hk/server/rest/services/common/landsd_rcd_1637310758814_80061/FeatureServer/0/query'
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

def landsd(c, tmp):
    """LandsD Road Centreline from CSDI, 3,000 per page, in HK1980; two GeoParquet files and the tile input. Returns its count."""
    rows = []
    for off in range(0, 200000, 3000):
        u = f'{LANDSD}?where=1%3D1&outFields=*&outSR=2326&orderByFields=OBJECTID&resultOffset={off}&resultRecordCount=3000&f=geojson'
        for attempt in range(4):
            try: d = json.load(urllib.request.urlopen(u, timeout=180)); break
            except Exception:
                if attempt == 3: raise
        assert 'features' in d, f'CSDI answered {str(d)[:200]}'
        rows += d['features']
        if len(d['features']) < 3000 and not d.get('exceededTransferLimit') and not d.get('properties', {}).get('exceededTransferLimit'): break
    ids = {f['properties']['OBJECTID'] for f in rows}
    assert len(ids) == len(rows) > 30000, f'LandsD Road Centreline: {len(rows)} rows, {len(ids)} ids'
    hk, src = os.path.join(tmp, 'landsd_hk1980.geojsonl'), os.path.join(tmp, 'landsd_centreline.geojsonl')
    with open(hk, 'w') as a, open(src, 'w') as b:
        for f in rows:
            p = {k: v for k, v in f['properties'].items() if v is not None}
            if isinstance(p.get('LASTUPDATEDATE'), (int, float)):   # epoch milliseconds to a date
                p['LASTUPDATEDATE'] = datetime.datetime.fromtimestamp(p['LASTUPDATEDATE'] / 1000, datetime.UTC).date().isoformat()
            g = f['geometry']
            if not g: continue
            g = {'type': g['type'], 'coordinates': [[round(x, 4), round(y, 4)] for x, y in g['coordinates']]} if g['type'] == 'LineString' else g
            a.write(json.dumps({'properties': p, 'geometry': g}, ensure_ascii=False) + '\n')
            b.write(json.dumps({'type': 'Feature', 'properties': p, 'geometry': ll(g), 'tippecanoe': {'minzoom': 12}}, ensure_ascii=False) + '\n')
    keys = sorted({k for f in rows for k in f['properties']})
    cols = ', '.join(f"properties->>'{k}' AS \"{k}\"" for k in keys)
    for name, path, crs in (('landsd_road_centreline_hk1980', hk, 'EPSG:2326'), ('landsd_road_centreline', src, 'EPSG:4326')):
        pq = os.path.join(sources.OUT, f'{name}.parquet')
        c.execute(f"""COPY (SELECT {cols}, ST_GeomFromGeoJSON(geometry::VARCHAR) AS geometry FROM read_json('{path}', format = 'newline_delimited',
                      columns = {{properties: 'JSON', geometry: 'JSON'}})) TO '{pq}.part' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 16384)""")
        os.replace(pq + '.part', pq)
        # DuckDB 1.4 writes GeoParquet metadata as WGS84 lon/lat; the HK1980 copy states its CRS in index.json and the card
    tiles = os.path.join(sources.OUT, 'landsd-centreline.pmtiles')
    subprocess.run(['tippecanoe', '-o', tiles + '.part.pmtiles', '-Z12', '-z16', '--drop-densest-as-needed', '--extend-zooms-if-still-dropping',
                    '--force', '--quiet', '-L', json.dumps({'file': src, 'layer': 'landsd_centreline'})], check=True)
    os.replace(tiles + '.part.pmtiles', tiles)
    return len(rows)

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
    counts['landsd_road_centreline'] = landsd(c, tmp)
    idx = {'built': datetime.datetime.now().isoformat(timespec='seconds'), 'counts': counts,
           'pmtiles': {'file': 'roads.pmtiles', 'layers': {L.lower(): {'minzoom': z} for L, z in LAYERS.items()}, 'minzoom': 8, 'maxzoom': 16,
                       'MB': round(os.path.getsize(tiles) / 1e6, 1)},
           'parquet': {f'{k}.parquet': round(os.path.getsize(os.path.join(sources.OUT, f'{k}.parquet')) / 1e6, 2) for k in [*counts, 'landsd_road_centreline_hk1980']},
           'landsd': {'pmtiles': {'file': 'landsd-centreline.pmtiles', 'layers': {'landsd_centreline': {'minzoom': 12}}, 'minzoom': 12, 'maxzoom': 16,
                                  'MB': round(os.path.getsize(os.path.join(sources.OUT, 'landsd-centreline.pmtiles')) / 1e6, 1)},
                      'crs': {'landsd_road_centreline_hk1980.parquet': 'EPSG:2326 (HK1980 Grid), coordinates as served', 'landsd_road_centreline.parquet': 'EPSG:4326'},
                      'source': f'Lands Department, Road Centreline (CSDI landsd_rcd_1637310758814_80061) {LANDSD}', 'licence': 'https://portal.csdi.gov.hk/csdi-webpage/doc/TNC'},
           'source': f'Transport Department, Road Network (2nd Generation) {URL}, snapshot {sha}, tippecanoe 2.79.0'}
    json.dump(idx, open(os.path.join(sources.OUT, 'index.json'), 'w'), indent=1)
    print(json.dumps(idx))

if __name__ == '__main__':
    main()
