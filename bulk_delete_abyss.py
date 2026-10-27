"""
Abyss.to Bulk Video Deleter
Deletes either:
  1. All Abyss videos recorded in uploaded_episodes.json
  2. All videos fetched directly from Abyss account via API /auth/login & /v1/files
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from mapping_crypto import decrypt_mapping, encrypt_mapping

STATE_FILE = "uploaded_episodes.json"


def get_abyss_token(api_key: str, email: str = "", password: str = "") -> str:
    """Gets JWT bearer token via login or falls back to API key."""
    if email and password:
        login_url = "https://api.abyss.to/auth/login"
        payload = json.dumps({"email": email, "password": password}).encode("utf-8")
        req = urllib.request.Request(
            login_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "curl/8.5.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                token = data.get("token")
                if token:
                    print("[+] Successfully authenticated with Abyss (JWT Token obtained)!")
                    return token
        except Exception as e:
            print(f"[!] Login attempt failed: {e}. Falling back to API Key.")

    return api_key.strip()


def delete_single_abyss_file(slug: str, token: str) -> bool:
    """Sends DELETE request to Abyss for a single slug."""
    url = f"https://api.abyss.to/v1/files/{urllib.parse.quote(slug)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "curl/8.5.0",
            "Accept": "*/*"
        },
        method="DELETE"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 204):
                return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Already deleted
            return True
        print(f"[!] HTTP {e.code} for slug '{slug}': {e.read().decode('utf-8', errors='ignore')}")
    except Exception as e:
        print(f"[!] Network error deleting slug '{slug}': {e}")
    return False


def main():
    abyss_api_key = os.getenv("ABYSS_API_KEY", "").strip()
    abyss_email = os.getenv("ABYSS_EMAIL", "").strip()
    abyss_password = os.getenv("ABYSS_PASSWORD", "").strip()
    mapping_password = os.getenv("MAPPING_PASSWORD", "").strip()

    if not abyss_api_key and not (abyss_email and abyss_password):
        print("[ERROR] Please provide ABYSS_API_KEY or (ABYSS_EMAIL and ABYSS_PASSWORD).", file=sys.stderr)
        sys.exit(1)

    token = get_abyss_token(abyss_api_key, abyss_email, abyss_password)
    if not token:
        print("[ERROR] Could not obtain valid Abyss authentication token.", file=sys.stderr)
        sys.exit(1)

    # Load mapping file to collect all uploaded Abyss slugs
    if not os.path.exists(STATE_FILE):
        print(f"[!] {STATE_FILE} not found. Nothing to delete.", file=sys.stderr)
        sys.exit(0)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        raw_doc = json.load(f)

    if isinstance(raw_doc, dict) and raw_doc.get("algorithm") == "AES-256-GCM":
        if not mapping_password:
            print("[ERROR] MAPPING_PASSWORD required to decrypt uploaded_episodes.json.", file=sys.stderr)
            sys.exit(1)
        history = decrypt_mapping(raw_doc, mapping_password)
    else:
        history = raw_doc

    slugs_to_delete = []
    for anime_key, anime_val in history.items():
        if not isinstance(anime_val, dict):
            continue
        eps = anime_val.get("episodes", {})
        for ep_num, ep_data in eps.items():
            slug = ep_data.get("slug")
            if slug:
                slugs_to_delete.append((anime_key, ep_num, slug))

    total = len(slugs_to_delete)
    print(f"[*] Found {total} Abyss video(s) in uploaded history.")

    if total == 0:
        print("[i] No Abyss slugs found to delete.")
        sys.exit(0)

    # Execute Deletions
    deleted_count = 0
    for idx, (anime_key, ep_num, slug) in enumerate(slugs_to_delete, 1):
        print(f"[{idx}/{total}] Deleting slug '{slug}' (Anime: {anime_key}, Ep: {ep_num})...", end=" ")
        ok = delete_single_abyss_file(slug, token)
        if ok:
            print("DONE")
            deleted_count += 1
            # Remove slug from local mapping
            if "slug" in history[anime_key]["episodes"][ep_num]:
                del history[anime_key]["episodes"][ep_num]["slug"]
            if "embed_url" in history[anime_key]["episodes"][ep_num]:
                del history[anime_key]["episodes"][ep_num]["embed_url"]
        else:
            print("FAILED")
        time.sleep(0.3)  # Gentle rate limit

    print(f"\n[SUMMARY] Successfully deleted {deleted_count}/{total} videos from Abyss.to!")

    # Save updated mapping file (preserving Telegram records)
    if mapping_password:
        enc = encrypt_mapping(history, mapping_password)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(enc, f, indent=2, ensure_ascii=False)
        print("[+] Updated encrypted uploaded_episodes.json (Abyss slugs removed, Telegram kept intact).")


if __name__ == "__main__":
    main()
