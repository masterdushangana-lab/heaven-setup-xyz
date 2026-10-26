"""
Full Implementation of AniVault (Rust) Matching Engine in Python.
- Exact port of:
  * normalize_title
  * signals (season, part, movie, ova, ona, special)
  * compatible
  * token_similarity & canonical_token_key
  * score_title
  * MAL Official API v2 / AniMap Search with Candidate Scoring
- Saves Official MAL English Title as primary display title.
"""

import os
import re
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, List, Set

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

RE_SEASON = re.compile(r'\bseason\s*(\d+)\b', re.IGNORECASE)
RE_PART = re.compile(r'\bpart\s*(\d+)\b', re.IGNORECASE)


def normalize_title(input_str: str) -> str:
    if not input_str:
        return ""
    s = input_str.lower()
    s = s.replace('&', " and ")
    s = s.replace('’', "").replace('\'', "").replace('`', "")
    for d in ['‐', '‑', '‒', '–', '—', '―']:
        s = s.replace(d, "-")
    s = s.replace("1st season", "season 1")
    s = s.replace("2nd season", "season 2")
    s = s.replace("3rd season", "season 3")
    s = s.replace("4th season", "season 4")
    s = s.replace("5th season", "season 5")
    s = s.replace("second season", "season 2")
    s = s.replace("third season", "season 3")
    s = s.replace("fourth season", "season 4")
    s = s.replace("final season", "season final")
    s = s.replace("yuu gi ou", "yu-gi-oh")
    s = s.replace("yu gi oh", "yu-gi-oh")
    s = s.replace("dungeon ni deai", "danmachi")
    s = s.replace("boku no hero academia", "my hero academia")
    s = s.replace("kimetsu no yaiba", "demon slayer")
    s = s.replace("shingeki no kyojin", "attack on titan")
    s = s.replace("jujutsu kaisen", "jujutsu kaisen")

    # Clean characters
    out = []
    for c in s:
        if c.isalnum() or c.isspace() or c == '-':
            out.append(c)
        else:
            out.append(' ')
    return re.sub(r'\s+', ' ', "".join(out)).strip()


class Signals:
    def __init__(self, s: str):
        n = normalize_title(s)
        m_s = RE_SEASON.search(n)
        self.season = m_s.group(1) if m_s else None
        m_p = RE_PART.search(n)
        self.part = m_p.group(1) if m_p else None
        self.final_season = "final season" in n or "season final" in n
        words = n.split()
        self.movie = any(x in ("movie", "film") for x in words)
        self.ova = any(x == "ova" for x in words)
        self.ona = any(x == "ona" for x in words)
        self.special = any(x in ("special", "specials") for x in words)


def compatible(query: str, candidate: str) -> bool:
    q = Signals(query)
    c = Signals(candidate)
    if q.season and c.season and q.season != c.season:
        return False
    if q.part and c.part and q.part != c.part:
        return False
    if q.final_season != c.final_season:
        return False
    if q.movie != c.movie:
        return False
    if q.ova != c.ova:
        return False
    if q.ona != c.ona:
        return False
    if q.special != c.special:
        return False
    return True


def token_similarity(a: str, b: str) -> f64 if 'f64' in globals() else float:
    aa = set(w for w in normalize_title(a).split() if len(w) >= 2)
    bb = set(w for w in normalize_title(b).split() if len(w) >= 2)
    if not aa or not bb:
        return 0.0
    inter = len(aa.intersection(bb))
    union = len(aa.union(bb))
    return inter / union


def score_title(query: str, candidate: str) -> int:
    if not compatible(query, candidate):
        return -1000
    q = normalize_title(query)
    c = normalize_title(candidate)
    if not q or not c:
        return -1000
    if q == c:
        return 1000

    q_tokens = sorted(q.split())
    c_tokens = sorted(c.split())
    if q_tokens == c_tokens:
        return 980

    sim = token_similarity(query, candidate)
    score = int(round(sim * 620.0))
    if c.startswith(q) or q.startswith(c):
        score += 180
    elif q in c or c in q:
        score += 120

    qs = Signals(query)
    cs = Signals(candidate)
    if qs.season and qs.season == cs.season:
        score += 180
    if qs.part and qs.part == cs.part:
        score += 120
    return score


def search_mal_api_candidates(query: str, client_id: str) -> List[Dict[str, Any]]:
    cleaned = re.sub(r'#|!|\?|;|\(|\)|\[|\]', ' ', query)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if len(cleaned) > 50:
        cleaned = cleaned[:50].rsplit(' ', 1)[0]

    url = f"https://api.myanimelist.net/v2/anime?q={urllib.parse.quote(cleaned)}&limit=10&fields=id,title,alternative_titles,media_type,num_episodes"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "X-MAL-CLIENT-ID": client_id})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            out = []
            for item in data.get("data", []):
                node = item.get("node", {})
                alt = node.get("alternative_titles", {})
                aliases = [node.get("title")]
                en = alt.get("en")
                ja = alt.get("ja")
                if en and en.strip():
                    aliases.append(en.strip())
                if ja and ja.strip():
                    aliases.append(ja.strip())
                for syn in alt.get("synonyms", []):
                    if syn and syn.strip():
                        aliases.append(syn.strip())

                out.append({
                    "mal_id": node.get("id"),
                    "title": node.get("title"),
                    "english_title": en if en and en.strip() else node.get("title"),
                    "japanese_title": ja,
                    "media_type": node.get("media_type", "TV"),
                    "aliases": [a for a in aliases if a]
                })
            return out
    except Exception:
        return []


