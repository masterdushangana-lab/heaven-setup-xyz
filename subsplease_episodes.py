"""
SubsPlease Real-Time Ingestion Detector
- Polls SubsPlease API (https://subsplease.org/api/?f=latest&tz=UTC)
- Resolves official MAL ID using AniVault Matching Engine
- Inspects uploaded_episodes.json (encrypted with AES-256-GCM)
- Filters out already uploaded episodes
- Emits GitHub Actions matrix of pending 1080p release tasks.
"""

import os
import sys
import json
import base64
import urllib.request
from typing import Dict, Any, List, Optional

from mapping_crypto import decrypt_mapping
from mal_resolver import resolve_mal_metadata
from detect_episodes import is_already_uploaded, is_episode_blacklisted, load_history

SUBSPLEASE_LATEST_API = "https://subsplease.org/api/?f=latest&tz=UTC"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def fetch_latest_subsplease() -> Dict[str, Any]:
    req = urllib.request.Request(SUBSPLEASE_LATEST_API, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[!] Error fetching SubsPlease API: {e}", file=sys.stderr)
        return {}


def main():
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    limit = int(os.getenv("MAX_EPISODES", "10").strip() or "10")

    print("==================================================================")
    print("      SUBSPLEASE REALTIME 1080P INGESTION ENGINE                  ")
    print("==================================================================")

    history = load_history(password) if password else {}
    deleted_records = history.get("__deleted__", {}) if history else {}

    raw_items = fetch_latest_subsplease()
    if not raw_items:
        print("[!] No releases retrieved from SubsPlease API.")
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
                f_out.write("has_items=false\nmatrix=[]\n")
        return

    print(f"[*] Retrieved {len(raw_items)} recent releases from SubsPlease.")
    pending_tasks = []

    for release_key, release_data in raw_items.items():
        if len(pending_tasks) >= limit:
            break

        show_name = release_data.get("show")
        ep_num = str(release_data.get("episode", "")).strip()

        if not show_name or not ep_num:
            continue

        # Ignore Batch torrents (e.g. "67-85", "01-12", "Batch")
        if "-" in ep_num or "batch" in ep_num.lower() or "batch" in release_key.lower():
            print(f"[-] Skipping batch release from auto-ingest: {release_key} (Ep: {ep_num})")
            continue

        # Look for 1080p magnet
        downloads = release_data.get("downloads", [])
        magnet_1080 = None
        for dl in downloads:
            if dl.get("res") == "1080":
                magnet_1080 = dl.get("magnet")
                break

        if not magnet_1080 and downloads:
            # Fallback to 720p if 1080p not available
            for dl in downloads:
                if dl.get("res") == "720":
                    magnet_1080 = dl.get("magnet")
                    break

        if not magnet_1080:
            continue

        # Resolve MAL ID & Official Metadata
        mal_info = resolve_mal_metadata(show_name, client_id=client_id)
        mal_id = mal_info.get("mal_id")

        # STRICT SAFETY: If MAL ID cannot be resolved, DO NOT QUEUE to prevent corrupting the mapping database!
        if not mal_id:
            print(f"[!] STRICT MAL GUARD: Skipping release '{show_name}' - Ep {ep_num} (MAL ID could not be resolved).")
            continue

        official_title = mal_info.get("title_english") or mal_info.get("title") or show_name

        # Check blacklists
        if is_episode_blacklisted(deleted_records, "", mal_id, official_title, ep_num):
            print(f"[-] Skipping blacklisted release: {official_title} - Ep {ep_num}")
            continue

        # Deduplication check
        if is_already_uploaded(history, "", mal_id, official_title, ep_num, upload_target=upload_target):
            # Also check by show_name
            if is_already_uploaded(history, "", mal_id, show_name, ep_num, upload_target=upload_target):
                continue

        task_key = f"{mal_id or 'nomal'}_{official_title}_ep{ep_num}".replace(" ", "_")
        clean_name = f"{official_title}_Ep_{ep_num}.mp4"

        item_meta = {
            "key": task_key,
            "title": official_title,
            "show_original": show_name,
            "episode": ep_num,
            "mal_id": mal_id,
            "magnet_url": magnet_1080,
            "filename": clean_name,
            "source": "subsplease",
            "upload_target": upload_target
        }

        meta_b64 = base64.b64encode(json.dumps(item_meta).encode("utf-8")).decode("utf-8")
        pending_tasks.append({
            "key": task_key,
            "meta_b64": meta_b64,
            "display": f"{official_title} - Ep {ep_num}"
        })
        print(f"[+] Queued New Release: {official_title} - Episode {ep_num} (MAL ID: {mal_id})")

    print(f"\n[*] Total New Episodes Queued: {len(pending_tasks)}")

    # GitHub Actions Outputs
    if os.getenv("GITHUB_OUTPUT"):
        has_items = "true" if pending_tasks else "false"
        matrix_json = json.dumps(pending_tasks)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
            f_out.write(f"has_items={has_items}\n")
            f_out.write(f"matrix={matrix_json}\n")


if __name__ == "__main__":
    main()
