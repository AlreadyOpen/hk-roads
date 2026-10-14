---
pretty_name: Hong Kong road network
license: other
license_name: data-gov-hk-terms-and-conditions
license_link: https://data.gov.hk/en/terms-and-conditions
tags:
  - geospatial
  - roads
  - transport
  - hong-kong
  - geoparquet
size_categories:
  - 100K<n<1M
---

# Hong Kong road network

Hong Kong's road network from the Transport Department's Intelligent Road Network (2nd generation): every layer and every
field of its file geodatabase; and the Lands Department's Road Centreline (1:1000 topography), for alignment with LandsD's other
1:1000 data. Rebuilt weekly by [AlreadyOpen/hk-roads](https://github.com/AlreadyOpen/hk-roads).

One Parquet file per layer (GeoParquet, `geometry` as WKB in EPSG:4326, every attribute as text exactly as published):

| File | What |
|---|---|
| `centerline.parquet` | road centrelines with street names (EN/TC), `ELEVATION` (level: 0 at grade, 1 and 2 elevated), `TRAVEL_DIRECTION`, route numbers |
| `speed_limit.parquet` | speed limit sections |
| `bus_only_lane.parquet`, `pedestrian_zone.parquet` | bus-only lanes and pedestrian zones, with their hours |
| `nsr.parquet` | no-stopping restrictions, by vehicle type and time |
| `turn.parquet` | restricted turn movements |
| `intersection.parquet`, `roundabout.parquet` | junctions and roundabouts |
| `traffic_features.parquet` | zebra crossings, yellow boxes, toll plazas, cul-de-sacs |
| `prohibition.parquet`, `vehicle_restriction.parquet`, `permit.parquet` | prohibition signs, vehicle restrictions (weight, height, width, length), permit signs |
| `run_in_out.parquet` | run-ins and run-outs of off-street car parks |
| `onstreetpark.parquet` | on-street parking spaces (id only) |
| `gisp_on_street_parking.parquet` | on-street parking details: meter, capacity, vehicle type, operating hours (placed at its own coordinates) |
| `tun_bridge_toll.parquet`, `tun_bridge_tv_toll.parquet` | tunnel and bridge tolls, fixed and time-varying (tables, no geometry) |
| `landsd_road_centreline_hk1980.parquet` | **Lands Department** Road Centreline: street centre lines from the 1:1000 topographic map, with street name (EN/TC), code and type. Geometry in **HK1980 Grid (EPSG:2326)**, the coordinates as the CSDI service returns them (on a 1/16 m grid), for aligning with iB1000/iRG1000 and other LandsD 1:1000 data |
| `landsd_road_centreline.parquet` | the same lines in EPSG:4326 |

Curved geometries in the source (arcs) are turned into lines. Vector tiles of the geometry layers for web maps (PMTiles,
z8-16, one tile layer per Parquet file name; the LandsD centreline in its own `landsd-centreline.pmtiles`, z12-16) are in the bucket
[alreadyopen/hk-roads](https://huggingface.co/buckets/alreadyopen/hk-roads).

```python
import duckdb
duckdb.sql("""SELECT SPEED_LIMIT, count(*) FROM 'hf://datasets/alreadyopen/hk-roads/speed_limit.parquet' GROUP BY 1 ORDER BY 2 DESC""")
```

A copy is mirrored to [Kaggle](https://www.kaggle.com/datasets/helenkwok/hk-roads) after every bake.

## Source and terms

Data from the Government of the Hong Kong SAR, Transport Department, via [DATA.GOV.HK](https://data.gov.hk/en-data/dataset/hk-td-tis_15-road-network-v2),
used under the [DATA.GOV.HK Terms and Conditions](https://data.gov.hk/en/terms-and-conditions). The Government owns the
intellectual property rights in the data. This dataset is derived from it, provided as is with no warranty, and not endorsed
by the Government.

The Road Centreline files are from the Lands Department via the [CSDI Portal](https://portal.csdi.gov.hk) (dataset
`landsd_rcd_1637310758814_80061`), used under the [CSDI terms and conditions](https://portal.csdi.gov.hk/csdi-webpage/doc/TNC),
and kept in their own files.
