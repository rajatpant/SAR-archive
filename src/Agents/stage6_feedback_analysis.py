"""
Stage 6: Feedback Analysis Agent

Purpose: Analyze model performance and recommend upstream config changes.
Reads Stage 5 training report and Stage 3 goal enrichment stats to produce
structured recommendations with HOW/WHAT categorization and confidence scores.

Detection rules:
  - Data scarcity   : total_scenes < threshold  → Stage 1: expand date range
  - High rejection  : rejection_rate > 80%      → Stage 3: relax suitability
  - High NaN        : nan_rate > nan_tolerance   → Stage 4: increase tolerance
  - Loss plateau    : Δloss last 3 epochs < 0.001 → Stage 5: halve LR
  - OOM / timeout   : error strings in log       → Stage 5: reduce batch_size

Config Schema:
{
    "model_report": "outputs/20260307/models/training_report.json",
    "goal_enrichment_stats": "outputs/20260307/stage3_goalenrichmentagent_manifest.json",
    "output_dir": "outputs/20260307",
    "current_configs_dir": "configs",
    "recommendation_threshold": 0.7,
    "rejection_rate_threshold": 0.8,
    "min_scenes_threshold": 10
}
"""
import sys
import json
from pathlib import Path
from typing import Tuple, List, Dict, Any, Optional
from datetime import datetime
import glob

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.Agents.base_agent import PipelineAgent, AgentResult


