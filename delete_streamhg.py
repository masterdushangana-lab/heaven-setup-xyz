"""
StreamHG Video Deletion Tool
Deletes video from StreamHG via official REST API:
GET https://streamhgapi.com/api/file/delete?key={STREAMHG_API_KEY}&file_code={file_code}
"""

import os
import sys
import json
import urllib.request
import urllib.parse

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def delete_from_streamhg(file_code: str, api_key: str) -> bool:
    if not file_code or not api_key:
        print("[ERROR] file_code and STREAMHG_API_KEY are required.", file=sys.stderr)
        return False

    # Extract raw file_code if full URL was passed
    if "/" in file_code:
        file_code = file_code.split("/")[-1].split("?")[0]

    api_url = f"https://streamhgapi.com/api/file/delete?key={urllib.parse.quote(api_key, safe='')}&file_code={file_code}"
    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"[*] StreamHG response: {data}")
            if data.get("status") == 200:
                print(f"[SUCCESS] Video '{file_code}' deleted permanently from StreamHG.")
                return True
            else:
                print(f"[!] StreamHG delete notice: {data.get('msg')}")
                return False
    except Exception as e:
        print(f"[ERROR] Failed to delete from StreamHG: {e}", file=sys.stderr)
        return False


def main():
    api_key = os.getenv("STREAMHG_API_KEY", "").strip() or os.getenv("ABYSS_API_KEY", "").strip()
    file_code = sys.argv[1].strip() if len(sys.argv) > 1 else os.getenv("FILE_ID", "").strip()

    if not file_code:
        print("Usage: python delete_streamhg.py <file_code_or_url>")
        sys.exit(1)

    if not api_key:
        print("[ERROR] STREAMHG_API_KEY environment variable is not configured.")
        sys.exit(1)

    success = delete_from_streamhg(file_code, api_key)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
