"""
Batch Backlog Migration to Abyss.to (AnimeHeaven with MegaPlay Fallback)
Features:
- Scans uploaded_episodes.json (AES-256-GCM encrypted or plaintext)
- Discovers all episodes that have Telegram data but MISSING Abyss.to upload
- Primary Source: AnimeHeaven (Resolves directly from Anime URL or Title)
- Fallback Source: MegaPlay (1080p Pure Direct MAL Engine with subtitle burning)
- Direct upload to Abyss.to (https://up.abyss.to/{ABYSS_API_KEY})
- Updates encrypted mapping and commits automatically.
"""

import os
import re
import sys
import time
import json
import secrets
import string
import random
import urllib.parse
import urllib.request
import subprocess
from typing import Dict, Any, List, Optional
from curl_cffi import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from mapping_crypto import encrypt_mapping, decrypt_mapping
from abyss_pool import AbyssAccountPool, get_abyss_api_keys

STATE_FILE = "uploaded_episodes.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
ANIMEHEAVEN_BASE = "https://animeheaven.me"
MEGAPLAY_BASE = "https://megaplay.buzz"

# MegaPlay Codec Keys
MEGAPLAY_AES_KEY = b'i?LMTAx0Q6,:}50U'.ljust(32, b'\0')[:32]
MEGAPLAY_IV = b"W0;27ToaUpl_P%'c"
MEGAPLAY_SIGNING_KEY = b'MpCdnT0k3n!9f2K#xQ7vL5mR8wN1pY4s'


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
        enc_data = encrypt_mapping(history, password)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(enc_data, f, indent=2, ensure_ascii=False)
    else:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    print(f"[+] Successfully saved updated mappings to {STATE_FILE}!")


