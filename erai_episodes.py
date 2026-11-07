"""
Erai-raws Autonomous Ingestion Detector (Matrix Parallel Engine)
- Fetches recent 75 releases from Erai-raws 1080p RSS feed (Nyaa)
- Resolves official MAL ID using AniVault Dual Engine (AniList GraphQL fallback with subsegment resolution)
- Traverses multi-season sequel chain for continuous episode numbers
- Multi-layer deduplication against encrypted uploaded_episodes.json
- Emits GitHub Actions dynamic matrix of pending tasks for parallel processing (up to 8 parallel runners)
"""

import os
import re
import sys
import json
import base64
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Set, Tuple

from mapping_crypto import decrypt_mapping
from mal_resolver import resolve_mal_metadata, normalize_title
from detect_episodes import is_already_uploaded, is_episode_blacklisted, load_history

ERAI_NYAA_RSS_URL = "https://nyaa.si/?page=rss&u=Erai-raws&q=1080p"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

TRACKERS = "".join(f"&tr={urllib.parse.quote(t)}" for t in [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce"
])


def normalize_episode(ep_str: str) -> str:
    s = str(ep_str).strip()
    s_clean = re.sub(r'v\d+$', '', s, flags=re.IGNORECASE)
    try:
        val = float(s_clean)
        return str(int(val)) if val.is_integer() else str(val)
    except Exception:
        return s_clean.lower()


def resolve_season_and_episode_offset(initial_mal_id: int, raw_ep_str: str) -> Tuple[int, str, str]:
    try:
        current_ep = int(raw_ep_str)
    except Exception:
        return initial_mal_id, "", raw_ep_str

    current_mal_id = initial_mal_id
    current_title = ""

    query = """
    query ($idMal: Int) {
      Media (idMal: $idMal, type: ANIME) {
        idMal
        episodes
        title {
          english
          romaji
        }
        relations {
          edges {
            relationType
            node {
              idMal
              format
              episodes
              title {
                english
                romaji
              }
            }
          }
        }
      }
    }
    """

    for step in range(10):
        try:
            req_data = json.dumps({"query": query, "variables": {"idMal": current_mal_id}}).encode("utf-8")
            req = urllib.request.Request(
                "https://graphql.anilist.co",
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                media = data.get("data", {}).get("Media")
                if not media:
                    break

                total_eps = media.get("episodes")
                title_obj = media.get("title", {})
                current_title = title_obj.get("english") or title_obj.get("romaji") or current_title

                if not total_eps or current_ep <= int(total_eps):
                    return current_mal_id, current_title, str(current_ep)

                sequel_node = None
                for edge in media.get("relations", {}).get("edges", []):
                    if edge.get("relationType") == "SEQUEL":
                        node = edge.get("node")
                        if node and node.get("format") in ("TV", "ONA", "TV_SHORT"):
                            sequel_node = node
                            break

                if sequel_node and sequel_node.get("idMal"):
                    current_ep -= int(total_eps)
                    current_mal_id = sequel_node["idMal"]
                    title_sub = sequel_node.get("title", {})
                    current_title = title_sub.get("english") or title_sub.get("romaji") or current_title
                else:
                    break
        except Exception:
            break

    return current_mal_id, current_title, str(current_ep)


def fetch_latest_erairaws() -> List[Dict[str, Any]]:
    req = urllib.request.Request(ERAI_NYAA_RSS_URL, headers={"User-Agent": USER_AGENT})
    items = []
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            xml_data = resp.read()
            root = ET.fromstring(xml_data)
            pattern = r"^\[Erai-raws\]\s*(.*?)\s*-\s*(\d+(?:\.\d+)?(?:v\d+)?)\s*(?:\[|\(|\.mkv|$)"

            for item in root.findall("./channel/item"):
                title_el = item.find("title")
                link_el = item.find("link")
                if title_el is None or not title_el.text:
                    continue

                raw_title = title_el.text.strip()
                link = link_el.text.strip() if link_el is not None and link_el.text else ""

                if "batch" in raw_title.lower() or "01 ~" in raw_title or "01-" in raw_title:
                    continue

                m = re.search(pattern, raw_title, re.IGNORECASE)
                if not m:
                    continue

                show_name = m.group(1).strip()
                ep_num = m.group(2).strip()

                info_hash = None
                for child in item:
                    if child.tag.endswith("infoHash") and child.text:
                        info_hash = child.text.strip()
                        break

                if info_hash:
                    magnet_url = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(raw_title)}{TRACKERS}"
                elif link:
                    magnet_url = link
                else:
                    continue

                items.append({
                    "show": show_name,
                    "episode": ep_num,
                    "magnet": magnet_url,
                    "raw_title": raw_title
                })

        # Deduplicate AVC vs HEVC: Prefer clean AVC (H.264) master releases, fallback to HEVC.
        unique_releases = {}
        for it in items:
            k = (it["show"].lower(), it["episode"])
            is_avc = "AVC" in it["raw_title"]
            if k not in unique_releases:
                unique_releases[k] = it
            elif not ("AVC" in unique_releases[k]["raw_title"]) and is_avc:
                unique_releases[k] = it

        items = list(unique_releases.values())
    except Exception as e:
        print(f"[!] Warning: Error fetching Erai-raws Nyaa RSS: {e}", file=sys.stderr)

    return items


