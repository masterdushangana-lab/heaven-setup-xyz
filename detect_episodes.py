"""
AnimeHeaven Episode Detector & Matrix Generator
Features:
- Mandatory MAL ID support for manual backlog uploads.
- Bulletproof 3-way deduplication (AnimeID, MAL ID, Title) to eliminate redundant re-uploads.
"""

import os
import re
import sys
import json
import html
import base64
import urllib.request
from typing import Dict, List, Optional, Set

from mal_resolver import resolve_mal_metadata, fetch_official_mal_info
from mapping_crypto import decrypt_mapping

ANIMEHEAVEN_BASE = "https://animeheaven.me"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STATE_FILE = "uploaded_episodes.json"


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
    return allowed if allowed else None


def get_headers(referer: str = ANIMEHEAVEN_BASE, cookie: str = "") -> dict:
    headers = {"User-Agent": USER_AGENT, "Referer": referer, "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def http_get(url: str, referer: str = ANIMEHEAVEN_BASE, cookie: str = "") -> str:
    req = urllib.request.Request(url, headers=get_headers(referer, cookie))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def load_history(password: str) -> Dict[str, dict]:
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
            print(f"[!] Warning reading encrypted history: {e}. Starting fresh.", file=sys.stderr)
            return {}
    return {}


def is_already_uploaded(history: dict, anime_id: str, mal_id: Optional[int], anime_title: str, episode_num: int) -> bool:
    """
    Bulletproof 3-way check:
    1. Checks if anime_id matches anywhere inside history items.
    2. Checks MAL ID if available.
    3. Checks primary title key.
    """
    ep_str = str(episode_num)

    # Check 1: By MAL ID key
    if mal_id and str(mal_id) in history:
        if ep_str in history[str(mal_id)].get("episodes", {}):
            return True

    # Check 2: By Title key
    if anime_title in history:
        if ep_str in history[anime_title].get("episodes", {}):
            return True

    # Check 3: Deep check across all entries by animeheaven_id or anime_url
    for entry_key, entry_val in history.items():
        saved_aid = entry_val.get("animeheaven_id")
        saved_mal = entry_val.get("mal_id")
        
        match = False
        if saved_aid and saved_aid == anime_id:
            match = True
        elif mal_id and saved_mal and int(saved_mal) == int(mal_id):
            match = True

        if match and ep_str in entry_val.get("episodes", {}):
            return True

    return False


def get_recent_releases(limit: int = 40) -> List[dict]:
    content = http_get(f"{ANIMEHEAVEN_BASE}/new.php")
    pattern = r"""<div class=['"]chart bc1['"]>[\s\S]*?<a href=['"]anime\.php\?([^'"]+)['"]><img class=['"]coverimg['"][^>]+alt=['"]([^'"]+)['"][^>]*>[\s\S]*?<div class=['"]chartepm[^'"]*['"]>(\d+)</div>[\s\S]*?<div class=['"]charttitlejp c['"]>([^<]*)</div>"""

    items = []
    seen = set()
    for match in re.finditer(pattern, content, re.IGNORECASE):
        anime_id = match.group(1).strip()
        raw_title = match.group(2)
        ep_num = match.group(3)
        jp_title = match.group(4).strip() if match.group(4) else None

        title = html.unescape(raw_title).strip()
        title = re.sub(r'\s+Banner$', '', title, flags=re.IGNORECASE)
        key = f"{anime_id}_ep{ep_num}"

        if key not in seen:
            seen.add(key)
            items.append({
                "key": key,
                "anime_id": anime_id,
                "anime_url": f"{ANIMEHEAVEN_BASE}/anime.php?{anime_id}",
                "title": title,
                "jp_title": jp_title if jp_title and jp_title != "-" else None,
                "episode": int(ep_num)
            })
            if len(items) >= limit:
                break
    return items


def find_episode_key_on_anime_page(anime_url: str, target_ep: int) -> Optional[str]:
    html_content = http_get(anime_url)
    pattern = r"""gate[ah]\(\s*['"]([a-f0-9]{32})['"]\s*\)[^>]*>(.*?)</a>"""
    matches = list(re.finditer(pattern, html_content, re.DOTALL | re.IGNORECASE))
    for m in matches:
        k = m.group(1)
        inner = m.group(2)
        w2 = re.search(r'class=\s*[\'"][^\'"]*watch2[^\'"]*[\'"][^>]*>(\d+)<', inner)
        if w2 and int(w2.group(1)) == target_ep:
            return k
        clean = re.sub(r'<[^>]+>', ' ', inner)
        clean = re.sub(r'\s+', ' ', clean).strip()
        ep_m = re.search(r'Episode\s+(\d+)', clean, re.IGNORECASE)
        if ep_m and int(ep_m.group(1)) == target_ep:
            return k

    raw_keys = re.findall(r'gate[ah]\(\s*[\'"]([a-f0-9]{32})[\'"]', html_content)
    if raw_keys and target_ep == 1 and len(raw_keys) == 1:
        return raw_keys[0]

    return None


def get_all_episodes_from_anime(anime_url: str, allowed_eps: Optional[Set[int]] = None) -> List[dict]:
    if not anime_url.startswith("http"):
        anime_url = f"{ANIMEHEAVEN_BASE}/{anime_url}"
    anime_id = anime_url.split("?")[-1]
    html_content = http_get(anime_url)

    # 1. Look for official AnimeHeaven main title divs (.infotitle)
    title_match = re.search(r'<div class=[\'"][^\'"]*infotitle\s+c[^\'"]*[\'"][^>]*>([^<]+)</div>', html_content)
    if not title_match:
        # 2. Look for <title> tag (e.g. "Kaiju No. 8 Anime | AnimeHeaven.Me")
        title_match = re.search(r'<title>([^<]+)</title>', html_content)
    
    raw_title = title_match.group(1) if title_match else anime_id
    # Clean out "Anime", "AnimeHeaven", "Watch" suffixes from title
    anime_title = re.sub(r'(?i)\s+(?:anime|animeheaven).*$', '', html.unescape(raw_title)).strip()
    # Strip any pipes or dashes
    anime_title = re.sub(r'\s*[|\-].*$', '', anime_title).strip()

    # Japanese Romaji Title (.infotitlejp)
    jp_match = re.search(r'<div class=[\'"][^\'"]*infotitlejp\s+c[^\'"]*[\'"][^>]*>([^<]+)</div>', html_content)
    if not jp_match:
        jp_match = re.search(r'<div class=[\'"]altname[^\'"]*[\'"][^>]*>([^<]+)</div>', html_content)
    jp_title = jp_match.group(1).strip() if jp_match else None

    pattern = r"""gate[ah]\(\s*['"]([a-f0-9]{32})['"]\s*\)[^>]*>(.*?)</a>"""
    matches = list(re.finditer(pattern, html_content, re.DOTALL | re.IGNORECASE))

    items = []
    seen_eps = set()
    for m in matches:
        k = m.group(1)
        inner = m.group(2)

        w2 = re.search(r'class=\s*[\'"][^\'"]*watch2[^\'"]*[\'"][^>]*>(\d+)<', inner)
        ep_num = int(w2.group(1)) if w2 else None

        if ep_num is None:
            clean = re.sub(r'<[^>]+>', ' ', inner)
            clean = re.sub(r'\s+', ' ', clean).strip()
            ep_match = re.search(r'Episode\s+(\d+)', clean, re.IGNORECASE)
            ep_num = int(ep_match.group(1)) if ep_match else None

        if ep_num is None or ep_num in seen_eps:
            continue
        if allowed_eps is not None and ep_num not in allowed_eps:
            continue

        seen_eps.add(ep_num)
        key = f"{anime_id}_ep{ep_num}"
        items.append({
            "key": key,
            "anime_id": anime_id,
            "anime_url": anime_url,
            "title": anime_title,
            "jp_title": jp_title,
            "episode": ep_num,
            "session_key": k
        })

    items.sort(key=lambda x: x["episode"])
    return items


def resolve_direct_download_url(anime_url: str, session_key: str) -> str:
    try:
        gate_html = http_get(
            f"{ANIMEHEAVEN_BASE}/gate.php",
            referer=f"{ANIMEHEAVEN_BASE}/",
            cookie=f"key={session_key}"
        )
        dl_match = re.search(r'href=[\'"](https?://[a-z0-9]+\.animeheaven\.me/video\.mp4\?[^\'"]+&d)[\'"]', gate_html)
        if dl_match:
            return dl_match.group(1)
    except Exception:
        pass

    return f"https://cz.animeheaven.me/video.mp4?{session_key}&d"


def main():
    target_anime_url = os.getenv("TARGET_ANIME_URL", "").strip()
    target_mal_id = os.getenv("TARGET_MAL_ID", "").strip()
    episode_range_str = os.getenv("EPISODE_RANGE", "").strip()
    mal_client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()
    allowed_eps = parse_episode_range(episode_range_str)

    history = load_history(mapping_password)
    pending = []
    mal_cache = {}

    def get_mal(title: str, jp_title: Optional[str]):
        cache_key = f"{title}_{jp_title}"
        if cache_key not in mal_cache:
            # If target_mal_id is provided explicitly by user, fetch official English Title directly from MAL API
            if target_mal_id and target_mal_id.isdigit():
                numeric_mal = int(target_mal_id)
                official_meta = fetch_official_mal_info(numeric_mal, mal_client_id) if mal_client_id else None
                if official_meta:
                    print(f"[*] Fetched Official MAL Title for Explicit ID {numeric_mal}: '{official_meta['title']}'", file=sys.stderr)
                    mal_cache[cache_key] = official_meta
                else:
                    mal_cache[cache_key] = {
                        "mal_id": numeric_mal,
                        "title": title,
                        "title_english": title,
                        "title_japanese": jp_title,
                        "media_type": "TV",
                        "total_episodes": None
                    }
            else:
                # Automatic match engine (AniVault port) for fresh releases
                mal_cache[cache_key] = resolve_mal_metadata(title, jp_title=jp_title, client_id=mal_client_id)

            mal_id = mal_cache[cache_key].get("mal_id")
            resolved_title = mal_cache[cache_key].get("title", title)
            print(f"[*] MAL Match for '{title}': MAL ID -> {mal_id or 'N/A'} | Title -> '{resolved_title}'", file=sys.stderr)
        return mal_cache[cache_key]

    if target_anime_url:
        print(f"[*] BACKLOG MODE: Target {target_anime_url} (Explicit MAL ID: {target_mal_id or 'None'})", file=sys.stderr)
        all_episodes = get_all_episodes_from_anime(target_anime_url, allowed_eps=allowed_eps)
        print(f"[*] Found {len(all_episodes)} total episodes matching filter.", file=sys.stderr)

        for item in all_episodes:
            mal_info = get_mal(item["title"], item.get("jp_title"))
            
            # 3-Way Deduplication check: AnimeID + MAL ID + Title
            if is_already_uploaded(history, item["anime_id"], mal_info.get("mal_id"), item["title"], item["episode"]):
                print(f"[-] Already uploaded in history: {item['title']} Ep {item['episode']}. Skipping duplicate.", file=sys.stderr)
                continue

            direct_url = resolve_direct_download_url(item["anime_url"], item["session_key"])
            safe_title = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', item["title"])
            filename = f"{safe_title}_EP{item['episode']:02d}.mp4"
            item_payload = {
                "key": item["key"],
                "title": item["title"],
                "episode": item["episode"],
                "stream_url": direct_url,
                "filename": filename,
                "anime_id": item["anime_id"],
                "anime_url": item["anime_url"],
                "mal_info": mal_info
            }
            item_payload["meta_b64"] = base64.b64encode(json.dumps(item_payload).encode("utf-8")).decode("utf-8")
            pending.append(item_payload)

    else:
        releases = get_recent_releases(limit=40)
        print(f"[*] FRESH MODE: Found {len(releases)} recent items.", file=sys.stderr)

        for item in releases:
            if allowed_eps is not None and item["episode"] not in allowed_eps:
                continue

            mal_info = get_mal(item["title"], item.get("jp_title"))
            if is_already_uploaded(history, item["anime_id"], mal_info.get("mal_id"), item["title"], item["episode"]):
                continue

            session_key = find_episode_key_on_anime_page(item["anime_url"], item["episode"])
            if not session_key:
                print(f"[!] Could not find session key for exact Episode {item['episode']} in {item['title']}. Skipping.", file=sys.stderr)
                continue

            direct_url = resolve_direct_download_url(item["anime_url"], session_key)
            safe_title = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', item["title"])
            filename = f"{safe_title}_EP{item['episode']:02d}.mp4"
            item_payload = {
                "key": item["key"],
                "title": item["title"],
                "episode": item["episode"],
                "stream_url": direct_url,
                "filename": filename,
                "anime_id": item["anime_id"],
                "anime_url": item["anime_url"],
                "mal_info": mal_info
            }
            item_payload["meta_b64"] = base64.b64encode(json.dumps(item_payload).encode("utf-8")).decode("utf-8")
            pending.append(item_payload)

    print(f"[+] Total new episodes queued for parallel upload: {len(pending)}", file=sys.stderr)

    github_output = os.getenv("GITHUB_OUTPUT")
    has_items = "true" if pending else "false"
    matrix_json = json.dumps(pending)

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"has_items={has_items}\n")
            f.write(f"matrix={matrix_json}\n")
    else:
        print(f"::set-output name=has_items::{has_items}")
        print(f"::set-output name=matrix::{matrix_json}")


if __name__ == "__main__":
    main()
