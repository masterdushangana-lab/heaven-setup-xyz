"""
Nyaa.si Manual Ingestion Resolver with Multi-Video Torrent Selection
- Resolves anime on Nyaa.si via search query, direct Nyaa URL, or Magnet link
- Inspects .torrent file structure to discover single vs multi-file batch torrents
- Maps each video file to its episode number and 1-based file index
- Allows selecting specific files via episode_range ('1-12', '5') or explicit selected_files ('1,2,5')
- Emits GitHub Actions matrix of selected episode tasks.
"""

import os
import re
import sys
import json
import base64
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional

from mapping_crypto import decrypt_mapping
from mal_resolver import resolve_mal_metadata
from detect_episodes import is_already_uploaded, is_episode_blacklisted, load_history
from torrent_processor import parse_torrent_bytes, extract_episode_number, parse_range_spec

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def fetch_nyaa_rss(query: str, category: str = "1_2") -> List[Dict[str, Any]]:
    """Fetches search results from Nyaa.si RSS."""
    enc_q = urllib.parse.quote(query)
    url = f"https://nyaa.si/?page=rss&q={enc_q}&c={category}&f=0"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    items = []
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read().decode("utf-8"))
        for item in root.findall(".//item"):
            title = item.find("title").text if item.find("title") is not None else ""
            torrent_url = item.find("link").text if item.find("link") is not None else ""
            info_hash = ""
            seeders = 0
            size = ""
            for child in item:
                tag = child.tag.split("}")[-1]
                if tag == "infoHash":
                    info_hash = child.text or ""
                elif tag == "seeders":
                    try:
                        seeders = int(child.text or 0)
                    except Exception:
                        seeders = 0
                elif tag == "size":
                    size = child.text or ""

            magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}" if info_hash else ""
            items.append({
                "title": title,
                "torrent_url": torrent_url,
                "magnet": magnet,
                "info_hash": info_hash,
                "seeders": seeders,
                "size": size
            })
    except Exception as e:
        print(f"[!] Error querying Nyaa RSS: {e}", file=sys.stderr)

    return items


