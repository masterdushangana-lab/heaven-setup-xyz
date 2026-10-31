"""
MAL-Indexed Tree History Syncer with Auto-Healing for Past N/A Entries
Encrypts uploaded_episodes.json using MAPPING_PASSWORD before saving to Git.
Automatically merges all downloaded artifacts (Mp4Upload, StreamHG, Telegram).
Zero external dependencies (uses standard library and local modules).
"""

import os
import sys
import json
import time
from typing import Dict, Any, Tuple
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


def auto_heal_history(history: dict, client_id: str) -> Tuple[dict, int]:
    """Scans and automatically resolves missing MAL IDs for existing entries in history."""
    if not isinstance(history, dict) or not client_id:
        return history, 0

    healed = {}
    healed_count = 0
    for key, data in history.items():
        if not isinstance(data, dict):
            continue
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
                healed_count += 1
            else:
                target_key = key
        else:
            target_key = key

        if target_key in healed:
            healed[target_key].setdefault("episodes", {}).update(data.get("episodes", {}))
        else:
            healed[target_key] = data

    return healed, healed_count


def save_history(history: dict, password: str):
    if password:
        output_doc = encrypt_mapping(history, password)
        print(f"[+] Encrypted {STATE_FILE} with AES-256-GCM.")
    else:
        output_doc = history
        print(f"[!] Warning: MAPPING_PASSWORD not set. Saved {STATE_FILE} unencrypted.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, indent=2, ensure_ascii=False)


def main():
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "downloaded_artifacts"
    password = os.getenv("MAPPING_PASSWORD", "").strip() or os.getenv("STATE_ENCRYPTION_PASSWORD", "").strip()
    mal_client_id = os.getenv("MAL_CLIENT_ID", "").strip()

    history = load_history(password)
    # Auto-heal any existing N/A entries in history if MAL_CLIENT_ID provided
    history, healed_count = auto_heal_history(history, mal_client_id)
    updated_count = 0

    # Scan directories for artifact JSONs
    candidate_dirs = [target_dir, "artifacts", "downloaded_artifacts"]
    scanned_paths = set()

    for c_dir in candidate_dirs:
        if os.path.exists(c_dir):
            for root, _, files in os.walk(c_dir):
                for f in files:
                    if f.endswith(".json") and f != STATE_FILE:
                        full_path = os.path.abspath(os.path.join(root, f))
                        if full_path in scanned_paths:
                            continue
                        scanned_paths.add(full_path)

                        try:
                            with open(full_path, "r", encoding="utf-8") as jf:
                                data = json.load(jf)

                            if not isinstance(data, dict):
                                continue

                            # Verify that this is an episode result artifact
                            has_video_upload = bool(
                                data.get("mp4upload") or 
                                data.get("streamhg") or 
                                data.get("telegram") or 
                                data.get("slug") or
                                data.get("embed_url")
                            )
                            if not has_video_upload:
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
                                history[primary_key]["mal_id"] = mal_id

                            history[primary_key].setdefault("episodes", {})

                            rank = data.get("new_php_rank")
                            if rank is not None:
                                computed_ts = int(time.time()) - int(rank)
                            else:
                                computed_ts = 1000000000 + int(ep_num if ep_num.isdigit() else 0)

                            ep_record = history[primary_key]["episodes"].get(ep_num, {})

                            # 1. Mp4Upload Support
                            if data.get("mp4upload") and isinstance(data["mp4upload"], dict) and data["mp4upload"].get("file_code"):
                                ep_record["mp4upload"] = data["mp4upload"]
                                fcode = data["mp4upload"]["file_code"]
                                ep_record["embed_url"] = data["mp4upload"].get("embed_url") or f"https://www.mp4upload.com/embed-{fcode}.html"
                                ep_record["slug"] = fcode
                                if data["mp4upload"].get("direct_mp4"):
                                    ep_record["direct_mp4"] = data["mp4upload"]["direct_mp4"]

                            # 2. StreamHG Support
                            elif data.get("streamhg") and isinstance(data["streamhg"], dict) and data["streamhg"].get("file_code"):
                                ep_record["streamhg"] = data["streamhg"]
                                fcode = data["streamhg"]["file_code"]
                                ep_record["embed_url"] = f"https://hgcloud.to/e/{fcode}"
                                ep_record["slug"] = fcode

                            # 3. Fallback embed / slug
                            elif data.get("embed_url") and not ep_record.get("embed_url"):
                                ep_record["embed_url"] = data["embed_url"]
                            if data.get("slug") and not ep_record.get("slug"):
                                ep_record["slug"] = data["slug"]

                            # 4. Telegram Backup Record
                            if data.get("telegram"):
                                ep_record["telegram"] = data["telegram"]

                            # 5. Metadata
                            if data.get("filename"):
                                ep_record["filename"] = data.get("filename")
                            if data.get("size_mb"):
                                ep_record["size_mb"] = data.get("size_mb")
                            ep_record["uploaded_at"] = computed_ts

                            history[primary_key]["episodes"][ep_num] = ep_record
                            updated_count += 1

                            provider = "Mp4Upload" if "mp4upload" in ep_record else ("StreamHG" if "streamhg" in ep_record else "Embed")
                            f_code = ep_record.get("slug") or "N/A"
                            has_tg = bool(ep_record.get("telegram"))
                            print(f"[+] Mapped MAL[{primary_key}] -> Ep {ep_num} -> {provider}: {f_code} | Telegram: {has_tg}")

                        except Exception as e:
                            print(f"[!] Error parsing {full_path}: {e}")

    if updated_count > 0 or healed_count > 0 or not os.path.exists(STATE_FILE):
        save_history(history, password)
        print(f"[DONE] Successfully synced {updated_count} episodes (healed {healed_count}) into {STATE_FILE}.")
    else:
        print(f"[DONE] No new episodes or heals to save. {STATE_FILE} remains untouched.")


if __name__ == "__main__":
    main()
