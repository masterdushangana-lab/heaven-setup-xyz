"""
Erai-raws 24/7 Real-Time Autonomous Ingestion Engine
- Polls Erai-raws 1080p RSS feed on a tight loop (every 5-10s)
- Resolves official MAL ID via AniVault Multi-Layer Matching Engine (AniList GraphQL fallback)
- Deduplicates against encrypted uploaded_episodes.json (and auto-retries remote sync)
- Downloads via high-speed aria2c (16-split connections)
- Burns official embedded English subtitles into 1080p MP4 via FFmpeg (<500MB)
- Uploads concurrently to Abyss.to Multi-Account Pool and Telegram
- Executes instant mapping sync & push to GitHub repository
- Sends Telegram Admin alert for successful completions
- Designed for continuous long-running execution (runs for up to 5.5 hours per job)
"""

import os
import re
import sys
import time
import json
import base64
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import concurrent.futures
from typing import Dict, Any, List, Optional, Tuple, Set

from mapping_crypto import decrypt_mapping, encrypt_mapping
from mal_resolver import resolve_mal_metadata, normalize_title
from detect_episodes import is_already_uploaded, is_episode_blacklisted, load_history
from abyss_pool import AbyssAccountPool
from torrent_processor import download_torrent, find_downloaded_videos, burn_subtitles_and_encode
from upload_single import upload_to_telegram, send_admin_notification
from instant_sync import record_and_push_upload

ERAI_NYAA_RSS_URL = "https://nyaa.si/?page=rss&u=Erai-raws&q=1080p"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Public Fast BitTorrent Trackers
TRACKERS = "".join(f"&tr={urllib.parse.quote(t)}" for t in [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce"
])


def normalize_episode(ep_str: str) -> str:
    """Normalizes episode strings for deduplication."""
    s = str(ep_str).strip()
    s_clean = re.sub(r'v\d+$', '', s, flags=re.IGNORECASE)
    try:
        val = float(s_clean)
        return str(int(val)) if val.is_integer() else str(val)
    except Exception:
        return s_clean.lower()


def resolve_season_and_episode_offset(initial_mal_id: int, raw_ep_str: str) -> Tuple[int, str, str]:
    """
    Iteratively traverses the SEQUEL chain across seasons
    until the continuous episode number maps to the exact season's MAL ID and relative episode.
    """
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

                # Find SEQUEL edge
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
    """Fetches the latest 75 releases from Erai-raws RSS."""
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
    except Exception as e:
        print(f"[!] Warning: Error fetching Erai-raws Nyaa RSS: {e}", file=sys.stderr)

    return items


