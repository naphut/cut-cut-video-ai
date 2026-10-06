import os
import shutil
import json
import tempfile
from datetime import datetime
from pathlib import Path
from utils.logger import logger

BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"

def ensure_directories():
    """Ensure temp and output directories exist."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "videos").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "audio").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "subtitles").mkdir(parents=True, exist_ok=True)

def get_output_subfolders(base_dir: str = None) -> list:
    """Return a list of existing subfolders inside the output directory."""
    ensure_directories()
    target_base = Path(base_dir) if base_dir and os.path.exists(base_dir) else OUTPUT_DIR
    subfolders = []
    try:
        for p in target_base.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                subfolders.append(p.name)
    except Exception:
        pass
    subfolders.sort(key=lambda s: s.lower())
    return subfolders

def parse_episode_from_filename(filename: str) -> tuple:
    """
    Parse a video filename to extract clean story title and episode number (Enpoin / ភាគ).
    Examples:
      - 'Download (3).mp4' -> ('Download', 3)
      - 'Naruto_Shippuden_EP12_1080p.mp4' -> ('Naruto Shippuden', 12)
      - 'safe_input_Bleach-04.mp4' -> ('Bleach', 4)
      - 'Drama_ភាគ_08.mkv' -> ('Drama', 8)
    """
    import re
    if not filename:
        return ("Story_Project", 1)

    stem = os.path.splitext(os.path.basename(filename))[0]
    # Remove system prefixes
    stem = re.sub(r'^(?:safe_input_|audio_|fast_export_|khmer_dub_|temp_)+', '', stem, flags=re.IGNORECASE)

    ep_num = 1
    # Match patterns like EP12, Episode 5, Part 3, ភាគ 04, Vol.2, E08
    ep_match = re.search(r'(?:(?:ep|episode|part|ភាគ|vol|volume|chapter)\s*[-_.]?\s*|(?<=[^a-zA-Z])e)(\d{1,4})', stem, flags=re.IGNORECASE)
    if ep_match:
        try:
            ep_num = int(ep_match.group(1))
            stem = stem[:ep_match.start()] + stem[ep_match.end():]
        except Exception:
            pass
    else:
        # Match parentheses like (3) or [12]
        paren_match = re.search(r'[\(\[]\s*(\d{1,4})\s*[\)\]]', stem)
        if paren_match:
            try:
                ep_num = int(paren_match.group(1))
                stem = stem[:paren_match.start()] + stem[paren_match.end():]
            except Exception:
                pass
        else:
            # Match trailing number like "Movie-04" or "Series_02"
            tail_num = re.search(r'[-_]\s*(\d{1,3})$', stem)
            if tail_num:
                try:
                    ep_num = int(tail_num.group(1))
                    stem = stem[:tail_num.start()]
                except Exception:
                    pass

    # Clean quality tags and leftover symbols
    stem = re.sub(r'(?:1080p|720p|480p|2160p|4k|x264|x265|hevc|web-?dl|bluray|h264|aac|mp4|mkv)', '', stem, flags=re.IGNORECASE)
    clean_title = re.sub(r'[-_.]+', ' ', stem).strip()
    if not clean_title:
        clean_title = "Story_Project"

    return (clean_title, max(1, ep_num))

def build_story_export_paths(folder_name: str, episode_num: int = 1, output_base_dir: str = None) -> dict:
    """
    Generate clean organized export paths for a story and episode.
    Creates folder structure: output/<folder_name>/ភាគ_<ep:02d>/
    """
    ensure_directories()
    clean_folder = str(folder_name or "Project").strip().replace("/", "_").replace("\\", "_")
    if not clean_folder:
        clean_folder = "Project"

    base = Path(output_base_dir) if output_base_dir and os.path.exists(output_base_dir) else OUTPUT_DIR
    ep_str = f"ភាគ_{int(episode_num):02d}"
    story_dir = base / clean_folder / ep_str
    story_dir.mkdir(parents=True, exist_ok=True)

    clean_stem = f"{clean_folder}_EP{int(episode_num):02d}"
    return {
        "story_dir": str(story_dir),
        "video_path": str(story_dir / f"{clean_stem}_KhmerDub.mp4"),
        "audio_path": str(story_dir / f"{clean_stem}_MasterVoice.wav"),
        "subtitles_path": str(story_dir / f"{clean_stem}_Subtitle.srt"),
    }

def get_temp_path(filename: str) -> str:
    """Return an absolute path inside the project temp directory."""
    ensure_directories()
    return str(TEMP_DIR / filename)

def get_output_path(filename: str) -> str:
    """Return an absolute path inside the project output directory."""
    ensure_directories()
    return str(OUTPUT_DIR / filename)

def generate_unique_filename(base_path: str, prefix: str = "khmer", subfolder: str = None, extension: str = None, custom_dir: str = None) -> str:
    """
    Generate developer-grade unique timestamped filename inside target output directory.
    If custom_dir is provided, saves directly inside custom_dir.
    """
    ensure_directories()
    if custom_dir and os.path.exists(custom_dir):
        target_dir = Path(custom_dir)
    elif subfolder:
        target_dir = OUTPUT_DIR / subfolder
    else:
        target_dir = OUTPUT_DIR
        
    target_dir.mkdir(parents=True, exist_ok=True)

    path_obj = Path(base_path)
    stem = path_obj.stem if path_obj.stem else "video"
    ext = extension if extension else (path_obj.suffix if path_obj.suffix else ".mp4")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{stem}_{timestamp}{ext}"
    output_path = target_dir / filename
    
    counter = 1
    while output_path.exists():
        filename = f"{prefix}_{stem}_{timestamp}_{counter:02d}{ext}"
        output_path = target_dir / filename
        counter += 1
        
    return str(output_path)

def clean_temp_directory():
    """Clean all temporary files generated during process."""
    if TEMP_DIR.exists():
        for item in TEMP_DIR.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            except Exception as e:
                print(f"Error cleaning {item}: {e}")

def save_json(data, filepath: str):
    """Save dictionary or list data to JSON file."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_json(filepath: str):
    """Load data from JSON file."""
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple, Set
import hashlib

