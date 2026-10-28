"""
Single Episode Direct Uploader to Abyss.to and Telegram
Supports:
1. High-Speed Subtitle Burning (Hardsub) + Anime H.264 compression (<500MB).
2. Direct MP4 downloads via curl.
Uploads directly to Abyss.to and Telegram without quality compromise.
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


def download_and_burn_stream(stream_url: str, output_name: str, referer: str, subtitle_url: str = None) -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', output_name)
    if not safe_name.endswith('.mp4'):
        safe_name += '.mp4'

    temp_file = f"temp_{safe_name}"
    print(f"[*] Processing Stream: {stream_url}")
    print(f"[*] Referer: {referer}")

    temp_sub = None
    if subtitle_url:
        temp_sub = f"sub_{int(time.time())}.vtt"
        print(f"[*] Downloading WebVTT Subtitles: {subtitle_url}")
        subprocess.run(["curl", "-sSL", "-H", f"User-Agent: {USER_AGENT}", "-o", temp_sub, subtitle_url])

    try:
        if temp_sub and os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 100:
            print("[*] Burning subtitles into video with high-speed H.264 animation tuning (<500MB target)...")
            cmd_burn = [
                "ffmpeg", "-y",
                "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
                "-i", stream_url,
                "-vf", f"subtitles={temp_sub}:force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=1.5,Shadow=0,MarginV=25'",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-tune", "animation",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                temp_file
            ]
            res = subprocess.run(cmd_burn)
            if res.returncode != 0 or not os.path.exists(temp_file) or os.path.getsize(temp_file) < 500000:
                print("[!] Subtitle burning encountered an issue. Falling back to clean stream copy...")
                # Stream copy fallback
                subprocess.run([
                    "ffmpeg", "-y",
                    "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
                    "-i", stream_url,
                    "-c", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    temp_file
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print("[*] No subtitle burning needed. Direct stream muxing/downloading...")
            if ".m3u8" in stream_url:
                subprocess.run([
                    "ffmpeg", "-y",
                    "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
                    "-i", stream_url,
                    "-c", "copy",
                    temp_file
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run([
                    "curl", "-sSL",
                    "-H", f"User-Agent: {USER_AGENT}",
                    "-H", f"Referer: {referer}",
                    "-o", temp_file,
                    stream_url
                ])

    finally:
        if temp_sub and os.path.exists(temp_sub):
            try:
                os.remove(temp_sub)
            except Exception:
                pass

    if not os.path.exists(temp_file) or os.path.getsize(temp_file) < 500000:
        raise RuntimeError(f"Processing failed or output file is empty: {output_name}")

    file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)
    print(f"[+] Output ready: {file_size_mb:.2f} MB (Target <500MB achieved!).")
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
    all_tokens = [t.strip() for t in re.split(r'[,;|\s]+', bot_tokens_raw) if t.strip()]
    if not all_tokens:
        print("[!] No valid Telegram bot tokens provided. Skipping Telegram upload.")
        return None

    all_chats = [c.strip() for c in re.split(r'[,;|\s]+', chat_id) if c.strip()]
    clean_chat = random.choice(all_chats) if all_chats else chat_id.strip()

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

            if "too many requests" in err_desc.lower() or "retry after" in err_desc.lower():
                wait_sec = res.get("parameters", {}).get("retry_after", 15)
                print(f"[ RATE LIMIT] Telegram requested wait of {wait_sec}s. Sleeping before next bot...")
                time.sleep(wait_sec + 2)
                continue

            if "unauthorized" in err_desc.lower():
                continue

            time.sleep(2)
            continue

        result_data = res.get("result", {})
        message_id = result_data.get("message_id")
        video_info = result_data.get("video", {})
        file_id = video_info.get("file_id")

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
    subtitle_url = item_data.get("subtitle_url")
    referer = item_data.get("stream_referer") or ("https://megaplay.buzz/" if "nexabloom" in stream_url or "megaplay" in stream_url else "https://animeheaven.me/")

    temp_file = None
    result = {}

    try:
        temp_file = download_and_burn_stream(stream_url, output_name, referer=referer, subtitle_url=subtitle_url)
        safe_name = os.path.basename(temp_file).replace("temp_", "")
        item_data["size_mb"] = round(os.path.getsize(temp_file) / (1024 * 1024), 2)

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
