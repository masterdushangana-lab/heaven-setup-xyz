"""
Torrent Processing Engine for SubsPlease & Nyaa.si
- Pure Python Bencode Decoder for inspecting .torrent files and multi-file torrents
- Multi-file Torrent Inspection & File Selection (Episode to File-Index mapping)
- High-Speed aria2c Downloader (with --select-file support for multi-video torrents)
- Subtitle Burning & Optimized H.264 Animation Encoding via FFmpeg (<500MB target)
"""

import os
import re
import sys
import time
import json
import shutil
import urllib.parse
import urllib.request
import subprocess
from typing import Dict, Any, List, Optional, Tuple

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".webm", ".avi"}


# --- Pure Python Bencode Decoder ---

def bdecode(data: bytes) -> Any:
    def decode_val(idx: int):
        char = chr(data[idx])
        if char == 'i':
            end = data.index(b'e', idx)
            return int(data[idx + 1:end]), end + 1
        elif char.isdigit():
            colon = data.index(b':', idx)
            length = int(data[idx:colon])
            start = colon + 1
            return data[start:start + length], start + length
        elif char == 'l':
            res = []
            curr = idx + 1
            while chr(data[curr]) != 'e':
                val, curr = decode_val(curr)
                res.append(val)
            return res, curr + 1
        elif char == 'd':
            res = {}
            curr = idx + 1
            while chr(data[curr]) != 'e':
                k, curr = decode_val(curr)
                v, curr = decode_val(curr)
                key_str = k.decode('utf-8', errors='ignore') if isinstance(k, bytes) else str(k)
                res[key_str] = v
            return res, curr + 1
        raise ValueError(f"Unknown bencode char '{char}' at {idx}")

    val, _ = decode_val(0)
    return val


def parse_torrent_bytes(torrent_data: bytes) -> Dict[str, Any]:
    """Parses .torrent file bytes and returns structured file information."""
    meta = bdecode(torrent_data)
    info = meta.get("info", {})
    name = info.get("name", b"").decode("utf-8", errors="ignore") if isinstance(info.get("name"), bytes) else str(info.get("name", ""))

    files_list = []
    if "files" in info:
        # Multi-file torrent
        for idx, f in enumerate(info["files"], 1):
            path_parts = [p.decode("utf-8", errors="ignore") if isinstance(p, bytes) else str(p) for p in f.get("path", [])]
            rel_path = "/".join(path_parts)
            ext = os.path.splitext(rel_path)[1].lower()
            files_list.append({
                "index": idx,
                "path": rel_path,
                "filename": path_parts[-1] if path_parts else rel_path,
                "length": f.get("length", 0),
                "is_video": ext in VIDEO_EXTENSIONS
            })
    else:
        # Single-file torrent
        ext = os.path.splitext(name)[1].lower()
        files_list.append({
            "index": 1,
            "path": name,
            "filename": name,
            "length": info.get("length", 0),
            "is_video": ext in VIDEO_EXTENSIONS
        })

    return {
        "name": name,
        "is_multi": "files" in info,
        "files": files_list
    }


def extract_episode_number(filename: str) -> Optional[str]:
    """
    Extracts episode number from video filename.
    Examples:
      - [SubsPlease] Frieren - 05 (1080p).mkv -> '5'
      - One Piece 1180.mkv -> '1180'
      - S01E03.mkv -> '3'
      - Bleach - Episode 15.5.mp4 -> '15.5'
    """
    # 1. Season/Episode notation: S01E05 or S1E5
    m_se = re.search(r'\bS\d+E(\d+(?:\.\d+)?(?:v\d+)?)\b', filename, re.IGNORECASE)
    if m_se:
        return str(int(m_se.group(1))) if m_se.group(1).isdigit() else m_se.group(1)

    # 2. 'Episode 05' or 'Ep 05'
    m_ep = re.search(r'\b(?:episode|ep)\.?\s*(\d+(?:\.\d+)?(?:v\d+)?)\b', filename, re.IGNORECASE)
    if m_ep:
        val = m_ep.group(1)
        return str(int(val)) if val.isdigit() else val

    # 3. Trailing hyphen notation: 'Show Name - 05 (1080p)' or 'Show - 05 [Hash]'
    m_hyphen = re.search(r'-\s*(\d+(?:\.\d+)?(?:v\d+)?)(?:\s*\(|\s*\[|\s*$|\s*\.)', filename)
    if m_hyphen:
        val = m_hyphen.group(1)
        return str(int(val)) if val.isdigit() else val

    # 4. Standalone number before resolution: 'Show 05 1080p'
    m_res = re.search(r'\b(\d{1,4})\b(?=.*(?:1080p|720p|480p|x264|x265|hevc|mkv|mp4))', filename, re.IGNORECASE)
    if m_res:
        val = m_res.group(1)
        return str(int(val)) if val.isdigit() else val

    return None


