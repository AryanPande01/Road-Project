"""
One-time PBF -> GeoPackage builder.

Usage:
  python scripts/osm_build.py --pbf ../osm/india-latest.osm.pbf --out data/india_roads.gpkg

This script attempts to use `pyrosm` to read the PBF and `geopandas` to
write a GeoPackage containing highway ways with a small `tags` JSON column.
If the required libraries are missing it prints instructions and exits.
"""
import os, sys, json, argparse

DRIVEABLE = ["motorway","trunk","primary","secondary","tertiary",
             "unclassified","residential","service","living_street",
             "motorway_link","trunk_link","primary_link","secondary_link",
             "tertiary_link","road"]
LAST_RESORT = ["footway","path","cycleway","pedestrian","track","steps"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", default=os.path.join("..","osm","india-latest.osm.pbf"))
    ap.add_argument("--out", default=os.path.join("..","data","india_roads.gpkg"))
    ap.add_argument("--layer", default="roads")
    args = ap.parse_args()

    pbf = os.path.abspath(args.pbf)
    out = os.path.abspath(args.out)

    try:
        from pyrosm import OSM
        import geopandas as gpd
        from shapely.geometry import LineString, MultiLineString
    except Exception as e:
        print("Missing dependencies. Install with:")
        print("  pip install pyrosm geopandas shapely rtree pyproj fiona")
        print("Or use conda-forge for geopandas/GDAL on Windows: https://anaconda.org/conda-forge/geopandas")
        sys.exit(1)

    if not os.path.exists(pbf):
        print(f"PBF file not found: {pbf}")
        sys.exit(1)

    os.makedirs(os.path.dirname(out), exist_ok=True)

    print(f"Reading PBF (this may take a while): {pbf}")
    osm = OSM(pbf)

    # Try a custom criteria read for highway ways. pyrosm's API varies by version,
    # so attempt a couple of call patterns and fail with instructions if none work.
    roads_gdf = None
    try:
        roads_gdf = osm.get_data_by_custom_criteria(custom_filter={"highway": True}, filter_type="keep")
    except Exception:
        try:
            roads_gdf = osm.get_data_by_custom_criteria({"highway": True})
        except Exception as e:
            print("Could not extract highway ways via pyrosm. Check pyrosm version and docs.")
            raise

    if roads_gdf is None or len(roads_gdf) == 0:
        print("No highway ways found in the PBF.")
        sys.exit(1)

    # Keep only relevant columns; create a JSON-serialised `tags` column so it's
    # preserved in the GeoPackage even if the driver doesn't support dicts.
    def build_tags(row):
        tags = {}
        for k in ("highway", "name", "lanes", "width", "oneway", "ref"):
            v = row.get(k)
            if v is not None:
                tags[k] = v
        return json.dumps(tags)

    print(f"Extracted {len(roads_gdf)} raw features; filtering highway types...")
    # Keep only LineString / MultiLineString geometries and reasonable highway tags
    roads_gdf = roads_gdf[roads_gdf.geometry.notnull()].copy()
    roads_gdf["highway"] = roads_gdf.get("highway")
    roads_gdf["tags"] = roads_gdf.apply(build_tags, axis=1)

    # Optional: prefer driveable classes but keep all so adapter can decide
    cols = [c for c in ["osm_id","highway","name","lanes","width","oneway","tags","geometry"] if c in roads_gdf.columns]
    out_gdf = roads_gdf[cols].copy()

    print(f"Writing {len(out_gdf)} ways to GeoPackage: {out}")
    try:
        out_gdf.to_file(out, layer=args.layer, driver="GPKG")
    except Exception as e:
        print("Failed to write GeoPackage. If this is a GDAL/GPKG install issue, consider using the lightweight SQLite+RTree alternative.")
        raise

    meta = {
        "source_pbf": pbf,
        "features": len(out_gdf),
        "layer": args.layer,
    }
    with open(out + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print("Done.")


if __name__ == "__main__":
    main()
