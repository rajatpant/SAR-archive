"""
Stage 1: Data Download Agent

Purpose: Acquire raw satellite data from external sources (Copernicus S3).
No business logic, no filtering, no metadata enrichment at this stage.

Determinism Requirements:
- Same input parameters → same files downloaded
- Download manifest tracks completion state
- Partial downloads resume cleanly
- No ML-goal-driven filtering

Config Schema:
{
    "aoi_geojson": "Nizhnesvirsky_lower_bay.geojson",  # optional; overrides bbox
    "bbox": [minx, miny, maxx, maxy],  # optional if aoi_geojson provided
    "date_range": ["2022-01-01", "2023-12-31"],
    "sensor": "sentinel-1" | "sentinel-2",
    "output_dir": "outputs/20260307",
    "max_items": 100,  # optional limit
    "skip_existing": true,  # skip already-downloaded files
    "sentinelhub_config": "configs/sentinelhub_credentials.json"  # optional
}
"""
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import json

# Add project root to path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult
from model.s3_utils import get_s3_client, list_s3_prefix, test_s3_connectivity, parse_s3_uri, download_s3_object

# Optional: Try to import STAC search - will be None if not available
try:
    from src.Query.Sentinel.SentinelHub.sentinel_queryutils_listings import get_sar_listings, get_true_color_images_listings
    STAC_AVAILABLE = True
except ImportError:
    STAC_AVAILABLE = False
    get_sar_listings = None
    get_true_color_images_listings = None


