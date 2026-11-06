"""
Unified Real-Time Ingestion Detector: SubsPlease + Erai-raws
- Polls SubsPlease API (Crunchyroll simulcasts)
- Polls Erai-raws RSS via Nyaa (Crunchyroll, Netflix, HIDIVE, Disney+, Amazon Prime)
- Resolves official MAL ID using AniVault Matching Engine
- Multi-Layer Deduplication:
    * Skips releases already present in encrypted uploaded_episodes.json
    * Cross-deduplicates releases between SubsPlease and Erai-raws within the same run
- Emits GitHub Actions matrix of pending release tasks for parallel processing.
"""

import os
import re
import sys
import json
import base64
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Set

from mapping_crypto import decrypt_mapping
from mal_resolver import resolve_mal_metadata
from detect_episodes import is_already_uploaded, is_episode_blacklisted, load_history

SUBSPLEASE_LATEST_API = "https://subsplease.org/api/?f=latest&tz=UTC"
ERAI_NYAA_RSS_URL = "https://nyaa.si/?page=rss&u=Erai-raws&q=1080p"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

TRACKERS = (
    "&tr=http%3A%2F%2Fnyaa.tracker.wf%3A7777%2Fannounce"
    "&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce"
    "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
    "&tr=udp%3A%2F%2Fexodus.desync.com%3A6969%2Fannounce"
    "&tr=udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce"
)


def normalize_episode(ep_str: str) -> str:
    """Normalizes episode strings (e.g. '06' -> '6', '19' -> '19', '7.5' -> '7.5')."""
    s = str(ep_str).strip()
    if s.isdigit():
        return str(int(s))
    return s.lower()


def fetch_latest_subsplease() -> Dict[str, Any]:
    req = urllib.request.Request(SUBSPLEASE_LATEST_API, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[!] Warning: Error fetching SubsPlease API: {e}", file=sys.stderr)
        return {}


def fetch_latest_erairaws() -> List[Dict[str, Any]]:
    """
    Fetches and parses Erai-raws 1080p releases from Nyaa RSS.
    Returns list of dicts: {'show': show_name, 'episode': ep_num, 'magnet': magnet_url, 'raw_title': title}
    """
    req = urllib.request.Request(ERAI_NYAA_RSS_URL, headers={"User-Agent": USER_AGENT})
    items = []
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            xml_data = resp.read()
            root = ET.fromstring(xml_data)

            # Regex pattern to extract show name and episode number from Erai-raws release title
            pattern = r"^\[Erai-raws\]\s*(.*?)\s*-\s*(\d+(?:\.\d+)?(?:v\d+)?)\b"

            for item in root.findall("./channel/item"):
                title_el = item.find("title")
                link_el = item.find("link")
                if title_el is None or not title_el.text:
                    continue

                raw_title = title_el.text.strip()
                link = link_el.text.strip() if link_el is not None and link_el.text else ""

                # Ignore batch torrents
                if "batch" in raw_title.lower() or "01 ~" in raw_title or "01-" in raw_title:
                    continue

                m = re.search(pattern, raw_title, re.IGNORECASE)
                if not m:
                    continue

                show_name = m.group(1).strip()
                ep_num = m.group(2).strip()

                # Extract infoHash for instant DHT magnet construction
                info_hash = None
                for child in item:
                    if child.tag.endswith("infoHash") and child.text:
                        info_hash = child.text.strip()
                        break

                if info_hash:
                    magnet_url = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(raw_title)}{TRACKERS}"
                elif link:
                    magnet_url = link
                else:
                    continue

                items.append({
                    "show": show_name,
                    "episode": ep_num,
                    "magnet": magnet_url,
                    "raw_title": raw_title
                })
    except Exception as e:
        print(f"[!] Warning: Error fetching Erai-raws Nyaa RSS: {e}", file=sys.stderr)

    return items