def download_torrent_file(torrent_url: str) -> Optional[bytes]:
    """Downloads raw .torrent bytes from URL."""
    try:
        req = urllib.request.Request(torrent_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception as e:
        print(f"[!] Error downloading .torrent file from {torrent_url}: {e}", file=sys.stderr)
        return None


def main():
    target_mal_id = os.getenv("TARGET_MAL_ID", "").strip()
    query_or_url = os.getenv("QUERY_OR_URL", "").strip()
    episode_range = os.getenv("EPISODE_RANGE", "all").strip()
    selected_files_str = os.getenv("SELECTED_FILES", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()

    print("==================================================================")
    print("      NYAA.SI MANUAL INGESTION & MULTI-VIDEO SELECTOR             ")
    print("==================================================================")
    print(f"[*] Target MAL ID: {target_mal_id}")
    print(f"[*] Query / URL: {query_or_url}")
    print(f"[*] Episode Range: {episode_range}")
    print(f"[*] Selected Files: {selected_files_str or 'Auto-detect from Range'}")
    print("==================================================================")

    if not target_mal_id:
        print("[ERROR] TARGET_MAL_ID is mandatory for Nyaa ingestion.", file=sys.stderr)
        sys.exit(1)

    if not query_or_url:
        print("[ERROR] QUERY_OR_URL is required (search term, Nyaa URL, or Magnet).", file=sys.stderr)
        sys.exit(1)

    history = load_history(password) if password else {}
    deleted_records = history.get("__deleted__", {}) if history else {}

    # 1. Resolve MAL Metadata
    mal_info = resolve_mal_metadata(f"anime id {target_mal_id}", client_id=client_id)
    official_title = mal_info.get("title_english") or mal_info.get("title") or f"Anime_{target_mal_id}"
    print(f"[+] Resolved Anime: {official_title} (MAL ID: {target_mal_id})")

    # 2. Resolve Torrent Source
    torrent_bytes = None
    torrent_source = None
    torrent_title = official_title

    if query_or_url.lower().startswith("magnet:?"):
        torrent_source = query_or_url
        print("[+] Direct Magnet Link provided.")
        try:
            parsed_m = urllib.parse.urlsplit(query_or_url)
            params_m = urllib.parse.parse_qs(parsed_m.query)
            info_hash = ""
            for xt in params_m.get("xt", []):
                if "urn:btih:" in xt.lower():
                    info_hash = xt.split(":")[-1].strip()
            dn = params_m.get("dn", [""])[0]
            if dn:
                torrent_title = dn

            m_results = []
            if info_hash:
                print(f"[*] Querying Nyaa RSS for magnet info_hash: {info_hash}...")
                m_results = fetch_nyaa_rss(info_hash, category="0_0")
            if not m_results and dn:
                print(f"[*] Querying Nyaa RSS for magnet display name: {dn}...")
                m_results = fetch_nyaa_rss(dn, category="1_2")
            if not m_results and dn:
                dn_clean = re.sub(r'\[.*?\]|\(.*?\)', '', dn).strip()
                if dn_clean:
                    print(f"[*] Querying Nyaa RSS for cleaned title: {dn_clean}...")
                    m_results = fetch_nyaa_rss(dn_clean, category="1_2")

            if m_results:
                chosen = m_results[0]
                if chosen.get("torrent_url"):
                    print(f"[+] Discovered matching .torrent on Nyaa: {chosen['title']}")
                    print(f"    URL: {chosen['torrent_url']}")
                    torrent_bytes = download_torrent_file(chosen["torrent_url"])
                    if torrent_bytes:
                        torrent_source = chosen["torrent_url"]
                        torrent_title = chosen["title"]
        except Exception as e:
            print(f"[!] Warning trying to resolve .torrent from magnet: {e}")
    elif "nyaa.si/view/" in query_or_url or "nyaa.si/download/" in query_or_url:
        m_id = re.search(r'/(?:view|download)/(\d+)', query_or_url)
        if m_id:
            tid = m_id.group(1)
            t_url = f"https://nyaa.si/download/{tid}.torrent"
            print(f"[*] Fetching .torrent for Nyaa ID: {tid}...")
            torrent_bytes = download_torrent_file(t_url)
            torrent_source = t_url
    elif query_or_url.startswith("http://") or query_or_url.startswith("https://"):
        if ".torrent" in query_or_url.lower():
            print(f"[*] Fetching direct .torrent from URL: {query_or_url}...")
            torrent_bytes = download_torrent_file(query_or_url)
            torrent_source = query_or_url
    else:
        # Search on Nyaa RSS
        search_query = query_or_url
        print(f"[*] Searching Nyaa RSS for '{search_query}'...")
        results = fetch_nyaa_rss(search_query)
        if not results:
            # Fallback search with official title + 1080p
            fallback_query = f"{official_title} 1080p"
            print(f"[*] No direct match. Trying fallback search: '{fallback_query}'...")
            results = fetch_nyaa_rss(fallback_query)

        if not results:
            print(f"[!] No torrents found on Nyaa for '{search_query}'.", file=sys.stderr)
            if os.getenv("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
                    f_out.write("has_items=false\nmatrix=[]\n")
            return

        # Sort by seeders descending
        results.sort(key=lambda x: x["seeders"], reverse=True)
        chosen = results[0]
        print(f"[+] Selected Best Torrent: {chosen['title']}")
        print(f"    Seeders: {chosen['seeders']} | Size: {chosen['size']}")
        torrent_title = chosen["title"]
        torrent_source = chosen["torrent_url"] or chosen["magnet"]
        if chosen["torrent_url"]:
            torrent_bytes = download_torrent_file(chosen["torrent_url"])

    # 3. Parse Torrent Files (Multi-file batch vs single file)
    parsed_files = []
    if torrent_bytes:
        try:
            torrent_meta = parse_torrent_bytes(torrent_bytes)
            parsed_files = [f for f in torrent_meta["files"] if f["is_video"]]
            print(f"[+] Torrent contains {len(parsed_files)} video file(s). (Multi-file: {torrent_meta['is_multi']})")
        except Exception as e:
            print(f"[!] Warning parsing .torrent structure: {e}")

    # Explicit user file indices (e.g. 1, 2, 5)
    explicit_indices = None
    if selected_files_str:
        explicit_indices = set()
        for s in selected_files_str.split(","):
            s = s.strip()
            if s.isdigit():
                explicit_indices.add(int(s))

    allowed_range = parse_range_spec(episode_range)
    selected_tasks = []

    if parsed_files:
        # Multi-file or parsed torrent file list
        for f in parsed_files:
            f_idx = f["index"]
            fname = f["filename"]
            ep_extracted = extract_episode_number(fname) or str(f_idx)

            # Filter by explicit indices or range
            if explicit_indices is not None:
                if f_idx not in explicit_indices:
                    continue
            elif allowed_range is not None:
                if str(int(ep_extracted) if ep_extracted.isdigit() else ep_extracted) not in allowed_range:
                    continue

            # Deduplication
            if is_already_uploaded(history, "", int(target_mal_id), official_title, ep_extracted, upload_target=upload_target):
                print(f"[-] Ep {ep_extracted} already uploaded. Skipping.")
                continue

            safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
            clean_filename = f"{safe_title}_Ep_{ep_extracted}.mp4"
            task_key = re.sub(r"_+", "_", f"{target_mal_id}_{safe_title}_ep{ep_extracted}")

            item_meta = {
                "key": task_key,
                "title": official_title,
                "episode": ep_extracted,
                "mal_id": int(target_mal_id),
                "torrent_source": torrent_source,
                "file_index": f_idx,
                "filename": clean_filename,
                "source": "nyaa",
                "upload_target": upload_target
            }

            meta_b64 = base64.b64encode(json.dumps(item_meta).encode("utf-8")).decode("utf-8")
            selected_tasks.append({
                "key": task_key,
                "meta_b64": meta_b64,
                "display": f"{official_title} - Ep {ep_extracted} (File #{f_idx})"
            })
            print(f"[+] Queued File #{f_idx}: {fname} -> Ep {ep_extracted}")
    else:
        # Single-file or raw magnet without pre-parsed files
        ep_single = episode_range if (episode_range and episode_range.isdigit()) else (extract_episode_number(torrent_title) or "1")
        safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
        clean_filename = f"{safe_title}_Ep_{ep_single}.mp4"
        task_key = re.sub(r"_+", "_", f"{target_mal_id}_{safe_title}_ep{ep_single}")

        item_meta = {
            "key": task_key,
            "title": official_title,
            "episode": ep_single,
            "mal_id": int(target_mal_id),
            "torrent_source": torrent_source,
            "file_index": 1,
            "filename": clean_filename,
            "source": "nyaa",
            "upload_target": upload_target
        }
        meta_b64 = base64.b64encode(json.dumps(item_meta).encode("utf-8")).decode("utf-8")
        selected_tasks.append({
            "key": task_key,
            "meta_b64": meta_b64,
            "display": f"{official_title} - Ep {ep_single}"
        })
        print(f"[+] Queued Single Episode: Ep {ep_single}")

    print(f"\n[*] Total Selected Episodes to Process: {len(selected_tasks)}")

    # GitHub Actions Outputs
    if os.getenv("GITHUB_OUTPUT"):
        has_items = "true" if selected_tasks else "false"
        matrix_json = json.dumps(selected_tasks)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f_out:
            f_out.write(f"has_items={has_items}\n")
            f_out.write(f"matrix={matrix_json}\n")


if __name__ == "__main__":
    main()
