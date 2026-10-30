"""
StreamHG Bulk Video Deleter
Deletes all StreamHG files recorded in uploaded_episodes.json
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from mapping_crypto import decrypt_mapping, encrypt_mapping
from delete_streamhg import delete_single_streamhg_file

STATE_FILE = "uploaded_episodes.json"


def main():
    streamhg_api_key = os.getenv("STREAMHG_API_KEY", "").strip() or os.getenv("ABYSS_API_KEY", "").strip()
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()

    if not streamhg_api_key:
        print("[ERROR] STREAMHG_API_KEY environment variable missing.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print(f"[!] {STATE_FILE} not found. Nothing to delete.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        doc = json.load(f)

    if doc.get("algorithm") == "AES-256-GCM":
        if not mapping_password:
            print("[ERROR] MAPPING_PASSWORD required to decrypt uploaded_episodes.json", file=sys.stderr)
            sys.exit(1)
        history = decrypt_mapping(doc, mapping_password)
    else:
        history = doc

    codes_to_delete = []
    for anime_key, anime in history.items():
        episodes = anime.get("episodes", {})
        for ep_num, ep in episodes.items():
            code = None
            if ep.get("streamhg") and isinstance(ep["streamhg"], dict):
                code = ep["streamhg"].get("file_code")
            elif ep.get("slug"):
                code = ep["slug"]
            if code:
                codes_to_delete.append(code)

    print(f"[*] Found {len(codes_to_delete)} StreamHG video(s) to delete.")
    deleted_count = 0
    for idx, code in enumerate(codes_to_delete, 1):
        print(f"[{idx}/{len(codes_to_delete)}] Deleting {code}...")
        if delete_single_streamhg_file(code, streamhg_api_key):
            deleted_count += 1
        time.sleep(0.2)

    print(f"[DONE] Successfully deleted {deleted_count}/{len(codes_to_delete)} videos from StreamHG.")


if __name__ == "__main__":
    main()