def fetch_in_progress_tasks(repo: str, token: str, current_run_id: str = "") -> Set[Tuple[str, str]]:
    """
    Queries GitHub Actions API to find episodes currently being processed in OTHER active workflow runs.
    Returns a set of (title_lowercase, normalized_ep) so newly triggered runs never duplicate work.
    """
    in_flight = set()
    if not repo or not token:
        return in_flight

    try:
        url_runs = f"https://api.github.com/repos/{repo}/actions/workflows/erai-auto.yml/runs?status=in_progress"
        req = urllib.request.Request(
            url_runs,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Erai-Active-Job-Detector"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            runs_data = json.loads(resp.read().decode("utf-8"))
            runs = runs_data.get("workflow_runs", [])

        for r in runs:
            r_id = str(r.get("id", ""))
            # Skip checking the current run itself
            if current_run_id and r_id == str(current_run_id):
                continue

            url_jobs = f"https://api.github.com/repos/{repo}/actions/runs/{r_id}/jobs?per_page=100"
            req_j = urllib.request.Request(
                url_jobs,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Erai-Active-Job-Detector"
                }
            )
            with urllib.request.urlopen(req_j, timeout=10) as resp_j:
                jobs_data = json.loads(resp_j.read().decode("utf-8"))
                for j in jobs_data.get("jobs", []):
                    j_name = j.get("name", "")
                    # Match pattern like: Parallel Ingest ([Erai-raws] Title - Ep 05)
                    m = re.search(r"\[Erai-raws\]\s*(.*?)\s*-\s*Ep(?:isode)?\s*(\d+(?:\.\d+)?)", j_name, re.IGNORECASE)
                    if m:
                        t = normalize_title(m.group(1).strip())
                        e = normalize_episode(m.group(2).strip())
                        in_flight.add((t, e))
                        print(f"  [*] Live In-Flight Job Detected: '{m.group(1).strip()}' - Ep {m.group(2).strip()} (Run ID: {r_id})")
    except Exception as e:
        print(f"  [!] Notice during active job check: {e}")

    return in_flight


