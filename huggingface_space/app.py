import os
import sys
import time
import json
import threading
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Set

ANIMEHEAVEN_URL = "https://animeheaven.me/new.php"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
POLL_INTERVAL_SECONDS = 5

GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# Global status for Web Dashboard
LATEST_STATUS = {
    "status": "Running",
    "last_check": "Never",
    "releases_tracked": 0,
    "last_triggered": "None",
    "target": ANIMEHEAVEN_URL,
    "interval": "5 seconds"
}


def fetch_latest_release_keys() -> Set[str]:
    req = urllib.request.Request(
        ANIMEHEAVEN_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://animeheaven.me/"
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    import re
    pattern = r"""<div class=['"]chart bc1['"]>[\s\S]*?<a href=['"]anime\.php\?([^'"]+)['"]>[\s\S]*?<div class=['"]chartepm[^'"]*['"]>(\d+)</div>"""
    keys = set()
    for m in re.finditer(pattern, html, re.IGNORECASE):
        anime_id = m.group(1).strip()
        ep_num = m.group(2).strip()
        keys.add(f"{anime_id}_ep{ep_num}")
    return keys


def trigger_github_upload_pipeline(new_episodes: list):
    if not GITHUB_REPO or not GITHUB_TOKEN:
        print("[!] GITHUB_REPO or GITHUB_TOKEN not configured in Hugging Face Secrets.", file=sys.stderr)
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/animeheaven-auto.yml/dispatches"
    payload = json.dumps({
        "ref": "main",
        "inputs": {
            "target_anime_url": "",
            "target_mal_id": "",
            "episode_range": "all"
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "HF-AnimeHeaven-5s-Watcher",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                print(f"[🔥 TRIGGERED] Dispatched GitHub Action for: {new_episodes}", flush=True)
                LATEST_STATUS["last_triggered"] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} ({len(new_episodes)} releases)"
            else:
                print(f"[!] GitHub API status: {resp.status}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[!] Failed to trigger GitHub Action: {e}", file=sys.stderr, flush=True)


def watcher_loop():
    print("[*] Starting 5-Second Realtime Watcher loop...", flush=True)
    known_keys: Set[str] = set()
    try:
        known_keys = fetch_latest_release_keys()
        LATEST_STATUS["releases_tracked"] = len(known_keys)
        print(f"[+] Baseline cache: {len(known_keys)} active releases.", flush=True)
    except Exception as e:
        print(f"[!] Initial baseline load warning: {e}", file=sys.stderr, flush=True)

    while True:
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
            latest_keys = fetch_latest_release_keys()
            LATEST_STATUS["last_check"] = time.strftime('%Y-%m-%d %H:%M:%S')
            LATEST_STATUS["releases_tracked"] = len(latest_keys)

            new_releases = latest_keys - known_keys
            if new_releases:
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                print(f"[⚡ NEW RELEASE DETECTED] [{timestamp}] -> {list(new_releases)}", flush=True)
                known_keys.update(latest_keys)
                trigger_github_upload_pipeline(list(new_releases))
        except Exception as e:
            print(f"[!] Network polling warning: {e}", file=sys.stderr, flush=True)
            time.sleep(2)


# Minimal Health Server to keep Hugging Face Space alive and Healthy on port 7860
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <title>AniAsia 5s Realtime Watcher</title>
          <meta http-equiv="refresh" content="5">
          <style>
            body {{ font-family: system-ui, sans-serif; background: #07090e; color: #f1f5f9; padding: 2rem; display: flex; justify-content: center; }}
            .card {{ background: #0f172a; border: 1px solid rgba(255,255,255,0.1); border-radius: 1rem; padding: 2rem; max-width: 520px; width: 100%; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
            h1 {{ font-size: 1.4rem; color: #ff3366; margin-bottom: 1rem; }}
            .status-badge {{ background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); padding: 0.3rem 0.8rem; border-radius: 9999px; font-weight: 700; font-size: 0.85rem; display: inline-block; margin-bottom: 1.5rem; }}
            .row {{ display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.9rem; }}
            .row span:first-child {{ color: #94a3b8; }}
            .row span:last-child {{ font-weight: 600; color: #fff; }}
          </style>
        </head>
        <body>
          <div class="card">
            <h1>⚡ AniAsia 5s Realtime Watcher</h1>
            <div class="status-badge">● 24/7 ACTIVE & POLLING</div>
            <div class="row"><span>Target URL:</span><span>animeheaven.me/new.php</span></div>
            <div class="row"><span>Polling Frequency:</span><span>Every 5 Seconds</span></div>
            <div class="row"><span>Tracked Releases:</span><span>{LATEST_STATUS['releases_tracked']} items</span></div>
            <div class="row"><span>Last Poll Timestamp:</span><span>{LATEST_STATUS['last_check']}</span></div>
            <div class="row"><span>Last Triggered:</span><span>{LATEST_STATUS['last_triggered']}</span></div>
            <div class="row"><span>Repository:</span><span>{GITHUB_REPO or 'Not Configured'}</span></div>
          </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        return  # Suppress HTTP access logging to keep console clean


def main():
    # Start background watcher daemon thread
    t = threading.Thread(target=watcher_loop, daemon=True)
    t.start()

    # Hugging Face default application port is 7860
    port = int(os.getenv("PORT", 7860))
    print(f"[*] Starting Hugging Face Web Server on port {port}...", flush=True)
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
