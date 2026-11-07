"""
Erai-raws 24/7 Real-Time Autonomous Sentinel Daemon
- Polls Erai-raws 1080p RSS (Nyaa) on a tight 5-10 second loop.
- Decrypts mapping to check for brand new releases.
- When ANY new release is detected:
    -> Instantly triggers the high-speed 'erai-auto.yml' Matrix Parallel pipeline (up to 8 parallel encoding runners) via GitHub Actions API!
    -> Marks detected items in local session memory to prevent repeated triggers.
- Runs continuously for up to 5 hours per session (auto-renews via GitHub cron).
- Zero external cron required. Zero risk of GitHub action abuse.
"""

import os
import re
import sys
import time
import json
import base64
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Set, Tuple

from mapping_crypto import decrypt_mapping
from mal_resolver import resolve_mal_metadata, normalize_title
from detect_episodes import is_already_uploaded, is_episode_blacklisted, load_history

ERAI_NYAA_RSS_URL = "https://nyaa.si/?page=rss&u=Erai-raws&q=1080p"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def normalize_episode(ep_str: str) -> str:
    s = str(ep_str).strip()
    s_clean = re.sub(r'v\d+$', '', s, flags=re.IGNORECASE)
    try:
        val = float(s_clean)
        return str(int(val)) if val.is_integer() else str(val)
    except Exception:
        return s_clean.lower()


def fetch_latest_erairaws() -> List[Dict[str, Any]]:
    req = urllib.request.Request(ERAI_NYAA_RSS_URL, headers={"User-Agent": USER_AGENT})
    items = []
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_data = resp.read()
            root = ET.fromstring(xml_data)
            pattern = r"^\[Erai-raws\]\s*(.*?)\s*-\s*(\d+(?:\.\d+)?(?:v\d+)?)\s*(?:\[|\(|\.mkv|$)"

            for item in root.findall("./channel/item"):
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue

                raw_title = title_el.text.strip()
                if "batch" in raw_title.lower() or "01 ~" in raw_title or "01-" in raw_title:
                    continue

                m = re.search(pattern, raw_title, re.IGNORECASE)
                if not m:
                    continue

                items.append({
                    "show": m.group(1).strip(),
                    "episode": m.group(2).strip(),
                    "raw_title": raw_title
                })

        # Deduplicate AVC & HEVC versions in feed: Prefer AVC (H.264)
        unique_releases = {}
        for it in items:
            k = (it["show"].lower(), it["episode"])
            is_avc = "AVC" in it["raw_title"]
            if k not in unique_releases:
                unique_releases[k] = it
            elif not ("AVC" in unique_releases[k]["raw_title"]) and is_avc:
                unique_releases[k] = it
        items = list(unique_releases.values())
    except Exception as e:
        print(f"[!] Erai RSS poll error: {e}", file=sys.stderr)

    return items


def trigger_matrix_pipeline(repo: str, token: str, count: int) -> bool:
    """Dispatches the erai-auto.yml Matrix Parallel Ingestion workflow via GitHub API."""
    url = f"https://api.github.com/repos/{repo}/actions/workflows/erai-auto.yml/dispatches"
    payload = json.dumps({
        "ref": "main",
        "inputs": {
            "max_episodes": str(min(20, max(1, count))),
            "upload_target": "both"
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Erai-Realtime-Sentinel",
            "Content-Type": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 204):
                print(f"[SUCCESS] 🚀 Dispatched 'erai-auto.yml' Matrix Pipeline (8 Parallel Workers) successfully!")
                return True
    except Exception as e:
        print(f"[!] Failed to dispatch matrix pipeline: {e}", file=sys.stderr)

    return False


def run_sentinel(poll_interval: int = 10, max_runtime_seconds: int = 18000):
    repo = os.getenv("GITHUB_REPOSITORY", "").strip() or "masterdushangana-lab/heaven-setup-xyz"
    token = os.getenv("GITHUB_TOKEN", "").strip()
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()

    if not token:
        print("[!] Warning: GITHUB_TOKEN not found. Matrix auto-dispatch requires token.", file=sys.stderr)

    print("==================================================================")
    print("      ERAI-RAWS 24/7 REAL-TIME SENTINEL (SUB-SECOND DETECTOR)     ")
    print(f"      Polling Interval: {poll_interval}s | Session Limit: {max_runtime_seconds}s")
    print(f"      Target Matrix Workflow: erai-auto.yml (Parallel 8 Workers)")
    print("==================================================================")

    start_time = time.time()
    seen_in_session: Set[Tuple[str, str]] = set()
    last_trigger_time = 0
    COOLDOWN_SECONDS = 90  # 90s cooldown between dispatch triggers to allow matrix to spin up

    while time.time() - start_time < max_runtime_seconds:
        try:
            items = fetch_latest_erairaws()
            history = load_history(password) if password else {}
            deleted = history.get("__deleted__", {}) if history else {}

            new_detected = []

            for it in items:
                show = it["show"]
                ep = it["episode"]

                if (show, ep) in seen_in_session:
                    continue

                # Quick local check against mapping
                mal_info = resolve_mal_metadata(show, client_id=client_id)
                mal_id = mal_info.get("mal_id")
                official_title = mal_info.get("title_english") or mal_info.get("title") or show

                if is_episode_blacklisted(deleted, "", mal_id, official_title, ep):
                    seen_in_session.add((show, ep))
                    continue

                if is_already_uploaded(history, "", mal_id, official_title, ep, upload_target="both") or \
                   is_already_uploaded(history, "", mal_id, show, ep, upload_target="both"):
                    seen_in_session.add((show, ep))
                    continue

                # NEW EPISODE CONFIRMED!
                print(f"[!] 🔥 REAL-TIME DETECTION: {official_title} - Ep {ep} (MAL ID: {mal_id})")
                new_detected.append((show, ep))
                seen_in_session.add((show, ep))

            if new_detected:
                now = time.time()
                if now - last_trigger_time >= COOLDOWN_SECONDS:
                    print(f"[*] Found {len(new_detected)} new releases! Firing Erai Matrix Parallel Pipeline...")
                    if token:
                        trigger_matrix_pipeline(repo, token, len(new_detected))
                        last_trigger_time = now
                    else:
                        print("[!] GITHUB_TOKEN missing, cannot trigger matrix automatically.", file=sys.stderr)
                else:
                    print(f"[*] New episodes detected, waiting for cooldown ({int(COOLDOWN_SECONDS - (now - last_trigger_time))}s remaining)...")

        except Exception as err:
            print(f"[!] Sentinel tick error: {err}", file=sys.stderr)

        time.sleep(poll_interval)

    print(f"[*] Sentinel session reached {max_runtime_seconds}s limit. Exiting cleanly for auto-renewal.")


if __name__ == "__main__":
    interval = int(os.getenv("POLL_INTERVAL", "10").strip() or "10")
    hours = float(os.getenv("MAX_RUNTIME_HOURS", "5").strip() or "5")
    max_run = int(os.getenv("MAX_RUNTIME_SECONDS", str(int(hours * 3600))).strip() or 18000)
    run_sentinel(poll_interval=interval, max_runtime_seconds=max_run)
