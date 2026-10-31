import os
import json
import base64
import time
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

STATE_FILE = "uploaded_episodes.json"
AES_SALT_LEN = 16
AES_NONCE_LEN = 12

def derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000
    )
    return kdf.derive(password.encode())

def load_history(password: str) -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "rb") as f:
        data = f.read()
    if not data:
        return {}
    try:
        salt = data[:AES_SALT_LEN]
        nonce = data[AES_SALT_LEN:AES_SALT_LEN + AES_NONCE_LEN]
        ciphertext = data[AES_SALT_LEN + AES_NONCE_LEN:]
        key = derive_key(password, salt)
        aesgcm = AESGCM(key)
        decrypted = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(decrypted.decode("utf-8"))
    except Exception as e:
        print(f"[!] Warning: Failed to decrypt {STATE_FILE}: {e}")
        return {}

def save_history(history: dict, password: str):
    raw_data = json.dumps(history, indent=2).encode("utf-8")
    salt = os.urandom(AES_SALT_LEN)
    nonce = os.urandom(AES_NONCE_LEN)
    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, raw_data, None)
    with open(STATE_FILE, "wb") as f:
        f.write(salt + nonce + ciphertext)

def fetch_mal_metadata(query_or_id: str, client_id: str) -> dict:
    if not client_id:
        return {}
    headers = {"X-MAL-CLIENT-ID": client_id}
    try:
        if str(query_or_id).isdigit():
            url = f"https://api.myanimelist.net/v2/anime/{query_or_id}?fields=id,title,alternative_titles,media_type"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                alt = data.get("alternative_titles", {})
                return {
                    "mal_id": data.get("id"),
                    "title": data.get("title"),
                    "title_english": alt.get("en") or data.get("title"),
                    "title_japanese": alt.get("ja"),
                    "media_type": data.get("media_type", "tv").upper()
                }
    except Exception as e:
        print(f"[!] MAL lookup error: {e}")
    return {}

def auto_heal_history(history: dict, client_id: str) -> tuple[dict, int]:
    healed = 0
    return history, healed

def main():
    password = os.getenv("STATE_ENCRYPTION_PASSWORD")
    if not password:
        print("[!] Error: STATE_ENCRYPTION_PASSWORD not provided.")
        return

    mal_client_id = os.getenv("MAL_CLIENT_ID", "")
    artifacts_dir = os.getenv("GITHUB_WORKSPACE", ".")

    history = load_history(password)
    history, healed_count = auto_heal_history(history, mal_client_id)
    updated_count = 0

    if os.path.exists(artifacts_dir):
        for root, _, files in os.walk(artifacts_dir):
            for f in files:
                if f.endswith(".json"):
                    full_path = os.path.join(root, f)
                    try:
                        with open(full_path, "r", encoding="utf-8") as jf:
                            data = json.load(jf)

                        if not (data.get("mp4upload") or data.get("telegram") or data.get("slug")):
                            continue

                        title = data.get("title", "Unknown Anime")
                        ep_num = str(data.get("episode", 1))
                        mal_info = data.get("mal_info") or {}
                        mal_id = mal_info.get("mal_id")

                        primary_key = str(mal_id) if mal_id else title

                        if primary_key not in history:
                            history[primary_key] = {
                                "mal_id": mal_id,
                                "title": title,
                                "title_english": mal_info.get("title_english") or title,
                                "title_japanese": mal_info.get("title_japanese"),
                                "media_type": mal_info.get("media_type", "TV"),
                                "animeheaven_id": data.get("anime_id"),
                                "animeheaven_url": data.get("anime_url"),
                                "episodes": {}
                            }
                        elif not history[primary_key].get("mal_id") and mal_id:
                            history[primary_key]["mal_id"] = mal_id

                        rank = data.get("new_php_rank")
                        if rank is not None:
                            computed_ts = int(time.time()) - int(rank)
                        else:
                            computed_ts = 1000000000 + int(ep_num if ep_num.isdigit() else 0)

                        ep_record = history[primary_key]["episodes"].get(ep_num, {})
                        if data.get("mp4upload") and isinstance(data["mp4upload"], dict) and data["mp4upload"].get("file_code"):
                            ep_record["mp4upload"] = data["mp4upload"]
                            ep_record["embed_url"] = data["mp4upload"].get("embed_url") or f"https://www.mp4upload.com/embed-{data['mp4upload'].get('file_code')}.html"
                            if data["mp4upload"].get("direct_mp4"):
                                ep_record["direct_mp4"] = data["mp4upload"]["direct_mp4"]
                        if data.get("telegram"):
                            ep_record["telegram"] = data["telegram"]
                        if data.get("filename"):
                            ep_record["filename"] = data.get("filename")
                        if data.get("size_mb"):
                            ep_record["size_mb"] = data.get("size_mb")
                        ep_record["uploaded_at"] = computed_ts

                        history[primary_key]["episodes"][ep_num] = ep_record
                        updated_count += 1
                        mp4_fcode = ep_record.get("mp4upload", {}).get("file_code") if isinstance(ep_record.get("mp4upload"), dict) else None
                        print(f"[+] Mapped MAL[{primary_key}] -> Ep {ep_num} -> Mp4Upload: {mp4_fcode} | Telegram: {bool(data.get('telegram'))}")

                    except Exception as e:
                        print(f"[!] Error parsing {full_path}: {e}")

    if updated_count > 0 or healed_count > 0 or not os.path.exists(STATE_FILE):
        save_history(history, password)
        print(f"[DONE] Synced {updated_count} episodes (healed {healed_count}) into {STATE_FILE}.")
    else:
        print(f"[DONE] No new episodes or heals to save. {STATE_FILE} remains untouched.")


if __name__ == "__main__":
    main()
