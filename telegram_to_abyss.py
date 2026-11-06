"""
Telegram to Abyss.to One-By-One Batch Migration Tool
- Scans uploaded_episodes.json (AES-256-GCM encrypted or plaintext)
- Discovers all episodes that have Telegram backups but NO Abyss.to upload
- Downloads each video from Telegram (rotating through a pool of up to 19 Telegram bots with auto-retry and failover)
- Uploads the video directly to Abyss.to (https://up.abyss.to/{ABYSS_API_KEY})
- Updates the history mapping with abyss: {slug, file_id, embed_url}
- Encrypts and saves uploaded_episodes.json
- Automatically cleans up local temp files immediately after each upload.
"""

import os
import re
import sys
import time
import json
import secrets
import string
import random
import subprocess
import urllib.parse
import urllib.request
from typing import Dict, Any, List, Optional
from mapping_crypto import encrypt_mapping, decrypt_mapping
from abyss_pool import AbyssAccountPool, get_abyss_api_keys

STATE_FILE = "uploaded_episodes.json"
MAX_RUN_SECONDS = int(os.getenv("MAX_RUN_SECONDS", "18000"))  # Default 5h (18,000s) out of 6h GitHub limit
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def load_history(password: str) -> dict:
    if not os.path.exists(STATE_FILE):
        print(f"[ERROR] {STATE_FILE} not found.", file=sys.stderr)
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            content = json.load(f)
        if isinstance(content, dict) and content.get("algorithm") == "AES-256-GCM":
            if not password:
                raise RuntimeError("MAPPING_PASSWORD required to decrypt uploaded_episodes.json")
            return decrypt_mapping(content, password)
        elif isinstance(content, dict):
            return content
    except Exception as e:
        print(f"[!] Error loading history: {e}", file=sys.stderr)
        return {}
    return {}


def save_history(history: dict, password: str):
    if password:
        enc_doc = encrypt_mapping(history, password)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(enc_doc, f, indent=2, ensure_ascii=False)
    else:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    print(f"[+] Successfully saved updated mappings to {STATE_FILE}!")


