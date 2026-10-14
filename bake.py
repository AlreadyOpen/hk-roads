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
  - TD's Traffic Aids Drawings (2nd generation; data.gov.hk hk-td-tis_16-traffic-aids-drawings-v2): the whole file geodatabase
    dTAD_IRNP.gdb.zip (~218 MB zipped, ~580 MB of tables): traffic signs (poles, plates, abbreviations), traffic lights, road
    markings (lines, symbols, annotation text), yellow boxes, crossings, railings, tactile paving, direction and pedestrian signs,
    every layer and every field. traffic-aids/<layer>.parquet (GeoParquet, EPSG:4326; a folder, as Kaggle allows only 50 top-level
    files) and traffic-aids.pmtiles (one tile layer per geodatabase layer, lower-case names, z16-18: street detail only)
  - index.json: counts per layer, sizes, source
Usage: python3 bake.py"""
import datetime, hashlib, json, os, shutil, subprocess, urllib.request, zipfile
import duckdb, fiona, fiona.transform
from pyproj import Transformer
import sources

URL = 'https://static.data.gov.hk/td/road-network-v2/RdNet_IRNP.gdb.zip'
AIDS = 'https://static.data.gov.hk/td/traffic-aids-drawings-v2/dTAD_IRNP.gdb.zip'
AIDS_REVISED = 'https://static.data.gov.hk/td/traffic-aids-drawings-v2/DATA_LAST_UPDATED_DATE.csv'
# the geodatabase's 49 layers (GDB_SystemCatalog, 2026-10-14; the annotation is in *_ANNO classes, where the data spec names
# *_ANNO_PT copies); a new layer is baked too, a missing one stops the bake
AIDS_LAYERS = {'DTAD_' + n for n in 'RD_AL_LINE DROP_KERB_LINE RUN_IN_OUT_LINE PED_REFUGE_LINE LV21_LINE CYC_PT MISC_PT GIPOLE_PT TY_BAR_LINE \
TW_STRIP_LINE TS_POLE_PT TS_POLE_LINE TS_FILLED TS_ABV_PT TS_ABV_LINE TS_MISC_LINE TS_PLATE_LINE TG_PATH_LINE RAILING_LINE YL_BOX_LINE YL_BOX_POLY \
CROSSING_LINE RD_MARK_LINE RD_MARK_LINE_C RD_MARK_SYM_LINE RD_MARK_SYM_PT LV22_LINE LV22_FILLED TRAFFIC_LIGHT_PT \
TRAFFIC_LIGHT_LINE TRAFFIC_LIGHT_FILLED LV23_LINE RST_ZONE_LINE PS_MISC_LINE PS_PLATE_LINE LV30_LINE PS_POLE_LINE PS_POLE_PT \
DS_POLE_LINE_C DS_MISC_LINE DS_PLATE_LINE LV38_LINE DS_POLE_LINE DS_POLE_PT DS_FILLED RD_MARK_ANNO TS_ABV_ANNO PS_ANNO'.split()} | {'UNKNOWN_LINE'}
# two layers of the data spec are not in the geodatabase, only in TD's CityGML (and KML) copies: read from the GML
AIDS_GML = ['DTAD_LV24_LINE', 'DTAD_PS_FILLED']
LANDSD = 'https://portal.csdi.gov.hk/server/rest/services/common/landsd_rcd_1637310758814_80061/FeatureServer/0/query'
TO_LL = Transformer.from_crs(2326, 4326, always_xy=True)
# tile layers: the zoom each starts at (dense layers later); table layers have no geometry and go to Parquet only
LAYERS = {'CENTERLINE': 8, 'SPEED_LIMIT': 12, 'BUS_ONLY_LANE': 12, 'PEDESTRIAN_ZONE': 12, 'ROUNDABOUT': 12, 'NSR': 14, 'TURN': 14,
          'INTERSECTION': 14, 'TRAFFIC_FEATURES': 14, 'RUN_IN_OUT': 14, 'PERMIT': 14, 'PROHIBITION': 14, 'VEHICLE_RESTRICTION': 14,
          'ONSTREETPARK': 15, 'GISP_ON_STREET_PARKING': 15}

def fetch(url=URL):
    """a zipped geodatabase, unzipped into the cache; a new download replaces it only when its bytes changed"""
    os.makedirs(sources.CACHE, exist_ok=True)
    name = os.path.basename(url); z = os.path.join(sources.CACHE, name)
    for attempt in range(4):
        try: urllib.request.urlretrieve(url, z + '.part'); break
        except Exception:
            if attempt == 3: raise
    h = hashlib.sha256()
    with open(z + '.part', 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    sha = h.hexdigest()[:16]; gdb = os.path.join(sources.CACHE, sha, name[:-4])
    if not os.path.isdir(gdb):
        with zipfile.ZipFile(z + '.part') as f: f.extractall(os.path.dirname(gdb))
    os.remove(z + '.part')
    return gdb, sha

def ll(g):
    """GeoJSON geometry dict, HK1980 -> lon/lat (7 decimals, ~1 cm)"""
    t = g['type']; c = g['coordinates']
    pt = lambda p: [round(v, 7) for v in TO_LL.transform(p[0], p[1])]
    f = {'Point': pt, 'MultiPoint': lambda cs: [pt(p) for p in cs], 'LineString': lambda cs: [pt(p) for p in cs],
         'MultiLineString': lambda ls: [[pt(p) for p in l] for l in ls]}[t]
    return {'type': t, 'coordinates': f(c)}

def rnd(c):
    """GeoJSON coordinates of any depth to 7 decimals (~1 cm), x and y only"""
    if not c: return []                                                       # an empty part of a multi-geometry, kept as published
    return [round(c[0], 7), round(c[1], 7)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]

def citygml(url):
    """TD's CityGML copy of one layer (gen:GenericCityObject, gen:*Attribute, lod4Geometry in HK1980): (keys, features in lon/lat).
    GDAL reads none of its features with TD's own .gfs, so it is parsed here."""
    import xml.etree.ElementTree as ET
    gml = '{http://www.opengis.net/gml}'; gen = '{http://www.opengis.net/citygml/generics/2.0}'
    for attempt in range(4):
        try: root = ET.fromstring(urllib.request.urlopen(url, timeout=300).read()); break
        except Exception:
            if attempt == 3: raise
    pts = lambda e: [[round(v, 7) for v in TO_LL.transform(*xy)] for xy in zip(*[iter(map(float, e.findtext(f'{gml}posList').split()))] * 2)]
    poly = lambda e: [pts(r) for r in e.iter(f'{gml}LinearRing')]
    keys, feats = [], []
    for o in root.iter(f'{gen}GenericCityObject'):
        p = {}
        for a in o:
            if a.tag.endswith('Attribute'):
                keys += [a.get('name')] * (a.get('name') not in keys)
                if (v := a.findtext(f'{gen}value')) not in (None, ''): p[a.get('name')] = v
        g, geo = None, next(iter(o.find(f'{gen}lod4Geometry') or []), None)
        if geo is not None:
            t = geo.tag[len(gml):]
            if t == 'LineString': g = {'type': 'LineString', 'coordinates': pts(geo)}
            elif t == 'MultiCurve': g = {'type': 'MultiLineString', 'coordinates': [pts(l) for l in geo.iter(f'{gml}LineString')]}
            elif t == 'Polygon': g = {'type': 'Polygon', 'coordinates': poly(geo)}
            elif t == 'MultiSurface': g = {'type': 'MultiPolygon', 'coordinates': [poly(q) for q in geo.iter(f'{gml}Polygon')]}
            else: raise ValueError(f'{url}: geometry {t} not handled')
        feats.append({'type': 'Feature', 'properties': p, 'geometry': g})
    assert feats, f'{url}: no features'
    return keys, feats

