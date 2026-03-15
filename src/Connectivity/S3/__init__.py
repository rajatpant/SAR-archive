"""
src.Connectivity.S3
───────────────────
Single entry-point for all S3 / CDSE eodata connectivity concerns.

Rules (from copilot instructions & LTM):
  - Normalize s3://EODATA → s3://eodata before any operation.
  - On 404 HeadObject, fall back to list_objects_v2 (prefix check).
  - Treat last path segment with no extension as a directory prefix.
  - Config reference: Global-Config/Connectivity/S3/config.json

Phase 4: save_images_data implementation moved here from src/save_images_data.py.
Backward-compat shim remains at src/save_images_data.py.
model/s3_utils.py (S3 low-level I/O) remains at its transitional location.
"""

from model.s3_utils import (      # noqa: F401
    get_s3_config,
    parse_s3_uri,
    get_s3_client,
    test_s3_connectivity,
    list_s3_prefix,
    download_s3_object,
    download_s3_bytes,
    get_rasterio_env,
    resolve_s3_asset,
    extract_center_chip_from_href,
)

__all__ = [
    "get_s3_config",
    "parse_s3_uri",
    "get_s3_client",
    "test_s3_connectivity",
    "list_s3_prefix",
    "download_s3_object",
    "download_s3_bytes",
    "get_rasterio_env",
    "resolve_s3_asset",
    "extract_center_chip_from_href",
]

from .save_images_data import (   # noqa: F401
    save_sar_images_metadata_and_s3,
    save_true_color_images_metadata_and_s3,
    download_nizhnesvirsky_lower_bay_geojson,
)

__all__ += [
    "save_sar_images_metadata_and_s3",
    "save_true_color_images_metadata_and_s3",
    "download_nizhnesvirsky_lower_bay_geojson",
]