def process_single_release(
    item_data: Dict[str, Any],
    abyss_pool: AbyssAccountPool,
    tg_bot_token: str,
    tg_chat_id: str,
    tg_admin_chat_id: str,
    tg_local_api_url: str,
    upload_target: str,
    mapping_pw: str
) -> bool:
    """Downloads, burns subtitles, compresses, uploads, and syncs mapping for one release."""
    magnet_url = item_data["magnet_url"]
    title = item_data["title"]
    episode = str(item_data["episode"])
    safe_filename = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', item_data.get("filename", f"{title}_Ep_{episode}.mp4"))
    if not safe_filename.endswith(".mp4"):
        safe_filename += ".mp4"

    print("\n" + "=" * 66)
    print(f"[*] INGESTING RELEASE: {title} - Episode {episode}")
    print(f"[*] Magnet: {magnet_url[:60]}...")
    print("=" * 66)

    temp_dl_dir = f"/tmp/erai_dl_{int(time.time())}_{os.getpid()}"
    encoded_mp4 = f"temp_encoded_{safe_filename}"
    result: Dict[str, Any] = {}

    try:
        # Step 1: Download via aria2c
        dl_success = download_torrent(magnet_url, temp_dl_dir, timeout_seconds=1800)
        if not dl_success:
            print(f"[!] aria2c failed to download torrent for {title} Ep {episode}", file=sys.stderr)
            return False

        videos = find_downloaded_videos(temp_dl_dir)
        if not videos:
            print(f"[!] No valid video file found after download in {temp_dl_dir}", file=sys.stderr)
            return False

        videos.sort(key=lambda x: os.path.getsize(x), reverse=True)
        raw_video = videos[0]
        raw_size_mb = os.path.getsize(raw_video) / (1024 * 1024)
        print(f"[+] Downloaded Raw Video: {raw_video} ({raw_size_mb:.1f} MB)")

        # Step 2: Burn Subtitles & Compress via FFmpeg
        country_code = item_data.get("country_of_origin", "JP")
        is_movie = item_data.get("is_movie", False) or (item_data.get("media_type") == "movie")
        burn_success = burn_subtitles_and_encode(raw_video, encoded_mp4, target_max_mb=500, country_code=country_code, is_movie=is_movie)
        if not burn_success:
            print(f"[!] FFmpeg subtitle burning failed for {title} Ep {episode}", file=sys.stderr)
            return False

        encoded_size_mb = round(os.path.getsize(encoded_mp4) / (1024 * 1024), 2)
        item_data["size_mb"] = encoded_size_mb
        caption = f"🎬 {title} - Episode {episode}"

        # Step 3: Concurrent Parallel Upload to Abyss & Telegram
        need_abyss = upload_target in ("both", "abyss")
        need_telegram = upload_target in ("both", "telegram")
        upload_futures = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            if need_abyss:
                if abyss_pool.total_accounts > 0:
                    fut_abyss = executor.submit(abyss_pool.upload, encoded_mp4, safe_filename)
                    upload_futures[fut_abyss] = "abyss"
                else:
                    print("[WARN] No Abyss accounts available in pool.")

            if need_telegram:
                if tg_bot_token and tg_chat_id:
                    fut_tg = executor.submit(upload_to_telegram, encoded_mp4, safe_filename, tg_bot_token, tg_chat_id, caption, tg_local_api_url)
                    upload_futures[fut_tg] = "telegram"
                else:
                    print("[WARN] Telegram bot token or chat ID not set.")

            for fut in concurrent.futures.as_completed(upload_futures):
                dest = upload_futures[fut]
                try:
                    res = fut.result()
                    if dest == "abyss" and res:
                        result["abyss"] = res
                        result["slug"] = res["slug"]
                        result["embed_url"] = res.get("embed_url") or f"https://player.abyssplayer.com/{res['slug']}"
                    elif dest == "telegram" and res:
                        result["telegram"] = res
                except Exception as up_err:
                    print(f"[!] {dest.capitalize()} upload error: {up_err}")
                    if upload_target == dest:
                        return False

        if upload_target == "both" and not (result.get("abyss") or result.get("telegram")):
            print("[!] Both Abyss and Telegram uploads failed.", file=sys.stderr)
            return False

        # Step 4: Admin Completion Alert
        if tg_bot_token and tg_admin_chat_id:
            slug = result.get("slug", "")
            tg_info = result.get("telegram")
            tg_post = tg_info.get("post_url") if isinstance(tg_info, dict) else ""
            slug_str = f" | ⚡ Abyss: <code>{slug}</code>" if slug else ""
            tg_str = f" | ✈️ <a href='{tg_post}'>Telegram Post</a>" if tg_post else ""

            admin_alert = (
                f"✅ <b>Erai-raws Ingest Complete:</b> {title} - Episode {episode}\n"
                f"📦 <b>Size:</b> {encoded_size_mb} MB{slug_str}{tg_str}"
            )
            send_admin_notification(tg_bot_token, tg_admin_chat_id, admin_alert)

        # Step 5: Instant Mapping Sync & Git Commit & Push
        if mapping_pw and (result.get("abyss") or result.get("telegram")):
            print("[*] Recording and pushing upload to encrypted mapping repository...")
            record_and_push_upload(item_data, result, mapping_pw)

        print(f"[SUCCESS] Completed {title} - Episode {episode}!")
        return True

    finally:
        if os.path.exists(temp_dl_dir):
            shutil.rmtree(temp_dl_dir, ignore_errors=True)
        if os.path.exists(encoded_mp4):
            try:
                os.remove(encoded_mp4)
            except Exception:
                pass