def get_telegram_download_url(file_id: str, bot_tokens: List[str], local_api_url: str = "") -> Optional[str]:
    """Resolves Telegram file_id to direct download URL using bot token pool."""
    for idx, token in enumerate(bot_tokens):
        base_api = local_api_url.rstrip("/") if local_api_url else "https://api.telegram.org"
        req_url = f"{base_api}/bot{token}/getFile?file_id={file_id}"
        try:
            req = urllib.request.Request(req_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data.get("result", {}).get("file_path"):
                file_path = data["result"]["file_path"]
                if local_api_url:
                    # Direct file on local bot API server
                    return f"{local_api_url.rstrip('/')}/file/bot{token}/{file_path}"
                return f"https://api.telegram.org/file/bot{token}/{file_path}"
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            print(f"  [!] Telegram getFile via Bot #{idx+1} warning: {e} -> {err_body}")
            time.sleep(1)
        except Exception as e:
            print(f"  [!] Telegram getFile via Bot #{idx+1} warning: {e}")
            time.sleep(1)
    return None


def download_telegram_video(download_url: str, target_file: str) -> bool:
    print(f"[*] Downloading Telegram video stream to {target_file}...")
    cmd = [
        "curl", "-sSL",
        "-A", USER_AGENT,
        "--connect-timeout", "30",
        "--max-time", "1800",
        "-o", target_file,
        download_url
    ]
    proc = subprocess.run(cmd)
    return proc.returncode == 0 and os.path.exists(target_file) and os.path.getsize(target_file) > 100000


def upload_to_abyss(temp_file: str, safe_name: str, pool_or_key: Any = None) -> Optional[dict]:
    if isinstance(pool_or_key, AbyssAccountPool):
        pool = pool_or_key
    elif isinstance(pool_or_key, str) and pool_or_key.strip():
        pool = AbyssAccountPool(keys=[pool_or_key.strip()])
    else:
        pool = AbyssAccountPool()

    return pool.upload(temp_file, safe_name)


def main():
    script_start_time = time.time()
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    abyss_pool = AbyssAccountPool()
    tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("TG_BOT_TOKEN", "").strip()
    local_api_url = os.getenv("TG_LOCAL_API_URL", "").strip()

    if abyss_pool.total_accounts == 0:
        print("[ERROR] At least one ABYSS_API_KEY or ABYSS_API_KEYS is required to upload to Abyss.to.", file=sys.stderr)
        sys.exit(1)

    if not tg_bot_token:
        print("[ERROR] TELEGRAM_BOT_TOKEN is required to download from Telegram.", file=sys.stderr)
        sys.exit(1)

    # Parse multi-token bot pool (up to 19 bots)
    all_tokens = [t.strip() for t in re.split(r'[,;|\s]+', tg_bot_token) if t.strip()]
    print(f"[*] Initialized Telegram Bot Pool with {len(all_tokens)} bot(s).")

    history = load_history(password)
    if not history:
        print("[ERROR] Failed to load history.", file=sys.stderr)
        sys.exit(1)

    # Identify pending episodes (Has Telegram but NO Abyss)
    pending_tasks = []
    for anime_key, anime_val in history.items():
        if not isinstance(anime_val, dict):
            continue
        episodes = anime_val.get("episodes", {})
        for ep_num, ep_info in episodes.items():
            if not isinstance(ep_info, dict):
                continue
            has_tg = bool(ep_info.get("telegram") and isinstance(ep_info["telegram"], dict) and ep_info["telegram"].get("file_id"))
            has_abyss = bool(
                (ep_info.get("abyss") and (ep_info["abyss"].get("slug") or ep_info["abyss"].get("file_id"))) or
                ep_info.get("slug")
            )
            if has_tg and not has_abyss:
                pending_tasks.append({
                    "anime_key": anime_key,
                    "anime_title": anime_val.get("title") or anime_val.get("title_english") or "Anime",
                    "ep_num": ep_num,
                    "ep_info": ep_info
                })

    print(f"==================================================================")
    print(f"[*] Found {len(pending_tasks)} episode(s) on Telegram needing Abyss.to upload.")
    print(f"==================================================================")

    if not pending_tasks:
        print("[DONE] All Telegram episodes already uploaded to Abyss.to!")
        return

    success_count = 0
    fail_count = 0
    stopped_early = False

    for idx, task in enumerate(pending_tasks, 1):
        elapsed_run = time.time() - script_start_time
        if elapsed_run >= MAX_RUN_SECONDS:
            stopped_early = True
            print(f"\n[!] TIME-GUARD TRIGGERED: Elapsed {elapsed_run/3600:.2f}h reached safety limit ({MAX_RUN_SECONDS/3600:.2f}h)!")
            print("[!] Gracefully stopping now so GitHub Actions can commit mapping and auto-retrigger next run.")
            if os.getenv("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
                    f_out.write("has_remaining=true\n")
            break

        anime_key = task["anime_key"]
        anime_title = task["anime_title"]
        ep_num = task["ep_num"]
        ep_info = task["ep_info"]

        tg_data = ep_info["telegram"]
        file_id = tg_data.get("file_id")

        print(f"\n[{idx}/{len(pending_tasks)}] Processing: {anime_title} - Episode {ep_num}")
        print(f"  Telegram Message ID: {tg_data.get('message_id')} | File ID: {file_id[:20]}...")

        # Rotate bot tokens for getFile
        random.shuffle(all_tokens)
        dl_url = get_telegram_download_url(file_id, all_tokens, local_api_url)

        if not dl_url:
            print(f"  [!] Failed to resolve Telegram download URL for Ep {ep_num}. Skipping.")
            fail_count += 1
            continue

        temp_name = f"temp_migrate_{ep_num}_{int(time.time())}.mp4"
        try:
            if not download_telegram_video(dl_url, temp_name):
                print(f"  [!] Failed to download video file from Telegram. Skipping.")
                fail_count += 1
                continue

            size_mb = os.path.getsize(temp_name) / (1024 * 1024)
            print(f"  [+] Downloaded from Telegram: {size_mb:.2f} MB")

            safe_name = f"{anime_title}_Ep_{ep_num}.mp4"
            abyss_res = abyss_pool.upload(temp_name, safe_name)

            if not abyss_res:
                if abyss_pool.all_exhausted():
                    stopped_early = True
                    print(f"\n[!] ALL ABYSS ACCOUNTS EXHAUSTED: Daily upload quotas reached across all {abyss_pool.total_accounts} account(s)!")
                    print("[!] Gracefully stopping now so GitHub Actions can commit mapping and auto-retrigger next run.")
                    if os.getenv("GITHUB_OUTPUT"):
                        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
                            f_out.write("has_remaining=true\n")
                    break
                print(f"  [!] Failed to upload to Abyss.to. Skipping.")
                fail_count += 1
                continue

            # Update mapping
            ep_info["abyss"] = abyss_res
            ep_info["slug"] = abyss_res["slug"]
            ep_info["embed_url"] = abyss_res["embed_url"]
            if not ep_info.get("size_mb"):
                ep_info["size_mb"] = round(size_mb, 2)

            history[anime_key]["episodes"][ep_num] = ep_info
            save_history(history, password)
            success_count += 1
            print(f"  [✓] Ep {ep_num} migrated & saved successfully!")

        finally:
            if os.path.exists(temp_name):
                try:
                    os.remove(temp_name)
                except Exception:
                    pass

        # Brief pause between episodes to protect API quotas
        time.sleep(2)

    if not stopped_early and (success_count + fail_count < len(pending_tasks)):
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
                f_out.write("has_remaining=true\n")

    print(f"\n==================================================================")
    print(f"[MIGRATION FINISHED] Success: {success_count} | Failed: {fail_count} | Total: {len(pending_tasks)}")
    print(f"==================================================================")


if __name__ == "__main__":
    main()
