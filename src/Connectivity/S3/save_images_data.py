
import os
from typing import Optional, Union, Any, Dict, List
import json
import math
import time
import inspect
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from ipyleaflet import Map, GeoJSON, basemaps   
import osmnx as ox
from typing import Optional, Union, Any, Dict, List
import geopandas as gpd


def save_sar_images_metadata_and_s3(listings, folder, location_str, time_range_str):
    """
    Save SAR image listings metadata and S3 paths to a JSON file.

    Args:
        listings (list): List of catalog search result items (features).
        folder (str): Path to the output folder.
    """
    os.makedirs(folder, exist_ok=True)
    subfolder = os.path.join(folder, location_str, time_range_str)
    os.makedirs(subfolder, exist_ok=True)
    subfolder = os.path.join(folder, location_str, time_range_str)
    os.makedirs(subfolder, exist_ok=True)

    metadata_path = os.path.join(subfolder, "image-list.json")
    sar_metadata = []

    for item in listings:
        assets = item.get("assets", {})
        measurement = assets.get("measurement", {})
        s3_href = measurement.get("href", "")
        # Only add if S3 path exists and is a SAR image
        if s3_href.startswith("s3://") and s3_href.endswith(".tiff"):
            sar_metadata.append({
                "id": item.get("id"),
                "datetime": item.get("properties", {}).get("datetime"),
                "s3_path": s3_href,
                "full_metadata": item
            })

    with open(metadata_path, "w") as f:
        json.dump(sar_metadata, f, indent=2)

    print(f"Saved {len(sar_metadata)} SAR metadata entries to {metadata_path}")

def save_true_color_images_metadata_and_s3(listings, folder, location_str, time_range_str):
    """
    Save true color image listings metadata and S3 paths to a JSON file.

    Args:
        listings (list): List of catalog search result items (features).
        folder (str): Path to the output folder.
    """
    os.makedirs(folder, exist_ok=True)
    subfolder = os.path.join(folder, location_str, time_range_str)
    os.makedirs(subfolder, exist_ok=True)
    
    metadata_path = os.path.join(subfolder, "image-list.json")
    true_color_metadata = []

    for item in listings:
        assets = item.get("assets", {})
        # Collect all asset hrefs that are S3 paths
        s3_paths = {}
        for band, asset_info in assets.items():
            href = asset_info.get("href", "")
            if href.startswith("s3://"):
                s3_paths[band] = href

        # Only add if there is at least one S3 path
        if s3_paths:
            entry = dict(item)  # Copy all fields from item
            entry["s3_paths"] = s3_paths
            true_color_metadata.append(entry)
        else:
            print(f"Skipping item {item.get('id')} - no valid S3 asset paths found.")

    with open(metadata_path, "w") as f:
        json.dump(true_color_metadata, f, indent=2)

    print(f"Saved {len(true_color_metadata)} true color metadata entries to {metadata_path}")

# Example usage:
# save_true_color_images_metadata_and_s3(true_color_images,


