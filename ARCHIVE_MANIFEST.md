# SAR Archive Manifest

**Archive baseline:** `cca111a8f1c6a74c9b189ce6684623352f334fac`

**Inventory captured:** 2026-10-19
**Purpose:** identify the SAR source retained in this historical repository; this is not an active runtime contract.

## Retained source and configuration

| Category | Tracked paths | Evidence |
|---|---|---|
| Stage agents | `src/Agents/stage0_env_check.py` through `src/Agents/stage6_feedback_analysis.py` | e.g. `src/Agents/stage1_download.py` |
| Connectivity | `src/Connectivity/` | e.g. `src/Connectivity/S3/save_images_data.py` |
| Query | `src/Query/` | e.g. `src/Query/Sentinel/SentinelHub/sentinel_queryutils_listings.py` |
| Enrichment | `src/Enrichment/` | e.g. `src/Enrichment/common/enrich_metadata_items.py` |
| Pipeline configuration | `configs/full_pipeline.json` | e.g. `configs/full_pipeline.json` |
| Pipeline entry points | `src/Tools/run_pipeline_agent.py`, `tools/run_pipeline_agent.py`, `tools/run_pipeline.py` | e.g. `tools/run_pipeline_agent.py` |
| Endpoint documentation | `Global-Config/Connectivity/` | e.g. `Global-Config/Connectivity/S3/config.json` |
| User controls | `User-Controls-Config/Nizhnesvirsky-Lower-Bay/` | e.g. `User-Controls-Config/Nizhnesvirsky-Lower-Bay/Default/Model-Controls/model_controls.json` |

## Complete tracked-path inventory at baseline

```text
Global-Config/Connectivity/S3/config.json
Global-Config/Connectivity/Sentinel/SentinelHub/config.json
User-Controls-Config/Nizhnesvirsky-Lower-Bay/20260315/Attribute-Controls/attribute_controls.json
User-Controls-Config/Nizhnesvirsky-Lower-Bay/20260315/Model-Controls/model_controls.json
User-Controls-Config/Nizhnesvirsky-Lower-Bay/Default/Attribute-Controls/attribute_controls.json
User-Controls-Config/Nizhnesvirsky-Lower-Bay/Default/Model-Controls/model_controls.json
configs/full_pipeline.json
src/Agents/stage0_env_check.py
src/Agents/stage1_download.py
src/Agents/stage2_universal_enrichment.py
src/Agents/stage3_goal_enrichment.py
src/Agents/stage4_data_prep.py
src/Agents/stage5_model_training.py
src/Agents/stage6_feedback_analysis.py
src/Connectivity/S3/__init__.py
src/Connectivity/S3/save_images_data.py
src/Connectivity/Sentinel/SentinelHub/__init__.py
src/Connectivity/Sentinel/__init__.py
src/Connectivity/__init__.py
src/Enrichment/S1/__init__.py
src/Enrichment/S2/__init__.py
src/Enrichment/__init__.py
src/Enrichment/common/__init__.py
src/Enrichment/common/enrich_metadata_items.py
src/Enrichment/common/enrich_one_from_mapping.py
src/Enrichment/common/enrich_with_stac_token.py
src/Query/Sentinel/S3/__init__.py
src/Query/Sentinel/SentinelHub/__init__.py
src/Query/Sentinel/SentinelHub/sentinel_queryutils.py
src/Query/Sentinel/SentinelHub/sentinel_queryutils_listings.py
src/Query/Sentinel/__init__.py
src/Query/__init__.py
src/Tools/run_pipeline_agent.py
tools/run_pipeline.py
tools/run_pipeline_agent.py
```

The baseline consists of 35 tracked files, including 28 Python files; for example, the stage-agent set spans `src/Agents/stage0_env_check.py` to `src/Agents/stage6_feedback_analysis.py`.

## Excluded local and generated artifacts

- No `.config/`, credential file, `.env` file, `.SAFE` scene, model checkpoint, or raster download path is tracked at the baseline; for example, the tracked tree contains endpoint documentation at `Global-Config/Connectivity/S3/config.json`, not `.config/aws/credentials`.
- A high-confidence text scan found no credential values; matches in `src/Agents/stage0_env_check.py` and `src/Query/Sentinel/SentinelHub/sentinel_queryutils_listings.py` are variable names and credential-loading references, not embedded values.
- No test suite, dependency manifest, or environment specification is tracked; for example, the root contains `configs/full_pipeline.json` but no `requirements.txt` or `environment.yml`.

## Inventory verification

From the repository root, compare this list against `git ls-files`; for example, `git ls-files src/Agents` lists the archived stage-agent files. The inventory applies to baseline `cca111a8f1c6a74c9b189ce6684623352f334fac`; future archival changes must update this manifest.
