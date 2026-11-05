"""
Abyss.to Keep-Alive & Direct File Verification Bot
1. Queries official Abyss File API (https://api.abyss.to/v1/files/{slug}?key={KEY})
2. Pings player embed (https://player.abyssplayer.com/{slug}) to keep CDN cached.
"""

import os
import sys
import json
import time
import urllib.parse
import urllib.request
import concurrent.futures
from typing import Optional
from mapping_crypto import encrypt_mapping, decrypt_mapping
from abyss_pool import get_abyss_api_keys

STATE_FILE = "uploaded_episodes.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def load_history(password: str) -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                content = json.load(f)
            if isinstance(content, dict) and content.get("algorithm") == "AES-256-GCM":
                if not password:
                    raise RuntimeError("MAPPING_PASSWORD required to decrypt uploaded_episodes.json")
                return decrypt_mapping(content, password)
            elif isinstance(content, dict):
                return content
        except Exception as e:
            print(f"[!] Warning reading history: {e}.", file=sys.stderr)
            return {}
    return {}


def ping_abyss_video(slug: str, api_key: str = "") -> dict:
    embed_url = f"https://player.abyssplayer.com/{slug}"
    result = {"slug": slug, "alive": False, "embed_url": embed_url}

    if api_key:
        api_url = f"https://api.abyss.to/v1/files/{slug}?key={urllib.parse.quote(api_key, safe='')}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("id") == slug or data.get("slug") == slug:
                    result["alive"] = True
                    return result
        except Exception as e:
            print(f"[!] API check for {slug} failed: {e}")

    try:
        req = urllib.request.Request(embed_url, headers={"User-Agent": USER_AGENT, "Referer": "https://abyss.to/"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status == 200:
                result["alive"] = True
    except Exception:
        pass

    return result


def main():
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    abyss_keys = get_abyss_api_keys()
    abyss_api_key = abyss_keys[0] if abyss_keys else ""

    if not password:
        print("[ERROR] MAPPING_PASSWORD not set in environment.", file=sys.stderr)
        sys.exit(1)

    print("==================================================================")
    print("            ABYSS.TO KEEP-ALIVE & VERIFICATION BOT                ")
    print("==================================================================")

    history = load_history(password)
    if not history:
        print("[!] No history loaded. Exiting.")
        return

    slugs = set()
    for anime_key, anime_val in history.items():
        if not isinstance(anime_val, dict):
            continue
        episodes = anime_val.get("episodes", {})
        for ep_num, ep_info in episodes.items():
            if not isinstance(ep_info, dict):
                continue
            slug = (
                ep_info.get("abyss", {}).get("slug")
                if isinstance(ep_info.get("abyss"), dict)
                else None
            ) or ep_info.get("slug")
            if slug:
                slugs.add(slug)

    print(f"[*] Discovered {len(slugs)} unique Abyss video file(s) across entire anime library.")
    if not slugs:
        return

    verified = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(ping_abyss_video, s, abyss_api_key): s for s in slugs}
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            if res.get("alive"):
                verified += 1

    print(f"[DONE] Verified {verified}/{len(slugs)} Abyss files alive and cached.")


if __name__ == "__main__":
    main()
