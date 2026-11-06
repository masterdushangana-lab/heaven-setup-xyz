"""
Nyaa.si Single Episode Worker
1. Downloads selected episode file index from torrent via aria2c (--select-file).
2. Burns official embedded subtitles into video via FFmpeg (<500MB target).
3. Concurrent upload to Abyss Multi-Account Pool & Telegram Channel.
4. Generates result artifact for sync & history encryption.
"""

import os
import re
import sys
import time
import json
import base64
import shutil
import concurrent.futures
from typing import Dict, Any, Optional

from abyss_pool import AbyssAccountPool
from torrent_processor import download_torrent, find_downloaded_videos, burn_subtitles_and_encode
from upload_single import upload_to_telegram, send_admin_notification
from instant_sync import record_and_push_upload


def main():
    meta_b64 = os.getenv("ITEM_META_B64", "").strip()
    if not meta_b64:
        print("[ERROR] ITEM_META_B64 environment variable missing.", file=sys.stderr)
        sys.exit(1)

    item_data = json.loads(base64.b64decode(meta_b64).decode("utf-8"))
    torrent_source = item_data.get("torrent_source")
    file_index = item_data.get("file_index", 1)
    title = item_data.get("title", "Anime")
    episode = str(item_data.get("episode", 1))
    safe_filename = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', item_data.get("filename", f"{title}_Ep_{episode}.mp4"))
    if not safe_filename.endswith(".mp4"):
        safe_filename += ".mp4"

    upload_target = os.getenv("UPLOAD_TARGET", item_data.get("upload_target", "both")).lower().strip()
    tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    tg_admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip() or os.getenv("ADMIN_CHAT_ID", "").strip()
    tg_local_api_url = os.getenv("TG_LOCAL_API_URL", "").strip()

    abyss_pool = AbyssAccountPool()

    print("==================================================================")
    print(f"[*] Processing Nyaa Episode: {title} - Episode {episode}")
    print(f"[*] Torrent Source: {torrent_source[:60]}... (File Index: {file_index})")
    print("==================================================================")

    temp_dl_dir = f"/tmp/nyaa_dl_{int(time.time())}_{os.getpid()}"
    encoded_mp4 = f"temp_encoded_{safe_filename}"
    result: Dict[str, Any] = {}

    try:
        # Step 1: Download specific selected file via aria2c
        selected_indices = [int(file_index)] if file_index else None
        dl_success = download_torrent(torrent_source, temp_dl_dir, selected_indices=selected_indices, timeout_seconds=2400)
        if not dl_success:
            raise RuntimeError(f"aria2c failed to download file #{file_index} for {title} Ep {episode}")

        videos = find_downloaded_videos(temp_dl_dir)
        if not videos:
            raise RuntimeError(f"No valid video file found after aria2c download in {temp_dl_dir}")

        # Pick largest video file if multiple
        videos.sort(key=lambda x: os.path.getsize(x), reverse=True)
        raw_video = videos[0]
        print(f"[+] Downloaded Raw Video: {raw_video} ({os.path.getsize(raw_video) / (1024*1024):.1f} MB)")

        # Step 2: Burn Subtitles & Compress
        country_code = item_data.get("country_of_origin") or item_data.get("country_code", "JP")
        is_movie = item_data.get("is_movie", False) or (item_data.get("media_type") == "movie") or (str(episode).lower() in ("movie", "film", "special"))
        burn_success = burn_subtitles_and_encode(raw_video, encoded_mp4, target_max_mb=500, country_code=country_code, is_movie=is_movie)
        if not burn_success:
            raise RuntimeError(f"FFmpeg subtitle burning failed for {title} Ep {episode}")

        encoded_size_mb = round(os.path.getsize(encoded_mp4) / (1024 * 1024), 2)
        item_data["size_mb"] = encoded_size_mb

        caption = f"🎬 {title} - Episode {episode}"

        # Step 3: Concurrent Parallel Upload to Abyss & Telegram
        need_abyss = upload_target in ("both", "abyss")
        need_telegram = upload_target in ("both", "telegram")

        upload_futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            if need_abyss:
                if abyss_pool.total_accounts > 0:
                    fut_abyss = executor.submit(abyss_pool.upload, encoded_mp4, safe_filename)
                    upload_futures[fut_abyss] = "abyss"
                else:
                    print("[WARN] No ABYSS_API_KEY(s) configured. Skipping Abyss.")

            if need_telegram:
                if tg_bot_token and tg_chat_id:
                    fut_tg = executor.submit(upload_to_telegram, encoded_mp4, safe_filename, tg_bot_token, tg_chat_id, caption, tg_local_api_url)
                    upload_futures[fut_tg] = "telegram"
                else:
                    print("[WARN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured. Skipping Telegram.")

            for fut in concurrent.futures.as_completed(upload_futures):
                dest = upload_futures[fut]
                try:
                    res = fut.result()
                    if dest == "abyss" and res:
                        result["abyss"] = res
                        result["slug"] = res["slug"]
                        result["embed_url"] = res.get("embed_url") or f"https://player.abyssplayer.com/{res['slug']}"
                    elif dest == "telegram" and res:
                        result["telegram"] = res
                except Exception as up_err:
                    print(f"[!] {dest.capitalize()} upload error: {up_err}")
                    if upload_target == dest:
                        raise

        if upload_target == "both" and not (result.get("abyss") or result.get("telegram")):
            raise RuntimeError("Both Abyss and Telegram uploads failed.")

        # Admin Completion Alert
        if tg_bot_token and tg_admin_chat_id:
            slug = result.get("slug", "")
            tg_info = result.get("telegram")
            tg_post = tg_info.get("post_url") if isinstance(tg_info, dict) else ""
            slug_str = f" | ⚡ Abyss: <code>{slug}</code>" if slug else ""
            tg_str = f" | ✈️ <a href='{tg_post}'>Telegram Post</a>" if tg_post else ""

            admin_alert = (
                f"✅ <b>Nyaa.si Ingest Complete:</b> {title} - Episode {episode}\n"
                f"📦 <b>Size:</b> {encoded_size_mb} MB{slug_str}{tg_str}"
            )
            send_admin_notification(tg_bot_token, tg_admin_chat_id, admin_alert)

        # Instant Mapping Sync & Git Commit
        mapping_pw = os.getenv("MAPPING_PASSWORD", "").strip() or os.getenv("STATE_ENCRYPTION_PASSWORD", "").strip()
        if mapping_pw and (result.get("abyss") or result.get("telegram")):
            print("[*] Executing Instant Mapping Sync & Push to GitHub...")
            record_and_push_upload(item_data, result, mapping_pw)

    finally:
        # Immediate cleanup of temporary files
        if os.path.exists(temp_dl_dir):
            try:
                shutil.rmtree(temp_dl_dir, ignore_errors=True)
            except Exception:
                pass
        if os.path.exists(encoded_mp4):
            try:
                os.remove(encoded_mp4)
            except Exception:
                pass

    result.update(item_data)
    out_dir = "artifacts"
    os.makedirs(out_dir, exist_ok=True)
    task_key = item_data.get("key", f"nyaa_{int(time.time())}")
    safe_key = re.sub(r'[^a-zA-Z0-9_\-]', '_', task_key)
    with open(os.path.join(out_dir, f"{safe_key}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[SUCCESS] Finished processing {title} - Episode {episode}!")


if __name__ == "__main__":
    main()
