#!/usr/bin/env python3
"""
Enrich all S1 and S2 items in a metadata_by_item directory.
Populates href and generates a report of successes/failures.
"""
import os
from typing import Optional, Union, Any, Dict, List
import json
import glob
import argparse
import tempfile
import shutil
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List , Optional, Union, Any, Dict, List

# Ensure project root (containing the "model" package) is on sys.path when this
# script is executed directly via "python src/enrich_metadata_items.py".
import sys
from pathlib import Path

_this_file = Path(__file__).resolve()
_project_root = _this_file.parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from model.s3_utils import get_s3_client, test_s3_connectivity
from src.Enrichment.common.enrich_one_from_mapping import s3_list_prefix, s3_get_text, extract_jp2_from_manifest, infer_band_from_filename, extract_scientific_metadata

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def _find_self_link(links: list[dict]) -> Optional[str]:
    if not isinstance(links, list):
        return None
    for l in links:
        if isinstance(l, dict) and l.get("rel") == "self" and l.get("href"):
            return l.get("href")
    if links and isinstance(links[0], dict):
        return links[0].get("href")
    return None

def atomic_write(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.', suffix='.tmp', dir=d)
    os.close(fd)
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, indent=2)
            fh.flush(); os.fsync(fh.fileno())
        shutil.move(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

def s3_list_prefix_debug(s3uri: str, client):
    if client is None:
        return []
    if not s3uri.startswith('s3://'):
        return []
    without = s3uri[5:]
    parts = without.split('/', 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ''
    keys = []
    kwargs = {'Bucket': bucket, 'Prefix': prefix}
    cont = None
    while True:
        try:
            resp = client.list_objects_v2(**kwargs) if cont is None else client.list_objects_v2(**{**kwargs, 'ContinuationToken': cont})
        except Exception as e:
            logging.error(f"S3 list failed for {s3uri}: {e}")
            return []
        for obj in resp.get('Contents', []) if resp.get('Contents') else []:
            keys.append(obj['Key'])
        if resp.get('IsTruncated'):
            cont = resp.get('NextContinuationToken')
        else:
            break
    return [f's3://{bucket}/{k}' for k in keys]

def infer_band_v2(fn: str):
    """
    Enhanced band inference for both S2 (B02, B03, etc) and S1 (VV, VH, HH, HV).
    """
    fn_lower = fn.lower()
    
    # Sentinel-2 patterns
    import re
    m = re.search(r"_b(0?\d|8a|8b|9a|10|11|12)[\._]", fn_lower)
    if m:
        return m.group(1).upper()
    
    # Sentinel-1 patterns
    # e.g. s1a-iw-grd-vh-20220104t153232...
    for pol in ['vv', 'vh', 'hh', 'hv']:
        if f"-{pol}-" in fn_lower or f"_{pol}_" in fn_lower:
            return pol.upper()
            
    # Fallback to imported one for Jp2
    return infer_band_from_filename(fn)

def enrich_item(item_data: Dict[str, Any], asset_hint: Optional[str], client) -> Dict[str, Any]:
    cid = item_data.get('id')
    feature = item_data.get('feature', {})
    links = feature.get('links', [])
    
    # Starting based on logic from enrich_one_from_mapping.py
    s3_assets = []
    if asset_hint:
        s3_assets.append(asset_hint)
    
    all_s3_objects = []
    scientific_meta = {}
    if not asset_hint:
        return {
            'id': cid,
            'enriched': False,
            'reason': 'No asset hint or inferred S3 URI'
        }

    for s3uri in s3_assets:
        logging.info(f"Listing {s3uri}")
        objs = s3_list_prefix_debug(s3uri, client)
        all_s3_objects.extend(objs)
        # try manifest names/MTD names
        for c in [s3uri.rstrip('/') + '/MTD_MSIL2A.xml', s3uri.rstrip('/') + '/MTD_TL.xml', s3uri.rstrip('/') + '/manifest.safe']:
            txt = s3_get_text(c, client)
            if txt:
                # Update scientific metadata if found in manifest
                found_meta = extract_scientific_metadata(txt, c)
                scientific_meta.update(found_meta)
                
                found = extract_jp2_from_manifest(txt)
                for fn in found:
                    base = '/'.join(c.split('/')[:-1] + [fn])
                    all_s3_objects.append(base)
        
        # If Sentinel-1, try looking at one of the annotation files if manifest didn't give everything
        if not scientific_meta.get('incidence_angle') and 'Sentinel-1' in s3uri:
            # We need to find annotation files in listed objects
            # Re-list or use already listed ones
            ann_candidates = [o for o in all_s3_objects if 'annotation/s1' in o.lower() and o.endswith('.xml')]
            if ann_candidates:
                txt = s3_get_text(ann_candidates[0], client)
                if txt:
                    found_meta = extract_scientific_metadata(txt, ann_candidates[0])
                    scientific_meta.update(found_meta)
    
    # Filter for interesting files (JP2 for S2, TIFF for S1)
    data_files = [o for o in sorted(set(all_s3_objects)) if o.lower().endswith(('.jp2', '.tiff', '.tif', '.jp2'))]
    
    bands = []
    band_map = {}
    for df in data_files:
        fn = os.path.basename(df)
        b = infer_band_v2(fn)
        if b:
            bands.append(b)
            band_map.setdefault(b, []).append(df)
            
    # Populate href
    # Priority: 1. Self link from metadata 2. Asset hint (S3) 3. None
    self_link = _find_self_link(links)
    href = self_link or asset_hint
    
    # Baseline suitability for specific feature families (band-only heuristic).
    # This will be refined later once metadata XML/XSD quality is assessed.
    suitability = {
        "geographical_features": "High" if "VV" in bands or "VH" in bands else "Low",
        "urban_features": "High" if "VV" in bands and "VH" in bands else "Medium",
        "agricultural_crops": "High" if "VH" in bands else "Medium"
    }

    # Detailed reasoning based on identified bands/assets
    notes = []
    if "VH" in bands and "VV" in bands:
        notes.append("Dual-polarization available: excellent for Urban (double-bounce) and Agricultural (volume scattering) analysis.")
    elif "VV" in bands:
        notes.append("Single-pol VV: useful for geographical water/coastline detection but limited for building structure analysis.")
    
    # Final assembly of enriched metadata
    # Merge properties from the original STAC feature (as fallbacks)
    props = feature.get('properties', {})
    
    # Standardize field names for the model:
    # incidence_angle, orbit_direction, polarization, resolution
    final_scientific = {
        'incidence_angle': scientific_meta.get('incidence_angle') or props.get('sar:center_incidence_angle') or props.get('view:incidence_angle'),
        'orbit_direction': scientific_meta.get('orbit_direction') or props.get('sat:orbit_state'),
        'polarization': scientific_meta.get('polarization') or (", ".join(props.get('sar:polarizations', [])) if isinstance(props.get('sar:polarizations'), list) else props.get('s1:polarization')),
        'resolution': scientific_meta.get('resolution') or props.get('sar:resolution_range') or props.get('s1:resolution'),
    }
    
    # Choose a primary training image href for this item. Prefer an
    # explicit s3_uri if present, else fall back to the first image file.
    primary_image_href = None
    if data_files:
        primary_image_href = data_files[0]

    enriched = {
        'id': cid,
        'href': href,
        'primary_asset_href': s3_assets[0] if s3_assets else None,
        's3_assets_referenced': s3_assets,
        's3_objects_listed': all_s3_objects,
        'image_files': data_files,
        'bands_detected': sorted(set(bands)),
        'band_to_files': band_map,
        'feature_suitability': suitability,
        'analysis_notes': notes,
        'scientific_metadata': final_scientific,
        'training_fields': {
            'sensor': 'S1' if cid.startswith('S1') else ('S2' if cid.startswith('S2') else None),
            'image_s3_href': primary_image_href,
            'incidence_angle': final_scientific.get('incidence_angle'),
            'orbit_direction': final_scientific.get('orbit_direction'),
            'polarization': final_scientific.get('polarization'),
            'resolution': final_scientific.get('resolution'),
        },
        'enriched': bool(all_s3_objects),
    }
    # Spread generic scientific fields to the top-level for easier access by DataChecker
    for k, v in final_scientific.items():
        if v:
            enriched[k] = v
            
    return enriched

def infer_s3_uri(cid: str) -> Optional[str]:
    if not cid:
        return None
    
    # S1 Example: S1A_IW_GRDH_1SDV_20220104T153232_20220104T153257_041313_04E94A_261E_COG.SAFE
    if cid.startswith("S1"):
        import re
        m = re.search(r"_(\d{4})(\d{2})(\d{2})T", cid)
        if m:
            year, month, day = m.groups()
            # Extract product type (e.g. GRDH)
            parts = cid.split('_')
            product = "GRDH" # default
            if len(parts) > 2:
                product = parts[2]
            
            if "_COG.SAFE" in cid:
                # Most GRDH COGs are under IW_GRDH_1S-COG
                return f"s3://EODATA/Sentinel-1/SAR/IW_GRDH_1S-COG/{year}/{month}/{day}/{cid}"
            else:
                # Standard products use their subfolder name
                return f"s3://EODATA/Sentinel-1/SAR/{product}/{year}/{month}/{day}/{cid}"
                
    # S2 Example: S2A_MSIL2A_20220120T091311_N0510_R050_T36VWN_20240503T094824.SAFE
    if cid.startswith("S2"):
        import re
        m = re.search(r"_(\d{4})(\d{2})(\d{2})T", cid)
        if m:
            year, month, day = m.groups()
            # On CDSE, L2A is often under L2A_N0500 or just L2A. 
            # We'll try L2A_N0500 as it's common in the mapping
            return f"s3://EODATA/Sentinel-2/MSI/L2A_N0500/{year}/{month}/{day}/{cid}"

    return None


def _safe_parse_xml(path: str) -> bool:
    """Return True if the given XML/XSD file parses cleanly, False otherwise."""
    try:
        ET.parse(path)
        return True
    except Exception:
        return False


def _inspect_metadata_quality(cid: str, meta_dir: str) -> Dict[str, Any]:
    """Inspect downloaded XML/XSD for a single item to derive basic quality signals.

    This operates only on locally downloaded sidecar files under
      meta_dir/metadata_files/<SAFE_ID>/

    It does not attempt to fully decode mission-specific fields, but checks that
    key annotation, calibration, and schema files exist and are syntactically
    valid XML. The result is used to refine feature_suitability levels.
    """
    base = os.path.join(meta_dir, 'metadata_files', cid)
    quality = {
        'has_manifest': False,
        'n_manifest_ok': 0,
        'n_manifest_total': 0,
        'n_annotation_ok': 0,
        'n_annotation_total': 0,
        'n_calibration_ok': 0,
        'n_calibration_total': 0,
        'n_rfi_ok': 0,
        'n_rfi_total': 0,
        'n_support_xsd_ok': 0,
        'n_support_xsd_total': 0,
    }

    if not os.path.isdir(base):
        return quality

    for root, dirs, files in os.walk(base):
        for fn in files:
            path = os.path.join(root, fn)
            low = path.lower()

            # Manifest
            if fn.lower() == 'manifest.safe':
                quality['n_manifest_total'] += 1
                if _safe_parse_xml(path):
                    quality['n_manifest_ok'] += 1
                continue

            # XML metadata (annotation, MTD, calibration, RFI, etc.)
            if low.endswith('.xml'):
                quality['n_annotation_total'] += 1
                ok = _safe_parse_xml(path)
                if ok:
                    quality['n_annotation_ok'] += 1

                # Specific calibration and RFI annotation flavours
                if '/annotation/calibration/' in low:
                    quality['n_calibration_total'] += 1
                    if ok:
                        quality['n_calibration_ok'] += 1
                if '/annotation/rfi/' in low:
                    quality['n_rfi_total'] += 1
                    if ok:
                        quality['n_rfi_ok'] += 1
                continue

            # Support XSD schemas
            if low.endswith('.xsd') and '/support/' in low:
                quality['n_support_xsd_total'] += 1
                if _safe_parse_xml(path):
                    quality['n_support_xsd_ok'] += 1

    quality['has_manifest'] = quality['n_manifest_ok'] > 0
    return quality


def _score_metadata_group(ok: int, total: int) -> float:
    """Return a 0–1 score for a metadata group based on parse success."""
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, ok / float(total)))


