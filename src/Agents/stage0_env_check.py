"""
Stage 0: Environment Validation Agent

Purpose: Verify the execution environment is ready before any pipeline stage runs.
Checks Python version, required packages, MPS/CUDA availability, disk space,
and required directories. Emits environment_snapshot.json.

Config Schema:
{
    "output_dir": "outputs/20260307",
    "min_free_disk_gb": 10.0,
    "min_python": [3, 9],
    "required_packages": ["torch", "rasterio", "boto3", "numpy", "pandas"]
}
"""
import sys
import os
import json
import shutil
import importlib
from pathlib import Path
from typing import Tuple, List, Dict, Any
from datetime import datetime

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult

# Packages that are critical (pipeline cannot run without them)
CRITICAL_PACKAGES = [
    "torch", "numpy", "pandas", "rasterio", "boto3",
    "json", "pathlib", "hashlib",
]

# Packages that are optional (warnings only)
OPTIONAL_PACKAGES = [
    "sentinelhub", "matplotlib", "sklearn", "tqdm", "PIL",
]

# Minimum free disk space in GB
DEFAULT_MIN_FREE_GB = 10.0

# Minimum Python version
DEFAULT_MIN_PYTHON = [3, 9]


class EnvironmentCheckAgent(PipelineAgent):
    """Stage 0 Agent: Environment validation before pipeline execution."""

    def get_stage_number(self) -> int:
        return 0

    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """No external inputs required — reads only system state."""
        return True, []

    def check_idempotency(self) -> Tuple[bool, str]:
        """Always re-run — environment can change between sessions."""
        return False, "Environment check always runs"

    def execute(self) -> AgentResult:
        """Run all environment checks and write snapshot."""
        errors: List[str] = []
        warnings: List[str] = []
        stats: Dict[str, Any] = {"phase": "environment_check"}

        # 1. Python version
        py_version = list(sys.version_info[:3])
        min_python = self.config.get("min_python", DEFAULT_MIN_PYTHON)
        stats["python_version"] = ".".join(str(x) for x in py_version)
        stats["python_executable"] = sys.executable
        if py_version[:2] < min_python:
            errors.append(
                f"Python {'.'.join(str(x) for x in py_version)} < "
                f"required {'.'.join(str(x) for x in min_python)}"
            )

        # 2. Package availability
        required = self.config.get("required_packages", CRITICAL_PACKAGES)
        pkg_status: Dict[str, Any] = {}
        for pkg in required:
            try:
                mod = importlib.import_module(pkg)
                version = getattr(mod, "__version__", "unknown")
                pkg_status[pkg] = {"available": True, "version": version}
            except ImportError:
                pkg_status[pkg] = {"available": False, "version": None}
                errors.append(f"Required package '{pkg}' not installed")

        for pkg in OPTIONAL_PACKAGES:
            try:
                mod = importlib.import_module(pkg)
                version = getattr(mod, "__version__", "unknown")
                pkg_status[pkg] = {"available": True, "version": version, "optional": True}
            except ImportError:
                pkg_status[pkg] = {"available": False, "version": None, "optional": True}
                warnings.append(f"Optional package '{pkg}' not installed")

        stats["packages"] = pkg_status

        # 3. MPS / CUDA availability
        compute = {"mps": False, "cuda": False, "device": "cpu"}
        try:
            import torch
            if torch.backends.mps.is_available():
                compute["mps"] = True
                compute["device"] = "mps"
            elif torch.cuda.is_available():
                compute["cuda"] = True
                compute["device"] = "cuda"
                compute["cuda_device_name"] = torch.cuda.get_device_name(0)
            else:
                warnings.append("No GPU backend (MPS/CUDA) available — will use CPU")
            compute["torch_version"] = torch.__version__
        except ImportError:
            warnings.append("torch not importable — cannot detect GPU backend")
        stats["compute"] = compute

        # 4. Disk space
        disk = shutil.disk_usage(str(ROOT))
        free_gb = disk.free / (1024 ** 3)
        total_gb = disk.total / (1024 ** 3)
        used_pct = (disk.used / disk.total) * 100
        stats["disk"] = {
            "free_gb": round(free_gb, 2),
            "total_gb": round(total_gb, 2),
            "used_pct": round(used_pct, 1),
            "path": str(ROOT),
        }
        min_free = self.config.get("min_free_disk_gb", DEFAULT_MIN_FREE_GB)
        if free_gb < min_free:
            errors.append(
                f"Disk space critically low: {free_gb:.1f} GB free "
                f"(minimum required: {min_free} GB)"
            )
        elif free_gb < min_free * 2:
            warnings.append(f"Disk space low: {free_gb:.1f} GB free")

        # 5. Required project directories
        required_dirs = [
            "src", "tools", "agents", "configs",
            "configs/model-params", "Data", "regions.json",
        ]
        missing_dirs = []
        for d in required_dirs:
            if not (ROOT / d).exists():
                missing_dirs.append(d)
        if missing_dirs:
            errors.append(f"Missing required directories: {missing_dirs}")
        stats["project_dirs"] = {
            d: (ROOT / d).exists() for d in required_dirs
        }

        # 6. Key config files
        key_files = [
            "configs/full_pipeline.json",
            "agents/base_agent.py",
            "agents/orchestrator.py",
        ]
        missing_files = [f for f in key_files if not (ROOT / f).exists()]
        if missing_files:
            errors.append(f"Missing key pipeline files: {missing_files}")
        stats["key_files"] = {
            f: (ROOT / f).exists() for f in key_files
        }

        # 7. Virtual environment detection
        in_venv = (
            hasattr(sys, "real_prefix")
            or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
        )
        stats["virtual_env"] = {
            "active": in_venv,
            "prefix": sys.prefix,
        }
        if not in_venv:
            warnings.append("Not running inside a virtual environment")

        # 8. External connectivity probes (fail-fast before any network I/O)
        # Skip in dry-run — connectivity cannot be tested without real network access
        connectivity: dict = {}
        if not self.dry_run:
            # S3 (Copernicus / EODATA) — single low-cost list call
            try:
                import boto3
                import os
                
                # Load credentials from canonical .config/aws/credentials via src.credentials
                from src.credentials import load_s3_credentials  # noqa: PLC0415
                s3_creds = load_s3_credentials()
                s3_endpoint = s3_creds["endpoint_url"]
                s3 = boto3.client(
                    "s3",
                    aws_access_key_id=s3_creds["aws_access_key_id"],
                    aws_secret_access_key=s3_creds["aws_secret_access_key"],
                    endpoint_url=s3_endpoint,
                    config=boto3.session.Config(s3={"addressing_style": "path"}),
                )
                    
                s3.list_objects_v2(Bucket="eodata", MaxKeys=1)
                connectivity["s3"] = {"reachable": True, "endpoint": s3_endpoint}
                print(f"  S3: ✅ reachable ({s3_endpoint})")
            except Exception as e:
                connectivity["s3"] = {
                    "reachable": False,
                    "endpoint": self.config.get("s3_endpoint", ""),
                    "error": str(e),
                }
                errors.append(
                    f"S3 connectivity failed — Stage 1 downloads will not work: {e}"
                )
                print(f"  S3: ❌ {e}")

            # SentinelHub — OAuth + catalog listing via SentinelHub SDK
            # Uses SHConfig + SentinelHubSession (official auth path) then
            # a single SentinelHubCatalog.get_info() call to prove API access.
            if not pkg_status.get("sentinelhub", {}).get("available"):
                # Package missing — attempt auto-install before probing
                import subprocess as _sp
                print("  SentinelHub: ⚙️  package missing — attempting pip install...")
                result = _sp.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", "sentinelhub"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    # Re-check after install
                    try:
                        import sentinelhub as _sh_test  # noqa: F401
                        pkg_status["sentinelhub"] = {"available": True, "auto_installed": True}
                        print("  SentinelHub: ✅ installed successfully")
                    except ImportError:
                        pkg_status["sentinelhub"] = {"available": False}
                else:
                    pkg_status["sentinelhub"] = {"available": False, "install_error": result.stderr[:200]}
                    warnings.append(f"sentinelhub auto-install failed: {result.stderr[:200]}")

            if pkg_status.get("sentinelhub", {}).get("available"):
                try:
                    from sentinelhub import SHConfig, SentinelHubSession, SentinelHubCatalog
                    # Load credentials from canonical .config/sentinelhub/config.toml
                    from src.credentials import load_sh_credentials, CredentialsError  # noqa: PLC0415
                    try:
                        sh_creds = load_sh_credentials()
                    except CredentialsError as _ce:
                        errors.append(f"SentinelHub credentials not found — Stage 0 FAIL: {_ce}")
                        connectivity["sentinelhub"] = {
                            "available": True,
                            "credentials_valid": False,
                            "error": str(_ce),
                        }
                        print(f"  SentinelHub: \u274c credentials not found: {_ce}")
                        sh_creds = None
                    if sh_creds:
                        sh_cfg = SHConfig()
                        sh_cfg.sh_client_id     = sh_creds["sh_client_id"]
                        sh_cfg.sh_client_secret = sh_creds["sh_client_secret"]
                        sh_cfg.sh_base_url      = sh_creds["sh_base_url"]
                        sh_cfg.sh_token_url     = sh_creds["sh_token_url"]
                        # SentinelHubSession performs the real OAuth token request
                        session = SentinelHubSession(config=sh_cfg)
                        _token = session.token  # raises on bad credentials
                        # Catalog probe — single get_info() call validates API access
                        catalog = SentinelHubCatalog(config=sh_cfg)
                        _info = catalog.get_info()  # lightweight, no data returned
                        connectivity["sentinelhub"] = {
                            "available": True,
                            "credentials_valid": True,
                            "oauth": "OK",
                            "catalog_api": "OK",
                        }
                        print("  SentinelHub: \u2705 OAuth + Catalog API OK")
                except Exception as e:
                    connectivity["sentinelhub"] = {
                        "available": True,
                        "credentials_valid": False,
                        "error": str(e),
                    }
                    errors.append(
                        f"SentinelHub probe failed — check client_id/client_secret: {e}"
                    )
                    print(f"  SentinelHub: ❌ {e}")
            else:
                connectivity["sentinelhub"] = {
                    "available": False,
                    "note": "Package not installed — STAC queries will use boto3 only",
                }
        else:
            connectivity["note"] = "Dry-run mode — connectivity probes skipped"

        stats["connectivity"] = connectivity
        output_dir = Path(self.config.get("output_dir", f"outputs/{datetime.utcnow().strftime('%Y%m%d')}"))
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = output_dir / "environment_snapshot.json"

        snapshot = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "success": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            **stats,
        }

        tmp_path = snapshot_path.with_suffix(".json.tmp")
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        tmp_path.rename(snapshot_path)

        # Print concise summary
        print(f"  Python {stats['python_version']} | Device: {compute['device']}")
        print(f"  Disk: {free_gb:.1f} GB free ({used_pct:.0f}% used)")
        avail = sum(1 for v in pkg_status.values() if v["available"] and not v.get("optional"))
        print(f"  Packages: {avail}/{len(required)} critical available")
        if warnings:
            for w in warnings[:3]:
                print(f"  ⚠  {w}")

        return AgentResult(
            success=len(errors) == 0,
            stage=self.stage_name,
            outputs=[str(snapshot_path)],
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """Verify snapshot file was written."""
        errors = []
        for path_str in result.outputs:
            p = Path(path_str)
            if not p.exists():
                errors.append(f"Snapshot not written: {path_str}")
            else:
                try:
                    with open(p) as f:
                        snap = json.load(f)
                    if "python_version" not in snap:
                        errors.append(f"{path_str}: missing python_version field")
                except Exception as e:
                    errors.append(f"{path_str}: JSON parse error: {e}")
        return len(errors) == 0, errors


if __name__ == "__main__":
    agent = EnvironmentCheckAgent(
        config={"output_dir": "outputs/env_check"},
        dry_run="--dry-run" in sys.argv,
    )
    result = agent.run()
    print("\nSnapshot:", result.outputs)
    sys.exit(0 if result.success else 1)