def http_get(url: str, referer: str = ANIMEHEAVEN_BASE, cookie: str = "") -> str:
    headers = {"User-Agent": USER_AGENT, "Referer": referer, "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def resolve_animeheaven_stream(anime_url: str, ep_num: str) -> Optional[str]:
    """Finds download URL on AnimeHeaven for a specific episode."""
    try:
        if not anime_url.startswith("http"):
            anime_url = f"{ANIMEHEAVEN_BASE}/{anime_url}"
        html_content = http_get(anime_url)
        target_norm = str(ep_num).strip().lower()

        pattern = r"""gate[ah]\(\s*['"]([a-f0-9]{32})['"]\s*\)[^>]*>(.*?)</a>"""
        matches = list(re.finditer(pattern, html_content, re.DOTALL | re.IGNORECASE))
        session_key = None

        for m in matches:
            k = m.group(1)
            inner = m.group(2)
            w2 = re.search(r'class=\s*[\'"][^\'"]*watch2[^\'"]*[\'"][^>]*>([^<]+)<', inner)
            if w2:
                w2_val = w2.group(1).strip().lower()
                if w2_val == target_norm:
                    session_key = k
                    break
                w2_clean_m = re.search(r"(\d+(?:\.\d+)?(?:v\d+)?)", w2_val)
                if w2_clean_m and w2_clean_m.group(1) == target_norm:
                    session_key = k
                    break

            clean = re.sub(r'<[^>]+>', ' ', inner).strip()
            ep_m = re.search(r'Episode\s*([0-9]+(?:\.[0-9]+)?(?:v[0-9]+)?)', clean, re.IGNORECASE)
            if ep_m and ep_m.group(1).lower() == target_norm:
                session_key = k
                break

        if not session_key and target_norm in ("1", "1.0", "01"):
            raw_keys = re.findall(r'gate[ah]\(\s*[\'"]([a-f0-9]{32})[\'"]', html_content)
            if len(raw_keys) == 1:
                session_key = raw_keys[0]

        if session_key:
            gate_html = http_get(f"{ANIMEHEAVEN_BASE}/gate.php", referer=f"{ANIMEHEAVEN_BASE}/", cookie=f"key={session_key}")
            dl_m = re.search(r'href=[\'"](https?://[a-z0-9]+\.animeheaven\.me/video\.mp4\?[^\'"]+&d)[\'"]', gate_html)
            if dl_m:
                return dl_m.group(1)
            return f"https://cz.animeheaven.me/video.mp4?{session_key}&d"
    except Exception as e:
        print(f"  [!] AnimeHeaven resolution error for Ep {ep_num}: {e}")
    return None


def decrypt_megaplay_codec(enc_hex: str) -> Optional[str]:
    try:
        raw_enc = bytes.fromhex(enc_hex)
        cipher = Cipher(algorithms.AES(MEGAPLAY_AES_KEY), modes.CBC(MEGAPLAY_IV), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(raw_enc) + decryptor.finalize()
        pad_len = padded[-1]
        if pad_len < 16:
            padded = padded[:-pad_len]
        return padded.decode("utf-8")
    except Exception:
        return None


def sign_megaplay_url(raw_url: str) -> str:
    path_m = re.search(r'/([a-f0-9]{32})/([a-f0-9]{32})/', raw_url, re.IGNORECASE)
    if not path_m:
        return raw_url
    exp = int(time.time()) + 86400
    file_id = path_m.group(1).lower()
    folder_id = path_m.group(2).lower()
    payload = f"{exp}|{file_id}/{folder_id}".encode("utf-8")
    import hmac, hashlib, base64
    sig = hmac.new(MEGAPLAY_SIGNING_KEY, payload, hashlib.sha256).digest()
    token = f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
    sep = "&" if "?" in raw_url else "?"
    return f"{raw_url}{sep}token={token}"


def resolve_megaplay_stream(session: requests.Session, mal_id: int, ep_num: int) -> Optional[dict]:
    """Fallback resolver using MegaPlay 1080p."""
    try:
        embed_url = f"{MEGAPLAY_BASE}/stream/mal/{mal_id}/{ep_num}/sub"
        r_player = session.get(embed_url, headers={"User-Agent": USER_AGENT, "Referer": "https://myanimelist.net/"}, timeout=10)
        if r_player.status_code != 200:
            return None
        m_fid = re.search(r'<title>File (\d+)', r_player.text)
        if not m_fid:
            return None
        fid = m_fid.group(1)
        src_res = session.get(f"{MEGAPLAY_BASE}/stream/getSources?id={fid}", headers={"Referer": embed_url, "X-Requested-With": "XMLHttpRequest"}, timeout=10)
        if src_res.status_code != 200:
            return None
        src_data = src_res.json()
        m3u8_url = None
        if src_data.get("enc"):
            dec_json = decrypt_megaplay_codec(src_data["enc"])
            if dec_json:
                parsed = json.loads(dec_json)
                raw_file = parsed[0]["file"] if isinstance(parsed, list) else parsed.get("file")
                if raw_file:
                    m3u8_url = sign_megaplay_url(raw_file)
        if not m3u8_url and src_data.get("sources"):
            raw_file = src_data["sources"][0]["file"] if isinstance(src_data["sources"], list) else src_data["sources"].get("file")
            if raw_file:
                m3u8_url = sign_megaplay_url(raw_file)
        if not m3u8_url:
            return None

        primary_sub = None
        for t in src_data.get("tracks", []):
            if t.get("kind") in ("captions", "subtitles") and t.get("file"):
                label = t.get("label", "").lower()
                lang = t.get("language", "").lower()
                if "english" in label or label.startswith("eng") or lang.startswith("en"):
                    primary_sub = t.get("file")
                    if t.get("default"):
                        break

        return {"stream_url": m3u8_url, "subtitle_url": primary_sub, "referer": f"{MEGAPLAY_BASE}/"}
    except Exception:
        return None


def download_stream(stream_url: str, output_name: str, referer: str, subtitle_url: Optional[str] = None) -> bool:
    print(f"  [*] Downloading stream -> {output_name}")
    temp_sub = None
    if subtitle_url:
        temp_sub = f"temp_sub_{int(time.time())}.vtt"
        subprocess.run(["curl", "-sSL", "-A", USER_AGENT, "-o", temp_sub, subtitle_url], timeout=30)

    try:
        ffmpeg_cmd = ["ffmpeg", "-y", "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n", "-i", stream_url]
        if temp_sub and os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 50:
            ffmpeg_cmd.extend(["-vf", f"subtitles={temp_sub}"])
            ffmpeg_cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "128k"])
        else:
            ffmpeg_cmd.extend(["-c", "copy"])
        ffmpeg_cmd.extend(["-movflags", "+faststart", output_name])

        proc = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=1800)
        return proc.returncode == 0 and os.path.exists(output_name) and os.path.getsize(output_name) > 1000000
    except Exception as e:
        print(f"  [!] Download stream exception: {e}")
        return False
    finally:
        if temp_sub and os.path.exists(temp_sub):
            try:
                os.remove(temp_sub)
            except Exception:
                pass


def upload_to_abyss(temp_file: str, safe_name: str, pool_or_key: Any = None) -> Optional[dict]:
    if isinstance(pool_or_key, AbyssAccountPool):
        pool = pool_or_key
    elif isinstance(pool_or_key, str) and pool_or_key.strip():
        pool = AbyssAccountPool(keys=[pool_or_key.strip()])
    else:
        pool = AbyssAccountPool()

    return pool.upload(temp_file, safe_name)


