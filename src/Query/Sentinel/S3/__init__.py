"""
src.Query.Sentinel.S3
──────────────────────
S3-level querying of Sentinel data (prefix listing, asset resolution,
URL normalization).  Separate from connectivity — this is about finding
the right S3 path for a known scene, not about auth.

Phase 2 note: re-exports from model/s3_utils.py.
"""

from model.s3_utils import (      # noqa: F401
    parse_s3_uri,
    list_s3_prefix,
    resolve_s3_asset,
)

__all__ = [
    "parse_s3_uri",
    "list_s3_prefix",
    "resolve_s3_asset",
]
