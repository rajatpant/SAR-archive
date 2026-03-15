"""
Stage 3: Goal Enrichment Agent

Purpose: Add goal-specific derived features (geographical, agricultural, urban).
Driven by ML goals and thresholds read from model/ml_tuned_catalog.json.

Determinism Requirements:
- Heuristics and thresholds live in ml_tuned_catalog.json, never hardcoded here
- Same catalog hash + same enriched metadata → same goal metadata
- Goal-specific JSONs versioned with catalog config hash
- All rejection reasons logged with counts
- Supports "all" as goal value to process all 3 goals in a single run

Config Schema:
{
    "input_dir": "<resolved from run_manifest: static.s1_enriched_dir>",
    "output_dir": "<resolved from run_manifest: stages.stage3.output>",
    "goal": "geographical" | "agricultural" | "urban" | "all",
    "catalog_path": "configs/model-params/ml_tuned_catalog.json",  # ML-tuned unified catalog
    "parameter_selection": "configs/model-params/parameterSelection-UnSupervised-Geographical.json",
    "suitability_thresholds": {}  # Optional overrides (prefer catalog values)
}
"""
import sys
from pathlib import Path
from typing import Tuple, List, Dict, Any, Optional
import json
import glob
import os

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult


class GoalEnrichmentAgent(PipelineAgent):
    """Stage 3 Agent: Goal-specific feature derivation."""
    
    def _apply_manifest_overrides(self) -> None:
        """Override input_dir (static enriched) and output_dir (Stage-3 output) from run_manifest."""
        _m_in = self._manifest_abs("static", "s1_enriched_dir")
        if _m_in is not None:
            self.config["input_dir"] = _m_in
        _m_out = self._manifest_abs("stages", "stage3", "output")
        if _m_out is not None:
            self.config["output_dir"] = _m_out

    def get_stage_number(self) -> int:
        return 3
    
    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """Validate input directory and goal config exist."""
        errors = []
        
        if "input_dir" not in self.config:
            errors.append("Missing required config key: input_dir")
        else:
            input_dir = Path(self.config["input_dir"])
            if not input_dir.exists():
                errors.append(f"Input directory does not exist: {input_dir}")
            else:
                # Check for enriched files from Stage 2
                enriched_files = glob.glob(str(input_dir / "*.enriched.json"))
                if not enriched_files:
                    errors.append(f"No .enriched.json files found in {input_dir}")
        
        if "goal" not in self.config:
            errors.append("Missing required config key: goal")
        elif self.config["goal"] not in ['geographical', 'agricultural', 'urban', 'all']:
            errors.append(f"Invalid goal: {self.config['goal']}. Must be geographical, agricultural, urban, or all")

        # Load and validate the ML-tuned catalog
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
                    errors.append(f"Catalog missing 'goals' key: {catalog_path}")
            except Exception as e:
                errors.append(f"Catalog parse error: {e}")

        # Optional: validate parameter_selection file exists
        if "parameter_selection" in self.config:
            param_file = Path(self.config["parameter_selection"])
            if not param_file.exists():
                errors.append(f"Parameter selection file not found: {param_file}")

        return len(errors) == 0, errors
    
    def check_idempotency(self) -> Tuple[bool, str]:
        """Check if goal enrichment already completed via manifest."""
        stage_num = self.get_stage_number()
        output_dir = Path(self.config["output_dir"])  # set by _apply_manifest_overrides or validate_inputs
        
        # Look for existing manifest
        goal = self.config.get("goal", "unknown")
        manifest_pattern = str(output_dir / f"stage{stage_num}_*_{goal}_manifest.json")
        manifest_files = glob.glob(manifest_pattern)
        
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
                        selected = stats.get("scenes_selected", 0)
                        return True, f"Already processed with {selected} scenes selected (config hash match)"
            except Exception:
                pass  # Manifest corrupted, re-run
        
        return False, "Goal enrichment needed"
    
    def execute(self) -> AgentResult:
        """Execute goal-specific enrichment and filtering for 1 or all goals."""
        input_dir = Path(self.config["input_dir"])
        output_dir = Path(self.config["output_dir"])  # set by _apply_manifest_overrides
        output_dir.mkdir(parents=True, exist_ok=True)

        goal_arg = self.config["goal"]
        all_goals = ["geographical", "agricultural", "urban"]
        goals_to_run = all_goals if goal_arg == "all" else [goal_arg]

        # Load ML-tuned catalog for thresholds
        catalog = self._load_catalog()

        # Aggregate stats across all goals when running "all"
        combined_outputs: List[str] = []
        combined_errors: List[str] = []
        combined_warnings: List[str] = []
        per_goal_stats: Dict[str, Any] = {}

        for goal in goals_to_run:
            print(f"\n  --- Goal: {goal} ---")
            result_goal = self._run_single_goal(
                goal, input_dir, output_dir, catalog
            )
            combined_outputs.extend(result_goal["outputs"])
            combined_errors.extend(result_goal["errors"])
            combined_warnings.extend(result_goal["warnings"])
            per_goal_stats[goal] = result_goal["stats"]

        total_selected = sum(s.get("scenes_selected", 0) for s in per_goal_stats.values())
        return AgentResult(
            success=(len(combined_errors) == 0 and total_selected > 0),
            stage=self.stage_name,
            outputs=combined_outputs,
            errors=combined_errors,
            warnings=combined_warnings,
            stats={
                "goals_processed": goals_to_run,
                "total_selected": total_selected,
                "per_goal": per_goal_stats,
                "catalog_hash": catalog.get("config_hash", "unknown"),
            }
        )

    def _load_catalog(self) -> Dict[str, Any]:
        """Load the ML-tuned unified catalog."""
        catalog_path = Path(self.config.get("catalog_path", ROOT / "configs/model-params/ml_tuned_catalog.json"))
        try:
            with open(catalog_path) as f:
                return json.load(f)
        except Exception as e:
            print(f"  Warning: Could not load catalog ({e}), using config thresholds only")
            return {"goals": {}}

    def _run_single_goal(
        self,
        goal: str,
        input_dir: Path,
        output_dir: Path,
        catalog: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process all enriched files for a single goal."""
        # Load parameter selection if provided
        suitability_rules = self._load_suitability_rules(goal=goal, catalog=catalog)
        
        # Find all enriched JSONs from Stage 2
        enriched_files = sorted(glob.glob(str(input_dir / "*.enriched.json")))

        if not enriched_files:
            return {
                "outputs": [], "errors": [],
                "warnings": [f"No enriched files found in {input_dir}"],
                "stats": {"scenes_total": 0, "scenes_selected": 0, "scenes_rejected": 0, "goal": goal}
            }

        print(f"  Processing {len(enriched_files)} enriched files for goal: {goal}")
        
        selected = 0
        rejected = 0
        rejection_reasons: Dict[str, int] = {}
        output_files: List[str] = []
        errors: List[str] = []

        for filepath in enriched_files:
            basename = os.path.basename(filepath)
            
            try:
                with open(filepath) as f:
                    data = json.load(f)
                
                # Apply goal-specific filtering
                is_suitable, reason = self._evaluate_suitability(data, suitability_rules, goal)
                
                # Create goal-specific JSON
                goal_data = dict(data)
                goal_data['goal'] = goal
                goal_data['suitability'] = 'High' if is_suitable else 'Low'
                goal_data['suitability_reason'] = reason
                goal_data['goal_enrichment_version'] = self.compute_config_hash()
                
                # Write goal-specific JSON
                out_name = basename.replace('.enriched.json', f'.{goal}.json')
                out_path = output_dir / out_name
                
                with open(out_path, 'w') as f:
                    json.dump(goal_data, f, indent=2)
                
                if is_suitable:
                    selected += 1
                    output_files.append(str(out_path))
                    print(f"    ✓ {basename} → {goal}: selected")
                else:
                    rejected += 1
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                    print(f"    ✗ {basename} → {goal}: rejected ({reason})")

            except Exception as e:
                errors.append(f"Failed to process {basename}: {e}")
                rejected += 1
                print(f"    ✗ ERROR processing {basename}: {e}")

        rejection_rate = (rejected / len(enriched_files) * 100) if enriched_files else 0

        warnings: List[str] = []
        if rejection_rate > 80:
            top_reasons = dict(sorted(rejection_reasons.items(), key=lambda x: x[1], reverse=True)[:3])
            warnings.append(
                f"[{goal}] High rejection rate ({rejection_rate:.1f}%). "
                f"Consider relaxing suitability thresholds. Top reasons: {top_reasons}"
            )

        return {
            "outputs": output_files,
            "errors": errors,
            "warnings": warnings,
            "stats": {
                "scenes_total": len(enriched_files),
                "scenes_selected": selected,
                "scenes_rejected": rejected,
                "rejection_rate_percent": round(rejection_rate, 1),
                "rejection_reasons": rejection_reasons,
                "goal": goal,
            },
        }
    
    def _load_suitability_rules(self, goal: str, catalog: Dict[str, Any]) -> Dict[str, Any]:
        """Load suitability rules from catalog, then apply config overrides.

        Priority: catalog thresholds → config suitability_thresholds overrides.
        """
        # 1. Base from catalog
        train_type = self.config.get("train_type", "unsupervised")
        catalog_goal = catalog.get("goals", {}).get(goal, {})
        catalog_entry = catalog_goal.get(train_type) or next(iter(catalog_goal.values()), {})
        rules: Dict[str, Any] = dict(
            catalog_entry.get("suitability_thresholds", {})
        )

        # 2. Fallback defaults if catalog is empty
        rules.setdefault("cloud_cover_max", 20)
        rules.setdefault("required_polarizations", ["VV", "VH"])

        # 3. Config-level overrides (explicit in full_pipeline.json)
        if "suitability_thresholds" in self.config:
            rules.update(self.config["suitability_thresholds"])

        return rules
    
    def _evaluate_suitability(self, data: Dict[str, Any], rules: Dict[str, Any], goal: str) -> Tuple[bool, str]:
        """Evaluate if a scene is suitable for the given goal.
        
        Returns:
            (is_suitable, reason)
        """
        # Must be enriched
        if not data.get('enriched'):
            return False, "Not enriched"
        
        if not data.get('primary_asset_href'):
            return False, "No primary asset"
        
        feature = data.get('feature', {})
        props = feature.get('properties', {})
        
        # Detect sensor type from basename or properties
        basename = data.get('id', '')
        is_sar = basename.startswith('S1') or 'sentinel-1' in str(data.get('primary_asset_href', '')).lower()
        is_optical = basename.startswith('S2') or 'sentinel-2' in str(data.get('primary_asset_href', '')).lower()
        
        # SAR-specific checks
        if is_sar:
            # Check for required polarizations
            polarizations = props.get('sar:polarizations', props.get('polarization', []))
            if isinstance(polarizations, str):
                polarizations = [polarizations]
            
            required_pols = rules.get('required_polarizations', [])
            if required_pols:
                missing_pols = [p for p in required_pols if p not in polarizations]
                if missing_pols:
                    return False, f"Missing polarizations: {missing_pols}"
        
        # Optical-specific checks
        if is_optical:
            # Check cloud cover
            cloud_cover = props.get('eo:cloud_cover', props.get('cloud_cover', 100))
            max_cloud = rules.get('cloud_cover_max', 20)
            if cloud_cover > max_cloud:
                return False, f"Cloud cover too high: {cloud_cover}% > {max_cloud}%"
        
        # Goal-specific rules
        if goal == 'geographical':
            # Geographical features need broad coverage
            # Check footprint size if available
            bbox = feature.get('bbox')
            if bbox and len(bbox) == 4:
                # Rough area calculation (not accurate for polar regions)
                minx, miny, maxx, maxy = bbox
                area_deg2 = (maxx - minx) * (maxy - miny)
                # Require minimum area (about 100km x 100km at equator ≈ 1 deg²)
                if area_deg2 < 0.5:
                    return False, f"Footprint too small: {area_deg2:.3f} deg²"
        
        elif goal == 'agricultural':
            # Agricultural needs growing season data
            # Check acquisition time
            acq_time = props.get('datetime', props.get('acquisition_time', ''))
            if acq_time:
                # Extract month
                try:
                    if 'T' in acq_time:
                        month = int(acq_time.split('T')[0].split('-')[1])
                    elif len(acq_time) >= 6:
                        month = int(acq_time[4:6])
                    else:
                        month = 0
                    
                    # Northern hemisphere growing season: April-September (months 4-9)
                    if month < 4 or month > 9:
                        return False, f"Outside growing season: month {month}"
                except Exception:
                    pass  # Can't parse date, allow it
        
        elif goal == 'urban':
            # Urban needs high resolution
            resolution = props.get('gsd', props.get('resolution', 999))
            if resolution > 20:  # meters
                return False, f"Resolution too coarse: {resolution}m > 20m"
        
        # Passed all checks
        return True, "Suitable"
    
    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """Validate goal-specific JSON files."""
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
                if 'goal' not in data:
                    errors.append(f"{output_file}: Missing 'goal' field")
                if 'suitability' not in data:
                    errors.append(f"{output_file}: Missing 'suitability' field")
                if 'suitability_reason' not in data:
                    errors.append(f"{output_file}: Missing 'suitability_reason' field")
            except Exception as e:
                errors.append(f"{output_file}: JSON validation failed: {e}")
        
        return len(errors) == 0, errors


if __name__ == "__main__":
    print("Stage 3 Goal Enrichment Agent")
    print("Use via orchestrator: python -m agents.orchestrator --stage 3")
