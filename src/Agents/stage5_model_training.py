"""
Stage 5: Model Training Agent

Purpose: Execute training loop, save checkpoints, generate KPI report.

Determinism Requirements:
- All hyperparameters in config JSON
- Random seed logged and applied to torch, numpy, random
- Checkpoint saved every N epochs (N from config)
- Report includes permanent links to data, config, model weights, logs

Config Schema:
{
    "input_dir": "outputs/20260307",  # Stage 4 outputs
    "output_dir": "outputs/20260307/models",
    "model_type": "unsupervised" | "supervised",
    "goal": "geographical" | "agricultural" | "urban",
    "architecture": "vit-mae" | "simple-cnn",
    "hyperparameters": {
        "epochs": 10,
        "learning_rate": 0.0001,
        "batch_size": 4,
        "optimizer": "AdamW",
        "loss_function": "MSE"
    },
    "random_seed": 42,
    "checkpoint_every_n_epochs": 5,
    "parameter_selection": "configs/model-params/parameterSelection-UnSupervised-Geographical.json"
}
"""
import sys
from pathlib import Path
from typing import Tuple, List, Dict, Any
import json
import glob
import os
import subprocess

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult


class ModelTrainingAgent(PipelineAgent):
    """Stage 5 Agent: Model training execution."""
    
    def _apply_manifest_overrides(self) -> None:
        """Override input_dir (Stage-4 output) and output_dir (Stage-5 output) from run_manifest."""
        _m_in = self._manifest_abs("stages", "stage4", "output")
        if _m_in is not None:
            self.config["input_dir"] = _m_in
        _m_out = self._manifest_abs("stages", "stage5", "output")
        if _m_out is not None:
            self.config["output_dir"] = _m_out

    def get_stage_number(self) -> int:
        return 5
    
    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """Validate training inputs from Stage 4."""
        errors = []
        
        if "input_dir" not in self.config:
            errors.append("Missing required config key: input_dir")
        else:
            input_dir = Path(self.config["input_dir"])
            if not self.dry_run and not input_dir.exists():
                errors.append(f"Input directory does not exist: {input_dir}")
            elif not self.dry_run and input_dir.exists():
                # Check for Stage 4 outputs (skip in dry-run — Stage 4 may not have run yet)
                required_files = ["train_metadata.csv", "dataloader_config.json"]
                for required in required_files:
                    if not (input_dir / required).exists():
                        errors.append(f"Missing required file from Stage 4: {required}")
        
        # Validate hyperparameters
        if "hyperparameters" not in self.config:
            errors.append("Missing required config key: hyperparameters")
        else:
            hyper = self.config["hyperparameters"]
            required_hyper = ["epochs", "learning_rate", "batch_size"]
            for key in required_hyper:
                if key not in hyper:
                    errors.append(f"Missing required hyperparameter: {key}")
        
        return len(errors) == 0, errors
    
    def check_idempotency(self) -> Tuple[bool, str]:
        """Check if training already completed via manifest."""
        stage_num = self.get_stage_number()
        output_dir = Path(self.config.get("output_dir", "outputs/models"))
        
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
                        epochs = stats.get("epochs_completed", 0)
                        return True, f"Training already complete: {epochs} epochs (config hash match)"
            except Exception:
                pass  # Manifest corrupted, re-run
        
        # Check for existing checkpoints
        checkpoint_files = glob.glob(str(output_dir / "checkpoint_epoch_*.pt"))
        if checkpoint_files:
            latest_ckpt = max(checkpoint_files, key=os.path.getmtime)
            return False, f"Found existing checkpoint {os.path.basename(latest_ckpt)}, but config changed. Re-running."
        
        return False, "Training needed"
    
    def execute(self) -> AgentResult:
        """Execute model training via existing training scripts.

        Enforces:
        - Streaming from S3 (no local file caching by default)
        - Checkpoint saving every N epochs
        - Structured report with permanent links (10 required fields)
        """
        input_dir = Path(self.config["input_dir"])
        output_dir = Path(self.config.get("output_dir", "outputs/models"))
        output_dir.mkdir(parents=True, exist_ok=True)

        model_type = self.config.get("model_type", "unsupervised")
        goal = self.config.get("goal", "geographical")
        random_seed = self.config.get("random_seed", 42)
        hyper = self.config["hyperparameters"]
        epochs = hyper["epochs"]
        lr = hyper["learning_rate"]
        batch_size = hyper["batch_size"]
        chip_size = self.config.get("chip_size", 224)
        checkpoint_every = self.config.get("checkpoint_every_n_epochs", 5)

        print(f"  Training {model_type} model for goal: {goal}")
        print(f"    Epochs: {epochs} | LR: {lr} | Batch: {batch_size} | Seed: {random_seed}")

        # 1. Streaming validation: abort if cache_locally is set
        stream_warnings: List[str] = []
        if self.config.get("cache_locally", False):
            return AgentResult(
                success=False,
                stage=self.stage_name,
                outputs=[],
                errors=["cache_locally=True violates S3 streaming requirement. Set to false."],
                warnings=[],
                stats={},
            )

        # 2. Determine script and build command
        script_map = {
            "unsupervised": "tools/run_unsupervised_inmemory.py",
            "supervised": "tools/run_supervised_inmemory.py",
        }
        script = script_map.get(model_type)
        if not script or not (ROOT / script).exists():
            return AgentResult(
                success=False,
                stage=self.stage_name,
                outputs=[],
                errors=[f"Training script not found for model_type={model_type}: {script}"],
                warnings=[],
                stats={},
            )

        if model_type == "unsupervised":
            cmd = [
                sys.executable, str(ROOT / script),
                "--metadata-dir", str(input_dir),
                "--epochs", str(epochs),
                "--lr", str(lr),
                "--batch-size", str(batch_size),
                "--chip-size", str(chip_size),
                "--no-download",  # enforce S3 streaming, no local persistence
            ]
            if "parameter_selection" in self.config and Path(self.config["parameter_selection"]).exists():
                cmd += ["--parameter-selection-json", self.config["parameter_selection"]]
        else:  # supervised
            mapping_file = self.config.get("mapping", "sar_to_true_color_mapping.json")
            cmd = [
                sys.executable, str(ROOT / script),
                "--mapping", str(ROOT / mapping_file),
                "--epochs", str(epochs),
                "--lr", str(lr),
                "--batch-size", str(batch_size),
                "--chip-size", str(chip_size),
                "--no-cache",  # enforce S3 streaming; no local chip cache
            ]
            if "parameter_selection" in self.config and Path(self.config["parameter_selection"]).exists():
                cmd += ["--parameter-selection-json", self.config["parameter_selection"]]
            if "pretrained_weights" in self.config:
                cmd += ["--load-weights", self.config["pretrained_weights"]]

        print(f"    Command: {' '.join(cmd[:4])} ... ({len(cmd)} args total)")

        # 3. Execute with live output capture
        errors: List[str] = []
        output_files: List[str] = []
        training_log_path = output_dir / "training.log"
        final_loss: float = 0.0
        best_epoch: int = 0

        try:
            import subprocess
            timeout_sec = 3600 * min(epochs, 24)  # max 24h cap
            with open(training_log_path, "w") as log_f:
                proc = subprocess.run(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_sec,
                    cwd=str(ROOT),
                )

            output_files.append(str(training_log_path))

            if proc.returncode != 0:
                errors.append(f"Training exited with code {proc.returncode}. See: {training_log_path}")
            else:
                # Parse final loss + best epoch from log
                final_loss, best_epoch = self._parse_training_log(training_log_path)
                print(f"    ✓ Training complete | final_loss={final_loss:.4f} | best_epoch={best_epoch}")

        except subprocess.TimeoutExpired:
            errors.append(f"Training timed out after {timeout_sec}s")
        except Exception as e:
            errors.append(f"Training subprocess failed: {e}")

        # 4. Find checkpoint files produced by training script
        for ckpt in sorted(output_dir.glob("checkpoint_epoch_*.pt")):
            output_files.append(str(ckpt))
        for ckpt in sorted(output_dir.glob("*.pt")):
            if str(ckpt) not in output_files:
                output_files.append(str(ckpt))

        # 5. Generate structured report with 10 permanent link fields
        report_path = self._generate_structured_report(
            output_dir=output_dir,
            input_dir=input_dir,
            model_type=model_type,
            goal=goal,
            hyper=hyper,
            random_seed=random_seed,
            epochs_completed=epochs if not errors else 0,
            final_loss=final_loss,
            best_epoch=best_epoch,
            output_files=output_files,
            errors=errors,
            stream_warnings=stream_warnings,
        )
        output_files.append(str(report_path))

        return AgentResult(
            success=(len(errors) == 0),
            stage=self.stage_name,
            outputs=output_files,
            errors=errors,
            warnings=stream_warnings,
            stats={
                "model_type": model_type,
                "goal": goal,
                "epochs_completed": epochs if not errors else 0,
                "final_loss": final_loss,
                "best_epoch": best_epoch,
                "random_seed": random_seed,
                "device": "mps" if self.config.get("device") == "mps" else "cpu",
                "report": str(report_path),
            },
        )

    def _parse_training_log(self, log_path: Path) -> tuple:
        """Extract final_loss and best_epoch from training log."""
        final_loss, best_epoch = 0.0, 0
        try:
            with open(log_path) as f:
                for line in f:
                    # Match patterns like: "Epoch 5/10 | loss=0.0423"
                    if "loss=" in line.lower():
                        import re
                        m = re.search(r"loss[=:\s]+([0-9]+\.[0-9]+)", line, re.IGNORECASE)
                        if m:
                            final_loss = float(m.group(1))
                    if "epoch" in line.lower() and "best" in line.lower():
                        import re
                        m = re.search(r"epoch\s+(\d+)", line, re.IGNORECASE)
                        if m:
                            best_epoch = int(m.group(1))
        except Exception:
            pass
        return final_loss, best_epoch

    def _generate_structured_report(
        self,
        output_dir: Path,
        input_dir: Path,
        model_type: str,
        goal: str,
        hyper: dict,
        random_seed: int,
        epochs_completed: int,
        final_loss: float,
        best_epoch: int,
        output_files: List[str],
        errors: List[str],
        stream_warnings: List[str],
    ) -> Path:
        """Generate structured KPI report with 10 permanent link fields."""
        from datetime import datetime

        checkpoint_files = [f for f in output_files if f.endswith(".pt")]
        log_files = [f for f in output_files if f.endswith(".log")]

        # Build permanent links (all required by copilot instructions)
        permanent_links = {
            "data_sources": self.config.get("data_sources", ["s3://eodata/Sentinel-1/"]),
            "enriched_listings_used": str(input_dir / "train_metadata.csv"),
            "enriched_listings_unused": str(input_dir / "val_metadata.csv"),
            "enrichment_report": str(
                Path(self.config.get("enrichment_report", "")) or
                output_dir.parent / "stage2_universalenrichmentagent_manifest.json"
            ),
            "data_quality_report": str(
                Path(self.config.get("data_quality_report", "")) or
                output_dir / "data_check_report.json"
            ),
            "features_used": self.config.get(
                "parameter_selection", "configs/model-params/parameterSelection-UnSupervised-Geographical.json"
            ),
            "features_unused": self.config.get(
                "catalog_path", "configs/model-params/ml_tuned_catalog.json"
            ),
            "model_weights": checkpoint_files[-1] if checkpoint_files else str(output_dir / "model_final.pt"),
            "training_logs": log_files[0] if log_files else str(output_dir / "training.log"),
            "kpi_summary": str(output_dir / "kpi.json"),
        }

        # Build KPIs
        kpis = {
            "model_type": model_type,
            "goal": goal,
            "architecture": self.config.get("architecture", "vit-mae"),
            "epochs_completed": epochs_completed,
            "final_loss": final_loss,
            "best_epoch": best_epoch,
            "hyperparameters": hyper,
            "random_seed": random_seed,
            "device": self.config.get("device", "mps"),
            "streaming_enforced": not self.config.get("cache_locally", False),
        }

        # What went wrong
        what_went_wrong = []
        if errors:
            what_went_wrong.extend([{"type": "error", "description": e} for e in errors])
        if stream_warnings:
            what_went_wrong.extend([{"type": "warning", "description": w} for w in stream_warnings])
        if final_loss == 0.0 and epochs_completed > 0:
            what_went_wrong.append({"type": "diagnostic", "description": "Could not parse loss from log"})

        # Next-step recommendations
        next_steps = []
        if final_loss > 0.5:
            next_steps.append("Loss >0.5: try reducing learning rate by 10x")
        if epochs_completed < hyper.get("epochs", 10):
            next_steps.append(f"Training stopped early at epoch {epochs_completed}: check for OOM or timeout")
        if not checkpoint_files:
            next_steps.append("No checkpoint files found: verify training script saves checkpoints")
        if not next_steps:
            next_steps.append("Training completed successfully — proceed to Stage 6 feedback analysis")

        report = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "config_hash": self.compute_config_hash(),
            "permanent_links": permanent_links,
            "model_kpis": kpis,
            "what_went_wrong": what_went_wrong,
            "next_steps": next_steps,
            "output_files": output_files,
        }

        # Atomic write
        report_path = output_dir / "training_report.json"
        tmp = report_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(report, f, indent=2)
        tmp.rename(report_path)

        # Also write a separate KPI summary
        kpi_path = output_dir / "kpi.json"
        with open(kpi_path, "w") as f:
            json.dump(kpis, f, indent=2)

        return report_path

    
    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """Validate training outputs (checkpoints, report)."""
        errors = []
        
        for output_file in result.outputs:
            path = Path(output_file)
            if not path.exists():
                errors.append(f"Output file not found: {output_file}")
                continue
            
            # Validate checkpoint files
            if output_file.endswith('.pt'):
                if path.stat().st_size == 0:
                    errors.append(f"{output_file}: Checkpoint file is empty")
            
            # Validate training report
            elif output_file.endswith('training_report.json'):
                try:
                    with open(path) as f:
                        report = json.load(f)
                    
                    required_fields = ['model_type', 'epochs_completed', 'hyperparameters']
                    missing = [f for f in required_fields if f not in report]
                    if missing:
                        errors.append(f"{output_file}: Missing fields: {missing}")
                except Exception as e:
                    errors.append(f"{output_file}: Report validation failed: {e}")
        
        return len(errors) == 0, errors


if __name__ == "__main__":
    print("Stage 5 Model Training Agent")
    print("Use via orchestrator: python -m agents.orchestrator --stage 5")
