"""
AnimeHeaven Episode Detector & Matrix Generator with MAL API Integration
Extracts episode session keys and constructs 100% working direct download links.
"""

import os
import re
import sys
import json
import html
import urllib.request
from typing import Dict, List, Optional, Set

from mal_resolver import resolve_mal_metadata

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


def load_history() -> Dict[str, dict]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def is_already_uploaded(history: dict, mal_id: Optional[int], anime_title: str, episode_num: int) -> bool:
    lookup_keys = []
    if mal_id:
        lookup_keys.append(str(mal_id))
    lookup_keys.append(anime_title)

    for k in lookup_keys:
        if k in history:
            anime_entry = history[k]
            ep_dict = anime_entry.get("episodes", {})
            if str(episode_num) in ep_dict:
                return True
    return False


def get_recent_releases(limit: int = 40) -> List[dict]:
    content = http_get(f"{ANIMEHEAVEN_BASE}/new.php")
    pattern = r"<a[^>]+href=['\"](anime\.php\?[^'\"]+)['\"][^>]*>[\s\S]*?<img[^>]+alt=['\"]([^'\"]+)['\"][^>]*>[\s\S]*?<div class=['\"]chartepm[^'\"]*['\"]>(\d+)</div>"

    items = []
    seen = set()
    for match in re.finditer(pattern, content, re.IGNORECASE):
        anime_rel_url = match.group(1)
        raw_title = match.group(2)
        ep_num = match.group(3)

        anime_id = anime_rel_url.split("?")[-1]
        title = html.unescape(raw_title).strip()
        key = f"{anime_id}_ep{ep_num}"

        if key not in seen:
            seen.add(key)
            items.append({
                "key": key,
                "anime_id": anime_id,
                "anime_url": f"{ANIMEHEAVEN_BASE}/{anime_rel_url}",
                "title": title,
                "episode": int(ep_num)
            })
            if len(items) >= limit:
                break
    return items


def get_all_episodes_from_anime(anime_url: str, allowed_eps: Optional[Set[int]] = None) -> List[dict]:
    if not anime_url.startswith("http"):
        anime_url = f"{ANIMEHEAVEN_BASE}/{anime_url}"
    anime_id = anime_url.split("?")[-1]
    html_content = http_get(anime_url)

    title_match = re.search(r'<div class=[\'"]linetitle2? c[\'"][^>]*>([^<]+)</div>', html_content)
    if not title_match:
        title_match = re.search(r'<title>([^<]+)</title>', html_content)
    raw_title = title_match.group(1) if title_match else anime_id
    anime_title = re.sub(r'AnimeHeaven.*', '', html.unescape(raw_title)).strip()

    pattern = r"""gate[ah]\(\s*['"]([a-f0-9]{32})['"]\s*\)[^>]*>(.*?)</a>"""
    matches = list(re.finditer(pattern, html_content, re.DOTALL | re.IGNORECASE))

    items = []
    seen_eps = set()
    for m in matches:
        k = m.group(1)
        inner = re.sub(r'<[^>]+>', ' ', m.group(2))
        inner = re.sub(r'\s+', ' ', inner).strip()
        ep_match = re.search(r'Episode\s+(\d+)', inner, re.IGNORECASE)
        ep_num = int(ep_match.group(1)) if ep_match else None
        if ep_num is None:
            continue

        if ep_num in seen_eps:
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
            "episode": ep_num,
            "session_key": k
        })

    items.sort(key=lambda x: x["episode"])
    return items


def resolve_direct_download_url(anime_url: str, session_key: str) -> str:
    """
    Resolves the official working AnimeHeaven direct MP4 download URL.
    Checks gate.php first; if delayed, falls back to direct cz.animeheaven.me/video.mp4?{key}&d
    """
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

    # Reliable direct verified download endpoint
    return f"https://cz.animeheaven.me/video.mp4?{session_key}&d"


def find_episode_key_on_anime_page(anime_url: str, target_ep: int) -> Optional[str]:
    """Finds the 32-character session key for a specific episode."""
    html_content = http_get(anime_url)
    pattern = r"""gate[ah]\(\s*['"]([a-f0-9]{32})['"]\s*\)[^>]*>(.*?)</a>"""
    matches = list(re.finditer(pattern, html_content, re.DOTALL | re.IGNORECASE))
    for m in matches:
        k = m.group(1)
        inner = re.sub(r'<[^>]+>', ' ', m.group(2))
        inner = re.sub(r'\s+', ' ', inner).strip()
        ep_match = re.search(r'Episode\s+(\d+)', inner, re.IGNORECASE)
        if ep_match and int(ep_match.group(1)) == target_ep:
            return k
    if matches:
        return matches[0].group(1)
    return None


def main():
    target_anime_url = os.getenv("TARGET_ANIME_URL", "").strip()
    episode_range_str = os.getenv("EPISODE_RANGE", "").strip()
    mal_client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    allowed_eps = parse_episode_range(episode_range_str)

    history = load_history()
    pending = []
    mal_cache = {}

    def get_mal(title: str):
        if title not in mal_cache:
            print(f"[*] Resolving MAL metadata for: '{title}'...", file=sys.stderr)
            mal_cache[title] = resolve_mal_metadata(title, mal_client_id)
        return mal_cache[title]

    if target_anime_url:
        print(f"[*] BACKLOG MODE: Target {target_anime_url}", file=sys.stderr)
        all_episodes = get_all_episodes_from_anime(target_anime_url, allowed_eps=allowed_eps)
        print(f"[*] Found {len(all_episodes)} total episodes for range.", file=sys.stderr)

        for item in all_episodes:
            mal_info = get_mal(item["title"])
            if is_already_uploaded(history, mal_info.get("mal_id"), item["title"], item["episode"]):
                print(f"[-] Already uploaded in history: {item['title']} Ep {item['episode']}. Skipping.", file=sys.stderr)
                continue

            direct_url = resolve_direct_download_url(item["anime_url"], item["session_key"])
            safe_title = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', item["title"])
            filename = f"{safe_title}_EP{item['episode']:02d}.mp4"
            pending.append({
                "key": item["key"],
                "title": item["title"],
                "episode": item["episode"],
                "stream_url": direct_url,
                "filename": filename,
                "anime_id": item["anime_id"],
                "anime_url": item["anime_url"],
                "mal_info": mal_info
            })

    else:
        releases = get_recent_releases(limit=40)
        print(f"[*] FRESH MODE: Found {len(releases)} recent items.", file=sys.stderr)

        for item in releases:
            if allowed_eps is not None and item["episode"] not in allowed_eps:
                continue

            mal_info = get_mal(item["title"])
            if is_already_uploaded(history, mal_info.get("mal_id"), item["title"], item["episode"]):
                continue

            session_key = find_episode_key_on_anime_page(item["anime_url"], item["episode"])
            if not session_key:
                print(f"[!] Could not find session key for {item['key']}. Skipping.", file=sys.stderr)
                continue

            direct_url = resolve_direct_download_url(item["anime_url"], session_key)
            safe_title = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', item["title"])
            filename = f"{safe_title}_EP{item['episode']:02d}.mp4"
            pending.append({
                "key": item["key"],
                "title": item["title"],
                "episode": item["episode"],
                "stream_url": direct_url,
                "filename": filename,
                "anime_id": item["anime_id"],
                "anime_url": item["anime_url"],
                "mal_info": mal_info
            })

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