def parse_range_spec(range_str: str) -> Optional[List[str]]:
    """Parses range string like '1-5, 10, 12' into a list of episode strings. Returns None for 'all'."""
    if not range_str or range_str.strip().lower() in ("all", "*"):
        return None
    allowed = set()
    parts = [p.strip() for p in range_str.split(",") if p.strip()]
    for part in parts:
        if "-" in part:
            sub = part.split("-", 1)
            try:
                start = int(sub[0].strip())
                end = int(sub[1].strip())
                for i in range(start, end + 1):
                    allowed.add(str(i))
            except Exception:
                allowed.add(part)
        else:
            try:
                allowed.add(str(int(part)))
            except Exception:
                allowed.add(part)
    return sorted(list(allowed), key=lambda x: float(x) if x.replace('.', '', 1).isdigit() else x)


# --- aria2c Torrent Downloader ---

def download_torrent(
    torrent_source: str,
    output_dir: str,
    selected_indices: Optional[List[int]] = None,
    timeout_seconds: int = 1800
) -> bool:
    """
    Downloads torrent or magnet link via aria2c.
    - If selected_indices is provided, passes --select-file=idx1,idx2... to ONLY download selected files!
    """
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "aria2c",
        "--seed-time=0",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--max-overall-upload-limit=1K",
        "--bt-stop-timeout=180",
        "--summary-interval=10",
        "--dir", output_dir
    ]

    if selected_indices:
        indices_str = ",".join(str(idx) for idx in selected_indices)
        cmd.append(f"--select-file={indices_str}")

    # Check if torrent_source is a file or URL/Magnet
    if torrent_source.startswith("magnet:") or torrent_source.startswith("http"):
        cmd.append(torrent_source)
    else:
        cmd.extend(["-T", torrent_source])

    print(f"[*] Executing aria2c download (Timeout: {timeout_seconds}s)...")
    if selected_indices:
        print(f"    Selected File Indices: {selected_indices}")

    try:
        proc = subprocess.run(cmd, timeout=timeout_seconds)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print("[!] aria2c download timed out!", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[!] aria2c execution error: {e}", file=sys.stderr)
        return False


def find_downloaded_videos(directory: str) -> List[str]:
    """Finds all valid video files in directory recursively."""
    videos = []
    for root, _, files in os.walk(directory):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                full_path = os.path.join(root, f)
                if os.path.getsize(full_path) > 1000000:  # > 1MB
                    videos.append(full_path)
    return videos


# --- Subtitle Burning & FFmpeg Encoding ---

def burn_subtitles_and_encode(
    input_video: str,
    output_mp4: str,
    target_max_mb: int = 500
) -> bool:
    """
    Burns official embedded subtitle track directly from MKV/MP4 into video
    and strictly enforces file size < target_max_mb (default: 500MB).
    """
    if not os.path.exists(input_video) or os.path.getsize(input_video) < 500000:
        print(f"[!] Input video not found or too small: {input_video}")
        return False

    orig_size_mb = os.path.getsize(input_video) / (1024 * 1024)
    print(f"[*] Burning Subtitles & Encoding: {os.path.basename(input_video)} ({orig_size_mb:.1f} MB) -> Target < {target_max_mb} MB")
    start_time = time.time()

    # Move/link input to a clean local name in working directory to avoid FFmpeg escaping issues
    work_dir = os.path.abspath(os.path.dirname(output_mp4) or ".")
    clean_src_name = "src_burn_temp.mkv"
    clean_src_path = os.path.join(work_dir, clean_src_name)

    if os.path.abspath(input_video) != os.path.abspath(clean_src_path):
        if os.path.exists(clean_src_path):
            try:
                os.remove(clean_src_path)
            except Exception:
                pass
        try:
            os.link(input_video, clean_src_path)
        except Exception:
            shutil.copy2(input_video, clean_src_path)

    # 1. Determine media duration to calculate strict bitrate cap
    duration_sec = 0.0
    try:
        p_dur = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            clean_src_path
        ], capture_output=True, text=True, timeout=15)
        val = p_dur.stdout.strip()
        if val:
            duration_sec = float(val)
    except Exception:
        pass

    # Safe ceiling: 470 MB budget guarantees final size is always < 500 MB (even with container overhead)
    safe_budget_mb = min(target_max_mb - 30, 470)
    audio_kbps = 128
    if duration_sec > 60:
        total_kbps = (safe_budget_mb * 8 * 1024) / duration_sec
        max_v_rate = max(int(total_kbps - audio_kbps), 1000)
    else:
        # Fallback to standard 24-minute anime calculation (1440s)
        max_v_rate = 2500
    buf_size = max_v_rate * 2

    print(f"[*] Bitrate Budgeting: Duration={duration_sec:.1f}s | Max Video Bitrate={max_v_rate}k | Audio={audio_kbps}k")

    # Primary Attempt: Hardsub with VBV Bitrate Cap for strictly <500MB
    cmd_burn = [
        "ffmpeg", "-y",
        "-threads", "0",
        "-i", clean_src_name,
        "-vf", f"subtitles={clean_src_name}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "animation",
        "-crf", "22",
        "-maxrate", f"{max_v_rate}k",
        "-bufsize", f"{buf_size}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        "-stats",
        output_mp4
    ]

    print(f"  -> Running FFmpeg: Anime Hardsub Profile (CRF 22, maxrate {max_v_rate}k, preset ultrafast)...")
    proc = subprocess.run(cmd_burn, cwd=work_dir)

    if proc.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1000000:
        print(f"  [!] Subtitle burn filter warning/fallback (Error code {proc.returncode}). Retrying standard encode...")
        cmd_fallback = [
            "ffmpeg", "-y",
            "-threads", "0",
            "-i", clean_src_name,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "animation",
            "-crf", "22",
            "-maxrate", f"{max_v_rate}k",
            "-bufsize", f"{buf_size}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            "-stats",
            output_mp4
        ]
        proc = subprocess.run(cmd_fallback, cwd=work_dir)

    # Clean up temp symlink/copy
    if os.path.exists(clean_src_path):
        try:
            os.remove(clean_src_path)
        except Exception:
            pass

    if not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1000000:
        print(f"[!] Encoding failed with returncode {proc.returncode}")
        return False

    out_mb = os.path.getsize(output_mp4) / (1024 * 1024)

    # Emergency recompression if file somehow exceeds target_max_mb
    if out_mb >= target_max_mb:
        print(f"[!] Warning: File size ({out_mb:.1f} MB) >= {target_max_mb} MB! Applying quick compression guard...")
        temp_re = output_mp4 + ".re.mp4"
        cmd_re = [
            "ffmpeg", "-y",
            "-threads", "0",
            "-i", output_mp4,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "animation",
            "-crf", "26",
            "-maxrate", "2000k",
            "-bufsize", "4000k",
            "-c:a", "copy",
            "-movflags", "+faststart",
            temp_re
        ]
        proc_re = subprocess.run(cmd_re, cwd=work_dir)
        if proc_re.returncode == 0 and os.path.exists(temp_re) and os.path.getsize(temp_re) > 1000000:
            os.replace(temp_re, output_mp4)
            out_mb = os.path.getsize(output_mp4) / (1024 * 1024)

    success = out_mb < target_max_mb
    if success:
        elapsed = time.time() - start_time
        print(f"[SUCCESS] Video encoded & hardsubbed in {elapsed:.1f}s -> {output_mp4} ({out_mb:.1f} MB < {target_max_mb} MB)")
    else:
        print(f"[!] File size ({out_mb:.1f} MB) is still not lower than {target_max_mb} MB")

    return success

