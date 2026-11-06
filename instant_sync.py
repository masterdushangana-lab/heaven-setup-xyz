"""
Direct In-Runner Mapping Syncer & Committer
Updates uploaded_episodes.json immediately after an episode upload (Abyss and/or Telegram)
and safely pushes to GitHub with atomic retry logic to prevent merge conflicts.
"""

import os
import sys
import json
import time
import subprocess
from typing import Dict, Any

from mapping_crypto import decrypt_mapping, encrypt_mapping

STATE_FILE = "uploaded_episodes.json"


def record_and_push_upload(
    item_meta: Dict[str, Any],
    upload_result: Dict[str, Any],
    password: str,
    max_retries: int = 5
) -> bool:
    """
    Encrypts and updates uploaded_episodes.json directly in the current runner,
    committing and pushing to main with automatic rebase/retry.
    """
    if not password:
        print("[!] MAPPING_PASSWORD not set. Cannot update encrypted mapping file.", file=sys.stderr)
        return False

    mal_id = item_meta.get("mal_id")
    title = item_meta.get("title") or item_meta.get("show_original") or "Unknown Title"
    ep_num = str(item_meta.get("episode", "")).strip()

    if not ep_num:
        print("[!] No episode number found in metadata. Skipping instant sync.")
        return False

    primary_key = str(mal_id) if mal_id else title

    for attempt in range(1, max_retries + 1):
        try:
            # 1. Fetch latest changes from remote to avoid drift
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True)

            # 2. Read and decrypt current history
            history = {}
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict) and raw.get("algorithm") == "AES-256-GCM":
                        history = decrypt_mapping(raw, password)
                    elif isinstance(raw, dict):
                        history = raw
                except Exception as read_err:
                    print(f"[*] Read history warning: {read_err}")

            # 3. Update entry
            if primary_key not in history:
                history[primary_key] = {
                    "mal_id": mal_id,
                    "title": title,
                    "title_english": item_meta.get("title_english") or title,
                    "title_japanese": item_meta.get("title_japanese"),
                    "episodes": {}
                }
            elif not history[primary_key].get("mal_id") and mal_id:
                history[primary_key]["mal_id"] = mal_id

            # Auto-migrate past title-keyed entry to this MAL ID primary_key
            if mal_id and title in history and title != primary_key:
                old_val = history.pop(title)
                if isinstance(old_val, dict) and "episodes" in old_val:
                    for o_ep, o_data in old_val["episodes"].items():
                        history[primary_key].setdefault("episodes", {}).setdefault(o_ep, o_data)

            if not history[primary_key].get("title_english") and item_meta.get("title_english"):
                history[primary_key]["title_english"] = item_meta["title_english"]

            history[primary_key].setdefault("episodes", {})
            ep_record = history[primary_key]["episodes"].get(ep_num, {})

            # Attach Abyss info
            ab_slug = upload_result.get("slug")
            ab_data = upload_result.get("abyss")
            if ab_data and isinstance(ab_data, dict):
                ab_slug = ab_data.get("slug") or ab_data.get("file_id") or ab_slug

            if ab_slug:
                ep_record["slug"] = ab_slug
                ep_record["embed_url"] = f"https://player.abyssplayer.com/{ab_slug}"
                ep_record["abyss"] = {
                    "slug": ab_slug,
                    "file_id": ab_slug,
                    "embed_url": ep_record["embed_url"]
                }

            # Attach Telegram info (fallback if abyss failed or both)
            tg_data = upload_result.get("telegram")
            if tg_data and isinstance(tg_data, dict):
                ep_record["telegram"] = tg_data

            # Timestamp & source
            ep_record["filename"] = item_meta.get("filename") or f"{title}_Ep_{ep_num}.mp4"
            ep_record["source"] = item_meta.get("source", "subsplease")
            ep_record["timestamp"] = int(time.time())

            history[primary_key]["episodes"][ep_num] = ep_record

            # 4. Encrypt and save back
            encrypted_doc = encrypt_mapping(history, password)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(encrypted_doc, f, indent=2, ensure_ascii=False)

            # 5. Git commit & push
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
            subprocess.run(["git", "add", STATE_FILE], check=True)

            status_check = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if not status_check.stdout.strip():
                print(f"[✓] Mapping already up to date for: {title} Ep {ep_num}")
                return True

            commit_msg = f"Auto: Instant Mapping Sync for {title} - Ep {ep_num}"
            subprocess.run(["git", "commit", "-m", commit_msg], check=True)

            push_res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)
            if push_res.returncode == 0:
                print(f"[SUCCESS] Mapping updated and pushed to Git instantly: {title} Ep {ep_num}!")
                return True
            else:
                print(f"[!] Push conflict on attempt {attempt}/{max_retries}. Pulling and retrying...")
                time.sleep(2 * attempt)

        except Exception as e:
            print(f"[!] Instant sync error (attempt {attempt}/{max_retries}): {e}", file=sys.stderr)
            time.sleep(2 * attempt)

    return False
