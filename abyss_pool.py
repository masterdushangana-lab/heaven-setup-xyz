"""
Abyss.to Multi-Account Pool & Automated Failover Manager
- Supports multiple Abyss accounts via:
    1. ABYSS_API_KEYS (comma, semicolon, space, or newline separated list)
    2. ABYSS_API_KEY (comma, semicolon, space, or newline separated list)
    3. Numbered variables: ABYSS_API_KEY_1, ABYSS_API_KEY_2, ABYSS_API_KEY_3...
    4. Fallbacks: STREAMHG_API_KEY, MP4UPLOAD_API_KEY
- Automatically detects:
    - Daily upload quota reached
    - Rate limit exceeded (HTTP 429)
    - Account storage limits / banned / unauthorized (HTTP 401, 403)
- Instantly fails over to the next available account in the pool
- Maintains active account state across batch migrations to prevent redundant retries on exhausted accounts.
"""

import os
import re
import sys
import time
import json
import secrets
import string
import subprocess
import urllib.parse
from typing import List, Optional, Tuple, Dict, Any

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

QUOTA_ERROR_KEYWORDS = [
    "quota",
    "daily upload limit",
    "upload limit",
    "limit reached",
    "limit exceeded",
    "daily limit",
    "exceeded your daily",
    "maximum daily",
    "storage full",
    "storage limit",
    "disk space",
    "bandwidth",
    "too many requests",
    "rate limit",
    "unauthorized",
    "invalid key",
    "banned",
    "blocked",
    "suspended",
    "account error",
    "not allowed"
]


def parse_keys_string(raw: str) -> List[str]:
    """Splits raw string of API keys by comma, semicolon, pipe, newline, or whitespace."""
    if not raw or not isinstance(raw, str):
        return []
    parts = re.split(r'[,;|\s\r\n]+', raw)
    cleaned = []
    for p in parts:
        clean = p.strip().strip("'\"").strip()
        if clean and len(clean) >= 2 and clean not in cleaned:
            cleaned.append(clean)
    return cleaned


def get_abyss_api_keys() -> List[str]:
    """
    Discovers all configured Abyss API keys from environment variables.
    Checks:
      - ABYSS_API_KEYS
      - ABYSS_API_KEY
      - ABYSS_API_KEY_1, ABYSS_API_KEY_2, ...
      - STREAMHG_API_KEY, MP4UPLOAD_API_KEY
    """
    all_keys: List[str] = []

    def add_keys(val: str):
        for k in parse_keys_string(val):
            if k not in all_keys:
                all_keys.append(k)

    # 1. Primary multi-key env
    if os.getenv("ABYSS_API_KEYS"):
        add_keys(os.getenv("ABYSS_API_KEYS", ""))

    # 2. Standard ABYSS_API_KEY (might contain multi-keys separated by commas)
    if os.getenv("ABYSS_API_KEY"):
        add_keys(os.getenv("ABYSS_API_KEY", ""))

    # 3. Check numbered keys (e.g. ABYSS_API_KEY_1 .. ABYSS_API_KEY_20)
    for i in range(1, 21):
        num_key = os.getenv(f"ABYSS_API_KEY_{i}")
        if num_key:
            add_keys(num_key)

    # 4. Check fallback legacy names
    if os.getenv("STREAMHG_API_KEY"):
        add_keys(os.getenv("STREAMHG_API_KEY", ""))
    if os.getenv("MP4UPLOAD_API_KEY"):
        add_keys(os.getenv("MP4UPLOAD_API_KEY", ""))

    # 5. Check any other ABYSS_KEY_* environment variables
    for env_k, env_v in os.environ.items():
        if re.match(r'^ABYSS_(?:API_)?KEY(?:_\d+)?$', env_k, re.IGNORECASE):
            add_keys(env_v)

    return all_keys


def mask_key(key: str) -> str:
    """Masks API key for secure logging."""
    if not key:
        return "EMPTY"
    if len(key) <= 8:
        return f"{key[:2]}***{key[-2:]}"
    return f"{key[:6]}...{key[-4:]}"


