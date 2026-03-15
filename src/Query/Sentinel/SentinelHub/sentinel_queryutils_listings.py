from __future__ import annotations

import os
from typing import Optional, Union, Any, Dict, List
import logging
from typing import Optional , Optional, Union, Any, Dict, List

import contextlib
import requests

try:
    import geopandas as gpd
except ImportError:
    gpd = None
import json

from sentinelhub import SHConfig


def _load_sh_config(config: Optional[Union[Dict[str, Any], SHConfig, str]]) -> SHConfig:
    """Load SHConfig from a JSON credentials file if provided.

    Accepts a SHConfig instance, a dict with 'sentinelhub_config', or a direct
    path to a JSON file containing client_id/client_secret and optional URLs.
    """
    if isinstance(config, SHConfig):
        return config

    sh_config = SHConfig()
    creds_path = None

    if isinstance(config, dict):
        creds_path = config.get("sentinelhub_config")
    elif isinstance(config, str):
        creds_path = config

    if creds_path:
        path = os.path.expanduser(creds_path)
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    creds = json.load(fh)
                sh_config.sh_client_id = creds.get("client_id") or sh_config.sh_client_id
                sh_config.sh_client_secret = creds.get("client_secret") or sh_config.sh_client_secret
                if creds.get("sh_base_url"):
                    sh_config.sh_base_url = creds.get("sh_base_url")
                if creds.get("sh_token_url"):
                    sh_config.sh_token_url = creds.get("sh_token_url")
            except Exception:
                logging.exception("Failed to load sentinelhub_config JSON: %s", path)
    elif config is None:
        # No explicit path — auto-load from canonical .config/sentinelhub/config.toml
        try:
            from src.credentials import load_sh_credentials  # noqa: PLC0415
            canon = load_sh_credentials()
            sh_config.sh_client_id = canon["sh_client_id"]
            sh_config.sh_client_secret = canon["sh_client_secret"]
            sh_config.sh_base_url = canon["sh_base_url"]
            sh_config.sh_token_url = canon["sh_token_url"]
        except Exception:
            logging.debug("_load_sh_config: auto-load from .config/ failed — continuing with bare SHConfig")

    return _ensure_config(sh_config)


# ------------------------- Core listing helpers ------------------------- #


def get_true_color_images_listings(aoi_bbox, aoi_size, config, time_interval, queryslots=None):
    """Retrieve Sentinel-2 true color image listings via Sentinel Hub Catalog.

    This function will try the normal sentinelhub catalog search first. If that
    fails due to authentication against the public services.sentinel-hub.com
    endpoint, it will fetch a token using the token URL from the user's config
    (for example CDSE) and retry the catalog search while injecting the
    Bearer token.
    """
    # lazy import so caller can set SH_BASE_URL/SH_TOKEN_URL env before import
    from sentinelhub import SentinelHubCatalog, DataCollection

    config = _load_sh_config(config)
    catalog = SentinelHubCatalog(config)

    try:
        search_iterator = catalog.search(
            DataCollection.SENTINEL2_L2A,
            bbox=aoi_bbox,
            time=time_interval,
        )
        return list(search_iterator)
    except Exception as e:
        err = str(e).lower()
        if 'invalid_client' in err or 'invalid client' in err:
            token = _fetch_token_from_config(config)
            with _use_bearer_token(token, host=None):
                search_iterator = catalog.search(
                    DataCollection.SENTINEL2_L2A,
                    bbox=aoi_bbox,
                    time=time_interval,
                )
                return list(search_iterator)
        raise


def get_sar_listings(
    aoi_bbox,
    time_interval,
    config,
    polarizations=("VV", "VH"),
    instrument_mode="IW",
    orbit_directions=None,
    max_items=50,
    include_assets=False,
):
    """Retrieve Sentinel-1 SAR listings from Sentinel Hub Catalog.

    Behaves like get_true_color_images_listings with a token-injection fallback
    for CDSE/custom Sentinel Hub instances.
    """
    # lazy import sentinelhub classes
    from sentinelhub import SentinelHubCatalog, DataCollection

    config = _load_sh_config(config)
    catalog = SentinelHubCatalog(config)

    features = None
    try:
        features = list(
            catalog.search(
                DataCollection.SENTINEL1_IW,
                bbox=aoi_bbox,
                time=time_interval,
                limit=max_items,
            )
        )
    except Exception as e:
        err = str(e).lower()
        if 'invalid_client' in err or 'invalid client' in err:
            config = _load_sh_config(config)

            # If a custom base URL (e.g. CDSE) is configured, bypass sentinelhub catalog
            # and call the provider's GET /collections/{id}/items endpoint directly.
            base_url = _get_base_url_from_config(config)
            bbox_list = [float(a) for a in list(aoi_bbox.bbox)] if hasattr(aoi_bbox, 'bbox') else list(aoi_bbox)
            features = None
            if base_url:
                try:
                    token = _fetch_token_from_config(config)
                    # Use the 'sentinel-1-grd' collection on CDSE
                    features = _direct_catalog_get_items(token, base_url, 'sentinel-1-grd', bbox_list, time_interval, count=max_items)
                    logging.info('Fetched %s features directly from CDSE %s', len(features), base_url)
                except Exception:
                    logging.exception('Direct GET to CDSE collection items failed; falling back to sentinelhub client')
                    features = None

            if features is None:
                # Fall back to sentinelhub catalog flows (with token injection if needed)
                catalog = SentinelHubCatalog(config)
                try:
                    features = list(
                        catalog.search(
                            DataCollection.SENTINEL1_IW,
                            bbox=aoi_bbox,
                            time=time_interval,
                            limit=max_items,
                        )
                    )
                except Exception as e:
                    err = str(e).lower()
                    if 'invalid_client' in err or 'invalid client' in err:
                        token = _fetch_token_from_config(config)
                        # inject token for all hosts (some sentinelhub client calls use the public
                        # services.sentinel-hub.com endpoint even when a custom SH base URL is set)
                        with _use_bearer_token(token, host=None):
                            features = list(
                                catalog.search(
                                    DataCollection.SENTINEL1_IW,
                                    bbox=aoi_bbox,
                                    time=time_interval,
                                    limit=max_items,
                                )
                            )
                    else:
                        raise

    # Process features (whether retrieved via catalog or direct GET)
    items = []
    for item in (features or []):
        normalized = _normalize_s1_props(item)
        if instrument_mode and normalized.get('instrumentMode') and instrument_mode not in str(normalized.get('instrumentMode')):
            continue
        if orbit_directions and normalized.get('orbitDirection') not in orbit_directions:
            continue
        pols = normalized.get('polarizations')
        if polarizations and pols and not any(p in pols for p in polarizations):
            continue
        if not include_assets and "assets" in item:
            item = dict(item)
            item.pop("assets", None)
        items.append(item)

    return items


