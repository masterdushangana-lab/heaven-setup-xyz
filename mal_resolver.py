"""
MAL (MyAnimeList) API Integration and Title Resolver
Resolves MAL IDs and Official Metadata from AnimeHeaven Titles.
Uses Official MAL API v2 (if MAL_CLIENT_ID provided) with automated fallback to Jikan API / AniMap.
"""

import os
import re
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, List

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def clean_query_title(title: str) -> str:
    """Removes Season / Part / Episode suffixes for cleaner MAL search queries."""
    t = re.sub(r'\(.*?\)|\[.*?\]', '', title)
    t = re.sub(r'(?i)\b(\d+(?:st|nd|rd|th)?\s+season|season\s+\d+|part\s+\d+|cour\s+\d+|episode\s+\d+)\b', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t if len(t) > 2 else title


def mal_official_search(query: str, client_id: str) -> Optional[Dict[str, Any]]:
    """Official MyAnimeList API v2 search endpoint."""
    cleaned = clean_query_title(query)
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
                    "media_type": node.get("media_type"),
                    "total_episodes": node.get("num_episodes")
                }
    except Exception as e:
        print(f"[!] MAL Official API search failed for '{query}': {e}")
    return None


def jikan_fallback_search(query: str) -> Optional[Dict[str, Any]]:
    """Unofficial Jikan API v4 fallback (no API key required)."""
    cleaned = clean_query_title(query)
    url = f"https://api.jikan.moe/v4/anime?q={urllib.parse.quote(cleaned)}&limit=3"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", [])
            if items:
                top = items[0]
                return {
                    "mal_id": top.get("mal_id"),
                    "title": top.get("title"),
                    "title_english": top.get("title_english") or top.get("title"),
                    "title_japanese": top.get("title_japanese"),
                    "media_type": top.get("type"),
                    "total_episodes": top.get("episodes")
                }
    except Exception as e:
        print(f"[!] Jikan fallback search failed for '{query}': {e}")
    return None


def resolve_mal_metadata(animeheaven_title: str, client_id: Optional[str] = None) -> Dict[str, Any]:
    """Resolves MAL ID and metadata with fallback."""
    if client_id:
        res = mal_official_search(animeheaven_title, client_id)
        if res:
            return res

    fallback_res = jikan_fallback_search(animeheaven_title)
    if fallback_res:
        return fallback_res

    # Default fallback when unindexed
    return {
        "mal_id": None,
        "title": animeheaven_title,
        "title_english": animeheaven_title,
        "title_japanese": None,
        "media_type": "TV",
        "total_episodes": None
    }
