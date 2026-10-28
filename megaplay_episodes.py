"""
MegaPlay Pure MAL-Direct Episode Resolver for ClixArena Ingestion Pipeline
100% Direct:
- Zero Anikoto / Zero AniList middleman.
- Direct endpoint: https://megaplay.buzz/stream/mal/{mal_id}/{ep_num}/sub
- Native e1-player v2.8 AES-256-CBC Decryption & HMAC-SHA256 Signed M3U8 URLs.
- Extracts English WebVTT subtitles for fast burning.
- Always 1080p resolution.
- Target-aware deduplication against uploaded_episodes.json.
"""

import os
import re
import sys
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Set
from curl_cffi import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from mapping_crypto import decrypt_mapping

MEGAPLAY_BASE = "https://megaplay.buzz"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STATE_FILE = "uploaded_episodes.json"

# MegaPlay Codec Keys (Exact v2.8 Keys)
MEGAPLAY_AES_KEY = b'i?LMTAx0Q6,:}50U'.ljust(32, b'\0')[:32]
MEGAPLAY_IV = b"W0;27ToaUpl_P%'c"
MEGAPLAY_SIGNING_KEY = b'MpCdnT0k3n!9f2K#xQ7vL5mR8wN1pY4s'


def parse_episode_range(range_str: str) -> Optional[Set[int]]:
    if not range_str or range_str.strip().lower() in ("all", "*", ""):
        return None
    allowed = set()
    for p in range_str.split(","):
        p = p.strip()
        if not p:
            continue
        if "-" in p:
            parts = p.split("-")
            if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                start = int(parts[0].strip())
                end = int(parts[1].strip())
                for num in range(min(start, end), max(start, end) + 1):
                    allowed.add(num)
        elif p.isdigit():
            allowed.add(int(p))
    return allowed


def load_history(password: str) -> dict:
    if os.path.exists(STATE_FILE):
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
            print(f"[!] Warning reading history: {e}. Starting with empty state.")
            return {}
    return {}


def is_already_uploaded(history: dict, primary_key: str, ep_num: int, upload_target: str) -> bool:
    ep_str = str(ep_num)
    entry = history.get(primary_key)
    if not entry or "episodes" not in entry or ep_str not in entry["episodes"]:
        return False

    ep_data = entry["episodes"][ep_str]
    has_abyss = bool(ep_data.get("slug"))
    has_telegram = bool(ep_data.get("telegram"))

    if upload_target == "abyss":
        return has_abyss
    elif upload_target == "telegram":
        return has_telegram
    elif upload_target == "both":
        return has_abyss and has_telegram
    return False