def download_nizhnesvirsky_lower_bay_geojson(
    output_path: str = "Nizhnesvirsky_lower_bay.geojson",
    bbox: tuple = (60.76, 33.66, 60.80, 33.70),  # (south, west, north, east) Overpass order
    timeout: int = 180,
    retries: int = 3,
    pause: float = 5.0,
    merge_crs: str = "EPSG:4326",
    verbose: bool = True,
) -> gpd.GeoDataFrame:
    """Download a GeoJSON shapefile for Nizhnesvirsky Lower Bay directly from Overpass.

    Fetches both linear waterways and water surface polygons, merges, de-duplicates and saves.

    Args:
        output_path: File path for resulting GeoJSON.
        bbox: (south, west, north, east) tuple in WGS84.
        timeout: Per-request timeout seconds.
        retries: Number of retry attempts per query on failure.
        pause: Seconds to wait between retries/backoff base.
        merge_crs: CRS to enforce on final GeoDataFrame.
        verbose: Whether to print progress messages.

    Returns:
        GeoDataFrame of merged features (may be empty if no data).
    """
    import requests
    from shapely.geometry import shape

    s, w, n, e = bbox

    # Overpass QL queries: lines and polygons
    line_query = f"""
    [out:json][timeout:{timeout}];
    (
      way["waterway"~"^(river|stream|canal|drain|ditch)$"]({s},{w},{n},{e});
      relation["waterway"~"^(river|stream|canal|drain|ditch)$"]({s},{w},{n},{e});
    );
    out body geom;
    """.strip()

    poly_query = f"""
    [out:json][timeout:{timeout}];
    (
      way["natural"="water"]({s},{w},{n},{e});
      relation["natural"="water"]({s},{w},{n},{e});
      way["water"~"^(river|reservoir|lake|pond)$"]({s},{w},{n},{e});
      relation["water"~"^(river|reservoir|lake|pond)$"]({s},{w},{n},{e});
    );
    out body geom;
    """.strip()

    endpoint = "https://overpass-api.de/api/interpreter"

    def run_query(q: str, label: str):
        for attempt in range(1, retries + 1):
            try:
                if verbose:
                    print(f"[download_shapefile] {label} attempt {attempt}")
                resp = requests.post(endpoint, data={"data": q}, timeout=timeout)
                if resp.status_code == 429:
                    # Rate limited – exponential backoff
                    wait = pause * attempt
                    if verbose:
                        print(f"[download_shapefile] 429 Too Many Requests; sleeping {wait:.1f}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                elements = data.get("elements", [])
                geoms = []
                props = []
                for el in elements:
                    if "geometry" not in el:
                        continue
                    try:
                        if el.get("type") == "way":
                            coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
                            if coords and coords[0] == coords[-1] and len(coords) >= 4:
                                from shapely.geometry import Polygon
                                geom_obj = Polygon(coords)
                            else:
                                from shapely.geometry import LineString
                                geom_obj = LineString(coords)
                        elif el.get("type") == "relation":
                            # Simplistic relation handling: skip complex multipolygons for brevity
                            continue
                        else:
                            continue
                        geoms.append(geom_obj)
                        attrs = {k: v for k, v in el.items() if k not in ("geometry",)}
                        tags = attrs.get("tags", {}) or {}
                        merged_attrs = {**{f"tag_{k}": v for k, v in tags.items()}, **attrs}
                        props.append(merged_attrs)
                    except Exception:
                        continue
                if not geoms:
                    if verbose:
                        print(f"[download_shapefile] {label} returned 0 geometries")
                    return gpd.GeoDataFrame(columns=["geometry"], geometry=[], crs="EPSG:4326")
                gdf_local = gpd.GeoDataFrame(props, geometry=geoms, crs="EPSG:4326")
                if verbose:
                    print(f"[download_shapefile] {label} fetched {len(gdf_local)} features")
                return gdf_local
            except Exception as exc:
                if attempt == retries:
                    if verbose:
                        print(f"[download_shapefile] {label} failed after {attempt} attempts: {exc}")
                    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                wait = pause * attempt
                if verbose:
                    print(f"[download_shapefile] {label} error: {exc} – retry in {wait:.1f}s")
                time.sleep(wait)

    gdf_lines = run_query(line_query, "waterway_lines")
    gdf_polys = run_query(poly_query, "water_polygons")

    frames = [g for g in (gdf_lines, gdf_polys) if g is not None and not g.empty]
    if not frames:
        if verbose:
            print("[download_shapefile] No data fetched – writing empty GeoJSON")
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        empty.to_file(output_path, driver="GeoJSON")
        return empty

    merged = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")
    # De-duplicate by WKB
    try:
        merged["_wkb"] = merged.geometry.apply(lambda g: g.wkb if g else None)
        merged = merged.drop_duplicates("_wkb").drop(columns="_wkb")
    except Exception:
        pass

    if merge_crs and merged.crs and merged.crs.to_string() != merge_crs:
        try:
            merged = merged.to_crs(merge_crs)
        except Exception:
            if verbose:
                print(f"[download_shapefile] CRS transform to {merge_crs} failed; keeping original")

    merged.to_file(output_path, driver="GeoJSON")
    if verbose:
        print(f"[download_shapefile] Saved {len(merged)} features to {output_path}")
    return merged
