"""
MAL-Indexed Tree History Syncer
Collects all artifact json outputs from parallel uploaders and updates uploaded_episodes.json
grouped cleanly by MAL ID and Anime Title with nested episode mappings.
"""

import os
import sys
import json
import time

STATE_FILE = "uploaded_episodes.json"


def load_history() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_history(history: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def main():
    artifacts_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts"
    if not os.path.exists(artifacts_dir):
        print(f"[*] Artifacts directory '{artifacts_dir}' does not exist. Nothing to sync.")
        return

    history = load_history()
    updated_count = 0

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

                    # Primary key: MAL ID as string, fallback to Title
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

                    # Add or update episode details
                    history[primary_key]["episodes"][ep_num] = {
                        "slug": data["slug"],
                        "embed_url": data.get("embed_url"),
                        "filename": data.get("filename"),
                        "size_mb": data.get("size_mb"),
                        "uploaded_at": int(time.time())
                    }
                    updated_count += 1
                    print(f"[+] Mapped MAL[{primary_key}] -> Episode {ep_num} -> {data['slug']}")

                except Exception as e:
                    print(f"[!] Error parsing {full_path}: {e}")

    save_history(history)
    print(f"[DONE] Successfully updated {updated_count} episodes in {STATE_FILE}.")


if __name__ == "__main__":
    main()
