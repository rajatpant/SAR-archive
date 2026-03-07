#!/usr/bin/env python3
"""
Run training/validation/testing pipeline with actual S3 downloads.

This version reads AWS creds from /home/codespace/.aws/s3.cfg (if present)
and performs recursive downloads for SAFE prefixes found in the mapping.

Usage:
  python3 tools/run_pipeline.py --sar-id <SAR_ID>

Be careful: this will download potentially large SAFE directories into `outputs/`.
"""
import os
from typing import Optional, Union, Any, Dict, List
import json
import argparse
from typing import Any, Dict, Optional , Optional, Union, Any, Dict, List
from model.s3_utils import get_s3_client

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
OUT_TRAIN = os.path.join(ROOT, 'model', 'inputs', 'training')
OUT_VAL = os.path.join(ROOT, 'model', 'inputs', 'validation')
OUT_TEST = os.path.join(ROOT, 'model', 'inputs', 'testing')
DOWNLOADS_DIR = os.path.join(ROOT, 'outputs')

os.makedirs(OUT_TRAIN, exist_ok=True)
os.makedirs(OUT_VAL, exist_ok=True)
os.makedirs(OUT_TEST, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def _safe_filename(item_id: str) -> str:
    return item_id.replace('/', '_').replace(':', '_').replace(' ', '_') + '.json'


def _write_trip(out_dir: str, trip: Dict[str, Any]):
    outp = os.path.join(out_dir, _safe_filename(trip['sar_id']))
    with open(outp, 'w', encoding='utf-8') as fh:
        json.dump(trip, fh, indent=2)
    return outp


def _s3_to_bucket_key(s3_uri: str) -> Optional[tuple]:
    if not s3_uri or not s3_uri.lower().startswith('s3://'):
        return None
    rest = s3_uri[5:]
    parts = rest.split('/', 1)
    if not parts:
        return None
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ''
    return (bucket, key)


def _download_s3_prefix(s3_client, bucket: str, prefix: str, local_base: str) -> int:
    count = 0
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            rel = key[len(prefix):] if key.startswith(prefix) else key
            local_path = os.path.join(local_base, rel.lstrip('/'))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            try:
                s3_client.download_file(bucket, key, local_path)
                count += 1
                print('Downloaded', key, '->', local_path)
            except Exception as e:
                print('Failed to download', key, e)
    return count


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sar-id', required=True, help='SAR id to process (e.g. S1A_... .SAFE)')
    p.add_argument('--min-intersection', type=float, default=50.0)
    p.add_argument('--post-process', action='store_true', help='run post-download processing after downloads')
    p.add_argument('--no-download', action='store_true', help='Do not download assets; use S3 URIs directly for training')
    args = p.parse_args()

    # load model_selection
    try:
        import importlib.util, sys
        ms_path = os.path.join(ROOT, 'model', 'model_selection.py')
        spec = importlib.util.spec_from_file_location('model_selection', ms_path)
        ms = importlib.util.module_from_spec(spec)
        sys.modules['model_selection'] = ms
        if spec and spec.loader:
            spec.loader.exec_module(ms)
    except Exception as e:
        print('Failed to load model_selection:', e)
        return

    trip = ms.select_triplet_for_sar(args.sar_id, min_intersection_pct=args.min_intersection)
    _write_trip(OUT_TRAIN, trip)
    _write_trip(OUT_VAL, trip)
    _write_trip(OUT_TEST, trip)
    print('Wrote trip files for', args.sar_id)

    # read AWS config
    aws_cfg_path = os.path.expanduser('/home/codespace/.aws/s3.cfg')
    # create client
    s3 = get_s3_client()

    if not s3:
        print('No S3 client available; cannot download data')
        return

    # download sar / train / val / test SAFE prefixes
    for role in ('sar', 'train', 'val', 'test'):
        part = trip.get(role)
        if not part:
            continue
        
        # Use sar_asset_href for sar role, true_asset_href for others
        s3uri = part.get('sar_asset_href') if role == 'sar' else part.get('true_asset_href')
        
        if not s3uri:
            print(f'No S3 URI found for {role}')
            continue
            
        if args.no_download:
            print(f'Skipping download for {role} (no-download enabled). URI: {s3uri}')
            continue

        bk = _s3_to_bucket_key(s3uri)
        if not bk:
            print('Skipping non-s3 uri', s3uri)
            continue
        bucket, key = bk
        # if key doesn't end with '/', treat as prefix by appending '/'
        prefix = key if key.endswith('/') else key + '/'
        local_base = os.path.join(DOWNLOADS_DIR, bucket, key)
        print(f'Syncing s3://{bucket}/{prefix} -> {local_base}')
        n = _download_s3_prefix(s3, bucket, prefix, local_base)
        print(f'Downloaded {n} objects for {role} ({s3uri})')

    if args.post_process:
        # run post-download processing to build model inputs
        try:
            pp_path = os.path.join(ROOT, 'tools', 'post_download_processing.py')
            spec2 = importlib.util.spec_from_file_location('post_download_processing', pp_path)
            pp = importlib.util.module_from_spec(spec2)
            if spec2 and spec2.loader:
                spec2.loader.exec_module(pp)
                # call main within module
                if hasattr(pp, 'main'):
                    pp.main()
        except Exception as e:
            print('Post-download processing failed:', e)


if __name__ == '__main__':
    main()
