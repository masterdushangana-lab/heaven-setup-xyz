"""
Automated Mp4Upload 100% Inactivity Protection, Direct MP4 Extractor & Auto-Clone Bot
Maintains Mp4Upload videos permanently active by:
1. Querying official Mp4Upload File Info API (/api/file/info?key={KEY}&file_code={CODE}) to check status.
2. Accessing direct embed player endpoint (https://www.mp4upload.com/embed-{code}.html) with realistic browser headers.
3. Automatically unpacking obfuscated JavaScript using packer.py to extract and save fresh direct .mp4 streaming links.
4. Auto-Cloning (/api/file/clone):
   If a file has reached 90+ days without view or is near expiration, the bot automatically creates a fresh clone
   on Mp4Upload, retrieves the NEW file_code, updates the mapping, and commits + pushes uploaded_episodes.json
   back to Git with AES-256-GCM encryption!
5. Sends Telegram admin status report upon job completion.
"""

import os
import re
import sys
import json
import time
import subprocess
import urllib.request
import urllib.parse
import concurrent.futures
from typing import Dict, List, Set, Optional
from mapping_crypto import encrypt_mapping, decrypt_mapping
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


def save_history(history: dict, password: str):
    if password:
        output_doc = encrypt_mapping(history, password)
        print("[+] Encrypted uploaded_episodes.json with AES-256-GCM.")
    else:
        output_doc = history
        print("[!] Warning: MAPPING_PASSWORD not set. Saved unencrypted.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, indent=2, ensure_ascii=False)


def ping_mp4upload_video(file_code: str, api_key: str = "") -> dict:
    """
    Refreshes activity on Mp4Upload and extracts direct MP4 streaming URL.
    1. Checks status via API (/api/file/info).
    2. Performs HTTP GET to embed player.
    3. Deobfuscates packed JS to extract direct .mp4 link.
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
        except Exception:
            pass

    # 2. HTTP GET to Embed Player to refresh view/access and parse source
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
                        match = re.search(r'player\.src\(\"([^\"]+?\.mp4[^\"]*)\"', unpacked) or \
                                re.search(r'src:\s*\"([^\"]+?\.mp4[^\"]*)\"', unpacked) or \
                                re.search(r'\"(https?://[^\"]+?\.mp4)\"', unpacked)
                        if match:
                            res["direct_mp4"] = match.group(1)
                    except Exception:
                        pass
                
                # Check plain HTML5 fallback
                if not res["direct_mp4"]:
                    m_plain = re.search(r'<source\s+src=[\"\'](https?://[^\s\"\'\<\>]+?\.mp4)[\"\']', html)
                    if m_plain:
                        res["direct_mp4"] = m_plain.group(1)
    except Exception as e:
        if res["status"] == "unknown":
            res["status"] = f"error: {e}"

    return res


def clone_mp4upload_file(file_code: str, api_key: str) -> Optional[dict]:
    """
    Creates an identical clone of the file in the account.
    GET /api/file/clone?key={key}&file_code={file_code}
    Returns { "filecode": new_code, "url": download_url }
    """
    if not api_key or not file_code:
        return None
    api_url = f"https://www.mp4upload.com/api/file/clone?key={urllib.parse.quote(api_key, safe='')}&file_code={file_code}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == 200 and data.get("result"):
                res = data["result"]
                new_code = None
                if isinstance(res, dict):
                    new_code = res.get("filecode") or res.get("file_code")
                elif isinstance(res, list) and res:
                    new_code = res[0].get("filecode") or res[0].get("file_code")
                if new_code:
                    return {"file_code": new_code}
    except Exception as e:
        print(f"[!] Warning cloning file {file_code}: {e}")
    return None


def commit_and_push_changes(commit_message: str):
    """Commits and pushes updated uploaded_episodes.json back to Git repository."""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", commit_message], check=True)
            subprocess.run(["git", "pull", "--rebase"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("[+] Successfully committed and pushed updated uploaded_episodes.json to Git!")
        else:
            print("[*] No changes detected in uploaded_episodes.json to commit.")
    except Exception as e:
        print(f"[!] Git commit/push error: {e}")


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
    print("MP4UPLOAD KEEP-ALIVE, DIRECT MP4 EXTRACTOR & AUTO-CLONE BOT")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 70)

    history = load_history(mapping_pass)
    if not history:
        print("[!] No history mapping found in uploaded_episodes.json. Exiting.")
        sys.exit(0)

    # Map file_code to list of (anime_key, ep_num)
    file_map: Dict[str, List[tuple]] = {}
    for mal_key, anime_data in history.items():
        episodes = anime_data.get("episodes", {})
        for ep_num, ep_info in episodes.items():
            fcode = None
            if ep_info.get("mp4upload") and isinstance(ep_info["mp4upload"], dict):
                fcode = ep_info["mp4upload"].get("file_code")
            elif ep_info.get("streamhg") and isinstance(ep_info["streamhg"], dict):
                fcode = ep_info["streamhg"].get("file_code")
            if fcode:
                file_map.setdefault(fcode, []).append((mal_key, ep_num))

    total_files = len(file_map)
    print(f"[*] Discovered {total_files} unique Mp4Upload video file(s) across entire anime library.")
    if total_files == 0:
        print("[*] No active video files to ping. Exiting.")
        sys.exit(0)

    active_count = 0
    deleted_count = 0
    error_count = 0
    cloned_count = 0
    direct_saved_count = 0
    has_db_updates = False
    start_time = time.time()

    # Ping concurrently across a max of 8 worker threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(ping_mp4upload_video, fcode, mp4upload_api_key): fcode
            for fcode in file_map
        }

        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            fcode = res["file_code"]
            status = res["status"]
            direct_mp4 = res.get("direct_mp4")

            # 1. Update direct_mp4 in mapping if extracted
            if direct_mp4:
                for a_key, ep_num in file_map[fcode]:
                    ep_obj = history[a_key]["episodes"][ep_num]
                    # Ensure mp4upload dict structure exists
                    if not ep_obj.get("mp4upload"):
                        ep_obj["mp4upload"] = {"file_code": fcode, "embed_url": res["embed_url"]}
                    if ep_obj["mp4upload"].get("direct_mp4") != direct_mp4:
                        ep_obj["mp4upload"]["direct_mp4"] = direct_mp4
                        ep_obj["direct_mp4"] = direct_mp4
                        direct_saved_count += 1
                        has_db_updates = True

            if status == "active":
                active_count += 1
                stream_info = f" | Direct: {direct_mp4[:45]}..." if direct_mp4 else ""
                print(f"[ALIVE] Mp4Upload: {fcode}{stream_info}")

                # Check if file has been in DB for > 90 days. If so, auto-clone it to reset 6-month timer to 0!
                for a_key, ep_num in file_map[fcode]:
                    ep_obj = history[a_key]["episodes"][ep_num]
                    uploaded_at = ep_obj.get("uploaded_at", 0)
                    days_active = (time.time() - uploaded_at) / 86400 if uploaded_at > 1000000000 else 0
                    
                    if days_active >= 90 and mp4upload_api_key:
                        print(f"[*] File {fcode} is {days_active:.0f} days old (Approaching 6-month threshold). Auto-Cloning to guarantee lifetime retention...")
                        clone_res = clone_mp4upload_file(fcode, mp4upload_api_key)
                        if clone_res and clone_res.get("file_code"):
                            new_code = clone_res["file_code"]
                            new_embed = f"https://www.mp4upload.com/embed-{new_code}.html"
                            print(f"  [CLONED] File {fcode} cloned -> NEW file_code: {new_code}! Inactivity timer reset to 0 days.")
                            
                            ep_obj["mp4upload"]["file_code"] = new_code
                            ep_obj["mp4upload"]["embed_url"] = new_embed
                            ep_obj["mp4upload"]["player_url"] = new_embed
                            ep_obj["embed_url"] = new_embed
                            ep_obj["uploaded_at"] = int(time.time())
                            cloned_count += 1
                            has_db_updates = True
                            break

            elif status == "deleted":
                deleted_count += 1
                print(f"[DELETED] Mp4Upload: {fcode} - Marked deleted/expired on host.")
            else:
                error_count += 1
                print(f"[WARN] Mp4Upload: {fcode} - {status}")

    # If any direct_mp4 was saved or files were cloned, save encrypted DB and push to Git!
    if has_db_updates:
        print("\n[*] Saving updated mappings (Direct MP4 links & Cloned Codes) to uploaded_episodes.json...")
        save_history(history, mapping_pass)
        commit_msg = f"Auto-Sync: Mp4Upload Keep-Alive (Direct Links Saved: {direct_saved_count}, Re-Cloned: {cloned_count})"
        commit_and_push_changes(commit_msg)

    elapsed = time.time() - start_time
    summary = (
        f"🛡️ <b>Mp4Upload Inactivity Protection Ping Completed</b>\n\n"
        f"📊 <b>Total Checked:</b> {total_files}\n"
        f"🟢 <b>Active &amp; Refreshed:</b> {active_count}\n"
        f"⚡ <b>Direct MP4 Saved:</b> {direct_saved_count}\n"
        f"🔄 <b>Auto-Cloned (0 Days Reset):</b> {cloned_count}\n"
        f"🔴 <b>Dead/Expired:</b> {deleted_count}\n"
        f"⏱️ <b>Duration:</b> {elapsed:.1f}s\n"
        f"💾 <b>Database Git Sync:</b> {'Committed & Pushed' if has_db_updates else 'Clean (No updates needed)'}"
    )
    print("\n" + "=" * 70)
    print(summary.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
    print("=" * 70)

    if bot_token and admin_chat_id:
        send_telegram_admin_alert(bot_token, admin_chat_id, summary)


if __name__ == "__main__":
    main()
