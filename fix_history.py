"""
Migrates and Fixes Existing N/A entries in uploaded_episodes.json
Scans history, resolves missing MAL IDs using Dual-Title resolver, and merges entries cleanly.
"""

import os
import sys
import json
from mal_resolver import resolve_mal_metadata
from mapping_crypto import decrypt_mapping, encrypt_mapping

STATE_FILE = "uploaded_episodes.json"


def fix_existing_history(password: str):
    if not os.path.exists(STATE_FILE):
        print(f"[*] {STATE_FILE} not found. Nothing to fix.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        content = json.load(f)

    is_encrypted = isinstance(content, dict) and content.get("algorithm") == "AES-256-GCM"
    if is_encrypted:
        if not password:
            print("ERROR: MAPPING_PASSWORD is required to decrypt and fix existing history.")
            sys.exit(1)
        history = decrypt_mapping(content, password)
    else:
        history = content

    print(f"[*] Loaded {len(history)} series from history. Checking for N/A MAL IDs...")

    fixed_history = {}
    updated_count = 0

    for key, data in history.items():
        current_mal = data.get("mal_id")
        title = data.get("title", "")
        jp_title = data.get("title_japanese")

        # If MAL ID is missing / N/A, re-resolve using enhanced dual-title matcher
        if not current_mal:
            print(f"[*] Fixing N/A for: '{title}'...")
            res = resolve_mal_metadata(title, jp_title=jp_title)
            new_mal = res.get("mal_id")
            if new_mal:
                print(f"  [SUCCESS] Found MAL ID: {new_mal} for '{title}'!")
                data["mal_id"] = new_mal
                data["title_english"] = res.get("title_english") or data.get("title_english") or title
                data["title_japanese"] = res.get("title_japanese") or jp_title
                data["media_type"] = res.get("media_type") or data.get("media_type", "TV")
                updated_count += 1
                new_key = str(new_mal)
            else:
                new_key = key
        else:
            new_key = key

        # Merge episodes if key changed
        if new_key in fixed_history:
            fixed_history[new_key]["episodes"].update(data.get("episodes", {}))
        else:
            fixed_history[new_key] = data

    print(f"[DONE] Fixed {updated_count} series with newly matched MAL IDs.")

    # Save back (encrypted if password provided)
    if password:
        out_doc = encrypt_mapping(fixed_history, password)
    else:
        out_doc = fixed_history

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, indent=2, ensure_ascii=False)
    print(f"[+] Saved updated history to {STATE_FILE}.")


if __name__ == "__main__":
    pwd = os.getenv("MAPPING_PASSWORD", "").strip()
    fix_existing_history(pwd)