class FileAccessStatus(str, Enum):
    ACCESSIBLE = "ACCESSIBLE"
    NOT_FOUND = "NOT_FOUND"
    ACCESS_DENIED = "ACCESS_DENIED"
    DIRECTORY = "DIRECTORY"
    EMPTY_FILE = "EMPTY_FILE"
    DECODE_ERROR = "DECODE_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

@dataclass
class FileAccessResult:
    status: FileAccessStatus
    message: str
    is_accessible: bool
    path: str
    error_detail: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

def get_user_friendly_file_access_message(result: FileAccessResult) -> Tuple[str, str]:
    """
    Format user-facing error dialog titles and messages for macOS & cross-platform:
    Never exposes raw Python tracebacks to the normal user.
    """
    path_display = result.path or ""
    if result.status == FileAccessStatus.ACCESS_DENIED:
        title = "Cannot Access Video File"
        msg = (
            "Cannot access this video file.\n\n"
            "macOS has restricted access to this location.\n\n"
            f"File: {path_display}\n\n"
            "Please allow Vide AI Studio / the application you used to launch it to access this folder in:\n\n"
            "System Settings\n"
            "→ Privacy & Security\n"
            "→ Files and Folders\n\n"
            "If the application is launched from Terminal, make sure Terminal has permission to access Desktop."
        )
        return title, msg
    elif result.status == FileAccessStatus.NOT_FOUND:
        title = "Video File Not Found"
        msg = (
            "Video file not found.\n\n"
            f"The selected file no longer exists:\n{path_display}"
        )
        return title, msg
    elif result.status == FileAccessStatus.DIRECTORY:
        title = "Invalid Video File"
        msg = (
            f"Cannot open a directory as a video file:\n{path_display}\n\n"
            "Please select a valid video file."
        )
        return title, msg
    elif result.status == FileAccessStatus.EMPTY_FILE:
        title = "Empty Video File"
        msg = (
            f"The selected file is empty (0 bytes):\n{path_display}\n\n"
            "Please select a valid video file."
        )
        return title, msg
    elif result.status == FileAccessStatus.DECODE_ERROR:
        title = "Cannot Open Video"
        msg = (
            "The video file could not be opened.\n\n"
            "The file may be inaccessible, corrupted, or encoded with an unsupported format."
        )
        return title, msg
    else:
        title = "Video Access Error"
        msg = f"Cannot open video file:\n{path_display}\n\n{result.message}"
        return title, msg

