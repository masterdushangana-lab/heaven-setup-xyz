"""
Direct Download URL Episode Resolver
- Resolves MAL metadata for specified MAL ID (via Official MAL API and AniList GraphQL)
- Packages direct HTTP/HTTPS video URL into GitHub Actions matrix task
- Supports automatic movie detection, country of origin resolution, and deduplication
"""

import os
import re
import sys
import json
import base64
import urllib.parse
import urllib.request
from typing import Dict, Any, List

from mapping_crypto import decrypt_mapping
from detect_episodes import is_already_uploaded, load_history


def fetch_metadata_by_mal_id(mal_id: int, client_id: str = "") -> Dict[str, Any]:
    """Queries Official MAL API and AniList GraphQL directly by exact MAL ID."""
    out = {
        "title": f"Anime_{mal_id}",
        "title_english": f"Anime_{mal_id}",
        "media_type": "tv",
        "country_of_origin": "JP"
    }

    # 1. Try Official MAL API
    if client_id:
        try:
            url = f"https://api.myanimelist.net/v2/anime/{mal_id}?fields=id,title,alternative_titles,media_type,num_episodes"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "X-MAL-CLIENT-ID": client_id})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                alt = data.get("alternative_titles", {})
                en = alt.get("en")
                official = en if (en and en.strip()) else data.get("title")
                if official:
                    out["title"] = official
                    out["title_english"] = official
                if data.get("media_type"):
                    out["media_type"] = str(data.get("media_type")).lower()
        except Exception as e:
            print(f"[!] MAL API lookup warning: {e}", file=sys.stderr)

    # 2. AniList GraphQL (resolves exact english/romaji title and countryOfOrigin)
    query = """
    query ($idMal: Int) {
      Media (idMal: $idMal, type: ANIME) {
        countryOfOrigin
        format
        title {
          english
          romaji
          native
        }
      }
    }
    """
    try:
        url = "https://graphql.anilist.co"
        req_data = json.dumps({"query": query, "variables": {"idMal": mal_id}}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            media = data.get("data", {}).get("Media")
            if media:
                titles = media.get("title", {})
                en = titles.get("english")
                rom = titles.get("romaji")
                nat = titles.get("native")
                official = en or rom or nat
                if official and (not out["title"] or out["title"].startswith("Anime_")):
                    out["title"] = official
                    out["title_english"] = official
                if media.get("format"):
                    out["media_type"] = str(media.get("format")).lower()
                if media.get("countryOfOrigin"):
                    out["country_of_origin"] = str(media.get("countryOfOrigin")).upper()
    except Exception as e:
        print(f"[!] AniList GraphQL lookup warning: {e}", file=sys.stderr)

    return out


def main():
    target_mal_id = os.getenv("TARGET_MAL_ID", "").strip()
    direct_url = os.getenv("DIRECT_URL", "").strip()
    episode_num = os.getenv("EPISODE_NUMBER", "1").strip()
    is_movie_input = os.getenv("IS_MOVIE", "auto").strip().lower()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()

    print("==================================================================")
    print("      DIRECT DOWNLOAD URL MANUAL INGESTION RESOLVER               ")
    print("==================================================================")
    print(f"[*] Target MAL ID: {target_mal_id}")
    print(f"[*] Direct URL: {direct_url[:80]}...")
    print(f"[*] Episode Number: {episode_num}")
    print(f"[*] Is Movie Setting: {is_movie_input}")
    print(f"[*] Upload Target: {upload_target}")
    print("==================================================================")

    if not target_mal_id:
        print("[ERROR] TARGET_MAL_ID is mandatory for Direct URL ingestion.", file=sys.stderr)
        sys.exit(1)

    if not direct_url or not direct_url.startswith("http"):
        print("[ERROR] A valid direct HTTP/HTTPS DIRECT_URL is required.", file=sys.stderr)
        sys.exit(1)

    history = load_history(password) if password else {}

    # 1. Resolve Anime Metadata
    meta = fetch_metadata_by_mal_id(int(target_mal_id), client_id=client_id)
    official_title = meta["title_english"] or meta["title"]
    media_type = meta["media_type"]
    country_code = meta["country_of_origin"]

    print(f"[+] Resolved Anime: {official_title} (MAL ID: {target_mal_id})")
    print(f"    Media Type: {media_type.upper()} | Country of Origin: {country_code}")

    # Determine Movie status
    if is_movie_input in ("yes", "true", "1"):
        is_movie = True
    elif is_movie_input in ("no", "false", "0"):
        is_movie = False
    else:
        # Auto-detect: media_type is movie or episode_num is "movie" or title has movie
        is_movie = (media_type in ("movie", "film")) or (episode_num.lower() in ("movie", "film")) or ("movie" in official_title.lower())

    print(f"[*] Movie Classification: {'YES (<1.5GB cap)' if is_movie else 'NO (<500MB episode cap)'}")

    # Deduplication Check
    if is_already_uploaded(history, "", int(target_mal_id), official_title, episode_num, upload_target=upload_target):
        print(f"[-] Episode {episode_num} of {official_title} is already uploaded. Skipping.")
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
                f_out.write("has_items=false\nmatrix=[]\n")
        return

    safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
    clean_filename = f"{safe_title}_Ep_{episode_num}.mp4"
    task_key = re.sub(r"_+", "_", f"{target_mal_id}_{safe_title}_ep{episode_num}")

    item_meta = {
        "key": task_key,
        "title": official_title,
        "episode": episode_num,
        "mal_id": int(target_mal_id),
        "direct_url": direct_url,
        "filename": clean_filename,
        "source": "direct_url",
        "upload_target": upload_target,
        "country_of_origin": country_code,
        "is_movie": is_movie
    }

    meta_b64 = base64.b64encode(json.dumps(item_meta).encode("utf-8")).decode("utf-8")
    selected_tasks = [{
        "key": task_key,
        "meta_b64": meta_b64,
        "display": f"{official_title} - {'Movie' if is_movie else 'Ep ' + str(episode_num)}"
    }]

    print(f"\n[+] Queued Task: {selected_tasks[0]['display']}")

    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
            f_out.write("has_items=true\n")
            f_out.write(f"matrix={json.dumps(selected_tasks)}\n")


if __name__ == "__main__":
    main()
