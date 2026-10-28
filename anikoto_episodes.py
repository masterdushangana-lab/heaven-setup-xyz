"""
Anikoto & Megaplay Manual Episode Resolver for ClixArena Ingestion Pipeline
Key Features:
- Direct MegaPlay Integration using exact codec:
  * AES-256-CBC Decryption: key = 'i?LMTAx0Q6,:}50U', iv = "W0;27ToaUpl_P%'c"
  * HMAC-SHA256 URL Signing: signingKey = 'MpCdnT0k3n!9f2K#xQ7vL5mR8wN1pY4s'
- Resolves master M3U8 + WebVTT Subtitle tracks.
- Fallback to Kwik.cx Direct MP4 when available.
- Target-aware deduplication against uploaded_episodes.json (Abyss / Telegram / Both).
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

from mal_resolver import resolve_mal_metadata
from mapping_crypto import decrypt_mapping

BASE = "https://anikoto.cz"
MEGAPLAY_BASE = "https://megaplay.buzz"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STATE_FILE = "uploaded_episodes.json"

# Megaplay Codec Keys (Compatible with e1-player v2.8)
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


def get_mal_anime_titles(mal_id: int):
    url = f"https://myanimelist.net/anime/{mal_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=12) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    main_title, en_title = None, None
    m_main = re.search(r'<h1[^>]*class="title-name[^"]*"[^>]*><strong>(.*?)</strong></h1>', html)
    if m_main:
        main_title = m_main.group(1).strip()

    m_en = re.search(r'<p[^>]*class="title-english[^"]*"[^>]*>(.*?)</p>', html)
    if m_en:
        en_title = m_en.group(1).strip()

    return main_title, en_title


def search_anikoto_cards(session, query: str):
    filter_url = f"{BASE}/filter?keyword={urllib.parse.quote(query)}"
    res = session.get(filter_url, timeout=25)
    if res.status_code != 200:
        return []

    pattern = r'<a\s+[^>]*href="([^"]*anikoto\.(?:cz|net))?(/watch/[^"/]+(?:/ep-\d+)?)"[^>]*>(.*?)</a>'
    matches = re.findall(pattern, res.text, re.DOTALL)
    results = []
    seen = set()

    for _, path, inner in matches:
        base_path = re.sub(r'/ep-\d+$', '', path)
        name = re.sub(r'<[^>]+>', '', inner).strip()
        if base_path not in seen and name and not name.isdigit():
            seen.add(base_path)
            results.append((base_path, name))

    return results


def find_anikoto_watch_page(session, target_mal_id: int, titles: list):
    variants = []
    for t in titles:
        if not t:
            continue
        cleaned = re.sub(r'[:\-–_!?#]', ' ', t).strip()
        words = cleaned.split()
        if len(words) >= 2:
            variants.append(" ".join(words[:2]))
        if len(words) >= 3:
            variants.append(" ".join(words[:3]))
        variants.append(cleaned)

    seen_vars = set()
    unique_vars = [v for v in variants if not (v in seen_vars or seen_vars.add(v))]

    candidates = []
    seen_paths = set()

    print(f"[*] Querying Anikoto search index with variants: {unique_vars[:3]}...")
    for q in unique_vars[:3]:
        cards = search_anikoto_cards(session, q)
        for path, name in cards:
            if path not in seen_paths:
                seen_paths.add(path)
                candidates.append((path, name))
        if candidates:
            break

    if not candidates:
        raise ValueError(f"No anime matches found on Anikoto for titles: {titles}")

    print(f"[*] Verifying candidates on Anikoto for MAL ID {target_mal_id}...")
    for path, name in candidates[:6]:
        watch_url = f"{BASE}{path}"
        try:
            r = session.get(watch_url, timeout=12)
            m_id = re.search(r'api/watch-order/(\d+)', r.text) or re.search(r'data-id="(\d+)"', r.text)
            if not m_id:
                continue
            anime_id = m_id.group(1)

            ep_list_url = f"{BASE}/ajax/episode/list/{anime_id}"
            ep_res = session.get(ep_list_url, headers={"X-Requested-With": "XMLHttpRequest", "Referer": watch_url}, timeout=12)
            ep_html = ep_res.json().get("result", "")

            if f'data-mal="{target_mal_id}"' in ep_html:
                print(f"[+] Exact Match Confirmed: '{name}' (Anikoto ID: {anime_id})")
                return anime_id, watch_url, ep_html
        except Exception:
            continue

    first_path, first_name = candidates[0]
    watch_url = f"{BASE}{first_path}"
    print(f"[!] Using candidate: '{first_name}' ({watch_url})")
    r = session.get(watch_url, timeout=12)
    anime_id = (re.search(r'api/watch-order/(\d+)', r.text) or re.search(r'data-id="(\d+)"', r.text)).group(1)
    ep_list_url = f"{BASE}/ajax/episode/list/{anime_id}"
    ep_res = session.get(ep_list_url, headers={"X-Requested-With": "XMLHttpRequest", "Referer": watch_url})
    return anime_id, watch_url, ep_res.json().get("result", "")


def parse_episodes_metadata(ep_html: str):
    episodes = {}
    for m in re.finditer(r'<a\s+([^>]+)>', ep_html):
        attrs = m.group(1)
        num_m = re.search(r'data-num="([^"]+)"', attrs)
        if not num_m:
            continue
        num_str = num_m.group(1)
        try:
            ep_num = float(num_str) if '.' in num_str else int(num_str)
        except ValueError:
            continue

        slug_m = re.search(r'data-slug="([^"]+)"', attrs)
        mal_m = re.search(r'data-mal="([^"]+)"', attrs)
        ts_m = re.search(r'data-timestamp="([^"]+)"', attrs)
        ids_m = re.search(r'data-ids="([^"]+)"', attrs)

        episodes[ep_num] = {
            "num": ep_num,
            "slug": slug_m.group(1) if slug_m else str(ep_num),
            "mal_id": mal_m.group(1) if mal_m else "",
            "timestamp": ts_m.group(1) if ts_m else "",
            "data_ids": ids_m.group(1) if ids_m else ""
        }
    return episodes


def decrypt_megaplay_codec(enc_b64: str) -> Optional[str]:
    """Decrypts Megaplay enc blob using official e1-player v2.8 AES-CBC keys."""
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


def resolve_megaplay_source(session, anikoto_data_ids: str, mal_id: int, ep_num: int) -> Optional[dict]:
    """Resolves signed Master M3U8 and WebVTT Subtitle tracks via Megaplay."""
    # Path A: Try directly via /stream/ani/{mal_id}/{ep_num}/sub
    embed_urls = [
        f"{MEGAPLAY_BASE}/stream/ani/{mal_id}/{ep_num}/sub"
    ]

    # Path B: Query server list for embed URL
    if anikoto_data_ids:
        try:
            res_sv = session.get(f"{BASE}/ajax/server/list?servers={anikoto_data_ids}", headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/"}, timeout=10)
            sv_html = res_sv.json().get("result", "")
            for sv_m in re.finditer(r'<li[^>]*data-link-id="([^"]+)"[^>]*>(.*?)</li>', sv_html):
                link_id = sv_m.group(1)
                res_embed = session.get(f"{BASE}/ajax/server?get={link_id}", headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/"}, timeout=10)
                url = res_embed.json().get("result", {}).get("url")
                if url and "megaplay.buzz" in url:
                    embed_urls.append(url)
        except Exception:
            pass

    for embed_url in embed_urls:
        try:
            r_player = session.get(embed_url, headers={"Referer": f"{MEGAPLAY_BASE}/api"}, timeout=10)
            m_fid = re.search(r'<title>File (\d+)', r_player.text)
            if not m_fid:
                continue
            fid = m_fid.group(1)

            r_src = session.get(f"{MEGAPLAY_BASE}/stream/getSources?id={fid}", headers={"Referer": embed_url, "X-Requested-With": "XMLHttpRequest"}, timeout=10)
            src_data = r_src.json()

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

            if m3u8_url:
                # Extract Subtitle tracks
                subs = []
                for t in src_data.get("tracks", []):
                    if t.get("kind") in ("captions", "subtitles") and t.get("file"):
                        subs.append({
                            "lang": t.get("label", "Unknown"),
                            "url": t.get("file"),
                            "default": bool(t.get("default"))
                        })

                # Prefer English subtitle as primary burn-in track
                primary_sub = None
                for s in subs:
                    if "eng" in s["lang"].lower() or s.get("default"):
                        primary_sub = s["url"]
                        break
                if not primary_sub and subs:
                    primary_sub = subs[0]["url"]

                return {
                    "m3u8": m3u8_url,
                    "referer": f"{MEGAPLAY_BASE}/",
                    "subtitle_url": primary_sub,
                    "all_subtitles": subs
                }
        except Exception:
            continue

    return None


def resolve_episode_stream(session, ep_meta: dict, quality="1080p"):
    mal_id = ep_meta["mal_id"]
    data_ids = ep_meta["data_ids"]
    ep_num = ep_meta["num"]
    filename = f"MAL_{mal_id}_EP_{ep_num}_{quality}.mp4"

    # Priority 1: Megaplay Buzz Stream + Subtitles
    print(f"[*] Querying Megaplay Stream & Subtitles for Episode {ep_num}...")
    mega_info = resolve_megaplay_source(session, data_ids, mal_id, int(ep_num))
    if mega_info:
        return {
            "stream_url": mega_info["m3u8"],
            "stream_referer": mega_info["referer"],
            "subtitle_url": mega_info["subtitle_url"],
            "filename": filename,
            "type": "hls_with_sub"
        }

    raise ValueError(f"Stream resolution failed for Episode {ep_num}")


def main():
    target_mal_id_str = os.getenv("TARGET_MAL_ID", "").strip()
    episode_range_str = os.getenv("EPISODE_RANGE", "all").strip()
    target_quality = os.getenv("TARGET_QUALITY", "1080p").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()

    if not target_mal_id_str or not target_mal_id_str.isdigit():
        print("[ERROR] TARGET_MAL_ID is mandatory for Anikoto manual downloader.", file=sys.stderr)
        sys.exit(1)

    mal_id = int(target_mal_id_str)
    print("=" * 70)
    print(f"ANIKOTO & MEGAPLAY HIGH-PERFORMANCE INGESTION ENGINE")
    print(f"MAL ID: {mal_id} | Range: {episode_range_str} | Quality: {target_quality} | Target: {upload_target}")
    print("=" * 70)

    main_title, en_title = get_mal_anime_titles(mal_id)
    display_title = en_title or main_title or f"Anime {mal_id}"
    print(f"[+] MAL Anime: '{main_title}' (English: '{en_title}')")

    session = requests.Session(impersonate="chrome120")
    anime_id, watch_url, ep_html = find_anikoto_watch_page(session, mal_id, [en_title, main_title])

    all_episodes = parse_episodes_metadata(ep_html)
    print(f"[+] Total Episodes Available: {len(all_episodes)}")

    allowed_range = parse_episode_range(episode_range_str)
    history = load_history(mapping_password)
    primary_key = str(mal_id)

    matrix_items = []

    for ep_num in sorted(all_episodes.keys()):
        if allowed_range is not None and int(ep_num) not in allowed_range:
            continue

        if is_already_uploaded(history, primary_key, int(ep_num), upload_target):
            print(f"[-] Episode {ep_num} is already uploaded to {upload_target}. Skipping (dedup).")
            continue

        print(f"[*] Resolving stream for Episode {ep_num}...")
        try:
            stream_info = resolve_episode_stream(session, all_episodes[ep_num], quality=target_quality)
            print(f"[+] Stream Resolved: {stream_info['filename']}")
            if stream_info.get("subtitle_url"):
                print(f"    Subtitle Track: {stream_info['subtitle_url']}")

            item_payload = {
                "key": f"anikoto_{mal_id}_ep{ep_num}",
                "anime_id": anime_id,
                "anime_url": watch_url,
                "title": display_title,
                "episode": int(ep_num),
                "stream_url": stream_info["stream_url"],
                "stream_referer": stream_info["stream_referer"],
                "subtitle_url": stream_info.get("subtitle_url"),
                "stream_type": stream_info["type"],
                "filename": stream_info["filename"],
                "quality": target_quality,
                "provider": "megaplay",
                "mal_info": {
                    "mal_id": mal_id,
                    "title_english": en_title or main_title,
                    "title_japanese": main_title,
                    "media_type": "TV"
                }
            }

            b64_meta = base64.b64encode(json.dumps(item_payload).encode("utf-8")).decode("utf-8")
            matrix_items.append({
                "key": item_payload["key"],
                "meta_b64": b64_meta
            })
        except Exception as err:
            print(f"[X] Episode {ep_num} error: {err}")

    github_output = os.getenv("GITHUB_OUTPUT")
    has_items = "true" if len(matrix_items) > 0 else "false"

    print("\n" + "=" * 70)
    print(f"RESOLUTION SUMMARY: {len(matrix_items)} Episodes queued for upload.")
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