def main():
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    limit = int(os.getenv("MAX_EPISODES", "30").strip() or "30")

    print("==================================================================")
    print("      UNIFIED SIMULCAST INGESTION: SUBSPLEASE + ERAI-RAWS         ")
    print("==================================================================")

    history = load_history(password) if password else {}
    deleted_records = history.get("__deleted__", {}) if history else {}

    pending_tasks = []
    # Set of (mal_id, normalized_ep) already queued in this current run
    queued_keys: Set[Tuple[int, str]] = set()

    def is_queued(mal_id: int, ep: str) -> bool:
        norm = normalize_episode(ep)
        return (mal_id, norm) in queued_keys or (mal_id, str(ep).strip()) in queued_keys

    def mark_queued(mal_id: int, ep: str):
        norm = normalize_episode(ep)
        queued_keys.add((mal_id, norm))
        queued_keys.add((mal_id, str(ep).strip()))

    # -------------------------------------------------------------
    # 1. Fetch & Process SubsPlease Releases
    # -------------------------------------------------------------
    sp_items = fetch_latest_subsplease()
    print(f"[*] Retrieved {len(sp_items)} recent releases from SubsPlease.")

    for release_key, release_data in sp_items.items():
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
            for dl in downloads:
                if dl.get("res") == "720":
                    magnet_1080 = dl.get("magnet")
                    break

        if not magnet_1080:
            continue

        # Resolve MAL ID & Official Metadata
        mal_info = resolve_mal_metadata(show_name, client_id=client_id)
        mal_id = mal_info.get("mal_id")

        if not mal_id:
            print(f"[!] STRICT MAL GUARD: Skipping release '{show_name}' - Ep {ep_num} (MAL ID could not be resolved).")
            continue

        official_title = mal_info.get("title_english") or mal_info.get("title") or show_name

        # Check blacklists
        if is_episode_blacklisted(deleted_records, "", mal_id, official_title, ep_num):
            print(f"[-] Skipping blacklisted release: {official_title} - Ep {ep_num}")
            continue

        # Deduplication check against history
        if is_already_uploaded(history, "", mal_id, official_title, ep_num, upload_target=upload_target) or \
           is_already_uploaded(history, "", mal_id, show_name, ep_num, upload_target=upload_target):
            print(f"[-] Skipping already uploaded release (History): {official_title} - Ep {ep_num}")
            continue

        # In-run queue deduplication check
        if is_queued(mal_id, ep_num):
            continue

        mark_queued(mal_id, ep_num)

        safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
        task_key = re.sub(r"_+", "_", f"{mal_id}_{safe_title}_ep{ep_num}")
        clean_name = f"{safe_title}_Ep_{ep_num}.mp4"

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
            "display": f"[SubsPlease] {official_title} - Ep {ep_num}"
        })
        print(f"[+] Queued SubsPlease Release: {official_title} - Episode {ep_num} (MAL ID: {mal_id})")

    # -------------------------------------------------------------
    # 2. Fetch & Process Erai-raws Releases (Zero Duplicates!)
    # -------------------------------------------------------------
    erai_items = fetch_latest_erairaws()
    print(f"[*] Retrieved {len(erai_items)} recent releases from Erai-raws.")

    for item in erai_items:
        if len(pending_tasks) >= limit:
            break

        show_name = item["show"]
        ep_num = str(item["episode"]).strip()
        magnet_url = item["magnet"]

        if not show_name or not ep_num or not magnet_url:
            continue

        # Resolve MAL ID & Official Metadata
        mal_info = resolve_mal_metadata(show_name, client_id=client_id)
        mal_id = mal_info.get("mal_id")

        if not mal_id:
            print(f"[!] STRICT MAL GUARD: Skipping Erai-raws '{show_name}' - Ep {ep_num} (MAL ID could not be resolved).")
            continue

        official_title = mal_info.get("title_english") or mal_info.get("title") or show_name

        # Check blacklists
        if is_episode_blacklisted(deleted_records, "", mal_id, official_title, ep_num):
            print(f"[-] Skipping blacklisted release: {official_title} - Ep {ep_num}")
            continue

        # Deduplication check against history (e.g. SubsPlease uploaded it yesterday/earlier)
        if is_already_uploaded(history, "", mal_id, official_title, ep_num, upload_target=upload_target) or \
           is_already_uploaded(history, "", mal_id, show_name, ep_num, upload_target=upload_target):
            print(f"[-] Skipping duplicate release: {official_title} - Ep {ep_num} (already uploaded in library)")
            continue

        # Deduplication check against current run (e.g. SubsPlease queued it above)
        if is_queued(mal_id, ep_num):
            print(f"[-] Skipping duplicate release: {official_title} - Ep {ep_num} (already queued via SubsPlease)")
            continue

        mark_queued(mal_id, ep_num)

        safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
        task_key = re.sub(r"_+", "_", f"{mal_id}_{safe_title}_ep{ep_num}")
        clean_name = f"{safe_title}_Ep_{ep_num}.mp4"

        item_meta = {
            "key": task_key,
            "title": official_title,
            "show_original": show_name,
            "episode": ep_num,
            "mal_id": mal_id,
            "magnet_url": magnet_url,
            "filename": clean_name,
            "source": "erai-raws",
            "upload_target": upload_target
        }

        meta_b64 = base64.b64encode(json.dumps(item_meta).encode("utf-8")).decode("utf-8")
        pending_tasks.append({
            "key": task_key,
            "meta_b64": meta_b64,
            "display": f"[Erai-raws] {official_title} - Ep {ep_num}"
        })
        print(f"[+] Queued Erai-raws Release: {official_title} - Episode {ep_num} (MAL ID: {mal_id})")

    print(f"\n==================================================================")
    print(f"[*] Total New Unique Episodes Queued: {len(pending_tasks)}")
    print("==================================================================")

    # GitHub Actions Outputs
    if os.getenv("GITHUB_OUTPUT"):
        has_items = "true" if pending_tasks else "false"
        matrix_json = json.dumps(pending_tasks)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
            f_out.write(f"has_items={has_items}\n")
            f_out.write(f"matrix={matrix_json}\n")


if __name__ == "__main__":
    main()
