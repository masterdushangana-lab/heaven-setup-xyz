"""
High-Frequency (5-Second) Silent Watcher Daemon for AnimeHeaven.me
- Polls AnimeHeaven /new.php silently every 5 seconds.
- Detects fresh episode releases instantly (0-5s latency).
- Automatically triggers the GitHub Actions upload pipeline via GitHub REST API.
- Can be run as a lightweight background daemon on your local PC or any VPS.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
from typing import Set

ANIMEHEAVEN_URL = "https://animeheaven.me/new.php"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
POLL_INTERVAL_SECONDS = 5

GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()


def fetch_latest_release_keys() -> Set[str]:
    """Fetches the latest release cards from animeheaven.me/new.php"""
    req = urllib.request.Request(
        ANIMEHEAVEN_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://animeheaven.me/"
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    import re
    pattern = r"""<div class=['"]chart bc1['"]>[\s\S]*?<a href=['"]anime\.php\?([^'"]+)['"]>[\s\S]*?<div class=['"]chartepm[^'"]*['"]>(\d+)</div>"""
    keys = set()
    for m in re.finditer(pattern, html, re.IGNORECASE):
        anime_id = m.group(1).strip()
        ep_num = m.group(2).strip()
        keys.add(f"{anime_id}_ep{ep_num}")
    return keys


def trigger_github_upload_pipeline(new_episodes_count: int):
    """Triggers the GitHub Actions pipeline via repository workflow_dispatch"""
    if not GITHUB_REPO or not GITHUB_TOKEN:
        print("[!] GITHUB_REPO or GITHUB_TOKEN not configured. Skipping workflow dispatch.", file=sys.stderr)
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/animeheaven-auto.yml/dispatches"
    payload = json.dumps({
        "ref": "main",
        "inputs": {
            "target_anime_url": "",
            "target_mal_id": "",
            "episode_range": "all"
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "AnimeHeaven-5s-Watcher",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                print(f"[🔥 TRIGGERED] Dispatched GitHub Actions upload pipeline for {new_episodes_count} new release(s)!", flush=True)
            else:
                print(f"[!] GitHub API status: {resp.status}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[!] Failed to trigger GitHub Action: {e}", file=sys.stderr, flush=True)


def main():
    print("=" * 60)
    print("  AnimeHeaven 5-Second Silent Realtime Watcher Daemon")
    print(f"  Target: {ANIMEHEAVEN_URL}")
    print(f"  Interval: {POLL_INTERVAL_SECONDS} seconds")
    print(f"  GitHub Repo: {GITHUB_REPO or 'Not Set (Set GITHUB_REPO)'}")
    print("=" * 60, flush=True)

    known_keys: Set[str] = set()

    # Initial warm-up load
    try:
        print("[*] Initializing baseline release cache...", flush=True)
        known_keys = fetch_latest_release_keys()
        print(f"[+] Baseline loaded with {len(known_keys)} active releases.", flush=True)
    except Exception as e:
        print(f"[!] Initial load warning: {e}. Will retry on next tick.", file=sys.stderr, flush=True)

    print("[*] Watching AnimeHeaven silently every 5 seconds... (Press Ctrl+C to stop)", flush=True)

    consecutive_errors = 0

    while True:
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
            latest_keys = fetch_latest_release_keys()
            consecutive_errors = 0

            # Detect differences
            new_releases = latest_keys - known_keys
            if new_releases:
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                print(f"\n[⚡ FRESH RELEASE DETECTED] [{timestamp}] -> {list(new_releases)}", flush=True)
                # Update cache
                known_keys.update(latest_keys)
                # Dispatch GitHub Actions upload pipeline immediately
                trigger_github_upload_pipeline(len(new_releases))

        except KeyboardInterrupt:
            print("\n[*] Watcher daemon stopped by user.", flush=True)
            sys.exit(0)
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors % 10 == 1:
                print(f"[!] Network warning ({consecutive_errors}): {e}", file=sys.stderr, flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