# ------------------------- Evalscript & request builder ------------------------- #


evalscript_sar = """
  function setup() {
    return {
      input: ["VV", "dataMask"],
      output: { bands: 2, sampleType: "FLOAT32"}
    }
  }


// visualizes decibels from -20 to +10
function toDb(linear) {
  var log = 10 * Math.log(linear) / Math.LN10
  return Math.max(0, (log + 20) / 30)
}

function evaluatePixel(sample) {
  var VV = sample.VV;

  return [toDb(VV),sample.dataMask];
}
"""


def _coerce_polygon_from_gdf(gdf: gpd.GeoDataFrame, buffer_meters_if_line: float = 50.0):
    if gdf.empty:
        raise ValueError("GeoDataFrame is empty; cannot derive geometry")
    geom = gdf.geometry.iloc[0]
    if geom is None:
        raise ValueError("First geometry is None")
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom

    # Need to turn line/point into area
    if geom.geom_type in ("LineString", "MultiLineString"):
        try:
            epsg = gdf.crs.to_epsg() if gdf.crs else None
        except Exception:
            epsg = None
        if epsg == 4326:
            deg_buffer = buffer_meters_if_line / 111320.0
            buffered = geom.buffer(deg_buffer)
        else:
            buffered = geom.buffer(buffer_meters_if_line)
        if buffered.geom_type in ("Polygon", "MultiPolygon"):
            return buffered
        hull = geom.convex_hull
        if hull.geom_type in ("Polygon", "MultiPolygon"):
            return hull

    if hasattr(geom, "convex_hull"):
        hull = geom.convex_hull
        if hull.geom_type in ("Polygon", "MultiPolygon"):
            return hull
    raise ValueError(f"Cannot coerce geometry of type {geom.geom_type} to Polygon/MultiPolygon")


def _normalize_s1_props(item: dict) -> dict:
    """Return a normalized dict of common SAR properties for filtering.

    Handles CDSE/STAC property names like 'sar:instrument_mode', 'sat:orbit_state',
    'sar:polarizations' and older names like 'instrumentMode', 'orbitDirection', 'polarization'.
    """
    props = item.get('properties', {}) or {}
    # instrument mode
    instrument_mode = (
        props.get('instrumentMode')
        or props.get('sar:instrument_mode')
        or props.get('sar:instrument-mode')
        or props.get('sar:product_type')
        or props.get('productType')
    )
    # orbit direction
    orbit = props.get('orbitDirection') or props.get('sat:orbit_state') or props.get('orbit')
    if isinstance(orbit, str):
        orbit = orbit.lower()

    # polarization: could be a list under sar:polarizations or a string under s1:polarization or polarization
    pol_list = None
    if 'sar:polarizations' in props and isinstance(props.get('sar:polarizations'), (list, tuple)):
        pol_list = [p.upper() for p in props.get('sar:polarizations')]
    elif 's1:polarization' in props and isinstance(props.get('s1:polarization'), str):
        # 'DV' etc. keep as uppercase
        pol_list = [props.get('s1:polarization').upper()]
    elif 'polarization' in props:
        p = props.get('polarization')
        if isinstance(p, list):
            pol_list = [pp.upper() for pp in p]
        elif isinstance(p, str):
            pol_list = [p.upper()]

    return {
        'instrumentMode': instrument_mode,
        'orbitDirection': orbit,
        'polarizations': pol_list,
        'raw_properties': props,
    }