def check_file_access(file_path: str) -> FileAccessResult:
    """
    macOS & cross-platform media file access and decoding validator:
    Distinguishes:
      - NOT_FOUND: File does not exist on disk
      - ACCESS_DENIED: macOS TCC permission or filesystem EACCES/EPERM restriction
      - DIRECTORY: Path points to a directory, not a media file
      - EMPTY_FILE: File size is 0 bytes
      - DECODE_ERROR: File is accessible (File Access: PASS), but media decoding failed (Media Decode: FAIL)
      - ACCESSIBLE: File is accessible and valid media stream was decoded (OpenCV or FFmpeg)
      - UNKNOWN_ERROR: Unexpected OS error
    Never constructs unsafe shell command strings.
    """
    if not file_path:
        return FileAccessResult(
            status=FileAccessStatus.NOT_FOUND,
            message="No file path provided",
            is_accessible=False,
            path=""
        )

    logger.info(f"[MEDIA] Selected path: {file_path}")

    # 1. Directory check
    try:
        if os.path.isdir(file_path):
            logger.info(f"[MEDIA] Path exists: True")
            logger.warning(f"[MEDIA] Is file: False (Directory: {file_path})")
            return FileAccessResult(
                status=FileAccessStatus.DIRECTORY,
                message=f"Path is a directory, not a video file:\n{file_path}",
                is_accessible=False,
                path=file_path
            )
    except PermissionError as e:
        logger.info(f"[MEDIA] Path exists: True")
        logger.info(f"[MEDIA] Is file: True")
        logger.info(f"[MEDIA] Read access: False")
        logger.error(f"[MEDIA] Permission error: {e}")
        return FileAccessResult(
            status=FileAccessStatus.ACCESS_DENIED,
            message="macOS has restricted access to this location.",
            is_accessible=False,
            path=file_path,
            error_detail=str(e)
        )
    except OSError as e:
        import errno
        if e.errno in (errno.EACCES, errno.EPERM):
            logger.info(f"[MEDIA] Path exists: True")
            logger.info(f"[MEDIA] Is file: True")
            logger.info(f"[MEDIA] Read access: False")
            logger.error(f"[MEDIA] Permission error: {e}")
            return FileAccessResult(
                status=FileAccessStatus.ACCESS_DENIED,
                message="macOS has restricted access to this location.",
                is_accessible=False,
                path=file_path,
                error_detail=str(e)
            )

    # 2. Existence check
    path_exists = False
    try:
        path_exists = os.path.exists(file_path)
    except PermissionError as e:
        logger.info(f"[MEDIA] Path exists: True")
        logger.info(f"[MEDIA] Is file: True")
        logger.info(f"[MEDIA] Read access: False")
        logger.error(f"[MEDIA] Permission error: {e}")
        return FileAccessResult(
            status=FileAccessStatus.ACCESS_DENIED,
            message="macOS has restricted access to this location.",
            is_accessible=False,
            path=file_path,
            error_detail=str(e)
        )
    except OSError as e:
        import errno
        if e.errno in (errno.EACCES, errno.EPERM):
            logger.info(f"[MEDIA] Path exists: True")
            logger.info(f"[MEDIA] Is file: True")
            logger.info(f"[MEDIA] Read access: False")
            logger.error(f"[MEDIA] Permission error: {e}")
            return FileAccessResult(
                status=FileAccessStatus.ACCESS_DENIED,
                message="macOS has restricted access to this location.",
                is_accessible=False,
                path=file_path,
                error_detail=str(e)
            )

    logger.info(f"[MEDIA] Path exists: {path_exists}")
    if not path_exists:
        logger.warning(f"[MEDIA] File does not exist: {file_path}")
        return FileAccessResult(
            status=FileAccessStatus.NOT_FOUND,
            message=f"Video file not found.\n\nThe selected file no longer exists:\n{file_path}",
            is_accessible=False,
            path=file_path
        )

    # 3. Binary read permission check (most reliable across macOS TCC & sandboxes)
    is_file = True
    read_access = False
    perm_err_str = None
    try:
        with open(file_path, 'rb') as f_test:
            _ = f_test.read(64)
            read_access = True
    except PermissionError as e:
        perm_err_str = str(e)
    except OSError as e:
        import errno
        if e.errno in (errno.EACCES, errno.EPERM):
            perm_err_str = str(e)
        elif e.errno == errno.ENOENT:
            return FileAccessResult(
                status=FileAccessStatus.NOT_FOUND,
                message=f"Video file not found.\n\nThe selected file no longer exists:\n{file_path}",
                is_accessible=False,
                path=file_path
            )
        else:
            perm_err_str = str(e)

    logger.info(f"[MEDIA] Is file: {is_file}")
    logger.info(f"[MEDIA] Read access: {read_access}")
    if not read_access:
        # Resilient Workspace Fallback: Check if identical file exists in project media_inputs/
        fname = os.path.basename(file_path)
        workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        media_inputs_dir = os.path.join(workspace_dir, "media_inputs")
        local_cand = None
        if os.path.exists(media_inputs_dir):
            for root, dirs, files in os.walk(media_inputs_dir):
                if fname in files:
                    cand_path = os.path.join(root, fname)
                    try:
                        with open(cand_path, 'rb') as cf:
                            if len(cf.read(64)) > 0:
                                local_cand = cand_path
                                break
                    except Exception:
                        pass
        if local_cand:
            logger.info(f"🔄 [MEDIA] Auto-resolved permission-restricted '{file_path}' to accessible workspace copy: '{local_cand}'")
            return check_file_access(local_cand)

        logger.error(f"[MEDIA] Permission error: {perm_err_str}")
        return FileAccessResult(
            status=FileAccessStatus.ACCESS_DENIED,
            message="macOS has restricted access to this location.",
            is_accessible=False,
            path=file_path,
            error_detail=perm_err_str
        )

    # 4. Empty file check
    try:
        file_size = os.path.getsize(file_path)
    except Exception:
        file_size = 0

    if file_size <= 0:
        logger.warning(f"[MEDIA] Empty file detected (0 bytes): {file_path}")
        return FileAccessResult(
            status=FileAccessStatus.EMPTY_FILE,
            message=f"The selected file is empty (0 bytes):\n{file_path}",
            is_accessible=False,
            path=file_path,
            metadata={"size_bytes": 0}
        )

    metadata: Dict[str, Any] = {"size_bytes": file_size}

    # 5. Media Decode check (OpenCV probe first)
    cv2_opened = False
    try:
        import cv2
        cap = cv2.VideoCapture(file_path)
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            cap.release()
            if w > 0 and h > 0:
                cv2_opened = True
                dur = (frames / fps) if fps > 0 else 0.0
                metadata.update({
                    "width": w,
                    "height": h,
                    "fps": round(fps, 3),
                    "frames": frames,
                    "duration": round(dur, 3)
                })
    except Exception as e_cv:
        logger.debug(f"[MEDIA] OpenCV probe notice: {e_cv}")

    if cv2_opened:
        logger.info(f"[MEDIA] File Access: PASS | Media Decode: PASS (OpenCV {metadata.get('width')}x{metadata.get('height')})")
        return FileAccessResult(
            status=FileAccessStatus.ACCESSIBLE,
            message="Video file is accessible and decoded successfully",
            is_accessible=True,
            path=file_path,
            metadata=metadata
        )

    # Case E: OpenCV cannot open but FFmpeg can -> Safe argument list execution
    ffprobe_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,duration:format=duration,size,format_name",
        "-of", "json",
        file_path
    ]
    err_reason = ""
    try:
        import subprocess
        res = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        logger.info(f"[MEDIA] ffprobe result: code={res.returncode}")
        if res.returncode == 0:
            probe_data = json.loads(res.stdout)
            streams = probe_data.get("streams", [])
            vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
            if vstream and int(vstream.get("width", 0)) > 0:
                fmt = probe_data.get("format", {})
                metadata.update({
                    "width": int(vstream.get("width", 0)),
                    "height": int(vstream.get("height", 0)),
                    "codec": vstream.get("codec_name", ""),
                    "duration": float(fmt.get("duration", 0.0) or 0.0)
                })
                logger.info(f"[MEDIA] File Access: PASS | Media Decode: PASS (FFmpeg fallback {metadata.get('width')}x{metadata.get('height')})")
                return FileAccessResult(
                    status=FileAccessStatus.ACCESSIBLE,
                    message="Video file is accessible and decoded successfully via FFmpeg",
                    is_accessible=True,
                    path=file_path,
                    metadata=metadata
                )
            else:
                err_reason = "No valid video stream found in container"
        else:
            err_reason = res.stderr.strip() or "FFmpeg could not parse container"
    except Exception as e_probe:
        err_reason = str(e_probe)

    # Case F: FFmpeg cannot open but the file itself is accessible
    # Differentiate: File Access: PASS, Media Decode: FAIL (instead of Permission Denied!)
    logger.error(f"[MEDIA] File Access: PASS | Media Decode: FAIL: {err_reason}")
    return FileAccessResult(
        status=FileAccessStatus.DECODE_ERROR,
        message="The video file could not be opened.\n\nThe file may be inaccessible, corrupted, or encoded with an unsupported format.",
        is_accessible=False,
        path=file_path,
        error_detail=err_reason,
        metadata=metadata
    )

