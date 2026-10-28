"""
Anikoto Manual Episode Resolver for ClixArena Ingestion Pipeline
Key Features:
- Pure Python & curl_cffi Chrome impersonation (Zero Playwright / Zero Headless Browser).
- Takes exact MAL ID, Episode / Episode Range (e.g. 1, 1-12, 1,3,5), and Quality (1080p / 720p / 360p).
- Scrapes Official MAL Japanese/English titles.
- Maps to Anikoto watch page and validates matching data-mal ID.
- Resolves Nekostream mapper API & Kwik to produce Direct MP4 URLs (https://vault-XX.owocdn.top/mp4/...).
- Deduplicates against uploaded_episodes.json (Target-aware: Abyss / Telegram / Both).
- Outputs standard GitHub Actions Matrix JSON.
"""

import os
import re
import sys
import json
import base64
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Set
from curl_cffi import requests

from mal_resolver import resolve_mal_metadata
from mapping_crypto import decrypt_mapping

BASE = "https://anikoto.cz"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STATE_FILE = "uploaded_episodes.json"
DELETED_FILE = "deleted_episodes.json"


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


def unpack_dean_edwards(packed_js: str) -> str:
    match = re.search(r"\}\('([\s\S]*?)',(\d+),(\d+),'([\s\S]*?)'\.split\('\|'\)", packed_js)
    if not match:
        match = re.search(r'\}\("([\s\S]*?)",(\d+),(\d+),"([\s\S]*?)"\.split\("\|"\)', packed_js)
    if not match:
        return ""

    p, a, c, k = match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).split('|')

    def base_n(num, base):
        chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if num < base:
            return chars[num]
        return base_n(num // base, base) + chars[num % base]

    lookup = {}
    for i in range(c):
        token = base_n(i, a)
        lookup[token] = k[i] if (i < len(k) and k[i]) else token

    def replace_word(m):
        w = m.group(0)
        return lookup.get(w, w)

    return re.sub(r'\b\w+\b', replace_word, p)


def get_mal_anime_titles(mal_id: int):
    """Scrapes official Japanese/Romaji and English titles directly from MyAnimeList page."""
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
    pattern = r'<a[^>]*data-id="[^"]*"[^>]*data-num="([^"]+)"[^>]*data-slug="([^"]+)"[^>]*data-mal="([^"]+)"[^>]*data-timestamp="([^"]+)"'
    episodes = {}
    for m in re.finditer(pattern, ep_html):
        num_str, slug, mal_id, timestamp = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            ep_num = float(num_str) if '.' in num_str else int(num_str)
            episodes[ep_num] = {
                "num": ep_num,
                "slug": slug,
                "mal_id": mal_id,
                "timestamp": timestamp
            }
        except ValueError:
            continue
    return episodes


def resolve_episode_direct_mp4(session, ep_meta: dict, quality="1080p", audio="sub"):
    mal_id = ep_meta["mal_id"]
    slug = ep_meta["slug"]
    timestamp = ep_meta["timestamp"]
    ep_num = ep_meta["num"]

    mapper_url = f"https://mapper.nekostream.site/api/mal/{mal_id}/{slug}/{timestamp}"
    mapper_res = session.get(mapper_url, headers={"Referer": f"{BASE}/"})
    data = mapper_res.json()

    kiwi_data = data.get("Kiwi", {}).get(audio, {}).get("download", {})
    pahe_url = kiwi_data.get(quality) or kiwi_data.get("1080p") or kiwi_data.get("720p") or kiwi_data.get("360p")
    if not pahe_url:
        raise ValueError(f"No stream found for Episode {ep_num} ({quality})")

    path_id = pahe_url.rstrip("/").split("/")[-1]
    test_url = f"https://testpahe.nekostream.site/{path_id}"
    redirect_res = session.get(test_url, headers={"Referer": pahe_url}, allow_redirects=False)
    kwik_url = redirect_res.headers.get("Location")
    if not kwik_url:
        raise ValueError(f"Could not get Kwik redirect for Ep {ep_num}")

    kwik_embed_url = kwik_url.replace("/f/", "/e/")
    kwik_res = session.get(kwik_embed_url, headers={"Referer": "https://kwik.cx/"})

    scripts = re.findall(r"<script[^>]*>([\s\S]*?)<\/script>", kwik_res.text)
    stream_m3u8_url = None

    for script in scripts:
        if "eval(function" in script:
            for b in script.split("eval(function")[1:]:
                unpacked = unpack_dean_edwards("eval(function" + b)
                m_src = re.search(r"source\s*=\s*\\?['\"]([^'\"]+\.m3u8[^'\"]*)\\?['\"]", unpacked)
                if m_src:
                    stream_m3u8_url = m_src.group(1).replace("\\", "").strip()
                    break
        if stream_m3u8_url:
            break

    if not stream_m3u8_url:
        raise ValueError(f"Failed to unpack Kwik player stream for Ep {ep_num}")

    clean_base = stream_m3u8_url.replace("/stream/", "/mp4/").replace("/uwu.m3u8", "")
    filename = f"MAL_{mal_id}_EP_{ep_num}_{quality}.mp4"
    direct_mp4_url = f"{clean_base}?file={filename}"

    # Verify head
    head_res = session.head(direct_mp4_url, headers={"Referer": "https://kwik.cx/"})
    file_size_mb = 0
    if head_res.status_code in (200, 206):
        content_len = int(head_res.headers.get("Content-Length", 0))
        file_size_mb = round(content_len / (1024 * 1024), 2)

    return direct_mp4_url, file_size_mb, filename


def main():
    target_mal_id_str = os.getenv("TARGET_MAL_ID", "").strip()
    episode_range_str = os.getenv("EPISODE_RANGE", "all").strip()
    target_quality = os.getenv("TARGET_QUALITY", "1080p").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()
    mal_client_id = os.getenv("MAL_CLIENT_ID", "").strip()

    if not target_mal_id_str or not target_mal_id_str.isdigit():
        print("[ERROR] TARGET_MAL_ID is mandatory for Anikoto manual downloader.", file=sys.stderr)
        sys.exit(1)

    mal_id = int(target_mal_id_str)
    print("=" * 70)
    print(f"ANIKOTO MANUAL EPISODE DETECTOR & DISPATCHER")
    print(f"MAL ID: {mal_id} | Range: {episode_range_str} | Quality: {target_quality} | Target: {upload_target}")
    print("=" * 70)

    # 1. Resolve Titles
    main_title, en_title = get_mal_anime_titles(mal_id)
    display_title = en_title or main_title or f"Anime {mal_id}"
    print(f"[+] MAL Anime: '{main_title}' (English: '{en_title}')")

    # 2. Match Anikoto
    session = requests.Session(impersonate="chrome120")
    anime_id, watch_url, ep_html = find_anikoto_watch_page(session, mal_id, [en_title, main_title])

    # 3. Parse Episodes
    all_episodes = parse_episodes_metadata(ep_html)
    print(f"[+] Total Episodes Available on Anikoto: {len(all_episodes)}")

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

        print(f"[*] Resolving direct MP4 for Episode {ep_num} ({target_quality})...")
        try:
            mp4_url, size_mb, filename = resolve_episode_direct_mp4(session, all_episodes[ep_num], quality=target_quality)
            print(f"[+] Found MP4: {filename} ({size_mb} MB)")

            item_payload = {
                "key": f"anikoto_{mal_id}_ep{ep_num}",
                "anime_id": anime_id,
                "anime_url": watch_url,
                "title": display_title,
                "episode": int(ep_num),
                "stream_url": mp4_url,
                "stream_referer": "https://kwik.cx/",
                "filename": filename,
                "quality": target_quality,
                "size_mb": size_mb,
                "provider": "anikoto",
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

    # Output GitHub Actions Environment variables
    github_output = os.getenv("GITHUB_OUTPUT")
    has_items = "true" if len(matrix_items) > 0 else "false"

    print("\n" + "=" * 70)
    print(f"ANIKOTO RESOLUTION SUMMARY: {len(matrix_items)} Episodes queued for upload.")
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
