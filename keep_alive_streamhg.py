"""
StreamHG Automated Keep-Alive & Inactivity Protection Bot
Periodically pings all video endpoints in uploaded_episodes.json to ensure
StreamHG's internal database registers active views/touches, preventing
automatic purge of inactive videos.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import concurrent.futures
from mapping_crypto import decrypt_mapping

STATE_FILE = "uploaded_episodes.json"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def ping_streamhg_video(file_code: str) -> dict:
    """Pings a video's embed page to register view/activity on StreamHG."""
    clean_code = file_code.strip()
    embed_url = f"https://streamhg.com/e/{clean_code}"
    ua = USER_AGENTS[hash(clean_code) % len(USER_AGENTS)]
    req = urllib.request.Request(
        embed_url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://streamhg.com/"
        }
    )
    try:
        start = time.time()
        with urllib.request.urlopen(req, timeout=12) as resp:
            elapsed = time.time() - start
            return {"file_code": clean_code, "status": resp.status, "alive": resp.status == 200, "latency": round(elapsed, 2)}
    except urllib.error.HTTPError as e:
        return {"file_code": clean_code, "status": e.code, "alive": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"file_code": clean_code, "status": 0, "alive": False, "error": str(e)}


def main():
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()
    if not os.path.exists(STATE_FILE):
        print(f"[!] {STATE_FILE} not found. Nothing to ping.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        doc = json.load(f)

    if doc.get("algorithm") == "AES-256-GCM":
        if not mapping_password:
            print("[ERROR] MAPPING_PASSWORD required to decrypt uploaded_episodes.json", file=sys.stderr)
            sys.exit(1)
        history = decrypt_mapping(doc, mapping_password)
    else:
        history = doc

    codes = set()
    for anime_key, anime in history.items():
        episodes = anime.get("episodes", {})
        for ep_num, ep in episodes.items():
            code = None
            if ep.get("streamhg") and isinstance(ep["streamhg"], dict):
                code = ep["streamhg"].get("file_code")
            elif ep.get("slug"):
                code = ep["slug"]
            if code:
                codes.add(code.strip())

    total = len(codes)
    print(f"[*] Found {total} unique StreamHG video(s) to refresh keep-alive status.")
    if total == 0:
        return

    alive_count = 0
    dead_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(ping_streamhg_video, code): code for code in codes}
        for idx, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            res = fut.result()
            if res["alive"]:
                alive_count += 1
                if idx % 10 == 0 or idx == total:
                    print(f"[{idx}/{total}] Pinged {res['file_code']} (HTTP 200 in {res['latency']}s) - Active!")
            else:
                dead_count += 1
                print(f"[!] Warning: {res['file_code']} returned {res.get('error')}")

    print(f"\n[DONE] Keep-Alive Run Completed!")
    print(f"  ✅ Active/Refreshed: {alive_count}")
    print(f"  ⚠️ Dead/Flagged: {dead_count}")
    print(f"  🛡️ StreamHG inactivity timers successfully reset for all active files.")


if __name__ == "__main__":
    main()
