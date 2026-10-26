"""
Single Episode Direct Uploader to Abyss.to
Downloads using verified AnimeHeaven Referer and uploads directly to Abyss without re-encoding.
"""

import os
import re
import sys
import json
import subprocess
import urllib.parse

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def upload_to_abyss(direct_url: str, output_name: str, abyss_api_key: str) -> dict:
    print(f"[*] Starting download & upload for: {output_name}")
    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', output_name)
    if not safe_name.endswith('.mp4'):
        safe_name += '.mp4'

    temp_file = f"temp_{safe_name}"
    try:
        # Step 1: Direct Download stream with proper Referer
        print(f"[*] Downloading source stream: {direct_url}")
        cmd_dl = [
            "curl", "-sSL",
            "-H", f"User-Agent: {USER_AGENT}",
            "-H", "Referer: https://animeheaven.me/",
            "--tcp-nodelay",
            "--connect-timeout", "30",
            "-o", temp_file,
            direct_url
        ]
        res = subprocess.run(cmd_dl)
        if res.returncode != 0 or not os.path.exists(temp_file) or os.path.getsize(temp_file) < 1000000:
            raise RuntimeError(f"Download failed or incomplete file size (exit code {res.returncode})")

        file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)
        print(f"[+] Download complete: {file_size_mb:.2f} MB. Preserving original resolution.")

        # Step 2: Upload to Abyss.to via up.abyss.to/{key}
        upload_endpoint = f"https://up.abyss.to/{urllib.parse.quote(abyss_api_key, safe='')}"
        cmd_up = [
            "curl", "-sS",
            "-H", "Expect:",
            "--http1.1",
            "--tcp-nodelay",
            "--buffer-size", "131072",
            "--connect-timeout", "30",
            "-F", f"file=@{temp_file};filename={safe_name}",
            upload_endpoint
        ]

        proc = subprocess.run(cmd_up, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Abyss upload curl error: {proc.stderr}")

        data = json.loads(proc.stdout)
        slug = data.get("slug") or data.get("file_id") or data.get("id")
        if not slug:
            raise RuntimeError(f"Unexpected Abyss response: {data}")

        print(f"[SUCCESS] Uploaded to Abyss! File Slug: {slug}")
        return {
            "success": True,
            "slug": slug,
            "embed_url": f"https://abyss.to/{slug}",
            "size_mb": round(file_size_mb, 2)
        }

    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


def main():
    if len(sys.argv) < 3:
        print("Usage: python upload_single.py <direct_url> <output_name> [<metadata_json>]")
        sys.exit(1)

    direct_url = sys.argv[1]
    output_name = sys.argv[2]
    meta_json_str = sys.argv[3] if len(sys.argv) > 3 else "{}"
    abyss_api_key = os.getenv("ABYSS_API_KEY")

    if not abyss_api_key:
        print("ERROR: ABYSS_API_KEY environment variable is required.")
        sys.exit(1)

    result = upload_to_abyss(direct_url, output_name, abyss_api_key)

    try:
        item_meta = json.loads(meta_json_str)
        result.update(item_meta)
    except Exception:
        pass

    out_dir = "artifacts"
    os.makedirs(out_dir, exist_ok=True)
    safe_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', output_name)
    with open(os.path.join(out_dir, f"{safe_base}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
