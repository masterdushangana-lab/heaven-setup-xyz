"""
StreamHG Single & Bulk Video Deleter
Deletes files directly from StreamHG via official API:
GET https://streamhgapi.com/api/file/delete?key={key}&file_code={file_code}
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from mapping_crypto import decrypt_mapping, encrypt_mapping

STATE_FILE = "uploaded_episodes.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"


def delete_single_streamhg_file(file_code: str, api_key: str) -> bool:
    """Deletes a single video on StreamHG by its file_code."""
    clean_code = file_code.strip()
    if "/" in clean_code:
        clean_code = clean_code.split("/")[-1].replace(".html", "").replace("e/", "")
    
    url = f"https://streamhgapi.com/api/file/delete?key={urllib.parse.quote(api_key, safe='')}&file_code={urllib.parse.quote(clean_code, safe='')}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == 200 or data.get("msg") == "OK":
                print(f"[SUCCESS] Deleted file ({clean_code}) from StreamHG!")
                return True
            else:
                print(f"[!] StreamHG delete warning for {clean_code}: {data}")
                return False
    except Exception as e:
        print(f"[!] Error deleting {clean_code} from StreamHG: {e}")
        return False


def main():
    streamhg_api_key = os.getenv("STREAMHG_API_KEY", "").strip() or os.getenv("ABYSS_API_KEY", "").strip()
    file_id = os.getenv("FILE_ID", "").strip() or (sys.argv[1] if len(sys.argv) > 1 else "")

    if not streamhg_api_key:
        print("[ERROR] STREAMHG_API_KEY environment variable missing.", file=sys.stderr)
        sys.exit(1)

    if not file_id:
        print("[ERROR] FILE_ID parameter missing.", file=sys.stderr)
        sys.exit(1)

    ok = delete_single_streamhg_file(file_id, streamhg_api_key)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