def _refine_feature_suitability(enriched: Dict[str, Any], meta_dir: str,
                                ml_usefulness_categories: Optional[Dict[str, Any]] = None,
                                metadata_field_catalog: Optional[Dict[str, Any]] = None,
                                ignored_fields: Optional[Dict[str, Any]] = None) -> None:
    """Refine feature_suitability using downloaded XML/XSD quality and ML configs.

    This is called *after* all metadata sidecars for the item have been
    downloaded. It upgrades/downgrades the initial band-based suitability to
    reflect whether key incidence/calibration/geometry fields are present and
    syntactically valid in the metadata.
    """
    cid = enriched.get('id') or 'unknown'

    # Inspect local metadata sidecars
    q = _inspect_metadata_quality(cid, meta_dir)

    # Derive simple 0–2 band scores shared across categories
    bands = set(enriched.get('bands_detected') or [])
    has_vv = 'VV' in bands
    has_vh = 'VH' in bands

    band_score_geo = 2 if (has_vv or has_vh) else 0
    band_score_urban = 2 if (has_vv and has_vh) else (1 if (has_vv or has_vh) else 0)
    band_score_agri = 2 if has_vh else (1 if has_vv else 0)

    # Metadata scores: how complete and well-formed are key files?
    ann_score = _score_metadata_group(q['n_annotation_ok'], q['n_annotation_total'])
    calib_score = _score_metadata_group(q['n_calibration_ok'], q['n_calibration_total'])
    rfi_score = _score_metadata_group(q['n_rfi_ok'], q['n_rfi_total'])
    xsd_score = _score_metadata_group(q['n_support_xsd_ok'], q['n_support_xsd_total'])
    manifest_score = 1.0 if q['has_manifest'] else 0.0

    # If a metadata_field_catalog is present, use it to understand which
    # specific Sentinel-1 fields are expected for each category and whether
    # their *source_files* are covered by our successfully parsed groups.
    # This turns the high-level ml_usefulness_categories into a concrete
    # coverage fraction per category.

    def _build_field_index(catalog_block: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        idx: Dict[str, Dict[str, Any]] = {}
        if not isinstance(catalog_block, list):
            return idx
        for entry in catalog_block:
            name = entry.get('field_name')
            if not name:
                continue
            idx[name] = entry
        return idx

    field_index_s1: Dict[str, Dict[str, Any]] = {}
    field_index_s2: Dict[str, Dict[str, Any]] = {}
    if metadata_field_catalog and isinstance(metadata_field_catalog.get('sentinel_1'), list):
        field_index_s1 = _build_field_index(metadata_field_catalog.get('sentinel_1'))
    if metadata_field_catalog and isinstance(metadata_field_catalog.get('sentinel_2'), list):
        field_index_s2 = _build_field_index(metadata_field_catalog.get('sentinel_2'))

    def _field_group_score(field_name: str, field_index: Dict[str, Dict[str, Any]]) -> float:
        """Approximate how well a logical field is supported by local sidecars.

        We map its documented source_files (from metadata_field_catalog) onto
        one of our quality groups (annotation, calibration, rfi, support,
        manifest) and then use the corresponding score.
        """
        info = field_index.get(field_name)
        if not info:
            return max(ann_score, calib_score, xsd_score, manifest_score)
        sources = info.get('source_files') or []
        best = 0.0
        for s in sources:
            s_low = str(s).lower()
            if 'manifest.safe' in s_low:
                best = max(best, manifest_score)
            if '/annotation/' in s_low:
                best = max(best, ann_score)
            if '/annotation/calibration/' in s_low:
                best = max(best, calib_score)
            if '/annotation/rfi/' in s_low:
                best = max(best, rfi_score)
            if '/support/' in s_low or s_low.endswith('.xsd'):
                best = max(best, xsd_score)
        if best == 0.0:
            best = max(ann_score, calib_score, xsd_score, manifest_score)
        return best

    # Normalise ignored_fields into simple lookup sets for S1/S2 logical fields
    ignored_s1: set = set()
    ignored_s2: set = set()
    if isinstance(ignored_fields, dict):
        for name, flag in ignored_fields.items():
            try:
                val = str(flag).strip().lower()
            except Exception:
                continue
            if val != 'y' and val not in ('yes', 'true', '1'):
                continue
            # We don't distinguish S1 vs S2 lists here; the logical
            # field names used in ml_usefulness_categories are shared
            # across sensors (sentinel_1_fields / sentinel_2_fields).
            ignored_s1.add(str(name))
            ignored_s2.add(str(name))

    def _category_coverage(cat_key: str, sensor: str) -> float:
        """Return 0–1 coverage for how many key S1 fields are well-backed.

        For Sentinel-1 products we only consider the sentinel_1_fields list in
        ml_usefulness_categories[cat_key]. A field counts as "covered" if its
        mapped group score exceeds a moderate threshold.
        """
        if not ml_usefulness_categories or not isinstance(enriched.get('id'), str):
            return 1.0

        if sensor == 'S1':
            if not field_index_s1 or not enriched['id'].startswith('S1'):
                return 1.0
            field_index = field_index_s1
        elif sensor == 'S2':
            if not field_index_s2 or not enriched['id'].startswith('S2'):
                return 1.0
            field_index = field_index_s2
        else:
            return 1.0
        cat_block = ml_usefulness_categories.get(cat_key) if isinstance(ml_usefulness_categories, dict) else None
        if not isinstance(cat_block, dict):
            return 1.0
        key = 'sentinel_1_fields' if sensor == 'S1' else 'sentinel_2_fields'
        fields = cat_block.get(key) or []
        fields = [f for f in fields if isinstance(f, str)]
        # Drop any fields the user has explicitly marked as ignored in
        # potential_further_enrichments.ignored_fields so they do not
        # influence coverage or suitability scores.
        ignore_set = ignored_s1 if sensor == 'S1' else ignored_s2
        fields = [f for f in fields if f not in ignore_set]
        if not fields:
            return 1.0
        covered = 0
        for fname in fields:
            if _field_group_score(fname, field_index) >= 0.6:
                covered += 1
        return covered / float(len(fields))
    def to_level(total_score: float) -> str:
        if total_score >= 3.0:
            return "High"
        if total_score >= 1.5:
            return "Medium"
        return "Low"

    # Geographical (SAR view): needs good incidence + calibration + basic geometry.
    meta_score_geo = 1.0 * ann_score + 1.0 * calib_score + 0.5 * xsd_score + 0.5 * manifest_score
    coverage_geo = _category_coverage('geographical_features', 'S1')
    meta_score_geo *= (0.5 + 0.5 * coverage_geo)
    total_geo = band_score_geo + meta_score_geo

    # Urban (SAR view): needs dual-pol, calibration, RFI / noise flags, schemas.
    meta_score_urban = 1.0 * ann_score + 1.0 * calib_score + 0.5 * rfi_score + 0.5 * xsd_score
    coverage_urban = _category_coverage('urban_features', 'S1')
    meta_score_urban *= (0.5 + 0.5 * coverage_urban)
    total_urban = band_score_urban + meta_score_urban

    # Agricultural (SAR view): emphasises annotation (timing, angle profiles) and calibration.
    meta_score_agri = 1.0 * ann_score + 1.0 * calib_score + 0.5 * xsd_score
    coverage_agri = _category_coverage('agricultural_crops', 'S1')
    meta_score_agri *= (0.5 + 0.5 * coverage_agri)
    total_agri = band_score_agri + meta_score_agri

    refined = enriched.get('feature_suitability', {}).copy()
    refined['geographical_features'] = to_level(total_geo)
    refined['urban_features'] = to_level(total_urban)
    refined['agricultural_crops'] = to_level(total_agri)
    enriched['feature_suitability'] = refined

    # Attach numeric suitability scores per-sensor for downstream training.
    # For Sentinel-1 items we expose category-wise continuous scores that
    # underlie the Low/Medium/High labels.
    cid = str(enriched.get('id') or '')
    suitability_score = enriched.get('suitability_score', {}).copy()
    if cid.startswith('S1'):
        suitability_score['s1'] = {
            'geographical_features': float(total_geo),
            'urban_features': float(total_urban),
            'agricultural_crops': float(total_agri),
        }

    # --- S2-specific suitability flag (feature_suitability_s2) ---
    # This provides an optical/S2 view of the same three categories, using
    # Sentinel-2 bands, SCL presence, and the sentinel_2_fields definitions in
    # ml_usefulness_categories and metadata_field_catalog.
    if isinstance(enriched.get('id'), str) and enriched['id'].startswith('S2'):
        bands_opt = set(enriched.get('bands_detected') or [])
        s3_objects = enriched.get('s3_objects_listed') or []
        has_scl = any('scl_20m' in str(o).lower() and str(o).lower().endswith('.jp2') for o in s3_objects)

        # Basic band-based scores
        vis_bands = {'B02', 'B03', 'B04'}
        veg_bands = {'B08', 'B04'}
        ten_m_urban = {'B02', 'B03', 'B04', 'B08'}

        # Geographical: SCL water mask and core visible bands
        geo_band_score_s2 = 0
        if bands_opt & vis_bands:
            geo_band_score_s2 += 1
        if has_scl:
            geo_band_score_s2 += 1

        # Urban: high-res bands plus SCL built-up information
        urban_band_score_s2 = 0
        if len(bands_opt & ten_m_urban) >= 3:
            urban_band_score_s2 += 1
        if has_scl:
            urban_band_score_s2 += 1

        # Agricultural: NDVI bands plus SCL cropland mask
        agri_band_score_s2 = 0
        if veg_bands.issubset(bands_opt):
            agri_band_score_s2 += 1
        if has_scl:
            agri_band_score_s2 += 1

        # Metadata quality for S2, scaled by sentinel_2_fields coverage
        meta_score_geo_s2 = 1.0 * ann_score + 0.5 * xsd_score + 0.5 * manifest_score
        coverage_geo_s2 = _category_coverage('geographical_features', 'S2')
        meta_score_geo_s2 *= (0.5 + 0.5 * coverage_geo_s2)

        meta_score_urban_s2 = 1.0 * ann_score + 0.5 * xsd_score + 0.5 * manifest_score
        coverage_urban_s2 = _category_coverage('urban_features', 'S2')
        meta_score_urban_s2 *= (0.5 + 0.5 * coverage_urban_s2)

        meta_score_agri_s2 = 1.0 * ann_score + 0.5 * xsd_score + 0.5 * manifest_score
        coverage_agri_s2 = _category_coverage('agricultural_crops', 'S2')
        meta_score_agri_s2 *= (0.5 + 0.5 * coverage_agri_s2)

        total_geo_s2 = geo_band_score_s2 + meta_score_geo_s2
        total_urban_s2 = urban_band_score_s2 + meta_score_urban_s2
        total_agri_s2 = agri_band_score_s2 + meta_score_agri_s2

        enriched['feature_suitability_s2'] = {
            'geographical_features': to_level(total_geo_s2),
            'urban_features': to_level(total_urban_s2),
            'agricultural_crops': to_level(total_agri_s2),
        }

        # Numeric S2 scores for Sentinel-2 items
        suitability_score.setdefault('s2', {})
        suitability_score['s2'] = {
            'geographical_features': float(total_geo_s2),
            'urban_features': float(total_urban_s2),
            'agricultural_crops': float(total_agri_s2),
        }

    if suitability_score:
        enriched['suitability_score'] = suitability_score


def _load_existing_report(meta_dir: str) -> Optional[Dict[str, Any]]:
    """Load an existing enrichment_report.json if present (for ML configs)."""
    report_path = os.path.join(meta_dir, "enrichment_report.json")
    if not os.path.exists(report_path):
        return None
    try:
        with open(report_path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None

def _download_metadata_files_for_item(enriched: Dict[str, Any], meta_dir: str, client) -> None:
    """Download XML/XSD/manifest metadata files for a single enriched item.

    This stores small metadata sidecars under
      meta_dir/metadata_files/<SAFE_ID>/<relative_path_inside_SAFE>

    Only non-imagery text metadata (xml, xsd, manifest.safe, MTD_*.xml) is
    downloaded. Large JP2/TIFF imagery is never persisted.
    """
    try:
        cid = enriched.get('id') or 'unknown'
        s3_objects = enriched.get('s3_objects_listed') or []
        s3_assets = enriched.get('s3_assets_referenced') or []
        if not s3_objects or not s3_assets or client is None:
            return

        # Choose a base SAFE prefix to preserve relative layout inside SAFE
        base_prefix = None
        for a in s3_assets:
            if isinstance(a, str) and a.endswith('.SAFE'):
                base_prefix = a.rstrip('/')
                break
        if base_prefix is None:
            # fall back to the first asset href
            first = s3_assets[0]
            if isinstance(first, str):
                base_prefix = first.rstrip('/')

        target_root = os.path.join(meta_dir, 'metadata_files', cid)

        for obj_uri in sorted(set(s3_objects)):
            if not isinstance(obj_uri, str):
                continue
            low = obj_uri.lower()
            # Skip obvious image or overview files
            if low.endswith(('.jp2', '.tif', '.tiff', '.jpg', '.jpeg', '.png', '.ovs')):
                continue

            is_meta = (
                low.endswith('.xml')
                or low.endswith('.xsd')
                or low.endswith('manifest.safe')
                or 'mtd_' in low
            )
            if not is_meta:
                continue

            # Compute relative path inside SAFE to keep structure shallow but informative
            rel_path = os.path.basename(obj_uri)
            if base_prefix and obj_uri.startswith(base_prefix):
                rel = obj_uri[len(base_prefix):].lstrip('/')
                if rel:
                    rel_path = rel

            target_path = os.path.join(target_root, rel_path)
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                txt = s3_get_text(obj_uri, client)
                if txt is None:
                    continue
                with open(target_path, 'w', encoding='utf-8') as fh:
                    fh.write(txt)
            except Exception as e:
                logging.warning(f"Failed to download metadata file {obj_uri} for {cid}: {e}")
    except Exception as e:
        logging.warning(f"Unexpected error while downloading metadata files: {e}")

def main():
    parser = argparse.ArgumentParser(description="Enrich all items in metadata_by_item")
    parser.add_argument("--dir", required=True, help="Path to metadata_by_item directory")
    parser.add_argument("--mapping", help="Optional path to sar_to_true_color_mapping.json for asset hints")
    parser.add_argument("--force", action="store_true", help="Force enrichment even if .enriched.json exists")
    args = parser.parse_args()
    
    meta_dir = args.dir
    if not os.path.isdir(meta_dir):
        logging.error(f"Directory not found: {meta_dir}")
        return

    # Load mapping for asset hints if available
    asset_hints = {}
    if args.mapping and os.path.exists(args.mapping):
        try:
            with open(args.mapping, 'r') as f:
                mapping = json.load(f)
                # S1 hints
                for entry in mapping.get('sar_to_true', []):
                    if entry.get('sar_id') and entry.get('sar_asset_href'):
                        asset_hints[entry['sar_id']] = entry['sar_asset_href']
                    # Also hints from matches
                    for m in entry.get('matches', []):
                        if m.get('true_id') and m.get('true_asset_href'):
                            asset_hints[m['true_id']] = m['true_asset_href']
                # S2 hints (reverse mapping)
                for entry in mapping.get('true_to_sar', []):
                    if entry.get('true_id') and entry.get('true_asset_href'):
                        asset_hints[entry['true_id']] = entry['true_asset_href']
        except Exception as e:
            logging.warning(f"Could not load mapping hints: {e}")

    client = get_s3_client()
    
    # Test S3 connectivity before starting enrichment
    logging.info("Testing S3 connectivity...")
    ok, err = test_s3_connectivity(client)
    if not ok:
        logging.error(f"S3 connectivity test failed: {err}")
        # Write a dedicated failure report
        report = {
            "summary": {"total": 0, "successful": 0, "failed": 0, "skipped": 0},
            "status": "failed",
            "failure_reason": f"S3 connectivity test failed: {err}",
            "diagnostic_tips": [
                "Check if CDSE S3 endpoint is accessible.",
                "Verify proxy settings if applicable.",
                "Confirm that s3://EODATA bucket allows unsigned access or that valid keys are in .s3cfg."
            ]
        }
        report_path = os.path.join(meta_dir, "enrichment_report.json")
        atomic_write(report_path, report)
        logging.error(f"Connectivity failure report written to {report_path}")
        return

    # If a previous enrichment_report.json exists with extended metadata such as
    # ml_usefulness_categories and metadata_field_catalog, load it so we can
    # (a) preserve those sections when writing a new report and (b) reuse
    # their configuration when refining feature_suitability.
    existing_report = _load_existing_report(meta_dir)

    files = sorted(glob.glob(os.path.join(meta_dir, "*.SAFE.json")))
    logging.info(f"Found {len(files)} items to process")
    
    successes = []
    failures = []
    skipped = []
    
    for fpath in files:
        cid_guess = os.path.basename(fpath).replace(".SAFE.json", "")
        out_path = fpath.replace(".json", ".enriched.json")
        
        if os.path.exists(out_path) and not args.force:
            logging.info(f"Skipping already enriched: {cid_guess}")
            skipped.append(cid_guess)
            continue

        try:
            with open(fpath, 'r') as f:
                item_data = json.load(f)
            
            cid = item_data.get('id')
            hint = asset_hints.get(cid)
            
            # If hint is missing, try to infer from feature assets if present
            if not hint:
                assets = item_data.get('feature', {}).get('assets', {})
                # Try to find an S3 URI in assets
                for a_key, a_val in assets.items():
                    if isinstance(a_val, dict) and a_val.get('href', '').startswith('s3://'):
                        hint = a_val['href']
                        break
            
            # Final fallback: Infer from ID
            if not hint:
                hint = infer_s3_uri(cid)
            
            enriched = enrich_item(item_data, hint, client)

            out_path = fpath.replace(".json", ".enriched.json")
            atomic_write(out_path, enriched)

            # Best-effort download of all small metadata sidecars (XML/XSD/manifest)
            _download_metadata_files_for_item(enriched, meta_dir, client)

            # Once all metadata is downloaded for this item, refine
            # feature_suitability by cross-checking data quality in the
            # downloaded XML/XSD. This uses both band information and
            # sidecar quality signals.
            pfe = (existing_report or {}).get('potential_further_enrichments') if existing_report else None
            ml_cfg = None
            ignored_fields = None
            if isinstance(pfe, dict):
                # ml_usefulness_categories may be stored either at the top
                # level of the report or nested under
                # potential_further_enrichments for backward compatibility.
                ml_cfg = pfe.get('ml_usefulness_categories')
                ignored_fields = pfe.get('ignored_fields')
            if ml_cfg is None and existing_report:
                ml_cfg = existing_report.get('ml_usefulness_categories')

            _refine_feature_suitability(
                enriched,
                meta_dir,
                ml_cfg,
                (existing_report or {}).get('metadata_field_catalog') if existing_report else None,
                ignored_fields,
            )

            # Re-write the enriched JSON with updated suitability.
            atomic_write(out_path, enriched)
            
            if enriched.get('enriched'):
                successes.append(cid)
                logging.info(f"Enriched: {cid}")
            else:
                failures.append({"id": cid, "reason": "No S3 objects found or listed"})
                logging.warning(f"Partial/Failed enrichment (no objects): {cid}")
                
        except Exception as e:
            failures.append({"id": os.path.basename(fpath), "reason": str(e)})
            logging.error(f"Error processing {fpath}: {e}")
            
    # Write report. If an existing report contains richer documentation
    # (metadata_field_catalog, ml_usefulness_categories, extended
    # potential_further_enrichments), preserve those sections.
    report = {
        "summary": {
            "total": len(files),
            "successful": len(successes),
            "failed": len(failures),
            "skipped": len(skipped)
        },
        "feature_suitability_heuristic": {
            "description": "Per-item feature_suitability flags (Low/Medium/High) for geographical_features, urban_features, and agricultural_crops are derived from a combination of detected SAR bands (VV/VH) and the presence and parse quality of key Sentinel-1 XML/XSD metadata sidecars.",
            "band_logic": {
                "geographical_features_s1": "Base score is highest when at least VV or VH backscatter is available in Sentinel-1.",
                "urban_features_s1": "Base score is highest when both VV and VH are present, with a weaker score when only one polarisation is available.",
                "agricultural_crops_s1": "Base score is highest when VH is present (volume scattering), with VV providing a weaker fallback.",
                "geographical_features_s2": "Base score is highest when S2 visible bands (B02/B03/B04) and SCL_20m are present.",
                "urban_features_s2": "Base score is highest when several 10 m bands (B02/B03/B04/B08) and SCL_20m are present.",
                "agricultural_crops_s2": "Base score is highest when S2 vegetation index bands (B04/B08) and SCL_20m are present."
            },
            "metadata_logic": {
                "inputs": "Downloaded manifest.safe, XML metadata (annotation, MTD, calibration and RFI where applicable), and support XSD files under metadata_files/<SAFE_ID>/.",
                "quality_scoring": "Each metadata group contributes a 0–1 score based on how many files in that group parse as valid XML.",
                "catalog_usage": "metadata_field_catalog and ml_usefulness_categories specify which Sentinel-1 and Sentinel-2 fields are most relevant for each category; a field is treated as well-supported when its documented source_files map to metadata groups that parse cleanly.",
                "aggregation": "For each category and sensor (S1/S2), a metadata score is computed from group quality and then scaled by the fraction of relevant fields whose source_files are well-supported. This metadata score is added to the band-based score and mapped to Low/Medium/High via numeric thresholds."
            },
            "levels": {
                "High": "Total band+metadata score >= 3.0.",
                "Medium": "Total band+metadata score in [1.5, 3.0).",
                "Low": "Total band+metadata score < 1.5."
            },
            "outputs": {
                "feature_suitability": "SAR-centric suitability (primarily Sentinel-1) with per-category Low/Medium/High.",
                "feature_suitability_s2": "Optical-centric suitability (Sentinel-2) with per-category Low/Medium/High, populated for S2 products only."
            }
        },
        "descriptions": {
            "href": "STAC-compliant link or URL to the metadata item.",
            "s3_assets_referenced": "The root S3 URI(s) used as a starting point for asset discovery.",
            "s3_objects_listed": "Full recursive listing of all files found within the .SAFE asset on S3.",
            "image_files": "Filtered list of relevant data files (.tiff for S1, .jp2 for S2).",
            "bands_detected": "Friendly names of detected bands (e.g., VV, VH for SAR; B02, B04 for Optical).",
            "band_to_files": "Mapping of band names to their specific S3 URIs."
        },
        "successful_ids": successes,
        "skipped_ids": skipped,
        "failures": failures
    }

    # If an existing report had richer potential_further_enrichments or
    # metadata_field_catalog/ml_usefulness_categories, carry them forward
    # and attach explicit catalog field bindings so downstream code can map
    # directly to metadata_field_catalog.field_name values.
    if existing_report:
        if existing_report.get("potential_further_enrichments"):
            report["potential_further_enrichments"] = existing_report["potential_further_enrichments"]
        if existing_report.get("metadata_field_catalog"):
            report["metadata_field_catalog"] = existing_report["metadata_field_catalog"]
        # Preserve any configured ml_usefulness_categories at the top level
        # so downstream tools (and humans) can continue to see the catalog
        # groupings that were used when computing suitability scores.
        if existing_report.get("ml_usefulness_categories"):
            report["ml_usefulness_categories"] = existing_report["ml_usefulness_categories"]

        # Derive explicit catalog field bindings for each high-level
        # category (geographical_features, urban_features,
        # agricultural_crops) from ml_usefulness_categories. This gives
        # machine-usable links from potential_further_enrichments to the
        # exact field_name values in metadata_field_catalog.
        ml_cfg = existing_report.get("ml_usefulness_categories")
        pfe = report.get("potential_further_enrichments")
        if isinstance(ml_cfg, dict) and isinstance(pfe, dict):
            bindings: Dict[str, Dict[str, Any]] = {}
            for cat_key in ("geographical_features", "urban_features", "agricultural_crops"):
                cat_block = ml_cfg.get(cat_key)
                if not isinstance(cat_block, dict):
                    continue
                entry: Dict[str, Any] = {}
                s1_list = [f for f in (cat_block.get("sentinel_1_fields") or []) if isinstance(f, str)]
                s2_list = [f for f in (cat_block.get("sentinel_2_fields") or []) if isinstance(f, str)]
                if s1_list:
                    entry["sentinel_1_fields"] = s1_list
                if s2_list:
                    entry["sentinel_2_fields"] = s2_list
                if entry:
                    bindings[cat_key] = entry
            if bindings:
                # Attach under a dedicated key so human-readable
                # relevant_fields / recommended_features remain intact
                # but tools can resolve exact catalog field names.
                pfe["catalog_field_bindings"] = bindings
                report["potential_further_enrichments"] = pfe
    report_path = os.path.join(meta_dir, "enrichment_report.json")
    atomic_write(report_path, report)
    logging.info(f"Report written to {report_path}")

if __name__ == "__main__":
    main()