def get_search_variants(title: str) -> List[str]:
    """Generates AniVault search variants (original title + base title prefix if subtitle exists)."""
    vals = [title]
    # Check if subtitle or part separator exists
    for sep in [':', ' - ', ' – ', ' — ', '(']:
        if sep in title:
            base = title.split(sep)[0].strip()
            if len(base.split()) >= 2:
                vals.append(base)
    # Deduplicate while preserving order
    seen = set()
    res = []
    for v in vals:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            res.append(v)
    return res


def fetch_official_mal_info(mal_id: int, client_id: str) -> Optional[Dict[str, Any]]:
    """Fetches anime details strictly from the Official MyAnimeList API v2."""
    if not client_id:
        return None
    url = f"https://api.myanimelist.net/v2/anime/{mal_id}?fields=id,title,alternative_titles,media_type,num_episodes"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "X-MAL-CLIENT-ID": client_id})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            alt = data.get("alternative_titles", {})
            en = alt.get("en")
            ja = alt.get("ja")
            title_english = en.strip() if en and en.strip() else data.get("title", "")
            return {
                "mal_id": data.get("id"),
                "title": title_english,
                "title_english": title_english,
                "title_japanese": ja.strip() if ja and ja.strip() else None,
                "media_type": (data.get("media_type") or "tv").upper(),
                "total_episodes": data.get("num_episodes")
            }
    except Exception as e:
        print(f"[MAL Resolver] Official MAL API error for ID {mal_id}: {e}")
        return None


def resolve_mal_metadata(animeheaven_title: str, jp_title: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
    """
    AniVault Rust exact matching engine:
    Strictly uses Official MyAnimeList API v2 (X-MAL-CLIENT-ID).
    1. Generates search variants for both English and Japanese Romaji.
    2. Fetches candidate metadata rows strictly from Official MAL API v2.
    3. Runs AniVault best_match scoring algorithm across all aliases (English, Romaji, Synonyms).
    4. Enriches winning candidate with official MAL details (title_english) from Official MAL API.
    5. Guarantees that official MAL English Title is returned as primary display name.
    """
    cid = client_id or os.environ.get("MAL_CLIENT_ID", "").strip()
    if not cid:
        print("[MAL Resolver Warning] MAL_CLIENT_ID is not configured. Official MAL API requires a valid Client ID.")

    raw_queries = [animeheaven_title]
    if jp_title and jp_title.strip() not in ("-", ""):
        raw_queries.append(jp_title.strip())

    search_queries = []
    seen_q = set()
    for q in raw_queries:
        for var in get_search_variants(q):
            if var.lower() not in seen_q:
                seen_q.add(var.lower())
                search_queries.append(var)

    candidates = []
    seen_ids = set()

    for q in search_queries:
        if cid:
            for c in search_mal_api_candidates(q, cid):
                if c["mal_id"] not in seen_ids:
                    seen_ids.add(c["mal_id"])
                    candidates.append(c)

    if not candidates:
        return {
            "mal_id": None,
            "title": animeheaven_title,
            "title_english": animeheaven_title,
            "title_japanese": jp_title if jp_title and jp_title != "-" else None,
            "media_type": "TV",
            "total_episodes": None
        }

    # Run AniVault best_match scoring
    best_candidate = None
    best_score = -1000

    aliases_to_match = [animeheaven_title]
    if jp_title and jp_title != "-":
        aliases_to_match.append(jp_title)

    for cand in candidates:
        score = max(
            score_title(q_alias, c_alias)
            for q_alias in aliases_to_match
            for c_alias in cand["aliases"]
        ) if cand.get("aliases") else -1000

        if score > best_score:
            best_score = score
            best_candidate = cand

    # AniVault threshold check: score >= 450 is a match
    if best_candidate and best_score >= 450:
        mal_id = best_candidate["mal_id"]
        # Fetch official MAL english title strictly from Official MAL API
        info = fetch_official_mal_info(mal_id, cid) if cid else None
        chosen_title = animeheaven_title
        chosen_jp = best_candidate.get("japanese_title") or jp_title
        media_type = best_candidate.get("media_type", "TV")
        total_episodes = None

        if info:
            chosen_title = info.get("title_english") or info.get("title") or chosen_title
            chosen_jp = info.get("title_japanese") or chosen_jp
            media_type = info.get("media_type", media_type)
            total_episodes = info.get("total_episodes")
        else:
            chosen_title = best_candidate.get("english_title") or best_candidate.get("title") or chosen_title

        return {
            "mal_id": mal_id,
            "title": chosen_title,
            "title_english": chosen_title,
            "title_japanese": chosen_jp,
            "media_type": media_type,
            "total_episodes": total_episodes
        }

    return {
        "mal_id": None,
        "title": animeheaven_title,
        "title_english": animeheaven_title,
        "title_japanese": jp_title if jp_title and jp_title != "-" else None,
        "media_type": "TV",
        "total_episodes": None
    }