def run_daemon(poll_interval: int = 10, max_runtime_seconds: int = 19800):
    """
    Main 24/7 Watch Loop:
    Polls Erai-raws RSS every poll_interval seconds.
    Processes newly released episodes immediately.
    Runs up to max_runtime_seconds (default 5.5 hours, safely within GitHub 6h limit).
    """
    password = os.getenv("MAPPING_PASSWORD", "").strip()
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    upload_target = os.getenv("UPLOAD_TARGET", "both").lower().strip()
    tg_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    tg_admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip() or os.getenv("ADMIN_CHAT_ID", "").strip()
    tg_local_api_url = os.getenv("TG_LOCAL_API_URL", "").strip()

    abyss_pool = AbyssAccountPool()

    print("==================================================================")
    print("      ERAI-RAWS 24/7 AUTONOMOUS REAL-TIME INGESTION DAEMON        ")
    print(f"      Polling Interval: {poll_interval}s | Max Runtime: {max_runtime_seconds}s")
    print("==================================================================")

    start_time = time.time()
    processed_in_session: Set[Tuple[Any, str]] = set()

    while time.time() - start_time < max_runtime_seconds:
        try:
            # Sync mapping from remote before checking
            history = load_history(password) if password else {}
            deleted_records = history.get("__deleted__", {}) if history else {}

            erai_items = fetch_latest_erairaws()

            for item in erai_items:
                show_name = item["show"]
                ep_num = str(item["episode"]).strip()
                magnet_url = item["magnet"]

                if not show_name or not ep_num or not magnet_url:
                    continue

                # Quick check against current daemon memory
                if (show_name, ep_num) in processed_in_session:
                    continue

                # Resolve MAL ID & Metadata
                mal_info = resolve_mal_metadata(show_name, client_id=client_id)
                mal_id = mal_info.get("mal_id")

                if not mal_id:
                    continue

                official_title = mal_info.get("title_english") or mal_info.get("title") or show_name

                # Multi-season offset resolution if continuous
                total_eps = mal_info.get("total_episodes")
                if total_eps and ep_num.isdigit() and int(ep_num) > int(total_eps):
                    new_mal_id, new_title, new_ep = resolve_season_and_episode_offset(mal_id, ep_num)
                    if new_mal_id and new_mal_id != mal_id:
                        mal_id = new_mal_id
                        official_title = new_title or official_title
                        ep_num = new_ep

                if (mal_id, ep_num) in processed_in_session:
                    continue

                # Check blacklist
                if is_episode_blacklisted(deleted_records, "", mal_id, official_title, ep_num):
                    processed_in_session.add((mal_id, ep_num))
                    continue

                # Check already uploaded in encrypted library
                if is_already_uploaded(history, "", mal_id, official_title, ep_num, upload_target=upload_target) or \
                   is_already_uploaded(history, "", mal_id, show_name, ep_num, upload_target=upload_target):
                    processed_in_session.add((mal_id, ep_num))
                    processed_in_session.add((show_name, ep_num))
                    continue

                # NEW RELEASE DETECTED!
                print(f"\n[!] 🔥 NEW RELEASE DETECTED: {official_title} - Episode {ep_num} (MAL ID: {mal_id})")

                safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", official_title)
                clean_name = f"{safe_title}_Ep_{ep_num}.mp4"
                task_key = re.sub(r"_+", "_", f"{mal_id}_{safe_title}_ep{ep_num}")

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

                # Execute full ingestion immediately in-runner
                success = process_single_release(
                    item_data=item_meta,
                    abyss_pool=abyss_pool,
                    tg_bot_token=tg_bot_token,
                    tg_chat_id=tg_chat_id,
                    tg_admin_chat_id=tg_admin_chat_id,
                    tg_local_api_url=tg_local_api_url,
                    upload_target=upload_target,
                    mapping_pw=password
                )

                if success:
                    processed_in_session.add((mal_id, ep_num))
                    processed_in_session.add((show_name, ep_num))

        except Exception as loop_err:
            print(f"[!] Daemon loop tick warning: {loop_err}", file=sys.stderr)

        # Sleep poll interval before next check
        time.sleep(poll_interval)

    print(f"[*] Daemon reached {max_runtime_seconds}s limit. Exiting cleanly for next scheduled session.")


if __name__ == "__main__":
    interval = int(os.getenv("POLL_INTERVAL", "10").strip() or "10")
    max_run = int(os.getenv("MAX_RUNTIME_SECONDS", "19800").strip() or "19800")
    run_daemon(poll_interval=interval, max_runtime_seconds=max_run)