class FeedbackAnalysisAgent(PipelineAgent):
    """Stage 6 Agent: Model performance analysis and recommendation engine."""

    def _apply_manifest_overrides(self) -> None:
        """Override output_dir (Stage-6) and inject stage3/stage5 dirs from run_manifest."""
        _m_out = self._manifest_abs("stages", "stage6", "output")
        if _m_out is not None:
            self.config["output_dir"] = _m_out
        _m_stage3 = self._manifest_abs("stages", "stage3", "output")
        if _m_stage3 is not None:
            self.config["stage3_output_dir"] = _m_stage3
        _m_stage5 = self._manifest_abs("stages", "stage5", "output")
        if _m_stage5 is not None:
            self.config["stage5_output_dir"] = _m_stage5

    def get_stage_number(self) -> int:
        return 6

    def validate_inputs(self) -> Tuple[bool, List[str]]:
        """Check that at least one upstream report exists to analyze."""
        errors = []

        # In dry-run mode, skip file checks — upstream stages haven't run yet
        if self.dry_run:
            return True, []

        report_path = self.config.get("model_report", "")
        has_report = report_path and Path(report_path).exists()

        # Auto-discover latest training report if not specified
        if not has_report:
            discovered = self._discover_latest_report()
            if discovered:
                self.config["model_report"] = str(discovered)
                has_report = True

        stats_path = self.config.get("goal_enrichment_stats", "")
        has_stats = stats_path and Path(stats_path).exists()

        if not has_report and not has_stats:
            errors.append(
                "No training report or goal enrichment stats found. "
                "Run at least Stage 3 or Stage 5 before Stage 6."
            )

        return len(errors) == 0, errors

    def check_idempotency(self) -> Tuple[bool, str]:
        """Always re-run feedback — new model reports may exist."""
        return False, "Feedback analysis always runs to reflect latest results"

    def execute(self) -> AgentResult:
        """Analyze reports and generate structured recommendations."""
        output_dir = Path(
            self.config.get("output_dir", f"outputs/{datetime.utcnow().strftime('%Y%m%d')}")
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        threshold = self.config.get("recommendation_threshold", 0.7)

        # Load available data sources
        training_report = self._load_json_safe(self.config.get("model_report", ""))
        goal_stats = self._load_goal_stats()

        # Run all 5 detection rules
        recommendations: List[Dict[str, Any]] = []
        recommendations += self._check_data_scarcity(training_report, goal_stats)
        recommendations += self._check_high_rejection_rate(goal_stats)
        recommendations += self._check_nan_rate(training_report)
        recommendations += self._check_loss_plateau(training_report)
        recommendations += self._check_oom_timeout(training_report)

        # Split by confidence threshold
        accepted = [r for r in recommendations if r["confidence"] >= threshold]
        below_threshold = [r for r in recommendations if r["confidence"] < threshold]

        # Categorize WHAT went wrong
        what_went_wrong = [
            {
                "category": r.get("category", "unknown"),
                "stage": r.get("target_stage"),
                "description": r.get("description"),
                "how_to_fix": r.get("how"),
            }
            for r in recommendations if r.get("severity") in ("high", "critical")
        ]

        # Generate proposed config updates
        proposed_config_updates = self._generate_config_updates(accepted)

        # Write recommendations.json
        recs_path = output_dir / "recommendations.json"
        recs_doc = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_recommendations": len(recommendations),
            "accepted_above_threshold": len(accepted),
            "confidence_threshold": threshold,
            "recommendations": accepted,
            "below_threshold": below_threshold,
            "what_went_wrong": what_went_wrong,
            "proposed_config_updates": proposed_config_updates,
        }
        tmp = recs_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(recs_doc, f, indent=2)
        tmp.rename(recs_path)

        # Print summary
        print(f"\n  Feedback Analysis Results:")
        print(f"  {'='*50}")
        if not recommendations:
            print("  ✅ No issues detected — pipeline is healthy")
        else:
            for rec in accepted:
                icon = "🔴" if rec["severity"] == "critical" else "🟡" if rec["severity"] == "high" else "🔵"
                print(f"  {icon} Stage {rec['target_stage']}: {rec['title']}")
                print(f"     Confidence: {rec['confidence']:.0%} | HOW: {rec['how'][:80]}")
        if below_threshold:
            print(f"\n  ℹ️  {len(below_threshold)} low-confidence items skipped (< {threshold:.0%})")
        if proposed_config_updates:
            print(f"\n  📝 {len(proposed_config_updates)} config updates proposed → {recs_path.name}")

        return AgentResult(
            success=True,
            stage=self.stage_name,
            outputs=[str(recs_path)],
            errors=[],
            warnings=[
                f"Human approval required before applying {len(accepted)} recommendations"
            ] if accepted else [],
            stats={
                "total_recommendations": len(recommendations),
                "accepted": len(accepted),
                "what_went_wrong": what_went_wrong,
                "recommendations": accepted,
                "proposed_config_updates": proposed_config_updates,
            },
        )

    # ------------------------------------------------------------------
    # Detection rules
    # ------------------------------------------------------------------

    def _check_data_scarcity(
        self, training_report: Optional[Dict], goal_stats: Optional[Dict]
    ) -> List[Dict[str, Any]]:
        """Rule: insufficient scenes → expand Stage 1 date range."""
        min_scenes = self.config.get("min_scenes_threshold", 10)
        scenes = 0

        if training_report:
            scenes = max(scenes, training_report.get("data_stats", {}).get("total_scenes", 0))
        if goal_stats:
            for gdata in goal_stats.values():
                scenes = max(scenes, gdata.get("scenes_selected", 0))

        if scenes == 0:
            return [{
                "title": "No training scenes detected",
                "category": "data",
                "severity": "critical",
                "target_stage": 1,
                "confidence": 0.95,
                "description": "Stage 6 detected 0 scenes. Pipeline health cannot be confirmed without real scene data.",
                "what": "Data scarcity: 0 scenes found — pipeline health unverifiable",
                "how": "Ensure Stage 1 downloads real .SAFE packages and Stage 3 selects ≥ 1 scene",
                "proposed_config_key": "stage1_download.max_items",
                "proposed_action": "increase_max_items",
            }]
        if scenes >= min_scenes:
            return []

        return [{
            "title": "Insufficient training scenes",
            "category": "data",
            "severity": "high",
            "target_stage": 1,
            "confidence": 0.9,
            "description": f"Only {scenes} scenes available; minimum is {min_scenes}",
            "what": f"Data scarcity: {scenes} scenes < minimum {min_scenes}",
            "how": "Expand date_range in Stage 1 config by ±30 days",
            "proposed_config_key": "stage1_download.date_range",
            "proposed_action": "expand_date_range_30_days",
        }]

    def _check_high_rejection_rate(
        self, goal_stats: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Rule: high rejection rate per goal → relax Stage 3 thresholds."""
        if not goal_stats:
            return []

        threshold = self.config.get("rejection_rate_threshold", 0.8)
        results = []

        for goal, stats in goal_stats.items():
            total = stats.get("scenes_total", 0)
            if total == 0:
                continue
            rejected = stats.get("scenes_rejected", 0)
            rate = rejected / total
            if rate <= threshold:
                continue

            top_reason = max(
                stats.get("rejection_reasons", {}).items(),
                key=lambda x: x[1],
                default=("unknown", 0),
            )[0]

            if "cloud" in top_reason.lower():
                how = f"Increase cloud_cover_max from 20 to 30 for goal '{goal}'"
                action = "increase_cloud_cover_max"
            elif "polarization" in top_reason.lower():
                how = "Ensure Stage 2 enrichment populates sar:polarizations field"
                action = "fix_stage2_enrichment"
            else:
                how = f"Review rejection reason '{top_reason[:60]}' and relax threshold"
                action = "manual_review"

            results.append({
                "title": f"High rejection rate — goal: {goal}",
                "category": "data_quality",
                "severity": "high",
                "target_stage": 3,
                "confidence": 0.85,
                "description": f"{rate:.0%} rejected for goal '{goal}'. Top reason: {top_reason[:80]}",
                "what": f"Stage 3 suitability filter too strict for goal '{goal}'",
                "how": how,
                "proposed_config_key": "stage3_goal_enrichment.suitability_thresholds",
                "proposed_action": action,
                "goal": goal,
            })

        return results

    def _check_nan_rate(
        self, training_report: Optional[Dict]
    ) -> List[Dict[str, Any]]:
        """Rule: high NaN rate → increase Stage 4 nan_tolerance."""
        if not training_report:
            return []

        nan_rate = training_report.get("model_kpis", {}).get("nan_rate", 0)
        tolerance = self.config.get("nan_tolerance", 0.2)

        if nan_rate <= tolerance:
            return []

        return [{
            "title": "High NaN rate in training chips",
            "category": "data_quality",
            "severity": "medium",
            "target_stage": 4,
            "confidence": 0.8,
            "description": f"NaN rate {nan_rate:.1%} exceeds tolerance {tolerance:.1%}",
            "what": "Chips contain excessive NaN/nodata pixels",
            "how": (
                f"Increase nan_tolerance in Stage 4 to {min(nan_rate * 1.2, 0.5):.2f}, "
                f"or improve cloud/nodata masking in data_prep.py"
            ),
            "proposed_config_key": "stage4_data_prep.nan_tolerance",
            "proposed_action": "increase_nan_tolerance",
        }]

    def _check_loss_plateau(
        self, training_report: Optional[Dict]
    ) -> List[Dict[str, Any]]:
        """Rule: loss plateau in last 3 epochs → reduce LR in Stage 5."""
        if not training_report:
            return []

        loss_history = training_report.get("loss_history", [])
        if len(loss_history) < 3:
            return []

        last_three = loss_history[-3:]
        delta = abs(max(last_three) - min(last_three))
        if delta >= 0.001:
            return []

        current_lr = (
            training_report.get("model_kpis", {})
            .get("hyperparameters", {})
            .get("learning_rate", 1e-4)
        )
        return [{
            "title": "Loss plateau detected",
            "category": "model",
            "severity": "medium",
            "target_stage": 5,
            "confidence": 0.75,
            "description": f"Loss Δ={delta:.6f} over last 3 epochs — model not improving",
            "what": f"Training plateau: Δloss={delta:.4f} < 0.001 threshold",
            "how": f"Reduce learning_rate from {current_lr} to {current_lr / 10} (10× reduction)",
            "proposed_config_key": "stage5_training.hyperparameters.learning_rate",
            "proposed_action": "reduce_lr_10x",
        }]

    def _check_oom_timeout(
        self, training_report: Optional[Dict]
    ) -> List[Dict[str, Any]]:
        """Rule: OOM or timeout in errors → reduce batch_size or epochs."""
        if not training_report:
            return []

        errors = training_report.get("what_went_wrong", [])
        error_text = " ".join(str(e.get("description", "")) for e in errors).lower()
        results = []

        if "out of memory" in error_text or "oom" in error_text:
            current_bs = (
                training_report.get("model_kpis", {})
                .get("hyperparameters", {})
                .get("batch_size", 4)
            )
            results.append({
                "title": "Out-of-memory error during training",
                "category": "process",
                "severity": "critical",
                "target_stage": 5,
                "confidence": 0.95,
                "description": "OOM error detected in training logs",
                "what": "Process error: GPU/MPS memory exhausted during training",
                "how": f"Reduce batch_size from {current_bs} to {max(1, current_bs // 2)}",
                "proposed_config_key": "stage5_training.hyperparameters.batch_size",
                "proposed_action": "halve_batch_size",
            })

        if "timed out" in error_text or "timeout" in error_text:
            results.append({
                "title": "Training timeout",
                "category": "process",
                "severity": "high",
                "target_stage": 5,
                "confidence": 0.9,
                "description": "Training exceeded time limit",
                "what": "Process error: training timed out",
                "how": "Reduce epochs count or enable checkpoint-based resumption",
                "proposed_config_key": "stage5_training.hyperparameters.epochs",
                "proposed_action": "reduce_epochs",
            })

        return results

    # ------------------------------------------------------------------
    # Config update generator
    # ------------------------------------------------------------------

    def _generate_config_updates(
        self, recommendations: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert accepted recommendations into concrete config update dicts."""
        updates = []
        seen: set = set()

        for rec in recommendations:
            key = rec.get("proposed_config_key", "")
            action = rec.get("proposed_action", "")
            if not key or key in seen:
                continue
            seen.add(key)

            update: Dict[str, Any] = {
                "config_key": key,
                "action": action,
                "rationale": rec.get("how", ""),
                "target_stage": rec.get("target_stage"),
                "confidence": rec.get("confidence"),
                "applied": False,  # human must set True to apply
            }

            if action == "expand_date_range_30_days":
                update["note"] = "Extend date_range[0] by -30 days, date_range[1] by +30 days"
            elif action == "increase_cloud_cover_max":
                update["new_value"] = 30
                update["note"] = f"Set cloud_cover_max=30 for goal '{rec.get('goal', 'all')}'"
            elif action == "reduce_lr_10x":
                update["note"] = "Divide current learning_rate by 10"
            elif action == "halve_batch_size":
                update["note"] = "Divide current batch_size by 2 (minimum 1)"
            elif action == "increase_nan_tolerance":
                update["note"] = "Set nan_tolerance = min(current_nan_rate × 1.2, 0.5)"

            updates.append(update)

        return updates

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_json_safe(self, path: str) -> Optional[Dict]:
        if not path or not Path(path).exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f"  Warning: could not load {path}: {e}")
            return None

    def _load_goal_stats(self) -> Dict[str, Any]:
        """Load per-goal rejection stats from Stage 3 manifest (auto-discover)."""
        stats_path = self.config.get("goal_enrichment_stats", "")
        if stats_path and Path(stats_path).exists():
            data = self._load_json_safe(stats_path)
            if data:
                return data.get("result", {}).get("stats", {}).get("per_goal", {})

        # Search in manifest-provided stage3 output dir first, then legacy fallbacks
        search_dirs = []
        if self.config.get("stage3_output_dir"):
            search_dirs.append(self.config["stage3_output_dir"])
        search_dirs += [
            str(ROOT / "Data" / "*" / "*" / "*" / "*" / "Stage-3" / "output"),
        ]
        for base in search_dirs:
            manifests = sorted(glob.glob(str(Path(base) / "stage3_*_manifest.json")))
            if manifests:
                data = self._load_json_safe(manifests[-1])
                if data:
                    return data.get("result", {}).get("stats", {}).get("per_goal", {})
        return {}

    def _discover_latest_report(self) -> Optional[Path]:
        """Auto-discover the most recent training_report.json."""
        candidates = []
        # Check manifest-provided stage5 output dir first
        if self.config.get("stage5_output_dir"):
            s5 = Path(self.config["stage5_output_dir"])
            candidates.extend(glob.glob(str(s5 / "training_report.json")))
            candidates.extend(glob.glob(str(s5 / "models" / "training_report.json")))
        # Legacy fallback patterns
        for pattern in [
            "Data/*/*/*/*/Stage-5/output/training_report.json",
        ]:
            candidates.extend(glob.glob(str(ROOT / pattern)))
        if candidates:
            return Path(max(candidates, key=lambda p: Path(p).stat().st_mtime))
        return None

    def validate_outputs(self, result: AgentResult) -> Tuple[bool, List[str]]:
        """Verify recommendations.json was written with required keys."""
        errors = []
        for path_str in result.outputs:
            p = Path(path_str)
            if not p.exists():
                errors.append(f"Output file not found: {path_str}")
            else:
                try:
                    doc = json.load(open(p))
                    if "recommendations" not in doc:
                        errors.append(f"{p.name}: missing 'recommendations' key")
                except Exception as e:
                    errors.append(f"{p.name}: JSON parse error: {e}")
        return len(errors) == 0, errors


if __name__ == "__main__":
    agent = FeedbackAnalysisAgent(
        config={
            "output_dir": "outputs/feedback_test",
            "recommendation_threshold": 0.7,
            "rejection_rate_threshold": 0.8,
        },
        dry_run="--dry-run" in sys.argv,
    )
    result = agent.run()
    print("\nOutputs:", result.outputs)
    print("Stats:", json.dumps(result.stats, indent=2))
    sys.exit(0 if result.success else 1)

