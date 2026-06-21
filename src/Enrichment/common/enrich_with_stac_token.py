#!/usr/bin/env python3
"""Fetch STAC item assets using a Bearer token and write per-item enriched JSONs.

Usage:
  python tools/enrich_with_stac_token.py --metadata-dir metadata_by_item --token <TOKEN> --regenerate-aggregate

This script looks for per-item JSON files in the metadata dir, finds the STAC `self` link,
fetches the STAC feature using the Authorization header, extracts assets (preferring JP2/TIFF),
and writes `<basename>.enriched.json`. It can also regenerate `sar_metadata.json`.
"""

import argparse
import glob
import json
import os
from typing import Optional, Union, Any, Dict, List
import sys
from urllib.parse import urlparse

import requests


def find_self_link(feature_obj):
    links = feature_obj.get("links") or []
    for l in links:
        if l.get("rel") == "self" and l.get("href"):
            return l.get("href")
    return None


def choose_primary_asset(assets):
    # assets: dict of name -> asset metadata
    # prefer jp2, then tiff/tif, then any asset whose href contains 'measurement' or 'SAFE'
    prefers = []
    for name, meta in assets.items():
        href = meta.get("href") if isinstance(meta, dict) else meta
        if not href:
            continue
        href_l = href.lower()
        if href_l.endswith(".jp2"):
            return href
        if href_l.endswith(".tiff") or href_l.endswith(".tif"):
            prefers.append(href)
    if prefers:
        return prefers[0]
    # fallback: any asset with 'measurement' or '.safe' or 'data' in href
    for name, meta in assets.items():
        href = meta.get("href") if isinstance(meta, dict) else meta
        if not href:
            continue
        href_l = href.lower()
        if "measurement" in href_l or ".safe" in href_l or "data" in href_l:
            return href
    # last resort: first asset href
    for name, meta in assets.items():
        href = meta.get("href") if isinstance(meta, dict) else meta
        if href:
            return href
    return None


def enrich_one(filepath, token, out_dir):
    with open(filepath, "r") as fh:
        data = json.load(fh)

    feature = data.get("feature") or data.get("stac") or {}
    self_href = find_self_link(feature)
    if not self_href:
        print(f"-> {os.path.basename(filepath)}  no self link, skipping")
        return False, None

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/geo+json, application/json"}
    try:
        r = requests.get(self_href, headers=headers, timeout=30)
    except Exception as e:
        print(f"-> {os.path.basename(filepath)}  request failed: {e}")
        return False, None

    if r.status_code != 200:
        print(f"-> {os.path.basename(filepath)}  HTTP {r.status_code}, skipping")
        return False, None

    stac_item = r.json()
    assets = stac_item.get("assets", {})
    primary = choose_primary_asset(assets)

    enriched = dict(data)
    enriched["enriched"] = True
    enriched["stac_item_assets"] = assets
    enriched["primary_asset_href"] = primary
    enriched["_stac_self_href"] = self_href

    base = os.path.basename(filepath)
    out_name = os.path.splitext(base)[0] + ".enriched.json"
    out_path = os.path.join(out_dir, out_name)
    with open(out_path, "w") as fh:
        json.dump(enriched, fh, indent=2)

    print(f"-> {base}  enriched -> {out_name}")
    return True, primary


def regenerate_aggregate(metadata_dir, out_path):
    items = []
    enriched_count = 0
    for f in sorted(glob.glob(os.path.join(metadata_dir, "*.json"))):
        if f.endswith(".enriched.json"):
            continue
        try:
            with open(f, "r") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        item_id = d.get("id") or d.get("summary", {}).get("id") or d.get("feature", {}).get("id")
        item = {"id": item_id, "datetime": None, "enriched": False, "primary_asset": None}
        # Include geometry and properties for recompute mapping
        feat = d.get("feature") or (d if d.get("type") == "Feature" else None)
        if feat:
            item["geometry"] = feat.get("geometry")
            item["properties"] = feat.get("properties")

        # look for enriched counterpart
        enriched_path = os.path.splitext(f)[0] + ".enriched.json"
        if os.path.exists(enriched_path):
            try:
                with open(enriched_path, "r") as eh:
                    ed = json.load(eh)
                item["enriched"] = True
                item["primary_asset"] = ed.get("primary_asset_href")
                enriched_count += 1
                # try to get datetime
                item["datetime"] = ed.get("summary", {}).get("datetime") or ed.get("feature", {}).get("properties", {}).get("datetime")
            except Exception:
                pass
        else:
            item["datetime"] = d.get("summary", {}).get("datetime") or d.get("feature", {}).get("properties", {}).get("datetime")

        items.append(item)

    agg = {"features": len(items), "enriched": enriched_count, "items": items}
    with open(out_path, "w") as fh:
        json.dump(agg, fh, indent=2)
    print(f"Wrote aggregate: {out_path}  features={len(items)} enriched={enriched_count}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--metadata-dir", default="metadata_by_item", help="Directory with per-item JSON files")
    p.add_argument("--token", required=True, help="STAC Bearer token")
    p.add_argument("--out-dir", default=None, help="Directory to write enriched jsons (defaults to metadata-dir)")
    p.add_argument("--regenerate-aggregate", action="store_true", help="Regenerate sar_metadata.json in metadata dir")
    args = p.parse_args()

    metadata_dir = args.metadata_dir
    if not os.path.isdir(metadata_dir):
        print(f"metadata-dir {metadata_dir} does not exist", file=sys.stderr)
        sys.exit(2)
    out_dir = args.out_dir or metadata_dir
    token = args.token

    files = sorted([f for f in glob.glob(os.path.join(metadata_dir, "*.json")) if not f.endswith('.enriched.json') and not os.path.basename(f).startswith('sar_metadata')])
    print(f"Found {len(files)} candidate metadata files in {metadata_dir}")
    successes = 0
    for fpath in files:
        ok, primary = enrich_one(fpath, token, out_dir)
        if ok:
            successes += 1

    if args.regenerate_aggregate:
        out_path = os.path.join(metadata_dir, "sar_metadata.json")
        regenerate_aggregate(metadata_dir, out_path)

    print(f"Enrichment complete. successful enrichments: {successes}/{len(files)}")


if __name__ == "__main__":
    main()