class DataDownloadAgent(PipelineAgent):
    """
    Stage 1 Agent: Download raw satellite data from Copernicus.
    
    Responsibilities:
    - Query STAC for scenes matching bbox + date range
    - Download .SAFE packages from S3
    - Validate file integrity (size, basic checks)
    - Track completed downloads in manifest
    - Support resume from partial downloads
    """
    
    def __init__(self, config: Dict[str, Any], dry_run: bool = False):
        """Initialize Stage 1 agent with S3 client."""
        super().__init__(config, dry_run)
        
        # Initialize S3 client for downloads
        self.s3_client = get_s3_client()
        
        # Test S3 connectivity on initialization
        self.s3_connected = False
        self.s3_error = None
        
    def _apply_manifest_overrides(self) -> None:
        """Override output_dir, aoi_geojson, bbox, date_range from run_manifest."""
        _m_out = self._manifest_abs("stages", "stage1", "output")
        if _m_out is not None:
            self.config["output_dir"] = _m_out
        _m_geo = self._manifest_abs("static", "bbox_geojson")
        if _m_geo is not None:
            self.config["aoi_geojson"] = _m_geo
        _rcfg = self._manifest_region_cfg()
        if _rcfg is not None:
            self.config["bbox"]       = _rcfg.get("s3_query_bbox", self.config.get("bbox"))
            self.config["date_range"] = _rcfg.get("dev_date_range", self.config.get("date_range"))

    def get_stage_number(self) -> int:
        return 1
    
    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """Validate required config parameters."""
        errors = []
        required = ["date_range", "sensor", "output_dir"]
        
        for key in required:
            if key not in self.config:
                errors.append(f"Missing required config key: {key}")
        
        aoi_geojson = self.config.get("aoi_geojson")
        bbox = self.config.get("bbox")
        if not aoi_geojson and not bbox:
            errors.append("Either aoi_geojson or bbox is required")

        if aoi_geojson:
            geojson_path = self._resolve_geojson_path(aoi_geojson)
            if not geojson_path.exists():
                errors.append(f"aoi_geojson not found: {geojson_path}")
        elif bbox is not None:
            if not isinstance(bbox, list) or len(bbox) != 4:
                errors.append("bbox must be [minx, miny, maxx, maxy]")
        
        # Validate date_range format
        if "date_range" in self.config:
            date_range = self.config["date_range"]
            if not isinstance(date_range, list) or len(date_range) != 2:
                errors.append("date_range must be [start_date, end_date]")
        
        # Validate sensor
        if "sensor" in self.config:
            if self.config["sensor"] not in ["sentinel-1", "sentinel-2"]:
                errors.append("sensor must be 'sentinel-1' or 'sentinel-2'")

        sh_cfg_path = self.config.get("sentinelhub_config")
        if sh_cfg_path:
            sh_cfg_path = Path(sh_cfg_path)
            if not sh_cfg_path.is_absolute():
                sh_cfg_path = ROOT / sh_cfg_path
            if not sh_cfg_path.exists():
                errors.append(f"sentinelhub_config not found: {sh_cfg_path}")
        
        # Test S3 connectivity
        print("  Testing S3 connectivity...")
        self.s3_connected, self.s3_error = test_s3_connectivity(self.s3_client)
        
        if not self.s3_connected:
            errors.append(f"S3 connectivity failed: {self.s3_error}")
            print(f"  ❌ S3 connection failed: {self.s3_error}")
        else:
            print("  ✅ S3 connection successful")
        
        # Warn if STAC search is not available (for testing scenarios)
        if not STAC_AVAILABLE:
            print("  ⚠️  STAC search not available (sentinelhub not installed)")
            print("     Stage 1 will work with S3 URIs provided directly in config")
        
        return len(errors) == 0, errors
    
    def check_idempotency(self) -> Tuple[bool, str]:
        """
        Check if download can be skipped (already completed).
        
        A download can be skipped if:
        - Download manifest exists
        - All items in manifest have "downloaded": true
        - Config hash matches
        """
        output_dir = Path(self.config["output_dir"])
        manifest_path = output_dir / "stage1_datadownloadagent_manifest.json"
        
        if not manifest_path.exists():
            return False, "No manifest found"
        
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            
            # Check config hash
            current_hash = self.compute_config_hash()
            manifest_hash = manifest.get("config_hash", "")
            
            if current_hash != manifest_hash:
                return False, "Config changed since last run"
            
            # Check if all downloads completed
            result_data = manifest.get("result", {})
            stats = result_data.get("stats", {})
            
            if stats.get("phase") == "completed":
                downloaded = stats.get("downloaded_count", 0)
                total = stats.get("total_items", 0)
                if downloaded == total and total > 0:
                    return True, f"All {total} items already downloaded"
            
            return False, "Download incomplete or failed previously"
            
        except Exception as e:
            return False, f"Error reading manifest: {e}"
    
    def execute(self) -> AgentResult:
        """
        Execute the download process.
        
        Steps:
        1. Query STAC for scene IDs matching criteria
        2. For each scene, check if already downloaded
        3. Download from S3 if needed
        4. Validate file integrity
        5. Track progress in manifest
        """
        output_dir = Path(self.config["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        
        aoi_geojson = self.config.get("aoi_geojson")
        if aoi_geojson:
            bbox = self._load_bbox_from_geojson(aoi_geojson)
            print(f"  AOI GeoJSON: {aoi_geojson}")
        else:
            bbox = self.config["bbox"]
        date_range = self.config["date_range"]
        sensor = self.config["sensor"]
        max_items = self.config.get("max_items", None)
        skip_existing = self.config.get("skip_existing", True)
        
        outputs = []
        errors = []
        warnings = []
        
        try:
            # Step 1: Query STAC for items
            print(f"  Querying STAC for {sensor} items...")
            print(f"  BBox: {bbox}")
            print(f"  Date Range: {date_range}")
            
            # Query STAC for items
            items = self._search_items(bbox, date_range, sensor, max_items or 10)
            
            if items is None:
                errors.append("STAC search failed - see warnings for details")
                return AgentResult(
                    success=False,
                    stage=self.stage_name,
                    outputs=[],
                    errors=errors,
                    warnings=warnings,
                    stats={"phase": "stac_search_failed"}
                )
            
            print(f"  Found {len(items)} items")
            
            if not items:
                warnings.append("No items found matching criteria")
                return AgentResult(
                    success=True,
                    stage=self.stage_name,
                    outputs=[],
                    errors=[],
                    warnings=warnings,
                    stats={
                        "phase": "completed",
                        "total_items": 0,
                        "downloaded_count": 0,
                        "skipped_count": 0
                    }
                )
            
            # Step 2-4: Download each item
            downloaded_count = 0
            skipped_count = 0
            
            for i, item in enumerate(items, 1):
                item_id = item.get("id", f"unknown_{i}")
                print(f"  [{i}/{len(items)}] Processing: {item_id}")
                
                # Extract S3 URI from item
                s3_uri = self._extract_s3_uri(item)
                
                if not s3_uri:
                    errors.append(f"{item_id}: No S3 URI found")
                    continue
                
                # Determine local path
                local_path = output_dir / item_id
                
                # Check if already exists and is non-empty
                # An empty directory = interrupted prior download — must retry
                if skip_existing and local_path.exists():
                    if local_path.is_dir() and not any(local_path.rglob("*")):
                        print(f"    ⚠️  Empty directory found — removing and re-downloading")
                        import shutil as _shutil
                        _shutil.rmtree(local_path)
                    else:
                        print(f"    Skipping (already exists)")
                        skipped_count += 1
                        outputs.append(str(local_path))
                        continue
                
                # Download from S3
                try:
                    print(f"    Downloading from {s3_uri}...")
                    self._download_s3_package(s3_uri, local_path)
                    downloaded_count += 1
                    outputs.append(str(local_path))
                    print(f"    ✅ Downloaded")
                except Exception as e:
                    error_msg = f"{item_id}: Download failed - {e}"
                    errors.append(error_msg)
                    print(f"    ❌ Failed: {e}")
            
            # Summary
            stats = {
                "phase": "completed",
                "total_items": len(items),
                "downloaded_count": downloaded_count,
                "skipped_count": skipped_count,
                "failed_count": len(errors)
            }
            
            success = len(errors) < len(items) * 0.5  # Success if < 50% failed
            
            return AgentResult(
                success=success,
                stage=self.stage_name,
                outputs=outputs,
                errors=errors,
                warnings=warnings,
                stats=stats
            )
            
        except Exception as e:
            return AgentResult(
                success=False,
                stage=self.stage_name,
                outputs=outputs,
                errors=[f"Fatal error: {e}"],
                warnings=warnings,
                stats={"phase": "failed", "error": str(e)}
            )
    
    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """
        Validate downloaded files.
        
        Checks:
        - Files exist
        - Files have non-zero size
        - Directory structure is intact
        """
        errors = []
        
        for output_path in result.outputs:
            path = Path(output_path)
            
            if not path.exists():
                errors.append(f"Output missing: {output_path}")
                continue
            
            # Check if it's a directory (SAFE packages)
            if path.is_dir():
                # Check that directory has files
                files = list(path.rglob("*"))
                if not files:
                    errors.append(f"Empty directory: {output_path}")
            
            # Check if it's a file
            elif path.is_file():
                if path.stat().st_size == 0:
                    errors.append(f"Zero-size file: {output_path}")
        
        return len(errors) == 0, errors
    
    def _extract_s3_uri(self, item: Dict[str, Any]) -> Optional[str]:
        """Extract S3 URI from STAC item."""
        # Try alternate.s3.href first
        for asset_key, asset in item.get("assets", {}).items():
            if not isinstance(asset, dict):
                continue
            s3_href = asset.get("alternate", {}).get("s3", {}).get("href")
            if s3_href and s3_href.startswith("s3://"):
                return self._normalize_s3_uri(s3_href)
        
        # Fallback to direct href
        for asset_key, asset in item.get("assets", {}).items():
            if not isinstance(asset, dict):
                continue
            href = asset.get("href", "")
            if href.startswith("s3://"):
                return self._normalize_s3_uri(href)
        
        return None

    def _normalize_s3_uri(self, s3_uri: str) -> str:
        if s3_uri.startswith("s3://EODATA/"):
            return "s3://eodata/" + s3_uri[len("s3://EODATA/"):]
        return s3_uri
    
    def _search_items(self, bbox: List[float], date_range: List[str], 
                      sensor: str, max_items: int) -> Optional[List[Dict[str, Any]]]:
        """
        Search for Sentinel items via STAC/Catalog.
        
        Returns None if search fails, empty list if no items found.
        """
        # If items provided directly in config, use those
        if "items" in self.config:
            return self.config["items"]
        
        # If STAC not available, return empty list (will be handled upstream)
        if not STAC_AVAILABLE:
            print("  ⚠️  No STAC client available and no items in config")
            return []
        
        try:
            # Use existing listing functions
            from sentinelhub.geometry import BBox
            from sentinelhub.constants import CRS
            
            # Convert bbox to BBox object
            bbox_tuple = tuple(bbox)  # type: ignore
            bbox_obj = BBox(bbox=bbox_tuple, crs=CRS.WGS84)  # type: ignore
            
            # Convert date_range to tuple
            time_interval = tuple(date_range)
            
            # Mock config for now (would need proper SHConfig in production)
            config = self.config
            
            if sensor == "sentinel-1":
                if get_sar_listings is None:
                    raise ImportError("get_sar_listings not available")
                items = get_sar_listings(
                    aoi_bbox=bbox_obj,
                    time_interval=time_interval,
                    config=config,
                    max_items=max_items,
                    include_assets=True
                )
            else:  # sentinel-2
                if get_true_color_images_listings is None:
                    raise ImportError("get_true_color_images_listings not available")
                items = get_true_color_images_listings(
                    aoi_bbox=bbox_obj,
                    aoi_size=None,
                    config=config,
                    time_interval=time_interval
                )[:max_items]
            
            return items
        
        except Exception as e:
            print(f"  ❌ STAC search failed: {e}")
            return None

    def _resolve_geojson_path(self, geojson_path: str) -> Path:
        path = Path(geojson_path)
        if not path.is_absolute():
            path = ROOT / path
        return path

    def _load_bbox_from_geojson(self, geojson_path: str) -> List[float]:
        path = self._resolve_geojson_path(geojson_path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        coords: List[Tuple[float, float]] = []

        def _iter_coords(obj):
            if isinstance(obj, (list, tuple)):
                if len(obj) == 2 and all(isinstance(v, (int, float)) for v in obj):
                    coords.append((float(obj[0]), float(obj[1])))
                else:
                    for item in obj:
                        _iter_coords(item)

        def _extract(geom):
            if not isinstance(geom, dict):
                return
            _iter_coords(geom.get("coordinates"))

        if isinstance(data, dict) and data.get("type") == "FeatureCollection":
            for feat in data.get("features", []):
                _extract(feat.get("geometry"))
        elif isinstance(data, dict) and data.get("type") == "Feature":
            _extract(data.get("geometry"))
        else:
            _extract(data)

        if not coords:
            raise ValueError(f"No coordinates found in AOI GeoJSON: {path}")

        xs, ys = zip(*coords)
        return [min(xs), min(ys), max(xs), max(ys)]
    
    def _download_s3_package(self, s3_uri: str, local_path: Path):
        """
        Download S3 package to local path.
        
        Args:
            s3_uri: S3 URI (e.g. s3://eodata/Sentinel-1/...)
            local_path: Local directory to download to
        """
        # Parse S3 URI
        if not s3_uri.startswith("s3://"):
            raise ValueError(f"Invalid S3 URI: {s3_uri}")
        
        bucket, key = parse_s3_uri(s3_uri)
        
        # Determine if this is a directory prefix or single file
        # Heuristic: if it ends in .SAFE or .SAFE/ or doesn't have an extension, it's a directory
        # Also check for common satellite metadata files
        is_safe_package = '.SAFE' in key
        path_parts = key.rstrip('/').split('/')
        last_part = path_parts[-1] if path_parts else ''
        has_extension = '.' in last_part and not last_part.startswith('.')
        
        is_directory = key.endswith('/') or not has_extension or is_safe_package
        
        if is_directory:
            # For .SAFE packages, we might want to download just the manifest or a subset in dev mode
            # but for now we follow the recursive path.
            # Special case: if it's a .SAFE package, try to see if it's actually an object itself (unlikely but possible)
            # or if we should just go straight to directory listing.
            self._download_s3_directory(bucket, key, local_path)
        else:
            # Attempt to download single file, with fallback to directory if 404
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Use head_object to verify file existence
                self.s3_client.head_object(Bucket=bucket, Key=key)
                download_s3_object(self.s3_client, s3_uri, str(local_path))
            except Exception as e:
                # If 404, it might be a directory prefix even if it looks like a file
                if "404" in str(e):
                    # Try listing objects with this key as a prefix
                    # We add a trailing slash to the prefix for safer listing if missing
                    search_prefix = key if key.endswith('/') else key + '/'
                    print(f"    File not found ({key}), trying directory prefix {search_prefix}...")
                    response = self.s3_client.list_objects_v2(Bucket=bucket, Prefix=search_prefix, MaxKeys=1)
                    if 'Contents' in response:
                        print(f"    Found directory contents for prefix {search_prefix}")
                        self._download_s3_directory(bucket, search_prefix, local_path)
                    else:
                        raise FileNotFoundError(f"S3 path {s3_uri} not found as file or directory")
                else:
                    raise e
    
    def _download_s3_directory(self, bucket: str, prefix: str, local_base: Path):
        """
        Recursively download all objects under an S3 prefix.
        
        Args:
            bucket: S3 bucket name
            prefix: S3 key prefix (directory path)
            local_base: Local base directory
        """
        local_base.mkdir(parents=True, exist_ok=True)
        
        # Ensure prefix ends with '/' for deterministic relative path calculation
        if prefix and not prefix.endswith('/') and not '.' in prefix.split('/')[-1]:
            prefix += '/'
            
        # List all objects under prefix
        paginator = self.s3_client.get_paginator('list_objects_v2')
        page_count = 0
        object_count = 0
        
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            page_count += 1
            
            for obj in page.get('Contents', []):
                key = obj['Key']
                
                # Compute relative path
                # Use a safer relative path calculation
                if key.startswith(prefix):
                    rel_path = key[len(prefix):].lstrip('/')
                else:
                    # Fallback for prefix-only matches
                    rel_path = Path(key).name
                    
                if not rel_path:  # Skip the prefix directory itself
                    continue
                
                # Heuristic: Dev vs Prod Disk Policy
                # If we are in "production" mode (many items), we only download XMLs/metadata
                # This aligns with .github/copilot-instructions.md:
                # "Production/Full Test: NEVER download full S1/S2 images (>=1GB). ALWAYS stream"
                is_large_image = rel_path.lower().endswith(('.jp2', '.tif', '.tiff'))
                if self.config.get("max_items", 0) > 1 and is_large_image:
                    # Skip image files in prod mode to preserve disk/memory
                    # We will use rasterio windowed reads (streaming) in later stages
                    continue

                local_file = local_base / rel_path
                
                # Skip if file exists and has correct size
                if local_file.exists() and local_file.stat().st_size == obj['Size']:
                    continue
                
                # Download file
                local_file.parent.mkdir(parents=True, exist_ok=True)
                s3_uri = f"s3://{bucket}/{key}"
                download_s3_object(self.s3_client, s3_uri, str(local_file))
                object_count += 1
        
        # If we are only downloading metadata, this is still a success for Stage 1
        print(f"    Processed {object_count} objects ({page_count} pages) under {prefix}")


def main():
    """CLI entry point for Stage 1 agent."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Stage 1: Data Download")
    parser.add_argument("--config", required=True, help="Path to download config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    parser.add_argument("--max_items", type=int, help="Maximum number of items to download")
    args = parser.parse_args()
    
    with open(args.config) as f:
        full_config = json.load(f)
    
    # Extract stage-specific config if it exists, otherwise use the whole thing
    if "stage1_download" in full_config:
        config = full_config["stage1_download"]
        # Merge global if available
        if "global" in full_config:
            for k, v in full_config["global"].items():
                if k not in config:
                    config[k] = v
    else:
        config = full_config
    
    if args.max_items is not None:
        config["max_items"] = args.max_items
    
    agent = DataDownloadAgent(config=config, dry_run=args.dry_run)
    result = agent.run()
    
    print(f"\n{'='*70}")
    print(f"Stage 1 Result: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"Downloaded: {result.stats.get('downloaded_count', 0)}")
    print(f"Skipped: {result.stats.get('skipped_count', 0)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Manifest: {result.manifest_path}")
    print(f"{'='*70}\n")
    
    exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
