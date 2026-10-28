"""
Single Episode Direct Uploader to Abyss.to and Telegram
Downloads using verified AnimeHeaven Referer.
Uploads to Abyss.to and/or Private Telegram Channel without re-encoding.
Compatible with standard Linux curl (ubuntu-latest).
Supports Multi-Bot Pool and Multi-Channel Sharding.
"""

import os
import re
import sys
import time
import json
import base64
import random
import subprocess
import urllib.parse
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def download_stream(direct_url: str, output_name: str) -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', output_name)
    if not safe_name.endswith('.mp4'):
        safe_name += '.mp4'

    temp_file = f"temp_{safe_name}"
    print(f"[*] Downloading source stream: {direct_url}")
    cmd_dl = [
        "curl", "-sSL",
        "-H", f"User-Agent: {USER_AGENT}",
        "-H", "Referer: https://animeheaven.me/",
        "--tcp-nodelay",
        "--connect-timeout", "30",
        "-o", temp_file,
        direct_url
    ]
    res = subprocess.run(cmd_dl)
    if res.returncode != 0 or not os.path.exists(temp_file) or os.path.getsize(temp_file) < 1000000:
        raise RuntimeError(f"Download failed or incomplete file size (exit code {res.returncode})")

    file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)
    print(f"[+] Download complete: {file_size_mb:.2f} MB.")
    return temp_file