def is_quota_or_account_error(resp_body: str, http_code: int = 0) -> Tuple[bool, str]:
    """
    Inspects HTTP status code and response body to determine if the error
    is due to quota exhaustion, rate limiting, or account blockage.
    """
    body_lower = (resp_body or "").lower()

    if http_code == 429:
        return True, "Rate Limit Exceeded (HTTP 429)"
    if http_code == 403:
        return True, "Daily Quota / Upload Limit / Access Forbidden (HTTP 403)"
    if http_code == 401:
        return True, "Unauthorized / Invalid Key (HTTP 401)"

    for kw in QUOTA_ERROR_KEYWORDS:
        if kw in body_lower:
            return True, f"Matched quota/account keyword: '{kw}'"

    # Check for JSON error payload with status >= 400
    try:
        data = json.loads(resp_body)
        if isinstance(data, dict):
            status = data.get("status")
            if status in (401, 403, 429):
                msg = data.get("message") or data.get("error") or str(data)
                return True, f"Abyss JSON status {status}: {msg}"
            err_msg = str(data.get("message", "") or data.get("error", "")).lower()
            for kw in QUOTA_ERROR_KEYWORDS:
                if kw in err_msg:
                    return True, f"Abyss JSON message: '{err_msg}'"
    except Exception:
        pass

    return False, ""


def extract_slug_from_response(resp_text: str) -> Optional[str]:
    """Extracts slug or file id from Abyss response JSON or text."""
    slug = None
    try:
        data = json.loads(resp_text)
        if isinstance(data, dict):
            slug = data.get("slug") or data.get("id") or data.get("file_id") or data.get("file_code")
            if not slug and isinstance(data.get("result"), dict):
                slug = data["result"].get("slug") or data["result"].get("id") or data["result"].get("file_id") or data["result"].get("file_code")
    except Exception:
        pass

    if not slug:
        m_slug = (
            re.search(r'["\']slug["\']\s*:\s*["\']([a-zA-Z0-9_-]{1,128})["\']', resp_text) or
            re.search(r'["\']id["\']\s*:\s*["\']([a-zA-Z0-9_-]{1,128})["\']', resp_text) or
            re.search(r'["\']file_id["\']\s*:\s*["\']([a-zA-Z0-9_-]{1,128})["\']', resp_text) or
            re.search(r'["\']file_code["\']\s*:\s*["\']([a-zA-Z0-9_-]{1,128})["\']', resp_text)
        )
        if m_slug:
            slug = m_slug.group(1).strip()

    return slug


