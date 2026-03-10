"""
Main entry point for running the full ML pipeline using the agent-based architecture.

This script initializes the PipelineOrchestrator and executes the pipeline stages
as defined in the provided configuration file.

Example:
    python -m tools.run_pipeline_agent --config configs/full_pipeline.json --start-stage 1 --end-stage 5
"""
import argparse
import json
from agents.orchestrator import PipelineOrchestrator

def main():
    """CLI entry point for pipeline orchestrator."""
    parser = argparse.ArgumentParser(description="Run ML Pipeline using Agent-based architecture")
    parser.add_argument("--config", default="configs/full_pipeline.json", help="Path to pipeline config")
    parser.add_argument("--start-stage", type=int, default=0, help="Start from stage (0-6)")
    parser.add_argument("--end-stage", type=int, default=6, help="End at stage (1-6)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--auto-apply-feedback", action="store_true", help="Auto-apply feedback recommendations")
    args = parser.parse_args()

    # Load config and inject CLI flags
    with open(args.config) as f:
        config = json.load(f)
    
    config["dry_run"] = args.dry_run
    config["auto_apply_feedback"] = args.auto_apply_feedback

    # Run pipeline
    orchestrator = PipelineOrchestrator(config=config)
    results = orchestrator.run_pipeline(args.start_stage, args.end_stage)

    # Exit with error code if any stage failed
    if any(not r.success for r in results.values()):
        exit(1)

if __name__ == "__main__":
    main()
