#!/usr/bin/env python3
"""
Small helper script: pick one SAR and one true-color sample from a mapping file
and perform S3 listing + manifest parsing to produce per-sample `.enriched.json`
files under the provided metadata_by_item output folder.

This script intentionally minimises HTTP calls (it talks only to S3) and uses
credentials from /home/codespace/.aws/s3.cfg if present.

Usage:
    python src/enrich_one_from_mapping.py --mapping /path/to/sar_to_true_color_mapping.json \
        --out-dir /path/to/metadata_by_item

If S3 listing fails the script will still create a mapping-based placeholder
`.enriched.json` so the file can be checked into git.
"""
import os, json, tempfile, shutil, argparse, glob
from typing import Optional, Union, Any, Dict, List
from model.s3_utils import get_s3_client as unified_get_s3_client


def get_s3_client(cfg_path: str = ''):
    return unified_get_s3_client()


def s3_list_prefix(s3uri: str, client):
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
        except Exception:
            return []
        for obj in resp.get('Contents', []) if resp.get('Contents') else []:
            keys.append(obj['Key'])
        if resp.get('IsTruncated'):
            cont = resp.get('NextContinuationToken')
        else:
            break
    return [f's3://{bucket}/{k}' for k in keys]


def s3_get_text(s3uri: str, client):
    if client is None or not s3uri.startswith('s3://'):
        return None
    without = s3uri[5:]
    parts = without.split('/', 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ''
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        # Read at most 2MB to avoid hanging on large files, which is plenty for XML metadata
        b = resp['Body'].read(2 * 1024 * 1024)
        try:
            return b.decode('utf-8')
        except Exception:
            return b.decode('latin-1', errors='ignore')
    except Exception:
        return None


def extract_jp2_from_manifest(txt: str):
    if not txt:
        return []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(txt)
        found = set()
        import re
        for el in root.iter():
            if el.text and '.jp2' in el.text:
                for m in re.findall(r"[A-Za-z0-9_\-\.]+\.jp2", el.text):
                    found.add(m)
            for v in el.attrib.values():
                if v and '.jp2' in v:
                    for m in re.findall(r"[A-Za-z0-9_\-\.]+\.jp2", v):
                        found.add(m)
        return sorted(found)
    except Exception:
        import re
        return sorted(set(re.findall(r"[A-Za-z0-9_\-\.]+\.jp2", txt)))


def infer_band_from_filename(fn: str):
    import re
    m = re.search(r"_B(0?\d|8A|8B|9A|10|11|12)\.jp2$", fn, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m2 = re.search(r"\bB(0?\d|8A|8B|9A|10|11|12)\b", fn, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).upper()
    return None


def extract_scientific_metadata(xml_content, filename):
    meta = {}
    try:
        import re
        # Sentinel-1
        m_inc = re.search(r'<(?:safe|gml):(?:incidenceAngle|angle)[^>]*>([^<]+)</(?:safe|gml):(?:incidenceAngle|angle)>', xml_content)
        if not m_inc:
            # S1 alternate
            m_inc = re.search(r'<incidenceAngle[^>]*>([^<]+)</incidenceAngle>', xml_content)
        
        # Sentinel-2 (MTD_MSIL2A or MTD_TL)
        if not m_inc:
            # Try <Mean_Viewing_Incidence_Angle> with <VALUE> (some versions)
            m_inc = re.search(r'<Mean_Viewing_Incidence_Angle[^>]*>\s*<VALUE[^>]*>([^<]+)</VALUE>', xml_content)
        if not m_inc:
            # Try <ZENITH_ANGLE> inside <Mean_Viewing_Incidence_Angle> (typical MTD_TL)
            # Use [\s\S]*? to handle any content between the tags including newlines
            m_inc = re.search(r'<Mean_Viewing_Incidence_Angle[^>]*>[\s\S]*?<ZENITH_ANGLE[^>]*>([^<]+)</ZENITH_ANGLE>', xml_content)
        if not m_inc:
            # Global Zenith Angle (MTD_MSIL2A)
            m_inc = re.search(r'<Mean_Sun_Zenith_Angle[^>]*>[\s\S]*?<VALUE[^>]*>([^<]+)</VALUE>', xml_content)

        if m_inc:
            meta['incidence_angle'] = m_inc.group(1).strip()

        # Orbit Direction (S1 and S2)
        m_orb = re.search(r'<(?:safe:)?orbitDirection[^>]*>([^<]+)</(?:safe:)?orbitDirection>', xml_content)
        if not m_orb:
            m_orb = re.search(r'<SENSING_ORBIT_DIRECTION[^>]*>([^<]+)</SENSING_ORBIT_DIRECTION>', xml_content)
        if m_orb:
            meta['orbit_direction'] = m_orb.group(1).strip()

        # Polarization (S1)
        m_pol = re.search(r'<transmitterReceiverPolarisation[^>]*>([^<]+)</transmitterReceiverPolarisation>', xml_content)
        if m_pol:
            meta['polarization'] = m_pol.group(1).strip()

        # Resolution (S2 mostly has fixed resolutions, or listed in filenames)
        m_res = re.search(r'<RESOLUTION[^>]*>([^<]+)</RESOLUTION>', xml_content)
        if m_res:
            meta['resolution'] = m_res.group(1).strip()
            
    except Exception:
        pass
    return meta


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mapping', required=True)
    p.add_argument('--out-dir', required=True)
    p.add_argument('--cfg', default='')
    args = p.parse_args()

    mapping = json.load(open(args.mapping, 'r', encoding='utf-8'))
    sar_to_true = mapping.get('sar_to_true', [])
    if not sar_to_true:
        sar_to_true = mapping.get('sar_with_matches_sample', [])
    
    if not isinstance(sar_to_true, list):
        sar_to_true = []

    client = get_s3_client(args.cfg)

    # pick first sar and its first match
    sar_choice = None
    true_choice = None
    sar_asset = None
    true_asset = None
    for entry in sar_to_true:
        sid = entry.get('sar_id')
        matches = entry.get('matches', []) or []
        if sid and matches:
            sar_choice = sid
            # pick first match
            for m in matches:
                true_choice = m.get('true_id')
                true_asset = m.get('true_asset_href')
                if true_choice:
                    break
            # sar_asset may be present
            if entry.get('sar_asset_href'):
                sar_asset = entry.get('sar_asset_href')
            if sar_choice and true_choice:
                break
    if not sar_choice or not true_choice:
        print('Could not pick SAR/true pair from mapping')
        return

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    created = []
    for cid, asset_hint, role in ((sar_choice, sar_asset, 'sar'), (true_choice, true_asset, 'true')):
        s3_assets = []
        # if asset_hint present, use it first
        if asset_hint:
            s3_assets.append(asset_hint)
        # try listing
        all_s3_objects = []
        scientific_meta = {}
        for s3uri in s3_assets:
            objs = s3_list_prefix(s3uri, client)
            all_s3_objects.extend(objs)
            # try manifest names
            # Extended candidates for S2 (MTD_TL is often nested)
            candidates = [s3uri.rstrip('/') + '/MTD_MSIL2A.xml', s3uri.rstrip('/') + '/MTD_TL.xml', s3uri.rstrip('/') + '/manifest.safe']
            # Search listed objects for MTD_TL.xml if not found yet
            candidates.extend([o for o in all_s3_objects if o.endswith('MTD_TL.xml') and o not in candidates])
            
            for c in candidates:
                txt = s3_get_text(c, client)
                if txt:
                    # Update scientific metadata if found in manifest
                    found_meta = extract_scientific_metadata(txt, c)
                    scientific_meta.update(found_meta)
                    
                    found = extract_jp2_from_manifest(txt)
                    for fn in found:
                        base = '/'.join(c.split('/')[:-1] + [fn])
                        all_s3_objects.append(base)
            
            # If S1, try looking at one of the annotation files if manifest didn't give everything
            if not scientific_meta.get('incidence_angle') and 'Sentinel-1' in s3uri:
                ann_candidates = [o for o in all_s3_objects if 'annotation/s1' in o.lower() and o.endswith('.xml')]
                if ann_candidates:
                    txt = s3_get_text(ann_candidates[0], client)
                    if txt:
                        found_meta = extract_scientific_metadata(txt, ann_candidates[0])
                        scientific_meta.update(found_meta)

        jp2s = [o for o in sorted(set(all_s3_objects)) if o.lower().endswith('.jp2')]
        bands = []
        band_map = {}
        for jp in jp2s:
            fn = os.path.basename(jp)
            b = infer_band_from_filename(fn)
            if b:
                bands.append(b)
                band_map.setdefault(b, []).append(jp)
        enriched = {
            'id': cid,
            's3_assets_referenced': s3_assets,
            's3_objects_listed': all_s3_objects,
            'jp2_files': jp2s,
            'bands_detected': sorted(set(bands)),
            'band_to_files': band_map,
            'scientific_metadata': scientific_meta,
            'enriched': bool(jp2s or all_s3_objects),
            'role': role,
        }
        # Add high-level scientific fields if present
        for k, v in scientific_meta.items():
            enriched[k] = v
            
        enriched_path = os.path.join(out_dir, cid.replace('/', '_').replace(':', '_') + '.enriched.json')
        atomic_write(enriched_path, enriched)
        created.append(enriched_path)

    print('Wrote enriched metadata files:', ', '.join(created))


if __name__ == '__main__':
    main()
