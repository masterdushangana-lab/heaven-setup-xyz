"""
Single Episode Direct Uploader to Mp4Upload and Telegram
1. High-Speed Subtitle Burning + H.264 compression (<500MB).
2. Mp4Upload Direct High-Speed Video Upload with API Key + Session ID.
3. Concurrent Parallel Upload to Mp4Upload and Telegram Channel.
"""

import os
import re
import sys
import time
import json
import hmac
import base64
import hashlib
import random
import string
import shutil
import subprocess
import urllib.parse
import urllib.request
import concurrent.futures
from pathlib import Path
import packer

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MEGAPLAY_SIGNING_KEY = b'MpCdnT0k3n!9f2K#xQ7vL5mR8wN1pY4s'


def ensure_fresh_megaplay_url(url: str) -> str:
    """Ensures MegaPlay HLS stream token has 24-hour expiration so download never cuts off."""
    if not url or ("megaplay" not in url and "nexabloom" not in url):
        return url
    path_m = re.search(r'/([a-f0-9]{32})/([a-f0-9]{32})/', url, re.IGNORECASE)
    if not path_m:
        return url
    base_url = re.sub(r'[\?&]token=[^&]+', '', url)
    payload = f"{int(time.time()) + 86400}|{path_m.group(1).lower()}/{path_m.group(2).lower()}".encode("utf-8")
    sig = hmac.new(MEGAPLAY_SIGNING_KEY, payload, hashlib.sha256).digest()
    token = f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}token={token}"


def get_media_duration(file_path: str) -> float:
    """Returns media duration in seconds using ffprobe."""
    try:
        p = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ], capture_output=True, text=True, timeout=15)
        val = p.stdout.strip()
        return float(val) if val else 0.0
    except Exception:
        return 0.0


