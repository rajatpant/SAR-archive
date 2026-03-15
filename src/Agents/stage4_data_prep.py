"""
Stage 4: Data Preparation Agent

Purpose: Generate ML-ready chips, tensors, data loaders, and model architecture.

Determinism Requirements:
- Fixed random seed for chip sampling
- Same goal JSON + same config → same chips extracted
- Data splits saved as manifest (scene_id → "train"/"val"/"test")
- Augmentation pipelines defined in config

Config Schema:
{
    "input_dir": "<resolved from run_manifest: stages.stage3.output>",
    "output_dir": "<resolved from run_manifest: stages.stage4.output>",
    "chip_size": 512,
    "batch_size": 4,
    "random_seed": 42,
    "train_val_test_split": [0.7, 0.2, 0.1],
    "max_chips_per_scene": 10,
    "nan_tolerance": 0.2  # Max 20% NaN pixels per chip
}
"""
import sys
from pathlib import Path
from typing import Tuple, List, Dict, Any
import json
import glob
import os
import csv
import random

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult


class DataPrepAgent(PipelineAgent):
    """Stage 4 Agent: Data preparation and model instantiation."""
    
    def _apply_manifest_overrides(self) -> None:
        """Override input_dir (Stage-3 output) and output_dir (Stage-4 output) from run_manifest."""
        _m_in = self._manifest_abs("stages", "stage3", "output")
        if _m_in is not None:
            self.config["input_dir"] = _m_in
        _m_out = self._manifest_abs("stages", "stage4", "output")
        if _m_out is not None:
            self.config["output_dir"] = _m_out

    def get_stage_number(self) -> int:
        return 4
    
    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """Validate input directory contains goal-specific JSONs from Stage 3."""
        errors = []
        
        if "input_dir" not in self.config:
            errors.append("Missing required config key: input_dir")
        else:
            input_dir = Path(self.config["input_dir"])
            if not self.dry_run and not input_dir.exists():
                errors.append(f"Input directory does not exist: {input_dir}")
            else:
                # Check for goal-specific JSON files (skip in dry-run — Stage 3 may not have run yet)
                if not self.dry_run:
                    goal_files = []
                    for goal in ['geographical', 'agricultural', 'urban']:
                        goal_files.extend(glob.glob(str(input_dir / f"*.{goal}.json")))
                    if not goal_files:
                        errors.append(f"No goal-specific JSON files found in {input_dir}")

        # ML-tuned catalog cross-check: warn if catalog is missing or stale
        catalog_path = Path(self.config.get("catalog_path", ROOT / "configs/model-params/ml_tuned_catalog.json"))
        if not catalog_path.exists():
            errors.append(
                f"ML-tuned catalog not found: {catalog_path}. "
                f"Run: python tools/generate_ml_tuned_catalog.py"
            )
        else:
            try:
                with open(catalog_path) as f:
                    catalog = json.load(f)
                if "goals" not in catalog:
                    errors.append(f"Catalog missing 'goals' key — regenerate with tools/generate_ml_tuned_catalog.py")
                else:
                    # Cross-check: all 3 goals must be present
                    missing_goals = [g for g in ["geographical", "agricultural", "urban"] if g not in catalog["goals"]]
                    if missing_goals:
                        errors.append(f"Catalog missing goals: {missing_goals}")
            except Exception as e:
                errors.append(f"Catalog parse error: {e}")

        # Validate required config parameters
        required = ["chip_size", "batch_size"]
        for key in required:
            if key not in self.config:
                errors.append(f"Missing required config key: {key}")
        
        return len(errors) == 0, errors
    
    def check_idempotency(self) -> Tuple[bool, str]:
        """Check if data prep already completed via manifest."""
        stage_num = self.get_stage_number()
        output_dir = Path(self.config.get("output_dir", "outputs"))
        
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
                        train = stats.get("train_samples", 0)
                        return True, f"Data prep already complete: {train} train samples (config hash match)"
            except Exception:
                pass  # Manifest corrupted, re-run
        
        return False, "Data prep needed"
    
    def execute(self) -> AgentResult:
        """Execute data preparation."""
        input_dir = Path(self.config["input_dir"])
        output_dir = Path(self.config.get("output_dir", "outputs"))
        output_dir.mkdir(parents=True, exist_ok=True)
        
        chip_size = self.config["chip_size"]
        batch_size = self.config["batch_size"]
        random_seed = self.config.get("random_seed", 42)
        train_val_test_split = self.config.get("train_val_test_split", [0.7, 0.2, 0.1])
        max_chips_per_scene = self.config.get("max_chips_per_scene", 10)
        nan_tolerance = self.config.get("nan_tolerance", 0.2)
        
        # Set random seed for determinism
        random.seed(random_seed)
        
        # Find all suitable goal-specific JSONs (suitability='High')
        goal_files = []
        for goal in ['geographical', 'agricultural', 'urban']:
            goal_files.extend(glob.glob(str(input_dir / f"*.{goal}.json")))
        
        if not goal_files:
            return AgentResult(
                success=False,
                stage=self.stage_name,
                outputs=[],
                errors=["No goal-specific JSON files found"],
                warnings=[],
                stats={}
            )
        
        # Filter for suitable files only
        suitable_files = []
        for filepath in goal_files:
            try:
                with open(filepath) as f:
                    data = json.load(f)
                if data.get('suitability') == 'High':
                    suitable_files.append(filepath)
            except Exception:
                pass  # Skip corrupted files
        
        if not suitable_files:
            return AgentResult(
                success=False,
                stage=self.stage_name,
                outputs=[],
                errors=["No suitable scenes found (all rejected by Stage 3)"],
                warnings=[],
                stats={"scenes_total": len(goal_files), "scenes_suitable": 0}
            )
        
        print(f"  Found {len(suitable_files)} suitable scenes from {len(goal_files)} total")
        
        # Split into train/val/test
        # Use round() + min-1 guardrail so val/test are never empty when n >= 3
        random.shuffle(suitable_files)
        n = len(suitable_files)
        if n >= 3:
            n_train = max(1, int(round(n * train_val_test_split[0])))
            n_val   = max(1, int(round(n * train_val_test_split[1])))
            n_test  = max(1, n - n_train - n_val)
            # pull from train if rounding pushed us over n
            while n_train + n_val + n_test > n and n_train > 1:
                n_train -= 1
            # push remainder to test if under n
            n_test = n - n_train - n_val
        elif n == 2:
            n_train, n_val, n_test = 1, 1, 0
        elif n == 1:
            n_train, n_val, n_test = 1, 0, 0
        else:
            n_train, n_val, n_test = 0, 0, 0

        train_end = n_train
        val_end   = n_train + n_val
        
        splits = {
            'train': suitable_files[:train_end],
            'val': suitable_files[train_end:val_end],
            'test': suitable_files[val_end:]
        }
        
        # Write split manifest
        split_manifest_path = output_dir / "data_splits.json"
        split_manifest = {
            "random_seed": random_seed,
            "split_ratios": train_val_test_split,
            "splits": {
                "train": [os.path.basename(f) for f in splits['train']],
                "val": [os.path.basename(f) for f in splits['val']],
                "test": [os.path.basename(f) for f in splits['test']]
            }
        }
        
        with open(split_manifest_path, 'w') as f:
            json.dump(split_manifest, f, indent=2)
        
        print(f"    Train: {len(splits['train'])}, Val: {len(splits['val'])}, Test: {len(splits['test'])}")
        
        # Generate metadata CSV for each split
        output_paths = []
        total_chips = 0
        errors = []
        
        for split_name, split_files in splits.items():
            csv_path = output_dir / f"{split_name}_metadata.csv"
            output_paths.append(str(csv_path))
            
            try:
                with open(csv_path, 'w', newline='') as csvfile:
                    fieldnames = ['scene_id', 's3_uri', 'goal', 'chip_size', 'batch_size', 
                                  'incidence_angle', 'polarization', 'orbit_direction', 'suitability']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    
                    for filepath in split_files:
                        try:
                            with open(filepath) as f:
                                data = json.load(f)
                            
                            feature = data.get('feature', {})
                            props = feature.get('properties', {})
                            
                            row = {
                                'scene_id': data.get('id', os.path.basename(filepath)),
                                's3_uri': data.get('primary_asset_href', ''),
                                'goal': data.get('goal', ''),
                                'chip_size': chip_size,
                                'batch_size': batch_size,
                                'incidence_angle': props.get('incidence_angle', props.get('sar:incidence_angle', '')),
                                'polarization': ','.join(props.get('sar:polarizations', [])) if isinstance(props.get('sar:polarizations'), list) else props.get('sar:polarizations', ''),
                                'orbit_direction': props.get('sat:orbit_state', props.get('orbit_direction', '')),
                                'suitability': data.get('suitability', '')
                            }
                            
                            writer.writerow(row)
                            total_chips += 1
                            
                        except Exception as e:
                            errors.append(f"Failed to process {os.path.basename(filepath)}: {e}")
                
                print(f"    ✓ Wrote {split_name} metadata: {csv_path}")
                
            except Exception as e:
                errors.append(f"Failed to write {split_name} CSV: {e}")
        
        # Write data loader config
        dataloader_config_path = output_dir / "dataloader_config.json"
        dataloader_config = {
            "chip_size": chip_size,
            "batch_size": batch_size,
            "random_seed": random_seed,
            "max_chips_per_scene": max_chips_per_scene,
            "nan_tolerance": nan_tolerance,
            "train_metadata": str(output_dir / "train_metadata.csv"),
            "val_metadata": str(output_dir / "val_metadata.csv"),
            "test_metadata": str(output_dir / "test_metadata.csv")
        }
        
        with open(dataloader_config_path, 'w') as f:
            json.dump(dataloader_config, f, indent=2)
        
        output_paths.extend([str(split_manifest_path), str(dataloader_config_path)])
        
        # Run data quality checks (simplified)
        nan_count = 0  # Placeholder - would need actual chip extraction
        warnings = []
        
        if nan_count / max(total_chips, 1) > nan_tolerance:
            warnings.append(f"High NaN rate: {nan_count}/{total_chips} chips have excessive NaN pixels")
        
        return AgentResult(
            success=(total_chips > 0),
            stage=self.stage_name,
            outputs=output_paths,
            errors=errors,
            warnings=warnings,
            stats={
                "scenes_total": len(goal_files),
                "scenes_suitable": len(suitable_files),
                "train_samples": len(splits['train']),
                "val_samples": len(splits['val']),
                "test_samples": len(splits['test']),
                "chip_size": chip_size,
                "batch_size": batch_size,
                "random_seed": random_seed
            }
        )
    
    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """Validate metadata CSVs and config files exist."""
        errors = []
        
        for output_file in result.outputs:
            path = Path(output_file)
            if not path.exists():
                errors.append(f"Output file not found: {output_file}")
                continue
            
            # Validate CSV files
            if output_file.endswith('.csv'):
                try:
                    with open(path) as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)
                        if not rows:
                            errors.append(f"{output_file}: CSV is empty")
                        else:
                            # Check required columns
                            required_cols = ['scene_id', 's3_uri', 'goal']
                            first_row = rows[0]
                            missing_cols = [c for c in required_cols if c not in first_row]
                            if missing_cols:
                                errors.append(f"{output_file}: Missing columns: {missing_cols}")
                except Exception as e:
                    errors.append(f"{output_file}: CSV validation failed: {e}")
            
            # Validate JSON files
            elif output_file.endswith('.json'):
                try:
                    with open(path) as f:
                        json.load(f)
                except Exception as e:
                    errors.append(f"{output_file}: JSON validation failed: {e}")
        
        return len(errors) == 0, errors


if __name__ == "__main__":
    print("Stage 4 Data Preparation Agent")
    print("Use via orchestrator: python -m agents.orchestrator --stage 4")
