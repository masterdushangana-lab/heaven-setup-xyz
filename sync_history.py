"""
MAL-Indexed Tree History Syncer with AES-256-GCM Password Encryption
Encrypts uploaded_episodes.json using MAPPING_PASSWORD before saving to Git.
"""

import os
import sys
import json
import time
from mapping_crypto import encrypt_mapping, decrypt_mapping

STATE_FILE = "uploaded_episodes.json"


def load_history(password: str) -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                content = json.load(f)
            # If already encrypted
            if isinstance(content, dict) and content.get("algorithm") == "AES-256-GCM":
                if not password:
                    raise RuntimeError("MAPPING_PASSWORD required to decrypt uploaded_episodes.json")
                return decrypt_mapping(content, password)
            elif isinstance(content, dict):
                return content
        except Exception as e:
            print(f"[!] Warning reading history: {e}. Starting with empty/decrypted state.")
            return {}
    return {}


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

    history = load_history(password)
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

                        history[primary_key]["episodes"][ep_num] = {
                            "slug": data["slug"],
                            "embed_url": data.get("embed_url"),
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