def upload_to_abyss(temp_file: str, safe_name: str, abyss_api_key: str) -> dict:
    print(f"[*] Uploading to Abyss.to: {safe_name}")
    upload_endpoint = f"https://up.abyss.to/{urllib.parse.quote(abyss_api_key, safe='')}"
    cmd_up = [
        "curl", "-sS",
        "-H", "Expect:",
        "--http1.1",
        "--tcp-nodelay",
        "--connect-timeout", "30",
        "-F", f"file=@{temp_file};filename={safe_name}",
        upload_endpoint
    ]

    proc = subprocess.run(cmd_up, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Abyss upload curl error: {proc.stderr}")

    try:
        data = json.loads(proc.stdout)
    except Exception as e:
        raise RuntimeError(f"Failed to parse Abyss response JSON: {proc.stdout[:200]}") from e

    slug = data.get("slug") or data.get("file_id") or data.get("id")
    if not slug:
        raise RuntimeError(f"Unexpected Abyss response: {data}")

    print(f"[SUCCESS] Uploaded to Abyss! File Slug: {slug}")
    return {
        "slug": slug,
        "embed_url": f"https://abyss.to/{slug}"
    }


def upload_to_telegram(temp_file: str, safe_name: str, bot_tokens_raw: str, chat_id: str, caption: str, local_api_url: str = "") -> dict:
    # Parse and extract multi-bot tokens (supports token1,token2,token3...)
    all_tokens = [t.strip() for t in re.split(r'[,;|\s]+', bot_tokens_raw) if t.strip()]
    if not all_tokens:
        print("[!] No valid Telegram bot tokens provided. Skipping Telegram upload.")
        return None

    # Parse multi-channels if provided (e.g. -100111,-100222,-100333)
    all_chats = [c.strip() for c in re.split(r'[,;|\s]+', chat_id) if c.strip()]
    clean_chat = random.choice(all_chats) if all_chats else chat_id.strip()

    # Shuffle tokens so concurrent GitHub runners distribute evenly across available bots
    random.shuffle(all_tokens)

    base_api = local_api_url.rstrip("/") if local_api_url else "https://api.telegram.org"
    print(f"[*] Uploading to Telegram Channel ({clean_chat}): {safe_name} via pool of {len(all_tokens)} Bots")

    last_error = None
    for token_idx, bot_token in enumerate(all_tokens, 1):
        clean_token = bot_token.strip()
        endpoint = f"{base_api}/bot{clean_token}/sendVideo"
        print(f"[*] Dispatching via Bot #{token_idx} to {clean_chat}...")

        cmd_tg = [
            "curl", "-sS",
            "--connect-timeout", "30",
            "--max-time", "1800",
            "-F", f"chat_id={clean_chat}",
            "-F", f"caption={caption}",
            "-F", "supports_streaming=true",
            "-F", f"video=@{temp_file};filename={safe_name}",
            endpoint
        ]

        proc = subprocess.run(cmd_tg, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"[!] Bot #{token_idx} curl network error: {proc.stderr}. Trying next bot in pool...")
            last_error = proc.stderr
            continue

        try:
            res = json.loads(proc.stdout)
        except Exception as e:
            print(f"[!] Bot #{token_idx} JSON parse error: {proc.stdout[:200]}. Trying next bot...")
            last_error = str(e)
            continue

        if not res.get("ok"):
            err_desc = str(res.get("description", res))
            print(f"[!] Bot #{token_idx} error: {err_desc}")
            last_error = err_desc

            # If rate limited, sleep the requested retry_after period before failover
            if "too many requests" in err_desc.lower() or "retry after" in err_desc.lower():
                wait_sec = res.get("parameters", {}).get("retry_after", 15)
                print(f"[⏳ RATE LIMIT] Telegram requested wait of {wait_sec}s. Sleeping before next bot...")
                time.sleep(wait_sec + 2)
                continue

            # If invalid token, try next bot
            if "unauthorized" in err_desc.lower():
                continue

            # Otherwise small sleep and try next bot
            time.sleep(2)
            continue

        result_data = res.get("result", {})
        message_id = result_data.get("message_id")
        video_info = result_data.get("video", {})
        file_id = video_info.get("file_id")

        # Clean channel ID for direct t.me link
        clean_chan_id = clean_chat
        if clean_chan_id.startswith("-100"):
            clean_chan_id = clean_chan_id[4:]
        elif clean_chan_id.startswith("-"):
            clean_chan_id = clean_chan_id[1:]

        post_url = f"https://t.me/c/{clean_chan_id}/{message_id}" if message_id else ""
        print(f"[SUCCESS] Uploaded to Telegram via Bot #{token_idx}! Msg ID: {message_id} | Link: {post_url}")

        return {
            "message_id": message_id,
            "file_id": file_id,
            "channel_id": clean_chat,
            "post_url": post_url
        }

    print(f"[!] All {len(all_tokens)} Telegram bots failed or were rate-limited. Last error: {last_error}")
    return None


def main():
    abyss_api_key = os.getenv("ABYSS_API_KEY", "").strip()
    tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    tg_local_api_url = os.getenv("TG_LOCAL_API_URL", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    meta_b64 = os.getenv("ITEM_META_B64", "").strip()

    if not meta_b64:
        print("[ERROR] ITEM_META_B64 environment variable missing.", file=sys.stderr)
        sys.exit(1)

    item_data = json.loads(base64.b64decode(meta_b64).decode("utf-8"))
    stream_url = item_data["stream_url"]
    output_name = item_data["filename"]

    temp_file = None
    result = {}

    try:
        # Step 1: Download stream directly from AnimeHeaven
        temp_file = download_stream(stream_url, output_name)
        safe_name = os.path.basename(temp_file).replace("temp_", "")

        # Step 2: Upload to Abyss if requested
        if upload_target in ("both", "abyss"):
            if abyss_api_key:
                try:
                    abyss_res = upload_to_abyss(temp_file, safe_name, abyss_api_key)
                    result.update(abyss_res)
                except Exception as ab_err:
                    print(f"[!] Abyss upload error: {ab_err}")
                    if upload_target == "abyss":
                        raise
            else:
                print("[WARN] ABYSS_API_KEY not configured. Skipping Abyss.")

        # Step 3: Upload to Telegram if requested
        if upload_target in ("both", "telegram"):
            if tg_bot_token and tg_chat_id:
                try:
                    ep_title = item_data.get("title", "Anime")
                    ep_num = item_data.get("episode", 1)
                    caption = f"🎬 {ep_title} - Episode {ep_num}"
                    tg_res = upload_to_telegram(temp_file, safe_name, tg_bot_token, tg_chat_id, caption, local_api_url=tg_local_api_url)
                    if tg_res:
                        result["telegram"] = tg_res
                except Exception as tg_err:
                    print(f"[!] Telegram upload error: {tg_err}")
                    if upload_target == "telegram":
                        raise
            else:
                print("[WARN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured. Skipping Telegram.")

        # If upload target was 'both' and at least one succeeded, do not crash!
        if upload_target == "both" and not (result.get("slug") or result.get("telegram")):
            raise RuntimeError("Both Abyss and Telegram uploads failed.")

    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

    result.update(item_data)
    out_dir = "artifacts"
    os.makedirs(out_dir, exist_ok=True)
    safe_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', output_name)
    with open(os.path.join(out_dir, f"{safe_base}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()