class MediaValidationStatus(str, Enum):
    OK = "OK"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    INVALID_MEDIA = "INVALID_MEDIA"
    NO_VIDEO_STREAM = "NO_VIDEO_STREAM"
    PROBE_FAILED = "PROBE_FAILED"

SUPPORTED_VIDEO_EXTENSIONS: Set[str] = {
    '.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.flv', '.wmv', '.ts'
}

@dataclass
class MediaValidationResult:
    status: MediaValidationStatus
    message: str
    is_valid: bool
    path: str
    metadata: Dict[str, Any]

def validate_media_file(file_path: str) -> MediaValidationResult:
    """
    Validate media file wrapping check_file_access for backwards compatibility.
    """
    acc = check_file_access(file_path)
    if acc.status == FileAccessStatus.ACCESSIBLE:
        return MediaValidationResult(
            status=MediaValidationStatus.OK,
            message=acc.message,
            is_valid=True,
            path=acc.path,
            metadata=acc.metadata
        )
    elif acc.status == FileAccessStatus.NOT_FOUND:
        return MediaValidationResult(
            status=MediaValidationStatus.FILE_NOT_FOUND,
            message=acc.message,
            is_valid=False,
            path=acc.path,
            metadata=acc.metadata
        )
    elif acc.status == FileAccessStatus.ACCESS_DENIED:
        return MediaValidationResult(
            status=MediaValidationStatus.PERMISSION_DENIED,
            message=acc.message,
            is_valid=False,
            path=acc.path,
            metadata=acc.metadata
        )
    elif acc.status == FileAccessStatus.DECODE_ERROR:
        return MediaValidationResult(
            status=MediaValidationStatus.PROBE_FAILED,
            message=acc.message,
            is_valid=False,
            path=acc.path,
            metadata=acc.metadata
        )
    else:
        return MediaValidationResult(
            status=MediaValidationStatus.INVALID_MEDIA,
            message=acc.message,
            is_valid=False,
            path=acc.path,
            metadata=acc.metadata
        )

