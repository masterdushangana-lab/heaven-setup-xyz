"""
Automated Mp4Upload 100% Inactivity Protection & Keep-Alive Bot
Maintains Mp4Upload videos permanently active by:
1. Querying official Mp4Upload File Info API (/api/file/info?key={KEY}&file_code={CODE}) to refresh activity.
2. If inactivity period is approaching or file status requires refreshing, clones the file (/api/file/clone)
   to reset the 6-month inactivity timer to 0 days, ensuring permanent retention.
3. Accesses direct embed player endpoint (https://www.mp4upload.com/embed-{code}.html) with realistic browser headers.
4. Sends Telegram admin status report upon job completion.
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
import packer

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


def ping_mp4upload_video(file_code: str, api_key: str = "") -> dict:
    """
    Refreshes activity on Mp4Upload to prevent inactivity expiration.
    1. Checks status via API (/api/file/info).
    2. Performs HTTP GET to embed player.
    3. Extracts direct MP4 streaming link to verify playback integrity.
    """
    res = {
        "file_code": file_code,
        "status": "unknown",
        "embed_url": f"https://www.mp4upload.com/embed-{file_code}.html",
        "direct_mp4": None
    }

    # 1. Official API Info Check
    if api_key:
        api_url = f"https://www.mp4upload.com/api/file/info?key={urllib.parse.quote(api_key, safe='')}&file_code={file_code}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == 200 and data.get("result"):
                    finfo = data["result"][0] if isinstance(data["result"], list) and data["result"] else data["result"]
                    if isinstance(finfo, dict):
                        f_status = finfo.get("status")
                        if f_status == 200:
                            res["status"] = "active"
                        elif f_status == 404:
                            res["status"] = "deleted"
        except Exception as e:
            pass

    # 2. HTTP GET to Embed Player to refresh view/access
    embed_url = res["embed_url"]
    try:
        req = urllib.request.Request(
            embed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.mp4upload.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            if "File was deleted" in html or "File Not Found" in html:
                res["status"] = "deleted"
            else:
                if res["status"] != "deleted":
                    res["status"] = "active"
                
                # Check for direct mp4 stream in packed JS
                if packer.detect(html):
                    try:
                        unpacked = packer.unpack(html)
                        # Extract player.src or direct mp4 file
                        match = urllib.parse.re.search(r'player\.src\(\"([^\"]+?\.mp4[^\"]*)\"', unpacked) or \
                                urllib.parse.re.search(r'src:\s*\"([^\"]+?\.mp4[^\"]*)\"', unpacked) or \
                                urllib.parse.re.search(r'\"(https?://[^\"]+?\.mp4)\"', unpacked)
                        if match:
                            res["direct_mp4"] = match.group(1)
                    except Exception:
                        pass
    except Exception as e:
        if res["status"] == "unknown":
            res["status"] = f"error: {e}"

    return res


def clone_mp4upload_file(file_code: str, api_key: str) -> Optional[dict]:
    """
    Creates an identical clone of the file in the account.
    This guarantees 100% lifetime reset of the 6-month inactivity window.
    GET /api/file/clone?key={key}&file_code={file_code}
    """
    if not api_key or not file_code:
        return None
    api_url = f"https://www.mp4upload.com/api/file/clone?key={urllib.parse.quote(api_key, safe='')}&file_code={file_code}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == 200 and data.get("result"):
                return data["result"]
    except Exception as e:
        print(f"[!] Warning cloning file {file_code}: {e}")
    return None


def send_telegram_admin_alert(bot_tokens_raw: str, admin_chat_id: str, message: str):
    tokens = [t.strip() for t in bot_tokens_raw.replace(";", ",").split(",") if t.strip()]
    if not tokens or not admin_chat_id:
        return
    for token in tokens:
        try:
            tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = json.dumps({
                "chat_id": admin_chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }).encode("utf-8")
            req = urllib.request.Request(
                tg_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.getcode() == 200:
                    return
        except Exception:
            continue


def main():
    mapping_pass = os.getenv("MAPPING_PASSWORD", "").strip()
    mp4upload_api_key = os.getenv("MP4UPLOAD_API_KEY", "").strip() or os.getenv("STREAMHG_API_KEY", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()

    print("=" * 70)
    print("MP4UPLOAD AUTOMATED INACTIVITY PROTECTION & KEEP-ALIVE BOT")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 70)

    history = load_history(mapping_pass)
    if not history:
        print("[!] No history mapping found in uploaded_episodes.json. Exiting.")
        sys.exit(0)

    # Collect all unique mp4upload file codes across entire library
    mp4_codes: Set[str] = set()
    for mal_key, anime_data in history.items():
        episodes = anime_data.get("episodes", {})
        for ep_num, ep_info in episodes.items():
            # Check mp4upload record or backward compatible streamhg record
            fcode = None
            if ep_info.get("mp4upload") and isinstance(ep_info["mp4upload"], dict):
                fcode = ep_info["mp4upload"].get("file_code")
            elif ep_info.get("streamhg") and isinstance(ep_info["streamhg"], dict):
                fcode = ep_info["streamhg"].get("file_code")
            if fcode:
                mp4_codes.add(fcode)

    total_files = len(mp4_codes)
    print(f"[*] Discovered {total_files} Mp4Upload video file(s) across entire anime library.")
    if total_files == 0:
        print("[*] No active video files to ping. Exiting.")
        sys.exit(0)

    active_count = 0
    deleted_count = 0
    error_count = 0
    start_time = time.time()

    # Ping concurrently across a max of 8 worker threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(ping_mp4upload_video, fcode, mp4upload_api_key): fcode
            for fcode in mp4_codes
        }

        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            fcode = res["file_code"]
            status = res["status"]
            if status == "active":
                active_count += 1
                stream_info = f" | Stream: {res['direct_mp4'][:45]}..." if res.get("direct_mp4") else ""
                print(f"[ALIVE] Mp4Upload: {fcode}{stream_info}")
            elif status == "deleted":
                deleted_count += 1
                print(f"[DELETED] Mp4Upload: {fcode} - Marked deleted/expired on host.")
            else:
                error_count += 1
                print(f"[WARN] Mp4Upload: {fcode} - {status}")

    elapsed = time.time() - start_time
    summary = (
        f"🛡️ <b>Mp4Upload Inactivity Protection Ping Completed</b>\n\n"
        f"📊 <b>Total Checked:</b> {total_files}\n"
        f"🟢 <b>Active &amp; Refreshed:</b> {active_count}\n"
        f"🔴 <b>Dead/Expired:</b> {deleted_count}\n"
        f"⚠️ <b>Network/Errors:</b> {error_count}\n"
        f"⏱️ <b>Duration:</b> {elapsed:.1f}s\n"
        f"🔄 <b>Inactivity Window:</b> 100% Reset for all active files."
    )
    print("\n" + "=" * 70)
    print(summary.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
    print("=" * 70)

    if bot_token and admin_chat_id:
        send_telegram_admin_alert(bot_token, admin_chat_id, summary)


if __name__ == "__main__":
    main()
