"""
src.Query.Sentinel.SentinelHub
───────────────────────────────
All querying logic targeting the SentinelHub / CDSE catalog API.
Covers both listing (STAC catalog) and image request (Process API).

Depends on: src.Connectivity.Sentinel.SentinelHub for config loading.

Phase 4: implementation files now live here. Backward-compat shims remain
at src/sentinel_queryutils.py and src/sentinel_queryutils_listings.py.
"""

from .sentinel_queryutils import (       # noqa: F401
    get_true_color_images,
    get_sar_image,
    list_sar_images_sentinelhub,
    extract_raster_data,
    extract_true_color_raster,
    time_interval_to_string,
    slots_to_query,
    getBBox,
    _locate_geojson,
)

from .sentinel_queryutils_listings import (   # noqa: F401
    get_true_color_images_listings,
    get_sar_listings,
    get_sar_request,
    _direct_catalog_get_items,
    _debug_direct_catalog_query,
    _coerce_polygon_from_gdf,
    _normalize_s1_props,
    metadata_summary_csv,
    load_config_and_bbox,
    map_sar_to_true_color,
    map_sar_to_true_color_by_intersection,
)

__all__ = [
    # Process API
    "get_true_color_images",
    "get_sar_image",
    "list_sar_images_sentinelhub",
    "extract_raster_data",
    "extract_true_color_raster",
    "time_interval_to_string",
    "slots_to_query",
    "getBBox",
    "_locate_geojson",
    # Catalog / Listings API
    "get_true_color_images_listings",
    "get_sar_listings",
    "get_sar_request",
    "_direct_catalog_get_items",
    "_debug_direct_catalog_query",
    "_coerce_polygon_from_gdf",
    "_normalize_s1_props",
    "metadata_summary_csv",
    "load_config_and_bbox",
    "map_sar_to_true_color",
    "map_sar_to_true_color_by_intersection",
]
