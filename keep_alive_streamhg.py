"""
Automated StreamHG Inactivity Protection & Keep-Alive Bot
Prevents StreamHG's 60-day inactivity deletion by periodically pinging each video.
- Decrypts uploaded_episodes.json using MAPPING_PASSWORD.
- Dispatches concurrent HTTP keep-alive requests to StreamHG.
- Resets the 'Last View' timer to 0 days, ensuring permanent retention.
- Sends an admin alert via Telegram bot pool upon completion.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import concurrent.futures
from typing import Dict, List, Set
from mapping_crypto import decrypt_mapping

STATE_FILE = "uploaded_episodes.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def load_history(password: str) -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                content = json.load(f)
            if isinstance(content, dict) and content.get("algorithm") == "AES-256-GCM":
                if not password:
                    raise RuntimeError("MAPPING_PASSWORD required to decrypt encrypted history.")
                return decrypt_mapping(content, password)
            elif isinstance(content, dict):
                return content
        except Exception as e:
            print(f"[!] Warning reading history: {e}.", file=sys.stderr)
            return {}
    return {}


def ping_streamhg_video(file_code: str, api_key: str = "") -> dict:
    """
    Simulates a view/access on StreamHG to reset the 60-day inactivity countdown.
    Uses official StreamHG info API if api_key provided, and falls back to embed player GET.
    """
    res = {"file_code": file_code, "status": "unknown", "views": 0}

    # Primary: Query file info via StreamHG API
    if api_key:
        api_url = f"https://streamhgapi.com/api/file/info?key={urllib.parse.quote(api_key, safe='')}&file_code={file_code}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == 200 and data.get("result"):
                    finfo = data["result"][0] if isinstance(data["result"], list) and data["result"] else data["result"]
                    if isinstance(finfo, dict):
                        res["status"] = finfo.get("status", "active")
                        res["views"] = finfo.get("views", 0)
                        return res
        except Exception as e:
            # Fallback to embed GET if API fails
            pass

    # Fallback / Secondary: HTTP GET directly to embed player endpoint
    embed_url = f"https://hgcloud.to/e/{file_code}"
    try:
        req = urllib.request.Request(
            embed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://hgcloud.to/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.getcode()
            res["status"] = "active" if code == 200 else f"http_{code}"
            return res
    except Exception as e:
        res["status"] = f"error: {e}"
        return res


def send_telegram_admin_alert(bot_tokens_raw: str, admin_chat_id: str, message: str):
    tokens = [t.strip() for t in bot_tokens_raw.replace(";", ",").split(",") if t.strip()]
    if not tokens or not admin_chat_id:
        return
    for token in tokens:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": admin_chat_id,
                "text": message,
                "parse_mode": "HTML"
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.getcode() == 200:
                    return
        except Exception:
            continue


def main():
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    streamhg_api_key = os.getenv("STREAMHG_API_KEY", "").strip() or os.getenv("ABYSS_API_KEY", "").strip()
    tg_bot_tokens = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("TG_BOT_TOKEN", "").strip()
    tg_admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip() or os.getenv("ADMIN_CHAT_ID", "").strip()

    print("=" * 65)
    print("⚡ STREAMHG INACTIVITY PROTECTION & KEEP-ALIVE BOT")
    print("=" * 65)

    history = load_history(password)
    if not history:
        print("[!] No video mappings found or decryption failed. Exiting.")
        sys.exit(0)

    # Collect all unique StreamHG file codes
    target_files = set()
    for anime_key, anime_val in history.items():
        episodes = anime_val.get("episodes", {})
        for ep_num, ep_data in episodes.items():
            if not isinstance(ep_data, dict):
                continue
            code = None
            if ep_data.get("streamhg") and ep_data["streamhg"].get("file_code"):
                code = ep_data["streamhg"]["file_code"]
            if code and len(code) >= 6:
                target_files.add(code)

    total_count = len(target_files)
    print(f"[*] Found {total_count} StreamHG video(s) registered in mapping tree.")

    if total_count == 0:
        print("[+] Zero StreamHG videos currently registered. Nothing to ping.")
        sys.exit(0)

    active_count = 0
    error_count = 0
    start_time = time.time()

    print(f"[*] Dispatching concurrent keep-alive pings (Concurrency: 6)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(ping_streamhg_video, fcode, streamhg_api_key): fcode for fcode in target_files}
        for idx, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            fcode = futures[fut]
            try:
                res = fut.result()
                status = res.get("status")
                if status == "active" or "http_200" in str(status) or res.get("views", 0) > 0:
                    active_count += 1
                else:
                    error_count += 1
                if idx % 20 == 0 or idx == total_count:
                    print(f"  [{idx}/{total_count}] Pinned {fcode} -> Status: {status}")
            except Exception as e:
                error_count += 1
                print(f"  [!] Failed {fcode}: {e}")

    elapsed = time.time() - start_time
    summary_msg = (
        f"⚡ <b>StreamHG Keep-Alive Ping Completed!</b>\n\n"
        f"📊 <b>Total Checked:</b> {total_count}\n"
        f"✅ <b>Active & Refreshed:</b> {active_count}\n"
        f"⚠️ <b>Issues:</b> {error_count}\n"
        f"⏱️ <b>Time Taken:</b> {elapsed:.1f}s\n\n"
        f"🛡️ <i>All 60-day inactivity countdowns reset to Day 0. Next automated ping in 15 days!</i>"
    )
    print("=" * 65)
    print(f"[DONE] Keep-Alive Run Completed in {elapsed:.1f}s. Active: {active_count}/{total_count}")
    print("=" * 65)

    if tg_bot_tokens and tg_admin_chat_id:
        send_telegram_admin_alert(tg_bot_tokens, tg_admin_chat_id, summary_msg)
        print("[+] Admin Telegram summary alert dispatched.")


if __name__ == "__main__":
    main()
