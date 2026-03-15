"""
Stage 2: Universal Enrichment Agent

Purpose: Augment metadata with universal sensor characteristics (orbit, polarization, cloud cover).
No ML-goal-driven filtering at this stage.

Determinism Requirements:
- Same scene ID → same enriched JSON (modulo timestamp)
- Enrichment marks "enrich ed": true in JSON root
- Idempotent: safe to re-run on already-enriched scenes
- No ML-goal vocabulary (e.g., "suitability_for_urban") at this stage

Config Schema:
{
    "input_dir": "metadata_by_item",  # Where metadata JSONs are
    "output_dir": "metadata_by_item",  # Where to write .enriched.json
    "s3_prefix": "s3://eodata/Sentinel-1/",
    "try_s3_discovery": true,
    "stac_token": null,  # Optional STAC bearer token
    "limit": 0  # 0 = all files
}
"""
import sys
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import json
import glob
import os
import tempfile
import shutil

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult
from model.s3_utils import get_s3_client


class UniversalEnrichmentAgent(PipelineAgent):
    """
    Stage 2 Agent: Enrich metadata with universal sensor characteristics.
    
    Integrates logic from:
    - tools/enrich_all.py
    - src/enrich_one_from_mapping.py
    - src/enrich_metadata_items.py
    """
    
    def _apply_manifest_overrides(self) -> None:
        """Override input_dir (Stage-1 outputs) and output_dir (static enriched) from run_manifest."""
        _m_in = self._manifest_abs("stages", "stage1", "output")
        if _m_in is not None:
            self.config["input_dir"] = _m_in
        _m_out = self._manifest_abs("static", "s1_enriched_dir")
        if _m_out is not None:
            self.config["output_dir"] = _m_out

    def get_stage_number(self) -> int:
        return 2
    
    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """Validate that input directory exists and contains metadata files."""
        errors = []
        
        if "input_dir" not in self.config:
            errors.append("Missing required config key: input_dir")
        else:
            input_dir = Path(self.config["input_dir"])
            if not input_dir.exists():
                errors.append(f"Input directory does not exist: {input_dir}")
            else:
                # Check for metadata files
                pattern = str(input_dir / "*.json")
                files = [
                    f for f in glob.glob(pattern)
                    if not f.endswith('.enriched.json')
                    and not os.path.basename(f).startswith('sar_metadata')
                    and os.path.basename(f) != 'sar_to_true_color_mapping.json'
                ]
                if not files:
                    errors.append(f"No metadata JSON files found in {input_dir}")

        # S3 connectivity pre-check (fail fast before batch enrichment)
        if self.config.get("try_s3_discovery", False) and not self.dry_run:
            s3_prefix = self.config.get("s3_prefix", "s3://eodata/")
            bucket = s3_prefix.replace("s3://", "").split("/")[0]
            try:
                s3 = get_s3_client()
                # Light check: HEAD the bucket (list with max 1 key)
                s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            except Exception as e:
                errors.append(
                    f"S3 connectivity test failed for bucket '{bucket}': {e}. "
                    f"Check AWS credentials and network access before enrichment."
                )

        return len(errors) == 0, errors
    
    def check_idempotency(self) -> Tuple[bool, str]:
        """Check if enrichment already completed via manifest."""
        stage_num = self.get_stage_number()
        output_dir = Path(self.config.get("output_dir", self.config.get("input_dir", "")))
        
        # Look for existing manifest
        manifest_files = glob.glob(str(output_dir / f"stage{stage_num}_*.json"))
        if manifest_files:
            latest_manifest = max(manifest_files, key=os.path.getmtime)
            try:
                with open(latest_manifest) as f:
                    manifest = json.load(f)
                
                # Check config hash
                current_hash = self.compute_config_hash()
                stored_hash = manifest.get("config_hash")
                
                if stored_hash == current_hash:
                    result = manifest.get("result", {})
                    if result.get("success"):
                        stats = result.get("stats", {})
                        count = stats.get("files_enriched", 0)
                        return True, f"Already enriched {count} files (config hash match)"
            except Exception as e:
                pass  # Manifest corrupted, re-run
        
        return False, "Enrichment needed"
    
    def execute(self) -> AgentResult:
        """Execute universal metadata enrichment."""
        input_dir = Path(self.config["input_dir"])
        output_dir = Path(self.config.get("output_dir", input_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        
        use_stac_token = self.config.get("stac_token") is not None
        stac_token = self.config.get("stac_token")
        s3_prefix = self.config.get("s3_prefix", "s3://eodata/Sentinel-1/")
        try_s3_discovery = self.config.get("try_s3_discovery", True)
        limit = self.config.get("limit", 0)
        
        # Find all metadata files (not already enriched)
        pattern = str(input_dir / "*.json")
        all_files = glob.glob(pattern)
        files = [
            f for f in all_files 
            if not f.endswith('.enriched.json') 
            and not os.path.basename(f).startswith('sar_metadata')
            and os.path.basename(f) != 'sar_to_true_color_mapping.json'
        ]
        # Also discover .SAFE directories — treat as metadata inputs via direct catalog sync
        safe_dirs = [
            str(p) for p in input_dir.iterdir()
            if p.is_dir() and p.name.endswith('.SAFE')
            and not (output_dir / f"{p.name}.enriched.json").exists()
        ]
        files = sorted(files) + sorted(safe_dirs)
        
        if limit > 0:
            files = files[:limit]
        
        if not files:
            return AgentResult(
                success=True,
                stage=self.stage_name,
                outputs=[],
                errors=[],
                warnings=[f"No files to enrich in {input_dir}"],
                stats={"files_processed": 0, "files_enriched": 0}
            )
        
        print(f"  Found {len(files)} files to enrich")
        
        successes = 0
        failures = 0
        errors = []
        output_files = []
        
        # Get S3 client for discovery
        s3_client = None
        if try_s3_discovery:
            try:
                s3_client = get_s3_client()
            except Exception as e:
                errors.append(f"Failed to initialize S3 client: {e}")
                try_s3_discovery = False
        
        for filepath in files:
            basename = os.path.basename(filepath)
            try:
                if os.path.isdir(filepath):
                    # .SAFE directories: bypass JSON parsing, use direct catalog sync
                    ok, primary = self._enrich_safe_directory(filepath, output_dir)
                elif use_stac_token:
                    ok, primary = self._enrich_with_stac(filepath, stac_token, output_dir)
                else:
                    ok, primary = self._enrich_local(
                        filepath, output_dir, 
                        s3_client=s3_client,
                        try_s3_discovery=try_s3_discovery, 
                        s3_prefix=s3_prefix
                    )
                
                if ok:
                    successes += 1
                    enriched_name = os.path.splitext(basename)[0] + '.enriched.json'
                    output_files.append(str(output_dir / enriched_name))
                    print(f"    ✓ {basename} → {primary[:60] if primary else 'enriched'}...")
                else:
                    failures += 1
                    print(f"    ✗ {basename} could not be enriched")
                    
            except Exception as e:
                failures += 1
                error_msg = f"Failed to enrich {basename}: {e}"
                errors.append(error_msg)
                print(f"    ✗ {error_msg}")
        
        return AgentResult(
            success=(successes > 0),
            stage=self.stage_name,
            outputs=output_files,
            errors=errors,
            warnings=[],
            stats={
                "files_total": len(files),
                "files_enriched": successes,
                "files_failed": failures
            }
        )
    
    def _enrich_safe_directory(self, safe_dir: str, out_dir: Path) -> Tuple[bool, Optional[str]]:
        """Enrich a downloaded .SAFE directory via direct SentinelHubCatalog sync.

        This is the preferred path when Stage 1 downloads .SAFE directories locally.
        Bypasses local XML/JSON parsing by querying CDSE STAC directly.
        """
        from src.Query.Sentinel.SentinelHub.sentinel_queryutils_listings import _load_sh_config
        from sentinelhub import SentinelHubCatalog, BBox, CRS, DataCollection

        safe_name = os.path.basename(safe_dir)
        try:
            sh_config = _load_sh_config(self.config.get("sentinelhub_config"))
            catalog = SentinelHubCatalog(config=sh_config)

            # Extract acquisition date from SAFE name (format: ...T153232...)
            parts = safe_name.split("_")
            timestamp = next((p for p in parts if len(p) == 15 and p[8] == "T"), None)
            if not timestamp:
                return False, None
            date_str = timestamp[:8]
            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            # Use a wide bbox covering CDSE's standard search area for the configured AOI
            bbox = BBox(bbox=[29.0, 59.5, 35.0, 62.0], crs=CRS.WGS84)
            search = catalog.search(
                DataCollection.SENTINEL1_IW,
                bbox=bbox,
                time=(date_fmt, date_fmt),
                fields={"include": ["id", "properties", "geometry", "assets"]}
            )
            results = list(search)

            # Match by ID prefix, stripping _COG suffix
            target_prefix = safe_name.replace(".SAFE", "").replace("_COG", "")
            feature = next((r for r in results if r["id"].startswith(target_prefix)), None)
            if not feature:
                return False, None

            # Resolve primary_asset_href from CDSE assets or derive it
            assets = feature.get("assets", {})
            primary = None
            for key in ["data", "product", "download"]:
                if key in assets:
                    href = assets[key].get("alternate", {}).get("s3", {}).get("href") or assets[key].get("href")
                    if href:
                        primary = href.replace("s3://EODATA", "s3://eodata").replace("s3://EOData", "s3://eodata")
                        break
            if not primary:
                # Heuristic fallback: derive from safe_name and date
                primary = f"s3://eodata/Sentinel-1/SAR/IW_GRDH_1S-COG/{date_fmt.replace('-', '/')}/{safe_name}/"

            enriched_data = {
                "id": safe_name,
                "found": True,
                "feature": feature,
                "enriched": True,
                "kind": "s1",
                "primary_asset_href": primary,
            }
            out_path = out_dir / f"{safe_name}.enriched.json"
            self._atomic_write_json(out_path, enriched_data)
            return True, primary
        except Exception as e:
            print(f"      .SAFE catalog sync failed for {safe_name}: {e}")
            return False, None

    def _enrich_with_stac(self, filepath: str, token: str, out_dir: Path) -> Tuple[bool, Optional[str]]:
        """Enrich using STAC API with bearer token."""
        try:
            from src.Tools.enrich_with_stac_token import enrich_one as stac_enrich_one
            return stac_enrich_one(filepath, token, str(out_dir))
        except Exception as e:
            print(f"      STAC enrichment failed: {e}")
            return False, None
    
    def _enrich_local(self, filepath: str, out_dir: Path, s3_client=None, 
                      try_s3_discovery: bool = True, s3_prefix: str = 's3://eodata/Sentinel-1/') -> Tuple[bool, Optional[str]]:
        """Enrich using local metadata and S3 discovery.
        
        Adapted from tools/enrich_all.py::enrich_local()
        """
        with open(filepath, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        
        if not isinstance(data, dict):
            return False, None
        
        feature = data.get('feature') or data.get('stac') or {}
        primary = self._find_primary_in_local(feature)
        
        # Try S3 discovery if no primary found
        if not primary and try_s3_discovery and s3_client:
            basename = os.path.splitext(os.path.basename(filepath))[0]
            primary = self._discover_via_s3(basename, s3_prefix, s3_client)
        
        # Write enriched JSON
        enriched = dict(data)
        enriched['enriched'] = bool(primary)
        enriched['primary_asset_href'] = primary
        if feature:
            enriched['feature'] = feature
        
        out_name = os.path.splitext(os.path.basename(filepath))[0] + '.enriched.json'
        out_path = out_dir / out_name
        
        # Atomic write
        self._atomic_write_json(out_path, enriched)
        
        return bool(primary), primary
    
    def _find_primary_in_local(self, feature: dict) -> Optional[str]:
        """Find primary asset href in feature dict."""
        # Try common locations for asset hrefs
        assets = feature.get('assets') or {}
        if isinstance(assets, dict):
            # Prefer named data/measurement assets
            for k in ('data', 'measurement', 'safe', 'visual'):
                a = assets.get(k)
                if isinstance(a, dict) and a.get('href'):
                    return a.get('href')
            # Fallback: any asset href
            for k, v in assets.items():
                if isinstance(v, dict) and v.get('href'):
                    return v.get('href')
                if isinstance(v, str) and (v.startswith('s3://') or v.startswith('http')):
                    return v
        
        # Check properties
        props = feature.get('properties') or {}
        for key in ('primary_asset_href', 's3_href', 'asset_href'):
            if props.get(key):
                return props.get(key)
        
        # Check links
        for l in feature.get('links') or []:
            if isinstance(l, dict) and l.get('href') and l.get('href').startswith('s3://'):
                return l.get('href')
        
        return None
    
    def _discover_via_s3(self, basename: str, s3_prefix: str, s3_client) -> Optional[str]:
        """Discover S3 URI via intelligent prefix search.
        
        Adapted from tools/enrich_all.py date-based discovery logic.
        """
        # Extract date from basename (e.g. 20221124T153240)
        parts = basename.split('_')
        year, month, day = None, None, None
        
        for p in parts:
            if len(p) > 14 and 'T' in p and p[0:8].isdigit():
                year, month, day = p[0:4], p[4:6], p[6:8]
                break
        
        # Use intelligent prefix if date found
        if s3_prefix in ('s3://eodata/Sentinel-1/', 's3://eoddata/Sentinel-1/'):
            if basename.startswith('S2') and year:
                # Sentinel-2 patterns
                for sub in ['MSI/L2A/', 'MSI/L2A_N0204/', 'MSI/L2A_N0500/']:
                    prefix = f's3://eodata/Sentinel-2/{sub}{year}/{month}/{day}/'
                    result = self._find_in_s3_listing(basename, prefix, s3_client)
                    if result:
                        return result
            elif basename.startswith('S1') and year:
                # Sentinel-1 patterns (both standard and COG)
                for sub in ['SAR/IW_GRDH_1S-COG/', 'SAR/IW_GRDH_1S/', 'SAR/SLC/', 'SAR/GRDH/']:
                    prefix = f's3://eodata/Sentinel-1/{sub}{year}/{month}/{day}/'
                    result = self._find_in_s3_listing(basename, prefix, s3_client)
                    if result:
                        return result
        else:
            # Use provided prefix directly
            return self._find_in_s3_listing(basename, s3_prefix, s3_client)
        
        return None
    
    def _find_in_s3_listing(self, basename: str, prefix: str, s3_client) -> Optional[str]:
        """Find object in S3 listing that matches basename."""
        try:
            # Parse S3 URI
            if not prefix.startswith('s3://'):
                return None
            
            bucket_and_prefix = prefix[5:]
            parts = bucket_and_prefix.split('/', 1)
            bucket = parts[0]
            key_prefix = parts[1] if len(parts) > 1 else ''
            
            # List objects with pagination
            paginator = s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix=key_prefix, PaginationConfig={'MaxItems': 1000})
            
            for page in pages:
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if basename in key:
                        return f's3://{bucket}/{key}'
            
            return None
        except Exception as e:
            # Silently fail (don't spam logs)
            return None
    
    def _atomic_write_json(self, path: Path, data: dict) -> None:
        """Write JSON atomically using temp file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=path.name + '.', 
            suffix='.tmp', 
            dir=str(path.parent)
        )
        os.close(fd)
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            shutil.move(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    
    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """Validate enriched JSON files exist and have correct structure."""
        errors = []
        
        for output_file in result.outputs:
            path = Path(output_file)
            if not path.exists():
                errors.append(f"Output file not found: {output_file}")
                continue
            
            try:
                with open(path) as f:
                    data = json.load(f)
                
                # Check required fields
                if 'enriched' not in data:
                    errors.append(f"{output_file}: Missing 'enriched' field")
                if data.get('enriched') and not data.get('primary_asset_href'):
                    errors.append(f"{output_file}: Enriched=true but no primary_asset_href")
            except Exception as e:
                errors.append(f"{output_file}: JSON validation failed: {e}")
        
        return len(errors) == 0, errors


if __name__ == "__main__":
    print("Stage 2 Universal Enrichment Agent")
    print("Use via orchestrator: python -m agents.orchestrator --stage 2")
