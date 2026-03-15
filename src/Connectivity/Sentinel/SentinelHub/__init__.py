"""
src.Connectivity.Sentinel.SentinelHub
─────────────────────────────────────
Single entry-point for all SentinelHub / CDSE connectivity concerns.

Rules (from copilot instructions & LTM):
  - ALWAYS load credentials via _load_sh_config — never construct SHConfig inline
    on macOS/Python 3.9 (TLS handshake hangs).
  - NEVER use catalog.search(item_ids=[...]) — CDSE returns 400. Use ids=[] or
    date+bbox with prefix-match.
  - Strip _COG from scene IDs before catalog lookups.
  - Config reference: Global-Config/Connectivity/Sentinel/SentinelHub/config.json

Phase 4: implementation lives at src/Query/Sentinel/SentinelHub/sentinel_queryutils_listings.py.
Backward-compat shim remains at src/sentinel_queryutils_listings.py.
"""

from src.Query.Sentinel.SentinelHub.sentinel_queryutils_listings import (           # noqa: F401
    _load_sh_config,
    _fetch_token_from_config,
    _get_base_url_from_config,
    _ensure_config,
    execute_request_with_token,
    _use_bearer_token,
    create_initial_sh_config,
    create_cdse_config,
    _exposed_normalize_s1_props,
)

__all__ = [
    "_load_sh_config",
    "_fetch_token_from_config",
    "_get_base_url_from_config",
    "_ensure_config",
    "execute_request_with_token",
    "_use_bearer_token",
    "create_initial_sh_config",
    "create_cdse_config",
    "_exposed_normalize_s1_props",
]