def main():
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    abyss_pool = AbyssAccountPool()
    limit = int(os.getenv("MIGRATE_LIMIT", "0").strip() or "0")

    if abyss_pool.total_accounts == 0:
        print("[ERROR] At least one ABYSS_API_KEY or ABYSS_API_KEYS is required to upload to Abyss.to.", file=sys.stderr)
        sys.exit(1)

    history = load_history(password)
    if not history:
        print("[ERROR] Empty history or failed decryption.", file=sys.stderr)
        sys.exit(1)

    pending_tasks = []
    for anime_key, anime_val in history.items():
        if not isinstance(anime_val, dict) or "episodes" not in anime_val:
            continue
        anime_url = anime_val.get("animeheaven_url") or anime_val.get("anime_url") or ""
        mal_id = anime_val.get("mal_id")
        anime_title = anime_val.get("title") or anime_val.get("title_english") or "Anime"

        for ep_num, ep_info in anime_val["episodes"].items():
            if not isinstance(ep_info, dict):
                continue
            has_tg = bool(ep_info.get("telegram"))
            has_abyss = bool((ep_info.get("abyss") and ep_info["abyss"].get("slug")) or ep_info.get("slug"))

            if has_tg and not has_abyss:
                pending_tasks.append({
                    "anime_key": anime_key,
                    "anime_url": anime_url,
                    "mal_id": mal_id,
                    "anime_title": anime_title,
                    "ep_num": ep_num,
                    "ep_info": ep_info
                })

    print(f"==================================================================")
    print(f"[*] Found {len(pending_tasks)} episode(s) needing Abyss.to upload.")
    print(f"==================================================================")

    if not pending_tasks:
        print("[DONE] All episodes already have Abyss.to upload!")
        return

    if limit > 0:
        pending_tasks = pending_tasks[:limit]
        print(f"[*] Processing limited batch of {limit} episode(s).")

    session = requests.Session(impersonate="chrome120")
    success_count = 0
    fail_count = 0

    for idx, task in enumerate(pending_tasks, 1):
        anime_key = task["anime_key"]
        anime_url = task["anime_url"]
        mal_id = task["mal_id"]
        anime_title = task["anime_title"]
        ep_num = task["ep_num"]
        ep_info = task["ep_info"]

        print(f"\n[{idx}/{len(pending_tasks)}] Processing: {anime_title} - Episode {ep_num}")

        stream_url = None
        referer = f"{ANIMEHEAVEN_BASE}/"
        subtitle_url = None

        # 1. Primary Source: AnimeHeaven
        if anime_url:
            print(f"  [1/2] Resolving via AnimeHeaven ({anime_url})...")
            stream_url = resolve_animeheaven_stream(anime_url, str(ep_num))

        # 2. Fallback Source: MegaPlay 1080p (if AnimeHeaven failed and mal_id exists)
        if not stream_url and mal_id:
            print(f"  [2/2] AnimeHeaven unavailable. Falling back to MegaPlay for MAL ID {mal_id}...")
            mega_res = resolve_megaplay_stream(session, int(mal_id), int(ep_num) if str(ep_num).isdigit() else 1)
            if mega_res:
                stream_url = mega_res["stream_url"]
                referer = mega_res["referer"]
                subtitle_url = mega_res["subtitle_url"]
                print(f"  [+] MegaPlay Stream Acquired (Subtitles: {bool(subtitle_url)})")

        if not stream_url:
            print(f"  [!] Both AnimeHeaven and MegaPlay failed for {anime_title} Ep {ep_num}. Skipping.")
            fail_count += 1
            continue

        temp_name = f"temp_source_{ep_num}_{int(time.time())}.mp4"
        try:
            if not download_stream(stream_url, temp_name, referer=referer, subtitle_url=subtitle_url):
                print(f"  [!] Failed to download stream. Skipping.")
                fail_count += 1
                continue

            size_mb = os.path.getsize(temp_name) / (1024 * 1024)
            safe_name = f"{anime_title}_Ep_{ep_num}.mp4"
            abyss_res = abyss_pool.upload(temp_name, safe_name)

            if not abyss_res:
                if abyss_pool.all_exhausted():
                    print(f"\n[!] ALL ABYSS ACCOUNTS HIT DAILY QUOTA / EXHAUSTED!")
                    print(f"[!] Stopping backlog sync cleanly to preserve quota and state.")
                    break
                print(f"  [!] Failed to upload to Abyss. Skipping.")
                fail_count += 1
                continue

            # Update mapping preserving telegram data
            ep_info["abyss"] = abyss_res
            ep_info["slug"] = abyss_res["slug"]
            ep_info["embed_url"] = abyss_res["embed_url"]
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

        time.sleep(2)

    print(f"\n==================================================================")
    print(f"[MIGRATION FINISHED] Success: {success_count} | Failed: {fail_count} | Total: {len(pending_tasks)}")
    print(f"==================================================================")


if __name__ == "__main__":
    main()
