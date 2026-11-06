"""
Global Video & Cloud Storage Wiper
- Decrypts uploaded_episodes.json
- Extracts all video slugs (Abyss multi-accounts) & Telegram message IDs
- Iterates through ALL Abyss API accounts in pool and issues DELETE requests
- Issues DELETE requests to Telegram Bot API for all channel messages
- Resets uploaded_episodes.json to a clean empty state (encrypted)
- Commits and pushes changes back to GitHub repository.
"""

import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
from typing import Dict, Any, List, Set

from mapping_crypto import decrypt_mapping, encrypt_mapping
from abyss_pool import AbyssAccountPool

STATE_FILE = "uploaded_episodes.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def delete_from_abyss(slug: str, api_key: str) -> bool:
    """Attempts to delete a file slug from Abyss.to using a specific API key."""
    if not slug or not api_key:
        return False
    url = f"https://api.abyss.to/v1/files/{urllib.parse.quote(slug)}?key={urllib.parse.quote(api_key)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("status") in (200, "success", True) or resp.status in (200, 204)
    except urllib.error.HTTPError as he:
        if he.code in (404, 410):
            # Already deleted
            return True
        return False
    except Exception:
        return False


def delete_from_telegram(bot_token: str, chat_id: str, message_id: int) -> bool:
    """Deletes a message from Telegram channel."""
    if not bot_token or not chat_id or not message_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    payload = json.dumps({"chat_id": chat_id, "message_id": message_id}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data.get("ok", False)
    except Exception:
        return False


def main():
    print("==================================================================")
    print("         GLOBAL CLOUD STORAGE & TELEGRAM VIDEO WIPER              ")
    print("==================================================================")

    password = os.getenv("MAPPING_PASSWORD", "").strip() or os.getenv("STATE_ENCRYPTION_PASSWORD", "").strip()
    tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or os.getenv("TG_CHANNEL_ID", "").strip()

    if not password:
        print("[!] ERROR: MAPPING_PASSWORD secret is required to decrypt history.", file=sys.stderr)
        sys.exit(1)

    # 1. Initialize Abyss Pool
    abyss_pool = AbyssAccountPool()
    all_abyss_keys = [acc.api_key for acc in abyss_pool.accounts]
    print(f"[*] Discovered {len(all_abyss_keys)} Abyss account keys in pool.")

    # 2. Decrypt uploaded_episodes.json
    if not os.path.exists(STATE_FILE):
        print("[!] uploaded_episodes.json does not exist. Nothing to wipe.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        raw_doc = json.load(f)

    history = {}
    if isinstance(raw_doc, dict) and raw_doc.get("algorithm") == "AES-256-GCM":
        history = decrypt_mapping(raw_doc, password)
    elif isinstance(raw_doc, dict):
        history = raw_doc

    if not history:
        print("[*] History mapping is already completely empty.")
        return

    # 3. Collect all video slugs & Telegram message IDs
    abyss_slugs: Set[str] = set()
    tg_messages: Set[int] = set()

    for anime_key, anime_data in list(history.items()):
        if anime_key.startswith("__"):
            continue
        if not isinstance(anime_data, dict):
            continue

        episodes = anime_data.get("episodes", {})
        for ep_num, ep_data in episodes.items():
            if not isinstance(ep_data, dict):
                continue

            # Abyss slug
            slug = ep_data.get("slug")
            ab_obj = ep_data.get("abyss", {})
            if isinstance(ab_obj, dict):
                slug = ab_obj.get("slug") or ab_obj.get("file_id") or slug
            if slug:
                abyss_slugs.add(str(slug).strip())

            # Telegram message ID
            tg_obj = ep_data.get("telegram", {})
            if isinstance(tg_obj, dict):
                msg_id = tg_obj.get("message_id")
                if msg_id:
                    try:
                        tg_messages.add(int(msg_id))
                    except Exception:
                        pass

    print(f"[*] Total unique Abyss video slugs found: {len(abyss_slugs)}")
    print(f"[*] Total unique Telegram channel messages found: {len(tg_messages)}")

    # 4. Delete Abyss Slugs across all multi-accounts
    abyss_deleted = 0
    if abyss_slugs and all_abyss_keys:
        print("\n[*] Starting deletion across Abyss accounts...")
        for idx, slug in enumerate(abyss_slugs, 1):
            success = False
            for k_idx, key in enumerate(all_abyss_keys, 1):
                if delete_from_abyss(slug, key):
                    success = True
                    break
            if success:
                abyss_deleted += 1
            if idx % 10 == 0 or idx == len(abyss_slugs):
                print(f"  -> Processed {idx}/{len(abyss_slugs)} Abyss slugs ({abyss_deleted} successfully deleted).")
            time.sleep(0.1)

    # 5. Delete Telegram messages
    tg_deleted = 0
    if tg_messages and tg_bot_token and tg_chat_id:
        print("\n[*] Starting deletion of Telegram channel messages...")
        for idx, msg_id in enumerate(tg_messages, 1):
            if delete_from_telegram(tg_bot_token, tg_chat_id, msg_id):
                tg_deleted += 1
            if idx % 10 == 0 or idx == len(tg_messages):
                print(f"  -> Processed {idx}/{len(tg_messages)} Telegram messages ({tg_deleted} deleted).")
            time.sleep(0.1)

    # 6. Reset History to Clean State
    print("\n[*] Resetting uploaded_episodes.json to clean empty state...")
    clean_history = {
        "__metadata__": {
            "last_wiped_at": int(time.time()),
            "wiped_abyss_count": abyss_deleted,
            "wiped_tg_count": tg_deleted
        }
    }
    encrypted_clean = encrypt_mapping(clean_history, password)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(encrypted_clean, f, indent=2, ensure_ascii=False)

    print("[+] uploaded_episodes.json encrypted and reset.")

    # 7. Git commit and push reset state
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        subprocess.run(["git", "commit", "-m", f"Global Wipe: Deleted {abyss_deleted} Abyss videos and {tg_deleted} Telegram posts"], check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("[SUCCESS] Repository updated with clean state!")
    except Exception as git_err:
        print(f"[!] Git commit error: {git_err}")

    print("\n==================================================================")
    print(f" WIPEOUT FINISHED: {abyss_deleted} Abyss videos & {tg_deleted} Telegram posts removed.")
    print("==================================================================")


if __name__ == "__main__":
    main()