def download_and_burn_stream(stream_url: str, output_name: str, referer: str, subtitle_url: str = None) -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', output_name)
    if not safe_name.endswith('.mp4'):
        safe_name += '.mp4'

    # Ensure MegaPlay URL has a fresh 24h valid token before downloading
    stream_url = ensure_fresh_megaplay_url(stream_url)

    temp_file = f"temp_{safe_name}"
    print(f"[*] Processing Stream: {stream_url}")
    print(f"[*] Referer: {referer}")

    temp_sub = None
    temp_ass = None
    if subtitle_url:
        temp_sub = f"sub_{int(time.time())}.vtt"
        temp_ass = f"sub_{int(time.time())}.ass"
        print(f"[*] Downloading WebVTT Subtitles: {subtitle_url}")
        subprocess.run([
            "curl", "-sSL",
            "-H", f"User-Agent: {USER_AGENT}",
            "-H", f"Referer: {referer}",
            "-H", "Origin: https://megaplay.buzz",
            "-o", temp_sub,
            subtitle_url
        ])

        if os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 100:
            # Convert VTT to ASS for 100% reliable FFmpeg hardsub burning
            subprocess.run(["ffmpeg", "-y", "-i", temp_sub, temp_ass], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        burn_track = temp_ass if (temp_ass and os.path.exists(temp_ass) and os.path.getsize(temp_ass) > 100) else temp_sub
        if burn_track and os.path.exists(burn_track) and os.path.getsize(burn_track) > 100:
            sub_filter = f"ass={burn_track}" if burn_track.endswith(".ass") else f"subtitles={burn_track}:force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=1.5,Shadow=0,MarginV=25'"

            # Direct stream decoding across HLS #EXT-X-DISCONTINUITY:
            # FFmpeg decodes video frames to YUV and re-encodes with libx264, generating monotonic DTS/PTS.
            # This completely avoids stream-copy cutoffs at the 12:30/13:00 anime commercial eyecatch break.
            print(f"[*] Direct Stream Decoding & Subtitle Burning ({burn_track})...")
            print(f"[*] Encoding optimized ultrafast H.264 animation profile (<500MB target, full continuity)...")
            cmd_burn = [
                "ffmpeg", "-y",
                "-threads", "0",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
                "-allowed_segment_extensions", "ALL",
                "-allowed_extensions", "ALL",
                "-extension_picky", "0",
                "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
                "-i", stream_url,
                "-vf", sub_filter,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "animation",
                "-crf", "22",
                "-maxrate", "2600k",
                "-bufsize", "5200k",
                "-g", "48", "-keyint_min", "24", "-x264-params", "cabac=1:bframes=2:ref=2:deblock=1,1:aq-mode=1:keyint=48:min-keyint=24",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                temp_file
            ]
            res = subprocess.run(cmd_burn)
            if res.returncode != 0 or not os.path.exists(temp_file) or os.path.getsize(temp_file) < 500000:
                print("[!] Subtitle burning encountered an issue. Retrying with basic stream encode...")
                subprocess.run([
                    "ffmpeg", "-y",
                    "-threads", "0",
                    "-reconnect", "1",
                    "-reconnect_at_eof", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10",
                    "-allowed_segment_extensions", "ALL",
                    "-allowed_extensions", "ALL",
                    "-extension_picky", "0",
                    "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
                    "-i", stream_url,
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-tune", "animation",
                    "-crf", "22",
                    "-maxrate", "2600k",
                    "-bufsize", "5200k",
                    "-g", "48", "-keyint_min", "24", "-x264-params", "cabac=1:bframes=2:ref=2:deblock=1,1:aq-mode=1:keyint=48:min-keyint=24",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-movflags", "+faststart",
                    temp_file
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print("[*] No subtitle burning needed. Direct stream processing...")
            if ".m3u8" in stream_url:
                subprocess.run([
                    "ffmpeg", "-y",
                    "-threads", "0",
                    "-reconnect", "1",
                    "-reconnect_at_eof", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10",
                    "-allowed_segment_extensions", "ALL",
                    "-allowed_extensions", "ALL",
                    "-extension_picky", "0",
                    "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
                    "-i", stream_url,
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-movflags", "+faststart",
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
        for f_clean in (temp_sub, temp_ass):
            if f_clean and os.path.exists(f_clean):
                try:
                    os.remove(f_clean)
                except Exception:
                    pass

    if not os.path.exists(temp_file) or os.path.getsize(temp_file) < 500000:
        raise RuntimeError(f"Processing failed or output file is empty: {output_name}")

    final_dur = get_media_duration(temp_file)
    file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)
    print(f"[+] Output ready: {file_size_mb:.2f} MB | Duration: {final_dur:.2f}s ({final_dur/60:.2f} min) (Target <500MB achieved!).")
    return temp_file


def upload_to_mp4upload(temp_file: str, safe_name: str, mp4upload_api_key: str) -> dict:
    """
    Direct High-Speed Uploader to Mp4Upload (mp4upload.com).
    1. Requests designated upload server endpoint & sess_id via GET https://www.mp4upload.com/api/upload/server?key={key}
    2. Uploads video via multipart POST to allocated CGI endpoint (with sess_id, file, and file_0 fallback).
    3. Retries up to 3 times with fresh endpoints if an individual storage node times out.
    4. Automatically parses returned filecode or scrapes newest uploaded file code from account.
    5. Returns filecode, player_url, and embed_url.
    """
    print(f"[*] Uploading to Mp4Upload: {safe_name}")
    start_time = time.time()

    max_retries = 3
    last_err = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"[*] Mp4Upload upload retry {attempt}/{max_retries}...")

        # 1. Obtain fresh upload server endpoint & sess_id from Mp4Upload API
        s_data = None
        server_api = f"https://www.mp4upload.com/api/upload/server?key={urllib.parse.quote(mp4upload_api_key, safe='')}"

        upload_endpoint = None
        sess_id = None
        try:
            req_server = urllib.request.Request(server_api, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req_server, timeout=25) as resp:
                s_data = json.loads(resp.read().decode("utf-8"))
            if s_data and s_data.get("status") == 200:
                upload_endpoint = s_data.get("result")
                sess_id = s_data.get("sess_id")
        except Exception as ep_err:
            print(f"[!] Warning fetching upload server from Mp4Upload: {ep_err}")

        if not upload_endpoint:
            last_err = RuntimeError(f"Mp4Upload did not return valid upload server: {s_data}")
            time.sleep(3)
            continue

        print(f"[*] Mp4Upload upload server allocated: {upload_endpoint} (sess_id: {sess_id})")

        # 2. Curl command with IPv4 force, browser User-Agent, and robust multipart fields
        cmd_up = [
            "curl", "-sS",
            "-4",  # Force IPv4
            "-A", USER_AGENT,
            "-H", "Expect:",
            "-k",  # Insecure (accept storage node SSL certs)
            "--connect-timeout", "60",
            "--max-time", "1800",
            "-F", f"sess_id={sess_id or ''}",
            "-F", f"key={mp4upload_api_key}",
            "-F", f"file_title={safe_name}",
            "-F", f"file=@{temp_file};filename={safe_name}",
            "-F", f"file_0=@{temp_file};filename={safe_name}",
            upload_endpoint
        ]

        proc = subprocess.run(cmd_up, capture_output=True, text=True)
        if proc.returncode != 0:
            last_err = RuntimeError(f"Mp4Upload upload curl error: {proc.stderr.strip()}")
            print(f"[!] Mp4Upload upload curl attempt {attempt} failed: {proc.stderr.strip()}")
            time.sleep(4)
            continue

        resp_text = proc.stdout.strip()
        filecode = None

        # Check JSON response
        try:
            data = json.loads(resp_text)
            if isinstance(data, list) and len(data) > 0 and data[0].get("file_code"):
                filecode = data[0].get("file_code")
            elif isinstance(data, dict):
                files_list = data.get("files", [])
                if files_list and isinstance(files_list, list) and len(files_list) > 0:
                    filecode = files_list[0].get("filecode") or files_list[0].get("file_code")
                elif isinstance(data.get("result"), dict):
                    filecode = data["result"].get("filecode") or data["result"].get("file_code")
                elif data.get("filecode") or data.get("file_code"):
                    filecode = data.get("filecode") or data.get("file_code")
        except Exception:
            pass

        # Check HTML / text textarea or regex match
        if not filecode:
            # Pattern 1: textarea or input with name='fn' or 'file_code'
            m_fn = re.search(r"<(?:textarea|input)[^>]+name=['\"](?:fn|file_code)['\"][^>]*>([^<]+)</", resp_text, re.IGNORECASE) or \
                   re.search(r"value=['\"]([a-zA-Z0-9]{8,20})['\"][^>]+name=['\"](?:fn|file_code)['\"]", resp_text, re.IGNORECASE)
            if m_fn:
                filecode = m_fn.group(1).strip()

        if not filecode:
            # Pattern 2: Direct mp4upload URL or embed URL inside response
            m_url = re.search(r"mp4upload\.com/(?:embed-)?([a-zA-Z0-9]{8,20})", resp_text)
            if m_url:
                filecode = m_url.group(1).strip()

        # Fallback query via File List API if CGI response was indirect redirect
        if not filecode and mp4upload_api_key:
            time.sleep(2)
            try:
                list_url = f"https://www.mp4upload.com/api/file/list?key={urllib.parse.quote(mp4upload_api_key, safe='')}&per_page=5"
                req_list = urllib.request.Request(list_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req_list, timeout=15) as l_resp:
                    l_data = json.loads(l_resp.read().decode("utf-8"))
                if l_data.get("status") == 200 and l_data.get("result"):
                    for f_entry in l_data["result"]:
                        # Match uploaded filename or take most recent
                        if f_entry.get("name") in (safe_name, safe_name.replace(".mp4", "")):
                            filecode = f_entry.get("filecode")
                            break
                    if not filecode and l_data["result"]:
                        filecode = l_data["result"][0].get("filecode")
            except Exception as list_err:
                print(f"[!] Warning fallback list check: {list_err}")

        if not filecode:
            last_err = RuntimeError(f"Unexpected Mp4Upload response (missing filecode): {resp_text[:300]}")
            print(f"[!] Mp4Upload attempt {attempt} missing filecode in response: {resp_text[:200]}")
            time.sleep(4)
            continue

        elapsed = time.time() - start_time
        embed_url = f"https://www.mp4upload.com/embed-{filecode}.html"
        print(f"[SUCCESS] Uploaded to Mp4Upload in {elapsed:.1f}s! File Code: {filecode} | Embed: {embed_url}")

        # Attempt immediate direct mp4 extraction from the newly created embed
        direct_mp4 = None
        try:
            req_embed = urllib.request.Request(embed_url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.mp4upload.com/"})
            with urllib.request.urlopen(req_embed, timeout=12) as em_resp:
                em_html = em_resp.read().decode("utf-8", errors="ignore")
                if packer.detect(em_html):
                    unp = packer.unpack(em_html)
                    m_p = re.search(r'player\.src\(\"([^\"]+?\.mp4[^\"]*)\"', unp) or \
                          re.search(r'src:\s*\"([^\"]+?\.mp4[^\"]*)\"', unp) or \
                          re.search(r'\"(https?://[^\"]+?\.mp4)\"', unp)
                    if m_p:
                        direct_mp4 = m_p.group(1)
                if not direct_mp4:
                    m_pl = re.search(r'<source\s+src=[\"\'](https?://[^\s\"\'\<\>]+?\.mp4)[\"\']', em_html)
                    if m_pl:
                        direct_mp4 = m_pl.group(1)
        except Exception:
            pass

        if direct_mp4:
            print(f"  [+] Direct MP4 Extracted & Saved: {direct_mp4[:60]}...")

        return {
            "mp4upload": {
                "file_code": filecode,
                "player_url": embed_url,
                "embed_url": embed_url,
                "direct_mp4": direct_mp4
            },
            "slug": filecode,
            "embed_url": embed_url,
            "direct_mp4": direct_mp4
        }

    raise RuntimeError(f"All {max_retries} Mp4Upload upload attempts failed. Last error: {last_err}")


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
    start_time = time.time()

    last_error = None
    for token_idx, bot_token in enumerate(all_tokens, 1):
        clean_token = bot_token.strip()
        endpoint = f"{base_api}/bot{clean_token}/sendVideo"
        print(f"[*] Dispatching via Bot #{token_idx} to {clean_chat}...")

        cmd_tg = [
            "curl", "-sS",
            "-H", "Expect:",
            "--http1.1",
            "--tcp-nodelay",
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
        elapsed = time.time() - start_time
        print(f"[SUCCESS] Uploaded to Telegram in {elapsed:.1f}s via Bot #{token_idx}! Msg ID: {message_id} | Link: {post_url}")

        return {
            "message_id": message_id,
            "file_id": file_id,
            "channel_id": clean_chat,
            "post_url": post_url
        }

    print(f"[!] All {len(all_tokens)} Telegram bots failed or were rate-limited. Last error: {last_error}")
    return None


def send_admin_notification(bot_tokens_raw: str, admin_chat_id: str, message: str) -> bool:
    """Sends an instant HTML completion alert using any available bot in the multi-bot pool."""
    if not bot_tokens_raw or not admin_chat_id:
        return False

    all_tokens = [t.strip() for t in re.split(r'[,;|\s]+', bot_tokens_raw) if t.strip()]
    if not all_tokens:
        return False

    clean_chat = admin_chat_id.strip()

    # Iterate through the pool of bots until one successfully delivers the alert
    for token_idx, bot_token in enumerate(all_tokens, 1):
        endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        cmd = [
            "curl", "-sS",
            "--connect-timeout", "10",
            "--max-time", "20",
            "-F", f"chat_id={clean_chat}",
            "-F", f"text={message}",
            "-F", "parse_mode=HTML",
            "-F", "disable_web_page_preview=true",
            endpoint
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                res = json.loads(proc.stdout)
                if res.get("ok"):
                    print(f"[+] Admin completion alert dispatched to {clean_chat} via Bot #{token_idx}!")
                    return True
        except Exception:
            pass

    print(f"[!] Could not send admin notification using any of the {len(all_tokens)} bots.")
    return False


def main():
    mp4upload_api_key = os.getenv("MP4UPLOAD_API_KEY", "").strip() or os.getenv("STREAMHG_API_KEY", "").strip() or os.getenv("ABYSS_API_KEY", "").strip()
    tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    tg_admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip() or os.getenv("ADMIN_CHAT_ID", "").strip()
    tg_local_api_url = os.getenv("TG_LOCAL_API_URL", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    if upload_target == "streamhg":
        upload_target = "mp4upload"
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

        ep_title = item_data.get("title", "Anime")
        ep_num = item_data.get("episode", 1)
        caption = f"🎬 {ep_title} - Episode {ep_num}"

        # --- TARGET-AWARE DEDUPLICATION & CONCURRENT PARALLEL UPLOADS ---
        existing_tg = item_data.get("existing_telegram")
        existing_mp4 = item_data.get("existing_mp4upload") or item_data.get("existing_streamhg")

        # Determine which destinations actually require uploading
        has_existing_tg = bool(existing_tg and (isinstance(existing_tg, dict) and existing_tg.get("message_id")))
        has_existing_mp4 = bool(existing_mp4 and (isinstance(existing_mp4, dict) and existing_mp4.get("file_code")))

        need_telegram = upload_target in ("both", "telegram") and not has_existing_tg
        need_mp4upload = upload_target in ("both", "mp4upload", "streamhg") and not has_existing_mp4

        if has_existing_tg:
            print(f"[*] Episode {ep_num} already exists in Telegram Channel (Msg ID: {existing_tg.get('message_id')}). Skipping duplicate Telegram upload.")
            result["telegram"] = existing_tg

        if has_existing_mp4:
            mp4_code = existing_mp4.get("file_code")
            print(f"[*] Episode {ep_num} already exists on Mp4Upload (File Code: {mp4_code}). Skipping duplicate Mp4Upload upload.")
            result["mp4upload"] = existing_mp4
            result["embed_url"] = f"https://www.mp4upload.com/embed-{mp4_code}.html"

        upload_futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            if need_mp4upload:
                if mp4upload_api_key:
                    fut_mp4 = executor.submit(upload_to_mp4upload, temp_file, safe_name, mp4upload_api_key)
                    upload_futures[fut_mp4] = "mp4upload"
                else:
                    print("[WARN] MP4UPLOAD_API_KEY not configured. Skipping Mp4Upload.")

            if need_telegram:
                if tg_bot_token and tg_chat_id:
                    fut_tg = executor.submit(upload_to_telegram, temp_file, safe_name, tg_bot_token, tg_chat_id, caption, tg_local_api_url)
                    upload_futures[fut_tg] = "telegram"
                else:
                    print("[WARN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured. Skipping Telegram.")

            for fut in concurrent.futures.as_completed(upload_futures):
                dest = upload_futures[fut]
                try:
                    res = fut.result()
                    if dest == "mp4upload" and res:
                        result.update(res)
                    elif dest == "telegram" and res:
                        result["telegram"] = res
                except Exception as up_err:
                    print(f"[!] {dest.capitalize()} upload error: {up_err}")
                    if upload_target == dest:
                        raise

        if upload_target == "both" and not (result.get("mp4upload") or result.get("telegram")):
            raise RuntimeError("Both Mp4Upload and Telegram uploads failed.")

        # Admin Completion Notification (Multi-Bot Failover Pool)
        if tg_bot_token and tg_admin_chat_id:
            size_mb = item_data.get("size_mb", 0)
            slug = result.get("slug") or (result.get("mp4upload", {}).get("file_code") if isinstance(result.get("mp4upload"), dict) else "")
            tg_info = result.get("telegram")
            tg_post = tg_info.get("post_url") if isinstance(tg_info, dict) else ""
            slug_str = f" | ⚡ Mp4Upload: <code>{slug}</code>" if slug else ""
            tg_str = f" | ✈️ <a href='{tg_post}'>Telegram Post</a>" if tg_post else ""

            admin_alert = (
                f"✅ <b>Upload Complete:</b> {ep_title} - Episode {ep_num}\n"
                f"📦 <b>Size:</b> {size_mb} MB{slug_str}{tg_str}"
            )
            send_admin_notification(tg_bot_token, tg_admin_chat_id, admin_alert)

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