def aids(c, tmp):
    """Traffic Aids Drawings: every layer to traffic-aids/<layer>.parquet and to traffic-aids.pmtiles. GDAL turns arcs into lines and
    reprojects (fiona.transform, all geometry types). A feature without geometry stays in the Parquet file (null geometry); tippecanoe
    skips it. Returns (counts, revised, sha, no-geometry counts)."""
    gdb, sha = fetch(AIDS)
    revised = urllib.request.urlopen(AIDS_REVISED, timeout=60).read().decode('utf-8-sig').strip().splitlines()[-1].strip()
    layers = fiona.listlayers(gdb); missing = AIDS_LAYERS - set(layers)
    assert not missing, f'Traffic Aids Drawings: layers gone from the geodatabase: {sorted(missing)}'
    out_dir = os.path.join(sources.OUT, 'traffic-aids'); os.makedirs(out_dir, exist_ok=True)
    counts, empty, tiles_in = {}, {}, []
    for L in sorted(layers) + AIDS_GML:
        name = L.lower(); src = os.path.join(tmp, f'aids_{name}.geojsonl'); n = e = 0
        with open(src, 'w') as out:
            if L in AIDS_GML:
                keys, feats = citygml(AIDS.rsplit('/', 1)[0] + f'/{L}.gml'); has_geom = True
            else:
                s = fiona.open(gdb, layer=L); keys = list(s.schema['properties']); has_geom = s.schema['geometry'] not in (None, 'None')
                feats = ({'type': 'Feature', 'properties': {k: v for k, v in dict(f.properties).items() if v is not None},
                          'geometry': dict(fiona.transform.transform_geom(s.crs, 'EPSG:4326', f.geometry)) if has_geom and f.geometry else None} for f in s)
            for f in feats:
                g = f['geometry']
                f['geometry'] = {'type': g['type'], 'coordinates': rnd(g['coordinates'])} if g and g.get('coordinates') else None
                e += has_geom and not f['geometry']
                out.write(json.dumps(f, ensure_ascii=False, default=str) + '\n'); n += 1
            if L not in AIDS_GML: s.close()
        counts[f'aids_{name}'] = n
        if e: empty[name] = e
        if has_geom: tiles_in += ['-L', json.dumps({'file': src, 'layer': name})]
        pq = os.path.join(out_dir, f'{name}.parquet')
        geom = ", ST_GeomFromGeoJSON(geometry::VARCHAR) AS geometry" if has_geom else ''
        cols = ', '.join(f"properties->>'{k}' AS \"{k}\"" for k in keys)
        c.execute(f"""COPY (SELECT {cols}{geom} FROM read_json('{src}', format = 'newline_delimited', columns = {{properties: 'JSON', geometry: 'JSON'}},
                      maximum_object_size = 268435456)) TO '{pq}.part' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 16384)""")
        os.replace(pq + '.part', pq)
    tiles = os.path.join(sources.OUT, 'traffic-aids.pmtiles')
    # z16-18 only (street detail); the densest tiles at z16-17 are thinned to fit, z18 keeps every feature (extended past 18 if needed)
    subprocess.run(['tippecanoe', '-o', tiles + '.part.pmtiles', '-Z16', '-z18', '--drop-densest-as-needed', '--extend-zooms-if-still-dropping',
                    '--force', '--no-progress-indicator', *tiles_in], check=True)
    os.replace(tiles + '.part.pmtiles', tiles)
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)                      # ~GBs of GeoJSON: free the runner's disk
    return counts, revised, sha, empty

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
    a_counts, a_revised, a_sha, a_empty = aids(c, tmp); counts |= a_counts
    idx = {'built': datetime.datetime.now().isoformat(timespec='seconds'), 'counts': counts,
           'pmtiles': {'file': 'roads.pmtiles', 'layers': {L.lower(): {'minzoom': z} for L, z in LAYERS.items()}, 'minzoom': 8, 'maxzoom': 16,
                       'MB': round(os.path.getsize(tiles) / 1e6, 1)},
           'parquet': {f'{k}.parquet': round(os.path.getsize(os.path.join(sources.OUT, f'{k}.parquet')) / 1e6, 2) for k in [*counts, 'landsd_road_centreline_hk1980'] if not k.startswith('aids_')},
           'landsd': {'pmtiles': {'file': 'landsd-centreline.pmtiles', 'layers': {'landsd_centreline': {'minzoom': 12}}, 'minzoom': 12, 'maxzoom': 16,
                                  'MB': round(os.path.getsize(os.path.join(sources.OUT, 'landsd-centreline.pmtiles')) / 1e6, 1)},
                      'crs': {'landsd_road_centreline_hk1980.parquet': 'EPSG:2326 (HK1980 Grid), coordinates as served', 'landsd_road_centreline.parquet': 'EPSG:4326'},
                      'source': f'Lands Department, Road Centreline (CSDI landsd_rcd_1637310758814_80061) {LANDSD}', 'licence': 'https://portal.csdi.gov.hk/csdi-webpage/doc/TNC'},
           'aids': {'pmtiles': {'file': 'traffic-aids.pmtiles', 'minzoom': 16, 'maxzoom': 18, 'layers': sorted(k[5:] for k in a_counts),
                                'MB': round(os.path.getsize(os.path.join(sources.OUT, 'traffic-aids.pmtiles')) / 1e6, 1)},
                    'parquet': {f'traffic-aids/{k[5:]}.parquet': round(os.path.getsize(os.path.join(sources.OUT, 'traffic-aids', f'{k[5:]}.parquet')) / 1e6, 2) for k in a_counts},
                    'no_geometry': a_empty, 'revised': a_revised, 'from_citygml': [l.lower() for l in AIDS_GML],
                    'source': f'Transport Department, Traffic Aids Drawings (2nd Generation) {AIDS}, snapshot {a_sha}, data revision cut-off {a_revised}'},
           'source': f'Transport Department, Road Network (2nd Generation) {URL}, snapshot {sha}, tippecanoe 2.79.0'}
    json.dump(idx, open(os.path.join(sources.OUT, 'index.json'), 'w'), indent=1)
    print(json.dumps(idx))

if __name__ == '__main__':
    main()
