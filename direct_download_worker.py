"""
Direct Download Single Video Worker
1. Downloads video directly via aria2c multi-connection (16 split connections).
2. Automatically identifies original audio track (matching country of origin, or single audio).
3. Automatically identifies English subtitle track and burns it into video (<500MB for episodes, <1.5GB for movies).
4. Concurrent upload to Abyss Multi-Account Pool & Telegram Channel.
5. Encrypts and syncs updated mapping to GitHub repository.
"""

import os
import re
import sys
import time
import json
import base64
import shutil
import subprocess
import concurrent.futures
from typing import Dict, Any, Optional

from abyss_pool import AbyssAccountPool
from torrent_processor import burn_subtitles_and_encode
from upload_single import upload_to_telegram, send_admin_notification
from instant_sync import record_and_push_upload

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def download_direct_file(url: str, output_path: str, timeout_seconds: int = 2400) -> bool:
    """Downloads direct HTTP/HTTPS video file using aria2c multi-connection."""
    out_dir = os.path.dirname(output_path) or "."
    out_name = os.path.basename(output_path)
    os.makedirs(out_dir, exist_ok=True)

    cmd = [
        "aria2c",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--summary-interval=10",
        "--user-agent", USER_AGENT,
        "-d", out_dir,
        "-o", out_name,
        url
    ]

    print(f"[*] Starting aria2c direct download: {url[:70]}...")
    try:
        proc = subprocess.run(cmd, timeout=timeout_seconds)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000:
            print(f"[+] Download complete: {os.path.getsize(output_path) / (1024*1024):.1f} MB")
            return True
    except Exception as e:
        print(f"[!] aria2c download error: {e}", file=sys.stderr)

    # Fallback to curl
    print("[*] Falling back to curl for direct download...")
    try:
        proc = subprocess.run([
            "curl", "-sSL",
            "-H", f"User-Agent: {USER_AGENT}",
            "-o", output_path,
            url
        ], timeout=timeout_seconds)
        return proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000
    except Exception as e:
        print(f"[!] curl download error: {e}", file=sys.stderr)
        return False


def main():
    meta_b64 = os.getenv("ITEM_META_B64", "").strip()
    if not meta_b64:
        print("[ERROR] ITEM_META_B64 environment variable missing.", file=sys.stderr)
        sys.exit(1)

    item_data = json.loads(base64.b64decode(meta_b64).decode("utf-8"))
    direct_url = item_data.get("direct_url")
    title = item_data.get("title", "Anime")
    episode = str(item_data.get("episode", "1"))
    country_code = item_data.get("country_of_origin", "JP")
    is_movie = item_data.get("is_movie", False)

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
    print(f"[*] Processing Direct Download: {title} - {'Movie' if is_movie else 'Episode ' + episode}")
    print(f"[*] Source URL: {direct_url[:70]}...")
    print(f"[*] Country Code: {country_code} | Movie: {is_movie}")
    print("==================================================================")

    temp_raw = f"/tmp/direct_raw_{int(time.time())}_{os.getpid()}.mkv"
    encoded_mp4 = f"temp_encoded_{safe_filename}"
    result: Dict[str, Any] = {}

    try:
        # Step 1: Direct Download
        dl_success = download_direct_file(direct_url, temp_raw, timeout_seconds=2400)
        if not dl_success:
            raise RuntimeError(f"Failed to download direct video from {direct_url}")

        raw_size_mb = os.path.getsize(temp_raw) / (1024 * 1024)
        print(f"[+] Downloaded Raw Video: {temp_raw} ({raw_size_mb:.1f} MB)")

        # Step 2: Auto Identify Language, Subtitles & Encode (Strict Size Enforced)
        target_mb = 1450 if is_movie else 500
        burn_success = burn_subtitles_and_encode(
            temp_raw,
            encoded_mp4,
            target_max_mb=target_mb,
            country_code=country_code,
            is_movie=is_movie
        )
        if not burn_success:
            raise RuntimeError(f"FFmpeg encoding / subtitle burning failed for {title}")

        encoded_size_mb = round(os.path.getsize(encoded_mp4) / (1024 * 1024), 2)
        item_data["size_mb"] = encoded_size_mb
        print(f"[+] Encoded Successfully: {encoded_mp4} ({encoded_size_mb} MB) [Target Ceiling: <{'1.5 GB' if is_movie else '500 MB'}]")

        caption = f"🎬 {title} - {'Movie' if is_movie else 'Episode ' + episode}"

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
                        result["direct_mp4"] = res.get("direct_mp4") or ""
                    elif dest == "telegram" and res:
                        result["telegram"] = res
                except Exception as e:
                    print(f"[!] Upload error to {dest}: {e}", file=sys.stderr)

        if not result:
            raise RuntimeError("Both Abyss and Telegram uploads failed.")

        # Step 4: Record & Push to History
        print("[*] Encrypting and recording upload to repository mapping...")
        record_and_push_upload(item_data, result)
        print("[SUCCESS] Direct video pipeline completed successfully!")

    finally:
        # Cleanup
        if os.path.exists(temp_raw):
            try:
                os.remove(temp_raw)
            except Exception:
                pass
        if os.path.exists(encoded_mp4):
            try:
                os.remove(encoded_mp4)
            except Exception:
                pass


if __name__ == "__main__":
    main()
