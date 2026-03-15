"""
src.Enrichment.common
──────────────────────
Common non-ML enrichment utilities shared across S1 and S2 sensors.
Covers atomic writes, S3 manifest parsing, band inference, and
metadata quality scoring.

Depends on:
  src.Connectivity.S3  — for get_s3_client
  src.Query.Sentinel.S3 — for s3_list_prefix

Phase 4: implementation files now live here. Backward-compat shims remain
at src/enrich_metadata_items.py and src/enrich_one_from_mapping.py.
"""

from .enrich_metadata_items import (     # noqa: F401
    atomic_write,
    _find_self_link,
    infer_band_v2,
    infer_s3_uri,
    _safe_parse_xml,
    _score_metadata_group,
    _load_existing_report,
)

from .enrich_one_from_mapping import (   # noqa: F401
    s3_list_prefix,
    s3_get_text,
    extract_jp2_from_manifest,
    infer_band_from_filename,
    extract_scientific_metadata,
    atomic_write as atomic_write_mapping,
)

__all__ = [
    "atomic_write",
    "_find_self_link",
    "infer_band_v2",
    "infer_s3_uri",
    "_safe_parse_xml",
    "_score_metadata_group",
    "_load_existing_report",
    "s3_list_prefix",
    "s3_get_text",
    "extract_jp2_from_manifest",
    "infer_band_from_filename",
    "extract_scientific_metadata",
]
