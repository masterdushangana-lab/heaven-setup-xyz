"""
MAL-Indexed Tree History Syncer with Auto-Healing for Past N/A Entries
Encrypts uploaded_episodes.json using MAPPING_PASSWORD before saving to Git.
Automatically heals existing N/A entries if new MAL ID metadata becomes available.
"""

import os
import sys
import json
import time
from mapping_crypto import encrypt_mapping, decrypt_mapping
from mal_resolver import resolve_mal_metadata

STATE_FILE = "uploaded_episodes.json"


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
            print(f"[!] Warning reading history: {e}. Starting with empty state.")
            return {}
    return {}


def auto_heal_history(history: dict, client_id: str) -> dict:
    """Scans and automatically resolves missing MAL IDs for existing entries in history."""
    healed = {}
    for key, data in history.items():
        current_mal = data.get("mal_id")
        title = data.get("title", "")
        jp_title = data.get("title_japanese")

        # If previous run had N/A, try to resolve it now using enhanced dual matcher
        if not current_mal:
            print(f"[*] Auto-healing past N/A entry for: '{title}'...")
            res = resolve_mal_metadata(title, jp_title=jp_title, client_id=client_id)
            new_mal = res.get("mal_id")
            if new_mal:
                print(f"  [FIXED] Successfully matched past anime to MAL ID: {new_mal}!")
                data["mal_id"] = new_mal
                data["title_english"] = res.get("title_english") or data.get("title_english") or title
                data["title_japanese"] = res.get("title_japanese") or jp_title
                data["media_type"] = res.get("media_type") or data.get("media_type", "TV")
                target_key = str(new_mal)
            else:
                target_key = key
        else:
            target_key = key

        if target_key in healed:
            healed[target_key]["episodes"].update(data.get("episodes", {}))
        else:
            healed[target_key] = data

    return healed


def save_history(history: dict, password: str):
    if password:
        output_doc = encrypt_mapping(history, password)
        print("[+] Encrypted uploaded_episodes.json with AES-256-GCM.")
    else:
        output_doc = history
        print("[!] Warning: MAPPING_PASSWORD not set. Saved unencrypted.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, indent=2, ensure_ascii=False)


def main():
    artifacts_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts"
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    mal_client_id = os.getenv("MAL_CLIENT_ID", "").strip()

    history = load_history(password)
    # Auto-heal any existing N/A entries in history
    history = auto_heal_history(history, mal_client_id)
    updated_count = 0

    if os.path.exists(artifacts_dir):
        for root, _, files in os.walk(artifacts_dir):
            for f in files:
                if f.endswith(".json"):
                    full_path = os.path.join(root, f)
                    try:
                        with open(full_path, "r", encoding="utf-8") as jf:
                            data = json.load(jf)

                        if not (data.get("success") and data.get("slug")):
                            continue

                        title = data.get("title", "Unknown Anime")
                        ep_num = str(data.get("episode", 1))
                        mal_info = data.get("mal_info") or {}
                        mal_id = mal_info.get("mal_id")

                        primary_key = str(mal_id) if mal_id else title

                        if primary_key not in history:
                            history[primary_key] = {
                                "mal_id": mal_id,
                                "title": title,
                                "title_english": mal_info.get("title_english") or title,
                                "title_japanese": mal_info.get("title_japanese"),
                                "media_type": mal_info.get("media_type", "TV"),
                                "animeheaven_id": data.get("anime_id"),
                                "animeheaven_url": data.get("anime_url"),
                                "episodes": {}
                            }
                        elif not history[primary_key].get("mal_id") and mal_id:
                            # Update existing unindexed entry with newly found MAL ID
                            history[primary_key]["mal_id"] = mal_id

                        history[primary_key]["episodes"][ep_num] = {
                            "slug": data["slug"],
                            "embed_url": f"https://player.abyssplayer.com/{data['slug']}",
                            "filename": data.get("filename"),
                            "size_mb": data.get("size_mb"),
                            "uploaded_at": int(time.time())
                        }
                        updated_count += 1
                        print(f"[+] Mapped MAL[{primary_key}] -> Ep {ep_num} -> {data['slug']}")

                    except Exception as e:
                        print(f"[!] Error parsing {full_path}: {e}")

    save_history(history, password)
    print(f"[DONE] Synced {updated_count} episodes into {STATE_FILE}.")


if __name__ == "__main__":
    main()