def get_sar_request(
    time_interval,
    config,
    river_gdf,
    buffer_meters_if_line: float = 50.0,
    fetch_metadata: bool = False,
    metadata_limit: int = 50,
    save_metadata_to: Optional[str] = None,
    polarizations=("VV", "VH"),
    instrument_mode="IW",
    orbit_directions=None,
):
    # lazy import sentinelhub classes used to build requests
    from sentinelhub import SentinelHubRequest, DataCollection, MimeType, Geometry

    config = _ensure_config(config)
    area_geom = _coerce_polygon_from_gdf(river_gdf, buffer_meters_if_line)

    request = SentinelHubRequest(
        evalscript=evalscript_sar,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL1_IW.define_from(
                    "s1iw", service_url=config.sh_base_url
                ),
                time_interval=time_interval,
                other_args={
                    "dataFilter": {
                        "resolution": "HIGH",
                        "mosaickingOrder": "mostRecent",
                        "orbitDirection": "ASCENDING",
                    },
                    "processing": {
                        "backCoeff": "SIGMA0_ELLIPSOID",
                        "orthorectify": True,
                        "demInstance": "COPERNICUS",
                        "speckleFilter": {
                            "type": "LEE",
                            "windowSizeX": 3,
                            "windowSizeY": 3,
                        },
                    },
                },
            )
        ],
        responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
        geometry=Geometry(area_geom, crs=river_gdf.crs),
        resolution=[20, 20],
        config=config,
        data_folder="./results",
    )

    if not fetch_metadata:
        return request

    # metadata flow (unchanged) -------------------------------------------------
    if metadata_limit > 100:
        logging.warning("Requested metadata_limit=%s exceeds catalog maximum (100); capping to 100.", metadata_limit)
    from sentinelhub import SentinelHubCatalog, BBox, CRS
    catalog = SentinelHubCatalog(config)
    try:
        gdf_wgs = river_gdf.to_crs("EPSG:4326")
    except Exception:
        gdf_wgs = river_gdf
    minx, miny, maxx, maxy = gdf_wgs.total_bounds
    bbox = BBox(bbox=[minx, miny, maxx, maxy], crs=CRS.WGS84)

    metadata = []
    max_per_request = 100
    max_total = 500
    requested = metadata_limit
    if requested > max_total:
        logging.warning("Requested metadata_limit=%s exceeds supported maximum (%s); capping to %s.", requested, max_total, max_total)
        requested = max_total

    from datetime import datetime, timedelta

    def _to_dt(t):
        if isinstance(t, str):
            return datetime.fromisoformat(t)
        return t

    start_t, end_t = time_interval
    start_dt = _to_dt(start_t)
    end_dt = _to_dt(end_t)
    total_seconds = (end_dt - start_dt).total_seconds()
    if total_seconds <= 0:
        time_slices = [(start_dt, end_dt)] * 1
    else:
        pages = min((requested + max_per_request - 1) // max_per_request, 5)
        slice_seconds = total_seconds / pages
        time_slices = []
        for i in range(pages):
            s = start_dt + timedelta(seconds=i * slice_seconds)
            e = start_dt + timedelta(seconds=(i + 1) * slice_seconds)
            if i == pages - 1:
                e = end_dt
            time_slices.append((s, e))

    seen_ids = set()
    for slice_start, slice_end in time_slices:
        if len(metadata) >= requested:
            break
        logging.info("Fetching catalog items for time slice %s - %s", slice_start, slice_end)
        search_iterator = catalog.search(
            DataCollection.SENTINEL1_IW,
            bbox=bbox,
            time=(slice_start, slice_end),
            limit=max_per_request,
        )
        try:
            for item in search_iterator:
                if len(metadata) >= requested:
                    break
                item_id = item.get("id") or item.get("properties", {}).get("identifier")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                metadata.append(item)
        except Exception as e:  # pragma: no cover - network/auth behavior
            err_str = str(e)
            if "invalid_client" in err_str.lower() or "invalid client" in err_str.lower():
                raise ValueError(
                    "Authentication with Sentinel Hub failed: invalid client credentials. "
                    "Check that your `SHConfig` contains a valid `sh_client_id` and `sh_client_secret`, "
                    "or that environment variables `SH_CLIENT_ID`/`SH_CLIENT_SECRET` are set. "
                    f"Original error: {err_str}"
                ) from e
            logging.exception("Failed to query Sentinel Hub Catalog for slice %s-%s: %s", slice_start, slice_end, err_str)
            raise RuntimeError(
                "Failed to query Sentinel Hub Catalog. This may be a network issue, a service outage, or a problem with your Sentinel Hub configuration. "
                f"Original error: {err_str}"
            ) from e

    return {"request": request, "metadata": metadata}


# ------------------------- Token helpers & execution ------------------------- #


def _fetch_token_from_config(config: Optional[object] = None):
    """Fetch an OAuth2 client_credentials token using SH_TOKEN_URL or config.toml.

    Returns the access_token string or raises on failure.
    """
    token_url = os.getenv('SH_TOKEN_URL')
    client_id = None
    client_secret = None
    if config is not None:
        client_id = getattr(config, 'sh_client_id', None)
        client_secret = getattr(config, 'sh_client_secret', None)

    client_id = client_id or os.getenv('SH_CLIENT_ID')
    client_secret = client_secret or os.getenv('SH_CLIENT_SECRET')

    # If token URL not set in env, try reading config.toml (cdse/default sections)
    if not token_url:
        try:
            try:
                import tomllib
            except Exception:
                import tomli as tomllib
        except Exception:
            tomllib = None
        if tomllib is not None:
            cfg_path = os.path.expanduser('~/.config/sentinelhub/config.toml')
            try:
                if os.path.exists(cfg_path):
                    with open(cfg_path, 'rb') as fh:
                        data = tomllib.load(fh)
                    for sec in ('cdse', 'CDSE', 'default-profile', 'default'):
                        s = data.get(sec) if isinstance(data, dict) else None
                        if s and isinstance(s, dict) and s.get('sh_token_url'):
                            token_url = s.get('sh_token_url')
                            break
                    if not token_url and isinstance(data, dict) and data.get('sh_token_url'):
                        token_url = data.get('sh_token_url')
            except Exception:
                pass

    if not token_url:
        token_url = 'https://services.sentinel-hub.com/oauth/token'

    if not client_id or not client_secret:
        raise ValueError('Missing client credentials for token fetch')

    resp = requests.post(token_url, auth=(client_id, client_secret), data={'grant_type': 'client_credentials'}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f'Token fetch failed ({resp.status_code}): {resp.text[:200]}')
    return resp.json().get('access_token')


def _debug_direct_catalog_query(token: str, bbox: list, time_interval: tuple):
    """Debug helper: perform a direct POST to the public catalog search endpoint
    with the provided Bearer token to verify whether the token is accepted.

    Returns the parsed JSON response on success, or raises the requests exception.
    """
    url = 'https://services.sentinel-hub.com/api/v1/catalog/1.0.0/search'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    body = {
        "bbox": bbox,
        "time": [time_interval[0], time_interval[1]],
        "collections": ["sentinel-1-grd", "sentinel-1-iw"],
        "limit": 10,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _direct_catalog_get_items(token: str, base_url: str, collection: str, bbox: list, time_interval: tuple, count: int = 100):
    """Use CDSE/STAC GET endpoint /collections/{id}/items with query params.

    Returns list of features on success.
    """
    base = base_url.rstrip('/')
    url = f"{base}/api/v1/catalog/1.0.0/collections/{collection}/items"
    headers = {'Authorization': f'Bearer {token}'}
    # format datetime as RFC3339 range
    start, end = time_interval
    def _ensure_rfc3339(t, is_end=False):
        if isinstance(t, str) and ('T' in t or 'Z' in t):
            return t
        if is_end:
            return f"{t}T23:59:59Z"
        return f"{t}T00:00:00Z"

    datetime_range = f"{_ensure_rfc3339(start, False)}/{_ensure_rfc3339(end, True)}"
    params = {
        'bbox': ','.join(str(float(x)) for x in bbox),
        'datetime': datetime_range,
        'count': str(count),
    }
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and 'features' in data:
        return data['features']
    return []


def _get_base_url_from_config(config: Optional[object] = None) -> Optional[str]:
    """Discover a Sentinel Hub base URL from environment or config.toml.

    Returns the first candidate found or None.
    """
    # environment first
    base = os.getenv('SH_BASE_URL') or os.getenv('SH_BASE') or os.getenv('SH_INSTANCE')
    if base:
        return base

    # try reading config.toml
    try:
        try:
            import tomllib
        except Exception:
            import tomli as tomllib
    except Exception:
        tomllib = None

    if tomllib is not None:
        cfg_path = os.path.expanduser('~/.config/sentinelhub/config.toml')
        try:
            if os.path.exists(cfg_path):
                with open(cfg_path, 'rb') as fh:
                    data = tomllib.load(fh)
                for sec in ('cdse', 'CDSE', 'default-profile', 'default'):
                    s = data.get(sec) if isinstance(data, dict) else None
                    if s and isinstance(s, dict) and s.get('sh_base_url'):
                        return s.get('sh_base_url')
                if isinstance(data, dict) and data.get('sh_base_url'):
                    return data.get('sh_base_url')
        except Exception:
            pass

    # finally check provided config
    try:
        if config is not None:
            b = getattr(config, 'sh_base_url', None)
            if b:
                return b
    except Exception:
        pass

    return None


def execute_request_with_token(request_obj, token: str):
    """Execute a SentinelHubRequest while injecting Authorization Bearer <token>.

    Monkeypatches requests.Session.request and SentinelHubSession._collect_new_token
    to ensure the sentinelhub client uses the provided token for outgoing requests.
    Restores original behavior after execution.
    """
    import requests as _requests
    import time
    orig_request = _requests.sessions.Session.request

    try:
        from sentinelhub.download.session import SentinelHubSession
        sentinelhub_session_cls = SentinelHubSession
        orig_collect = sentinelhub_session_cls._collect_new_token
    except Exception:
        sentinelhub_session_cls = None
        orig_collect = None

    def _wrapped(self, method, url, *args, **kwargs):
        host = os.getenv('SH_BASE_URL') or getattr(getattr(request_obj, 'config', None), 'sh_base_url', None)
        try:
            # Inject if host is not specified (meaning: inject for all requests),
            # or if the configured host appears in the outbound URL.
            if (not host) or (host in str(url)):
                headers = kwargs.setdefault('headers', {})
                if 'Authorization' not in headers:
                    headers['Authorization'] = f'Bearer {token}'
        except Exception:
            pass
        return orig_request(self, method, url, *args, **kwargs)

    _requests.sessions.Session.request = _wrapped
    if sentinelhub_session_cls is not None:
        def _collect_new_token(self, request=None, _token=None):
            return {
                'access_token': token,
                'token_type': 'bearer',
                'expires_in': 3600,
                'expires_at': int(time.time()) + 3600,
            }
        sentinelhub_session_cls._collect_new_token = _collect_new_token
    try:
        return request_obj.get_data()
    finally:
        _requests.sessions.Session.request = orig_request
        if sentinelhub_session_cls is not None and orig_collect is not None:
            sentinelhub_session_cls._collect_new_token = orig_collect


@contextlib.contextmanager
def _use_bearer_token(token: str, host: Optional[str] = None):
    """Context manager that temporarily injects Authorization header and
    patches sentinelhub session token collection to return the provided token.
    """
    import requests as _requests
    import time

    orig_request = _requests.sessions.Session.request
    try:
        from sentinelhub.download.session import SentinelHubSession
        sentinelhub_session_cls = SentinelHubSession
        orig_collect = sentinelhub_session_cls._collect_new_token
    except Exception:
        sentinelhub_session_cls = None
        orig_collect = None

    def _wrapped(self, method, url, *args, **kwargs):
        try:
            # Inject if host not provided (apply to all requests) or if host matches
            if (not host) or (host in str(url)):
                headers = kwargs.setdefault('headers', {})
                if 'Authorization' not in headers:
                    headers['Authorization'] = f'Bearer {token}'
        except Exception:
            pass
        return orig_request(self, method, url, *args, **kwargs)

    _requests.sessions.Session.request = _wrapped
    if sentinelhub_session_cls is not None and orig_collect is not None:
        def _collect_new_token(self, request=None, _token=None):
            return {
                'access_token': token,
                'token_type': 'bearer',
                'expires_in': 3600,
                'expires_at': int(time.time()) + 3600,
            }

        sentinelhub_session_cls._collect_new_token = _collect_new_token

    try:
        yield
    finally:
        _requests.sessions.Session.request = orig_request
        if sentinelhub_session_cls is not None and orig_collect is not None:
            sentinelhub_session_cls._collect_new_token = orig_collect


# ------------------------- Config handling & utilities ------------------------- #


def _ensure_config(config: Optional[object]):
    """Ensure a sentinelhub.SHConfig-like object with client id/secret is provided.

    - Reads credentials from environment variables (SH_CLIENT_ID/SH_CLIENT_SECRET) if present.
    - Raises ValueError if credentials are missing.
    """
    if config is None:
        # Import SHConfig lazily so environment overrides are effective
        from sentinelhub import SHConfig
        config = SHConfig()

    # Allow populating credentials from environment variables if they exist
    env_id = os.getenv("SH_CLIENT_ID")
    env_secret = os.getenv("SH_CLIENT_SECRET")
    if env_id and env_secret:
        # Only set if not already provided
        if not getattr(config, "sh_client_id", None):
            config.sh_client_id = env_id
        if not getattr(config, "sh_client_secret", None):
            config.sh_client_secret = env_secret

    client_id = getattr(config, "sh_client_id", None)
    client_secret = getattr(config, "sh_client_secret", None)
    if not client_id or not client_secret:
        logging.error(
            "Sentinel Hub credentials are missing. Provide them via `SHConfig` or environment variables SH_CLIENT_ID/SH_CLIENT_SECRET."
        )
        raise ValueError(
            "Sentinel Hub credentials are missing in SHConfig. "
            "Set `sh_client_id` and `sh_client_secret` in your `SHConfig` instance "
            "or provide them via environment variables `SH_CLIENT_ID` and `SH_CLIENT_SECRET`. "
            "See https://sentinelhub-py.readthedocs.io/en/latest/configure.html for details."
        )

    return config


def metadata_summary_csv(metadata: list, csv_path: str, fetched_count: Optional[int] = None, requested_count: Optional[int] = None):
    """Write a compact CSV summarizing SAR metadata items.

    Columns: id, acquisition_datetime, instrumentMode, platform, orbitDirection, polarization, footprint_wkt, asset_urls

    Appends a final summary row with `fetched_count` and `requested_count` to indicate how many items were written vs requested.
    """
    import csv
    rows = []
    for item in metadata:
        props = item.get("properties", {})
        item_id = item.get("id") or props.get("identifier") or ""
        date = props.get("datetime") or props.get("acquisitionDate") or ""
        instrument = props.get("instrumentMode", "")
        # Normalized fields
        sensor = props.get("platform", "")
        relative_orbit = props.get("relativeOrbitNumber") or props.get("relativeOrbit") or ""
        product_type = props.get("productType") or props.get("type") or ""
        platform = props.get("platform", "")
        orbit = props.get("orbitDirection", "")
        pol = props.get("polarization", "")
        geom = item.get("geometry")
        footprint = "" if geom is None else json.dumps(geom)
        # Compute approximate footprint area in m^2 if geometry provided
        footprint_area = ""
        try:
            if geom:
                g = gpd.GeoSeries([geom], crs="EPSG:4326")
                g = g.to_crs("EPSG:3857")
                footprint_area = float(g.geometry[0].area)
        except Exception:
            footprint_area = ""

        # Cloud coverage: try common keys
        cloud = props.get("cloudCover") or props.get("eo:cloud_cover") or props.get("cloud_cover") or ""

        assets = item.get("assets") or {}
        asset_urls = []
        for a in assets.values():
            if isinstance(a, dict):
                url = a.get("href") or a.get("url")
                if url:
                    asset_urls.append(url)
        rows.append({
            "id": item_id,
            "datetime": date,
            "sensor": sensor,
            "relative_orbit": relative_orbit,
            "product_type": product_type,
            "instrumentMode": instrument,
            "platform": platform,
            "orbitDirection": orbit,
            "polarization": pol,
            "footprint": footprint,
            "footprint_m2": footprint_area,
            "cloud_cover": cloud,
            "asset_urls": ";".join(asset_urls),
        })

    fieldnames = [
        "id",
        "datetime",
        "sensor",
        "relative_orbit",
        "product_type",
        "instrumentMode",
        "platform",
        "orbitDirection",
        "polarization",
        "footprint",
        "footprint_m2",
        "cloud_cover",
        "asset_urls",
        "fetched_count",
        "requested_count",
    ]
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        # Write a summary row at the end
        summary = {k: "" for k in fieldnames}
        summary["id"] = "__SUMMARY__"
        if fetched_count is None:
            summary["fetched_count"] = str(len(rows))
        else:
            summary["fetched_count"] = str(fetched_count)
        if requested_count is None:
            summary["requested_count"] = ""
        else:
            summary["requested_count"] = str(requested_count)
        writer.writerow(summary)


__all__ = ["get_true_color_images_listings", "get_sar_listings", "get_sar_request", "metadata_summary_csv"]


def load_config_and_bbox(aoi_path: str):
    """Load Sentinel Hub configuration from ~/.config/sentinelhub/config.toml (if present)
    and return a tuple (config, aoi_bbox).

    - aoi_path: path to GeoJSON AOI
    - returns: (SHConfig instance, sentinelhub.BBox instance)

    This function does lazy imports of sentinelhub classes so callers can set
    environment variables before the package initializes.
    """
    import os
    from typing import Optional, Union, Any, Dict, List
    try:
        import tomllib
    except Exception:
        try:
            import tomli as tomllib
        except Exception:
            tomllib = None

    # Import sentinelhub classes lazily
    try:
        from sentinelhub import SHConfig, BBox, CRS
    except Exception:
        SHConfig = None
        BBox = None
        CRS = None

    cfg = SHConfig() if SHConfig is not None else None

    # Try load toml config (same heuristics as previous implementation)
    toml_path = os.path.expanduser('~/.config/sentinelhub/config.toml')
    if tomllib and os.path.exists(toml_path):
        try:
            with open(toml_path, 'rb') as fh:
                data = tomllib.load(fh)
        except Exception:
            data = {}
    else:
        data = {}

    def find_key(d, key):
        if not isinstance(d, dict):
            return None
        if key in d:
            return d[key]
        for v in d.values():
            if isinstance(v, dict) and key in v:
                return v[key]
        return None

    client_id = None
    client_secret = None
    instance_id = None
    base_url = None
    token_url = None

    for k in ('cdse', 'default-profile', 'default', ''):
        section = data.get(k) if k else data
        if not section:
            continue
        if isinstance(section, dict):
            client_id = client_id or (section.get('sh_client_id') or section.get('client_id') or section.get('SH_CLIENT_ID'))
            client_secret = client_secret or (section.get('sh_client_secret') or section.get('client_secret') or section.get('SH_CLIENT_SECRET'))
            instance_id = instance_id or (section.get('instance_id') or section.get('sh_instance_id') or section.get('instance'))
            base_url = base_url or (section.get('sh_base_url') or section.get('base_url'))
            token_url = token_url or (section.get('sh_token_url') or section.get('token_url'))

    # fallback nested
    if (not client_id or not client_secret) and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                client_id = client_id or v.get('sh_client_id') or v.get('client_id')
                client_secret = client_secret or v.get('sh_client_secret') or v.get('client_secret')
                instance_id = instance_id or v.get('instance_id')
                base_url = base_url or v.get('sh_base_url')
                token_url = token_url or v.get('sh_token_url')

    # export env overrides so sentinelhub package picks them up
    if token_url:
        os.environ.setdefault('SH_TOKEN_URL', token_url)
    if base_url:
        os.environ.setdefault('SH_BASE_URL', base_url)

    if client_id and cfg is not None:
        try:
            cfg.sh_client_id = client_id
        except Exception:
            pass
    if client_secret and cfg is not None:
        try:
            cfg.sh_client_secret = client_secret
        except Exception:
            pass
    if instance_id and cfg is not None:
        try:
            cfg.instance_id = instance_id
        except Exception:
            pass
    if base_url and cfg is not None:
        try:
            cfg.sh_base_url = base_url
        except Exception:
            pass

    # Load AOI and compute WGS84 bbox
    import geopandas as gpd

    if not os.path.exists(aoi_path):
        raise FileNotFoundError(f"AOI not found: {aoi_path}")
    gdf = gpd.read_file(aoi_path)
    try:
        gdf_wgs = gdf.to_crs('EPSG:4326')
    except Exception:
        gdf_wgs = gdf
    minx, miny, maxx, maxy = gdf_wgs.total_bounds

    if BBox is None or CRS is None:
        # sentinelhub not importable; return None config and raw bbox list
        return cfg, [minx, miny, maxx, maxy]

    aoi_bbox = BBox(bbox=[minx, miny, maxx, maxy], crs=CRS.WGS84)
    return cfg, aoi_bbox


# Expose normalize for tests
def _exposed_normalize_s1_props(item: dict) -> dict:
    return _normalize_s1_props(item)

__all__.extend(["load_config_and_bbox", "_exposed_normalize_s1_props"])


def create_initial_sh_config(path: Optional[str] = None, prompt: bool = True) -> str:
    """Create a minimal Sentinel Hub TOML config for first-time setup.

    - If `path` is None, writes to ~/.config/sentinelhub/config.toml
    - If `prompt` is True and values are not found in environment, interactively prompt the user
      for `sh_client_id` and `sh_client_secret`. Non-interactive environments fall back to env-only.

    Returns the path to the written file.
    """
    import getpass

    if path is None:
        path = os.path.expanduser("~/.config/sentinelhub/config.toml")
    else:
        path = os.path.expanduser(path)

    cfg_dir = os.path.dirname(path)
    os.makedirs(cfg_dir, exist_ok=True)

    # Try to pick values from environment first
    env_client = os.getenv("SH_CLIENT_ID")
    env_secret = os.getenv("SH_CLIENT_SECRET")
    env_instance = os.getenv("SH_INSTANCE_ID") or os.getenv("SH_INSTANCE")
    env_base = os.getenv("SH_BASE_URL")

    client_id = env_client
    client_secret = env_secret
    instance_id = env_instance
    base_url = env_base

    if prompt:
        try:
            if not client_id:
                client_id = input("Enter Sentinel Hub client id (sh_client_id): ").strip()
            if not client_secret:
                client_secret = getpass.getpass("Enter Sentinel Hub client secret (sh_client_secret): ").strip()
            if not instance_id:
                instance_id = input("Optional: instance id (press Enter to skip): ").strip() or None
            if not base_url:
                base_url = input("Optional: base URL (press Enter to skip): ").strip() or None
        except Exception:
            # non-interactive environment; fall back to env-only
            pass

    # minimal TOML writing
    lines = []
    lines.append('# Sentinel Hub config generated by sentinel_queryutils_listings.create_initial_sh_config')
    lines.append('[default]')
    if client_id:
        lines.append(f'sh_client_id = "{client_id}"')
    if client_secret:
        lines.append(f'sh_client_secret = "{client_secret}"')
    if instance_id:
        lines.append(f'instance_id = "{instance_id}"')
    if base_url:
        lines.append(f'sh_base_url = "{base_url}"')

    # allow writing common token URL override for certain hosts (CDSE)
    token_url = os.getenv('SH_TOKEN_URL')
    if token_url:
        lines.append(f'sh_token_url = "{token_url}"')

    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')

    return path


def create_cdse_config(path: Optional[str] = None, prompt: bool = True) -> str:
    """Convenience helper to create a config.toml prefilled for the CDSE Sentinel Hub instance.

    It sets sh_token_url and sh_base_url to CDSE defaults and then calls
    create_initial_sh_config to write the file (interactive prompts collect client id/secret).
    """
    cdse_token = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    cdse_base = "https://sh.dataspace.copernicus.eu"
    # Temporarily export these so create_initial_sh_config will pick them up
    os.environ.setdefault('SH_TOKEN_URL', cdse_token)
    os.environ.setdefault('SH_BASE_URL', cdse_base)
    return create_initial_sh_config(path=path, prompt=prompt)


def map_sar_to_true_color(
    sar_metadata_path: str,
    true_color_root: str,
    out_json: str,
    spatial_tolerance_km: float = 5.0,
    time_tolerance_days: float = 3.0,
):
    """Map each SAR metadata item to one or more true-color items.

    Parameters
    - sar_metadata_path: path to SAR metadata JSON (list of STAC features)
    - true_color_root: directory containing true-color image-list JSON files (will be searched recursively)
    - out_json: path to write the mapping JSON
    - spatial_tolerance_km: match true-color items whose centroid is within this many kilometers of the SAR centroid
    - time_tolerance_days: match true-color items within this many days of the SAR acquisition

    The output JSON will be a list of mappings like:
      {
        "sar_id": "...",
        "sar_datetime": "...",
        "matches": [
           {"true_id": "...", "distance_m": 1234.5, "time_diff_s": 3600},
           ...
        ]
      }
    """
    import glob
    from shapely.geometry import shape
    import pandas as pd

    logging.info("Loading SAR metadata from %s", sar_metadata_path)
    with open(sar_metadata_path, "r", encoding="utf-8") as fh:
        sar_list = json.load(fh)

    # Load all true-color JSON files under true_color_root
    tc_files = list(glob.glob(os.path.join(true_color_root, "**", "*.json"), recursive=True))
    true_list = []
    for tf in tc_files:
        try:
            with open(tf, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    true_list.extend(data)
        except Exception:
            logging.debug("Skipping unreadable true color file: %s", tf)

    # Helper to build GeoDataFrame from STAC features
    def _build_gdf(items):
        rows = []
        for it in items:
            fid = it.get("id") or (it.get("properties", {}) or {}).get("identifier")
            geom = it.get("geometry")
            if not geom:
                continue
            try:
                geom_shp = shape(geom)
            except Exception:
                continue
            props = it.get("properties", {})
            dt = props.get("datetime") or props.get("acquisitionDate") or props.get("date")
            try:
                dt_parsed = pd.to_datetime(dt) if dt is not None else None
            except Exception:
                dt_parsed = None
            rows.append({"id": fid, "datetime": dt_parsed, "geometry": geom_shp})
        if not rows:
            return gpd.GeoDataFrame(columns=["id", "datetime", "geometry"]).set_geometry("geometry")
        gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        return gdf

    sar_gdf = _build_gdf(sar_list)
    true_gdf = _build_gdf(true_list)

    if sar_gdf.empty:
        logging.warning("No SAR metadata features found in %s", sar_metadata_path)
    if true_gdf.empty:
        logging.warning("No true-color metadata features found under %s", true_color_root)

    # Prepare centroids in metric CRS for distance calculations
    sar_gdf_m = sar_gdf.to_crs("EPSG:3857")
    true_gdf_m = true_gdf.to_crs("EPSG:3857")
    sar_centroids = sar_gdf_m.geometry.centroid
    true_centroids = true_gdf_m.geometry.centroid

    results = []
    tol_m = spatial_tolerance_km * 1000.0
    max_time_s = time_tolerance_days * 86400.0

    # Precompute arrays for speed
    true_ids = true_gdf["id"].tolist()
    true_dt = true_gdf["datetime"].tolist()
    true_coords = [ (pt.x, pt.y) for pt in true_centroids ]

    for idx, row in sar_gdf.iterrows():
        sar_id = row["id"]
        sar_dt = row["datetime"]
        sar_cent = sar_centroids.iloc[idx]
        matches = []
        # brute force compare — datasets expected to be modest in size
        for j, tid in enumerate(true_ids):
            tcent = true_centroids.iloc[j]
            dist = sar_cent.distance(tcent)
            if dist <= tol_m:
                tdt = true_dt[j]
                time_diff = None
                if sar_dt is not None and tdt is not None:
                    time_diff = abs((pd.Timestamp(sar_dt) - pd.Timestamp(tdt)).total_seconds())
                # If time information present, filter by time tolerance
                if time_diff is not None and time_diff > max_time_s:
                    continue
                # Accept match
                matches.append({"true_id": tid, "distance_m": float(dist), "time_diff_s": time_diff})
        results.append({"sar_id": sar_id, "sar_datetime": str(sar_dt), "matches": matches})

    # Save mapping
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    logging.info("Wrote SAR->true-color mapping to %s (items: %d)", out_json, len(results))
    return results


def map_sar_to_true_color_by_intersection(
    sar_metadata_path: str,
    true_color_root: str,
    out_json: str,
    buffer_meters: float = 5000.0,
    time_tolerance_days: float = 3.0,
):
    """Map SAR items to true-color items by polygon intersection with optional buffering.

    - buffer_meters: buffer SAR polygons (in meters) before testing intersection with true-color polygons.
    - time_tolerance_days: maximum allowed time difference in days.
    """
    import glob
    from shapely.geometry import shape
    import pandas as pd

    logging.info("Loading SAR metadata from %s", sar_metadata_path)
    with open(sar_metadata_path, "r", encoding="utf-8") as fh:
        sar_list = json.load(fh)

    tc_files = list(glob.glob(os.path.join(true_color_root, "**", "*.json"), recursive=True))
    true_list = []
    for tf in tc_files:
        try:
            with open(tf, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    true_list.extend(data)
        except Exception:
            logging.debug("Skipping unreadable true color file: %s", tf)

    def _build(items):
        rows = []
        for it in items:
            fid = it.get("id")
            geom = it.get("geometry")
            if not geom:
                continue
            try:
                geom_shp = shape(geom)
            except Exception:
                continue
            props = it.get("properties", {})
            dt = props.get("datetime")
            try:
                dt_parsed = pd.to_datetime(dt) if dt is not None else None
            except Exception:
                dt_parsed = None
            rows.append({"id": fid, "datetime": dt_parsed, "geometry": geom_shp})
        if not rows:
            return gpd.GeoDataFrame(columns=["id", "datetime", "geometry"]).set_geometry("geometry")
        gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        return gdf

    sar_gdf = _build(sar_list)
    true_gdf = _build(true_list)

    # Project to metric CRS for buffering/intersection
    sar_m = sar_gdf.to_crs("EPSG:3857")
    true_m = true_gdf.to_crs("EPSG:3857")

    max_time_s = time_tolerance_days * 86400.0
    results = []

    # Build spatial index for true-color polygons
    if not true_m.empty:
        true_sindex = true_m.sindex
    else:
        true_sindex = None

    for idx, srow in sar_m.iterrows():
        sid = srow["id"]
        sgeom = srow["geometry"]
        sdt = srow["datetime"]
        # buffer SAR geometry
        try:
            sbuf = sgeom.buffer(buffer_meters)
        except Exception:
            sbuf = sgeom

        matches = []
        if true_sindex is not None:
            # candidate indices from bounding box
            candidate_idx = list(true_sindex.intersection(sbuf.bounds))
            for j in candidate_idx:
                trow = true_m.iloc[j]
                tgeom = trow["geometry"]
                if not sbuf.intersects(tgeom):
                    continue
                tdt = trow["datetime"]
                time_diff = None
                if sdt is not None and tdt is not None:
                    time_diff = abs((pd.Timestamp(sdt) - pd.Timestamp(tdt)).total_seconds())
                    if time_diff > max_time_s:
                        continue
                matches.append({"true_id": trow["id"], "time_diff_s": time_diff})

        results.append({"sar_id": sid, "sar_datetime": str(sdt), "matches": matches})

    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    logging.info("Wrote SAR->true-color intersection mapping to %s (items: %d)", out_json, len(results))
    return results