def ensure_accessible_video_file(file_path: str, allow_copy: bool = True) -> str:
    """
    Ensure the video file is accessible for preview and processing:
    1. If file is not accessible, return file_path as-is without copying.
    2. If directly readable, return immediately with zero copy.
    3. If allow_copy=True, create a safe working APFS clone representation in temp/.
    """
    if not file_path:
        return file_path

    acc = check_file_access(file_path)
    if not acc.is_accessible:
        return file_path

    resolved = str(Path(file_path).resolve())

    # 1. Instant check: Can OpenCV read this file directly? (0ms instant access)
    try:
        import cv2
        test_cap = cv2.VideoCapture(resolved)
        if test_cap.isOpened():
            test_cap.release()
            return resolved
    except Exception:
        pass

    workspace_dir = str(Path(__file__).resolve().parent.parent)
    if resolved.startswith(workspace_dir):
        return resolved

    if not allow_copy:
        return resolved

    ensure_directories()
    # Collision-proof hash based on resolved path
    path_hash = hashlib.md5(resolved.encode('utf-8')).hexdigest()[:8]
    clean_name = Path(file_path).name
    safe_name = f"safe_input_{path_hash}_{clean_name}"
    safe_path = get_temp_path(safe_name)

    # 2. If safe_path already exists and matches size or is valid, use it
    if os.path.exists(safe_path) and os.path.getsize(safe_path) > 1000:
        try:
            if os.path.getsize(safe_path) == os.path.getsize(file_path):
                logger.info(f"✅ Using cached safe video: {safe_path}")
                return safe_path
        except Exception:
            return safe_path

    # 3. macOS APFS Instant Clone (Copy-on-Write, zero time & zero extra disk space)
    try:
        import subprocess
        res = subprocess.run(["cp", "-c", file_path, safe_path], capture_output=True)
        if res.returncode == 0 and os.path.exists(safe_path) and os.path.getsize(safe_path) > 1000:
            logger.info(f"✅ Instant APFS clone created: {safe_path}")
            return safe_path
    except Exception:
        pass

    # 4. Standard copy fallback (non-destructive to original)
    copied = False
    try:
        shutil.copyfile(file_path, safe_path)
        copied = True
    except Exception:
        try:
            with open(file_path, 'rb') as src_f, open(safe_path, 'wb') as dst_f:
                while True:
                    chunk = src_f.read(1024 * 1024)
                    if not chunk:
                        break
                    dst_f.write(chunk)
            copied = True
        except Exception:
            pass

    if copied and os.path.exists(safe_path) and os.path.getsize(safe_path) > 1000:
        logger.info(f"✅ Successfully prepared safe accessible video copy: {safe_path}")
        return safe_path

    # If copy failed but we already have an existing cached safe file, return it
    if os.path.exists(safe_path) and os.path.getsize(safe_path) > 1000:
        logger.info(f"Using existing cached safe video: {safe_path}")
        return safe_path

    logger.warning(f"Could not copy external video to safe path, using original: {file_path}")
    return resolved


