# SAR Archive

This repository preserves the historical SAR pipeline. It is an archival snapshot, not the active development repository; ongoing Vedic SLM, NLP, and graph work belongs in [rajatpant/sar](https://github.com/rajatpant/sar).

## Archived contents

- The historical stage-agent implementation is retained under `src/Agents/stage0_env_check.py` through `src/Agents/stage6_feedback_analysis.py`.
- SAR connectivity and query helpers are retained under `src/Connectivity/` and `src/Query/`, for example `src/Connectivity/S3/save_images_data.py`.
- Metadata enrichment code is retained under `src/Enrichment/`, for example `src/Enrichment/common/enrich_metadata_items.py`.
- Pipeline configuration and runtime entry points are retained at `configs/full_pipeline.json`, `src/Tools/run_pipeline_agent.py`, `tools/run_pipeline_agent.py`, and `tools/run_pipeline.py`.
- Connectivity endpoint documentation and user controls are retained under `Global-Config/` and `User-Controls-Config/`.

The complete tracked-path inventory and archive baseline are recorded in [ARCHIVE_MANIFEST.md](./ARCHIVE_MANIFEST.md).

## Runtime and validation

This archive snapshot contains 28 Python files but no tracked test suite, dependency manifest, or environment specification; for example, `src/Agents/stage1_download.py` is present while no `requirements.txt`, `environment.yml`, or `pyproject.toml` is tracked. Consequently, the archive alone does not define a reproducible runtime environment.

Credentials must remain local and must not be committed; use the ignored `.config/` location for any historical local setup. Scene downloads, outputs, model checkpoints, and logs are also local/generated artifacts and are covered by [.gitignore](./.gitignore).

## Maintenance policy

Treat the repository as historical and read-only. Do not add new SAR features here as part of active SLM work; use the active repository for current development. Reopen this archive only for explicitly requested historical investigation or narrowly scoped SAR maintenance.