class AbyssAccountPool:
    """
    Manages a pool of Abyss accounts, handles automatic upload failover,
    and isolates exhausted accounts.
    """

    def __init__(self, keys: Optional[List[str]] = None, user_agent: str = USER_AGENT):
        if keys is None:
            keys = get_abyss_api_keys()
        self.keys: List[str] = [k for k in keys if k]
        self.active_idx: int = 0
        self.exhausted_indices: set = set()
        self.user_agent: str = user_agent

        if self.keys:
            print(f"[*] [AbyssPool] Initialized with {len(self.keys)} account(s):")
            for idx, k in enumerate(self.keys, 1):
                print(f"    Account #{idx}: {mask_key(k)}")
        else:
            print("[!] [AbyssPool] Warning: No Abyss API keys configured in environment!", file=sys.stderr)

    @property
    def total_accounts(self) -> int:
        return len(self.keys)

    @property
    def available_accounts(self) -> int:
        return max(0, len(self.keys) - len(self.exhausted_indices))

    def all_exhausted(self) -> bool:
        return self.available_accounts == 0 or self.active_idx >= len(self.keys)

    def get_current_key(self) -> Optional[str]:
        while self.active_idx < len(self.keys):
            if self.active_idx not in self.exhausted_indices:
                return self.keys[self.active_idx]
            self.active_idx += 1
        return None

    def mark_exhausted(self, idx: int, reason: str = ""):
        self.exhausted_indices.add(idx)
        key = self.keys[idx] if idx < len(self.keys) else ""
        print(f"\n" + "=" * 66)
        print(f"[!] [AbyssPool] ACCOUNT #{idx + 1} ({mask_key(key)}) EXHAUSTED / QUOTA HIT!")
        print(f"[!] Reason: {reason}")
        print("=" * 66)

        # Advance to next non-exhausted account
        while self.active_idx < len(self.keys) and self.active_idx in self.exhausted_indices:
            self.active_idx += 1

        if self.active_idx < len(self.keys):
            next_key = self.keys[self.active_idx]
            print(f"[AUTO-FAILOVER] >>> Switching to Abyss Account #{self.active_idx + 1} ({mask_key(next_key)}) <<<")
        else:
            print(f"[CRITICAL] All {len(self.keys)} Abyss account(s) have exhausted their daily quota/limits!\n")

    def upload(
        self,
        temp_file: str,
        safe_name: str,
        max_retries_per_account: int = 2,
        upload_timeout: int = 3600
    ) -> Optional[Dict[str, Any]]:
        """
        Uploads a video to Abyss using the active account.
        If the active account hits quota or fails, it automatically marks
        it exhausted and fails over to the next account in the pool.
        """
        if not self.keys:
            print("[!] [AbyssPool] Upload failed: No Abyss API keys available.", file=sys.stderr)
            return None

        if not os.path.exists(temp_file):
            print(f"[!] [AbyssPool] Upload failed: Local file not found: {temp_file}", file=sys.stderr)
            return None

        file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)

        while not self.all_exhausted():
            curr_idx = self.active_idx
            api_key = self.keys[curr_idx]
            masked = mask_key(api_key)

            random_base = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(25))
            abyss_filename = f"{random_base}.mp4"
            upload_url = f"https://up.abyss.to/{urllib.parse.quote(api_key.strip(), safe='')}"

            print(f"[*] Uploading to Abyss via Account #{curr_idx + 1}/{len(self.keys)} ({masked})")
            print(f"    File: '{abyss_filename}' (Original: {safe_name} | {file_size_mb:.2f} MB)")

            start_time = time.time()
            account_failed = False

            for attempt in range(1, max_retries_per_account + 1):
                if attempt > 1:
                    print(f"[*] Account #{curr_idx + 1} retry {attempt}/{max_retries_per_account}...")

                cmd_up = [
                    "curl", "-sS",
                    "-A", self.user_agent,
                    "-H", "Expect:",
                    "--connect-timeout", "30",
                    "--max-time", str(upload_timeout),
                    "--http1.1",
                    "--tcp-nodelay",
                    "-w", "\n__HTTP_STATUS__:%{http_code}",
                    "-F", f"file=@{temp_file};filename={abyss_filename}",
                    upload_url
                ]

                try:
                    proc = subprocess.run(cmd_up, capture_output=True, text=True, timeout=upload_timeout + 60)
                except subprocess.TimeoutExpired:
                    print(f"[!] Curl upload timed out after {upload_timeout}s.")
                    continue
                except Exception as e:
                    print(f"[!] Curl upload execution exception: {e}")
                    continue

                raw_output = proc.stdout or ""
                http_code = 0

                # Extract __HTTP_STATUS__
                if "__HTTP_STATUS__:" in raw_output:
                    parts = raw_output.rsplit("__HTTP_STATUS__:", 1)
                    resp_text = parts[0].strip()
                    try:
                        http_code = int(parts[1].strip())
                    except Exception:
                        pass
                else:
                    resp_text = raw_output.strip()

                if proc.returncode != 0 and not resp_text:
                    err_msg = proc.stderr.strip()
                    print(f"[!] Abyss curl attempt {attempt} error (HTTP {http_code}): {err_msg}")
                    time.sleep(3)
                    continue

                # Check if upload succeeded with a slug
                slug = extract_slug_from_response(resp_text)
                if slug:
                    elapsed = time.time() - start_time
                    embed_url = f"https://player.abyssplayer.com/{slug}"
                    print(f"[SUCCESS] Uploaded to Abyss Account #{curr_idx + 1} ({masked}) in {elapsed:.1f}s!")
                    print(f"          Slug: {slug} | Embed: {embed_url}")
                    return {
                        "slug": slug,
                        "file_id": slug,
                        "embed_url": embed_url,
                        "account_index": curr_idx + 1,
                        "account_key_masked": masked
                    }

                # If no slug, inspect for quota or account limits
                is_quota, quota_reason = is_quota_or_account_error(resp_text, http_code)
                if is_quota:
                    self.mark_exhausted(curr_idx, f"{quota_reason} | HTTP {http_code} | Body: {resp_text[:200]}")
                    account_failed = True
                    break  # Break retry loop, immediately try next account in outer loop

                print(f"[!] Abyss attempt {attempt} missing slug: {resp_text[:200]} (HTTP {http_code})")
                time.sleep(3)

            if not account_failed:
                # If all retries failed on this account without quota keyword (e.g. repeated 500s or silent failures)
                self.mark_exhausted(curr_idx, f"Repeated upload failures without slug (Last HTTP {http_code})")

        print("[!] [AbyssPool] Upload failed: All accounts in pool are exhausted.", file=sys.stderr)
        return None
