"""
src.Enrichment.S1
──────────────────
S1-specific non-ML enrichment: scene-level metadata quality inspection,
feature suitability scoring, metadata file download, and catalog sync.

Primary external resource: CDSE catalog (via Connectivity.Sentinel.SentinelHub)
and S3 eodata (via Connectivity.S3).

Phase 4: implementation lives at src/Enrichment/common/enrich_metadata_items.py.
Backward-compat shim remains at src/enrich_metadata_items.py.
"""

from src.Enrichment.common.enrich_metadata_items import (    # noqa: F401
    enrich_item,
    s3_list_prefix_debug,
    _inspect_metadata_quality,
    _refine_feature_suitability,
    _download_metadata_files_for_item,
    main as enrich_all_main,
)

__all__ = [
    "enrich_item",
    "s3_list_prefix_debug",
    "_inspect_metadata_quality",
    "_refine_feature_suitability",
    "_download_metadata_files_for_item",
    "enrich_all_main",
]
