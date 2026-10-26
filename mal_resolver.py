"""
MAL (MyAnimeList) API Integration and Title Resolver
Resolves MAL IDs and Official Metadata with high accuracy.
Order of resolution:
1. Official MAL API v2 (with sanitized queries avoiding 400 Bad Request)
2. AniMap.id Search API (high-speed, no rate limits, exact MAL ID mappings)
3. Safe fallback when unindexed
"""

import os
import re
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def sanitize_for_mal(title: str) -> str:
    """
    Cleans title for MyAnimeList API v2:
    - Normalizes unicode curly quotes and special punctuation (which cause MAL 400 Bad Request).
    - Strips hashtags, exclamation marks, and Season/Part suffixes.
    - Limits length to 60 characters for clean keyword matching.
    """
    t = title.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    t = t.replace('–', '-').replace('—', '-').replace('―', '-')
    t = t.replace('é', 'e').replace('è', 'e').replace('á', 'a')
    t = re.sub(r'#|!|\?|;|\(|\)|\[|\]', ' ', t)
    t = re.sub(r'(?i)\b(\d+(?:st|nd|rd|th)?\s+season|season\s+\d+|part\s+\d+|cour\s+\d+|episode\s+\d+)\b', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) > 60:
        t = t[:60].rsplit(' ', 1)[0]
    return t if len(t) > 2 else title


def mal_official_search(query: str, client_id: str) -> Optional[Dict[str, Any]]:
    """Official MyAnimeList API v2 search endpoint."""
    cleaned = sanitize_for_mal(query)
    url = f"https://api.myanimelist.net/v2/anime?q={urllib.parse.quote(cleaned)}&limit=5&fields=id,title,alternative_titles,media_type,num_episodes,main_picture"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "X-MAL-CLIENT-ID": client_id
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", [])
            if items:
                node = items[0].get("node", {})
                return {
                    "mal_id": node.get("id"),
                    "title": node.get("title"),
                    "title_english": node.get("alternative_titles", {}).get("en") or node.get("title"),
                    "title_japanese": node.get("alternative_titles", {}).get("ja"),
                    "media_type": node.get("media_type", "TV"),
                    "total_episodes": node.get("num_episodes")
                }
    except Exception as e:
        # Silent fallback without noisy logs
        pass
    return None


def animap_search(query: str) -> Optional[Dict[str, Any]]:
    """AniMap MAL mapping search endpoint (from AniVault). Highly reliable, zero rate limits."""
    cleaned = sanitize_for_mal(query)
    url = f"https://animap.id/api/search?q={urllib.parse.quote(cleaned)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            items = json.loads(resp.read().decode("utf-8"))
            if isinstance(items, list) and items:
                top = items[0]
                titles = top.get("titles", [])
                primary = titles[0] if titles else query
                return {
                    "mal_id": top.get("mal_id"),
                    "title": primary,
                    "title_english": primary,
                    "title_japanese": None,
                    "media_type": "TV",
                    "total_episodes": None
                }
    except Exception:
        pass
    return None


def resolve_mal_metadata(animeheaven_title: str, client_id: Optional[str] = None) -> Dict[str, Any]:
    """Resolves MAL ID with multi-tier fallback."""
    # Step 1: Official MAL API v2
    if client_id:
        res = mal_official_search(animeheaven_title, client_id)
        if res:
            return res

    # Step 2: AniMap Search (AniVault Rust mapping engine)
    res_animap = animap_search(animeheaven_title)
    if res_animap:
        return res_animap

    # Step 3: Default safe fallback
    return {
        "mal_id": None,
        "title": animeheaven_title,
        "title_english": animeheaven_title,
        "title_japanese": None,
        "media_type": "TV",
        "total_episodes": None
    }