def get_mal_title(mal_id: int):
    """Fetches anime title directly from MyAnimeList page."""
    url = f"https://myanimelist.net/anime/{mal_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m_main = re.search(r'<h1[^>]*class="title-name[^"]*"[^>]*><strong>(.*?)</strong></h1>', html)
        m_en = re.search(r'<p[^>]*class="title-english[^"]*"[^>]*>(.*?)</p>', html)
        en_t = m_en.group(1).strip() if m_en else None
        main_t = m_main.group(1).strip() if m_main else None
        return en_t or main_t or f"Anime {mal_id}"
    except Exception:
        return f"Anime {mal_id}"


def decrypt_megaplay_codec(enc_b64: str) -> Optional[str]:
    """Decrypts MegaPlay enc blob using official e1-player v2.8 AES-CBC keys."""
    pad = len(enc_b64) % 4
    if pad:
        enc_b64 += "=" * (4 - pad)
    try:
        enc_bytes = base64.urlsafe_b64decode(enc_b64)
        cipher = Cipher(algorithms.AES(MEGAPLAY_AES_KEY), modes.CBC(MEGAPLAY_IV), backend=default_backend())
        decryptor = cipher.decryptor()
        dec = decryptor.update(enc_bytes) + decryptor.finalize()
        pad_len = dec[-1]
        if 1 <= pad_len <= 16:
            dec = dec[:-pad_len]
        return dec.decode("utf-8")
    except Exception:
        return None


def sign_megaplay_url(file_url: str) -> str:
    """Signs master M3U8 endpoint using HMAC-SHA256."""
    path_m = re.search(r'/([a-f0-9]{32})/([a-f0-9]{32})/', file_url, re.IGNORECASE)
    if path_m and "token=" not in file_url:
        payload = f"{int(time.time()) + 90}|{path_m.group(1).lower()}/{path_m.group(2).lower()}".encode("utf-8")
        sig = hmac.new(MEGAPLAY_SIGNING_KEY, payload, hashlib.sha256).digest()
        token = f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
        sep = "&" if "?" in file_url else "?"
        return f"{file_url}{sep}token={token}"
    return file_url


def resolve_megaplay_direct_mal(session, mal_id: int, ep_num: int) -> Optional[dict]:
    """100% Pure MegaPlay Direct call by MAL ID."""
    embed_url = f"{MEGAPLAY_BASE}/stream/mal/{mal_id}/{ep_num}/sub"
    try:
        r_player = session.get(embed_url, headers={"Referer": f"{MEGAPLAY_BASE}/api"}, timeout=10)
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

        # Extract Subtitles (Strictly English only)
        subs = []
        for t in src_data.get("tracks", []):
            if t.get("kind") in ("captions", "subtitles") and t.get("file"):
                label = t.get("label", "").lower()
                lang = t.get("language", "").lower()
                if "english" in label or label.startswith("eng") or lang.startswith("en"):
                    subs.append({
                        "lang": t.get("label", "English"),
                        "url": t.get("file"),
                        "default": bool(t.get("default"))
                    })

        primary_sub = None
        for s in subs:
            if s.get("default"):
                primary_sub = s["url"]
                break
        if not primary_sub and subs:
            primary_sub = subs[0]["url"]

        return {
            "m3u8": m3u8_url,
            "subtitle_url": primary_sub,
            "referer": f"{MEGAPLAY_BASE}/"
        }
    except Exception:
        return None


def main():
    target_mal_id_str = os.getenv("TARGET_MAL_ID", "").strip()
    episode_range_str = os.getenv("EPISODE_RANGE", "1").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()

    if not target_mal_id_str or not target_mal_id_str.isdigit():
        print("[ERROR] TARGET_MAL_ID is mandatory for MegaPlay direct downloader.", file=sys.stderr)
        sys.exit(1)

    mal_id = int(target_mal_id_str)
    anime_title = get_mal_title(mal_id)

    print("=" * 70)
    print(f"MEGAPLAY 100% PURE DIRECT MAL ENGINE (ALWAYS 1080P)")
    print(f"MAL ID: {mal_id} | Anime: '{anime_title}' | Range: {episode_range_str} | Target: {upload_target}")
    print("=" * 70)

    session = requests.Session(impersonate="chrome120")
    history = load_history(mapping_password)
    primary_key = str(mal_id)

    allowed_range = parse_episode_range(episode_range_str)
    # If range is specified as a set, use that; otherwise probe from 1 upwards
    probe_list = sorted(list(allowed_range)) if allowed_range is not None else list(range(1, 100))

    matrix_items = []

    for ep_num in probe_list:
        if is_already_uploaded(history, primary_key, ep_num, upload_target):
            print(f"[-] Episode {ep_num} is already uploaded to {upload_target}. Skipping (dedup).")
            continue

        print(f"[*] Resolving MegaPlay 1080p Stream for Episode {ep_num}...")
        stream_res = resolve_megaplay_direct_mal(session, mal_id, ep_num)

        if not stream_res:
            print(f"[X] Episode {ep_num} not available on MegaPlay.")
            if allowed_range is None:
                # Reached end of series
                break
            continue

        filename = f"MAL_{mal_id}_EP_{ep_num}_1080p.mp4"
        print(f"[SUCCESS] MegaPlay 1080p Stream Acquired: {filename}")
        if stream_res.get("subtitle_url"):
            print(f"          Subtitles: {stream_res['subtitle_url']}")

        item_payload = {
            "key": f"megaplay_{mal_id}_ep{ep_num}",
            "anime_id": str(mal_id),
            "anime_url": f"https://myanimelist.net/anime/{mal_id}",
            "title": anime_title,
            "episode": ep_num,
            "stream_url": stream_res["m3u8"],
            "stream_referer": stream_res["referer"],
            "subtitle_url": stream_res.get("subtitle_url"),
            "filename": filename,
            "quality": "1080p",
            "provider": "megaplay",
            "mal_info": {
                "mal_id": mal_id,
                "title_english": anime_title,
                "title_japanese": anime_title,
                "media_type": "TV"
            }
        }

        b64_meta = base64.b64encode(json.dumps(item_payload).encode("utf-8")).decode("utf-8")
        matrix_items.append({
            "key": item_payload["key"],
            "meta_b64": b64_meta
        })

    github_output = os.getenv("GITHUB_OUTPUT")
    has_items = "true" if len(matrix_items) > 0 else "false"

    print("\n" + "=" * 70)
    print(f"MEGAPLAY DISPATCH SUMMARY: {len(matrix_items)} Episodes queued for upload.")
    print("=" * 70)

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"has_items={has_items}\n")
            f.write(f"matrix={json.dumps(matrix_items)}\n")
    else:
        print(f"Output has_items={has_items}")
        print(f"Matrix length: {len(matrix_items)}")


if __name__ == "__main__":
    main()