def main():
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    limit = int(os.getenv("MAX_EPISODES", "20").strip() or "20")
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    gh_repo = os.getenv("GITHUB_REPOSITORY", "").strip() or "masterdushangana-lab/heaven-setup-xyz"
    current_run_id = os.getenv("CURRENT_RUN_ID", "").strip()

    print("==================================================================")
    print("      ERAI-RAWS MATRIX DETECTOR: 1080P SIMULCAST INGESTION        ")
    print("==================================================================")

    history = load_history(password) if password else {}
    deleted_records = history.get("__deleted__", {}) if history else {}

    # Live In-Flight Check: Prevent duplicate queuing when multiple workflow runs trigger concurrently!
    in_flight_tasks = fetch_in_progress_tasks(gh_repo, gh_token, current_run_id) if gh_token else set()
    if in_flight_tasks:
        print(f"[*] Found {len(in_flight_tasks)} episodes actively encoding/uploading in concurrent runs. Deduplication active.")

    pending_tasks = []
    queued_keys = set()

    def is_queued(mal_id: Optional[int], title: str, ep: str) -> bool:
        norm_ep = normalize_episode(ep)
        ep_raw = str(ep).strip()
        if mal_id:
            if (mal_id, norm_ep) in queued_keys or (mal_id, ep_raw) in queued_keys:
                return True
        norm_t = normalize_title(title)
        if (norm_t, norm_ep) in queued_keys or (norm_t, ep_raw) in queued_keys:
            return True
        return False

    def mark_queued(mal_id: Optional[int], title: str, ep: str):
        norm_ep = normalize_episode(ep)
        ep_raw = str(ep).strip()
        if mal_id:
            queued_keys.add((mal_id, norm_ep))
            queued_keys.add((mal_id, ep_raw))
        norm_t = normalize_title(title)
        queued_keys.add((norm_t, norm_ep))
        queued_keys.add((norm_t, ep_raw))

    erai_items = fetch_latest_erairaws()
    print(f"[*] Retrieved {len(erai_items)} recent releases from Erai-raws.")

    for item in erai_items:
        if len(pending_tasks) >= limit:
            break

        show_name = item["show"]
        ep_num = str(item["episode"]).strip()
        magnet_url = item["magnet"]

        if not show_name or not ep_num or not magnet_url:
            continue

        mal_info = resolve_mal_metadata(show_name, client_id=client_id)
        mal_id = mal_info.get("mal_id")

        if not mal_id:
            print(f"[!] STRICT MAL GUARD: Skipping Erai-raws '{show_name}' - Ep {ep_num} (MAL ID could not be resolved).")
            continue

        official_title = mal_info.get("title_english") or mal_info.get("title") or show_name

        total_eps = mal_info.get("total_episodes")
        if total_eps and ep_num.isdigit() and int(ep_num) > int(total_eps):
            new_mal_id, new_title, new_ep = resolve_season_and_episode_offset(mal_id, ep_num)
            if new_mal_id and new_mal_id != mal_id:
                print(f"[+] Re-mapped continuous release across seasons: {new_title} - Episode {new_ep} (MAL ID: {new_mal_id})")
                mal_id = new_mal_id
                official_title = new_title or official_title
                ep_num = new_ep

        if is_episode_blacklisted(deleted_records, "", mal_id, official_title, ep_num):
            print(f"[-] Skipping blacklisted release: {official_title} - Ep {ep_num}")
            continue

        if is_already_uploaded(history, "", mal_id, official_title, ep_num, upload_target=upload_target) or \
           is_already_uploaded(history, "", mal_id, show_name, ep_num, upload_target=upload_target):
            print(f"[-] Skipping duplicate release: {official_title} - Ep {ep_num} (already in library)")
            continue

        # Concurrent in-flight check: Skip if another active workflow run is already processing it
        norm_ep_check = normalize_episode(ep_num)
        if (normalize_title(official_title), norm_ep_check) in in_flight_tasks or \
           (normalize_title(show_name), norm_ep_check) in in_flight_tasks:
            print(f"[-] Skipping active release: {official_title} - Ep {ep_num} (currently processing in concurrent workflow run)")
            continue

        if is_queued(mal_id, official_title, ep_num) or is_queued(mal_id, show_name, ep_num):
            continue

        mark_queued(mal_id, official_title, ep_num)
        mark_queued(mal_id, show_name, ep_num)

        safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
        task_key = re.sub(r"_+", "_", f"{mal_id}_{safe_title}_ep{ep_num}")
        clean_name = f"{safe_title}_Ep_{ep_num}.mp4"

        item_meta = {
            "key": task_key,
            "title": official_title,
            "show_original": show_name,
            "episode": ep_num,
            "mal_id": mal_id,
            "magnet_url": magnet_url,
            "filename": clean_name,
            "source": "erai-raws",
            "country_of_origin": mal_info.get("country_of_origin", "JP"),
            "upload_target": upload_target
        }

        meta_b64 = base64.b64encode(json.dumps(item_meta).encode("utf-8")).decode("utf-8")
        pending_tasks.append({
            "key": task_key,
            "meta_b64": meta_b64,
            "display": f"[Erai-raws] {official_title} - Ep {ep_num}"
        })
        print(f"[+] Queued Erai-raws Release: {official_title} - Episode {ep_num} (MAL ID: {mal_id})")

    print("\n" + "=" * 66)
    print(f"[*] Total New Episodes Queued: {len(pending_tasks)}")
    print("==================================================================")

    # Emit outputs for GitHub Actions matrix
    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            if pending_tasks:
                f.write("has_items=true\n")
                f.write(f"matrix={json.dumps(pending_tasks)}\n")
            else:
                f.write("has_items=false\n")
                f.write("matrix=[]\n")
    else:
        print(f"Result: has_items={len(pending_tasks) > 0}, count={len(pending_tasks)}")


if __name__ == "__main__":
    main()