def export_segments_to_srt(segments: list, filepath: str, text_key: str = "original_text") -> str:
    """
    Format and save a list of Segment objects or dictionaries as a standard UTF-8 SRT file.
    text_key can be 'original_text', 'khmer_text', or 'translated_text'.
    """
    import os
    from utils.gemini_parser import seconds_to_srt_time
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    lines = []
    for idx, s in enumerate(segments, 1):
        st = s.start if hasattr(s, 'start') else float(s.get('start', 0.0) if isinstance(s, dict) else 0.0)
        et = s.end if hasattr(s, 'end') else float(s.get('end', st + 1.0) if isinstance(s, dict) else st + 1.0)
        st_str = seconds_to_srt_time(st)
        et_str = seconds_to_srt_time(et)

        txt = ""
        if hasattr(s, text_key):
            txt = getattr(s, text_key, "")
        elif isinstance(s, dict):
            txt = s.get(text_key) or (s.get("text", "") if text_key == "original_text" else "")
        if not txt and text_key == "original_text":
            txt = getattr(s, 'text', '') if hasattr(s, 'text') else (s.get('text', '') if isinstance(s, dict) else '')
        txt = str(txt or "").strip()

        lines.append(f"{idx}\n{st_str} --> {et_str}\n{txt}\n")

    content = "\n".join(lines)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"📄 [SRT Export] Saved {len(segments)} segments to: {filepath}")
    return filepath

