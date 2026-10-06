import os
import sys
import json
import hashlib
import time
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import numpy as np
from utils.logger import logger
from utils.file_utils import get_temp_path, ensure_directories

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CACHE_BASE_DIR = os.path.join(WORKSPACE_DIR, "temp", "cache")
THUMBNAIL_CACHE_DIR = os.path.join(CACHE_BASE_DIR, "thumbnails")
WAVEFORM_CACHE_DIR = os.path.join(CACHE_BASE_DIR, "waveforms")
AI_TTS_CACHE_DIR = os.path.join(CACHE_BASE_DIR, "ai_tts")
PROXY_CACHE_DIR = os.path.join(CACHE_BASE_DIR, "proxies")

for d in (THUMBNAIL_CACHE_DIR, WAVEFORM_CACHE_DIR, AI_TTS_CACHE_DIR, PROXY_CACHE_DIR):
    os.makedirs(d, exist_ok=True)


class CacheService:
    """
    Centralized persistent multi-tier caching system for:
    1. Filmstrip video thumbnails (persisted to disk as JPEG)
    2. Real audio waveform peak amplitudes (persisted as JSON/Numpy)
    3. AI TTS audio segments (persisted as WAV)
    4. Lightweight preview proxy video streams
    """

    @staticmethod
    def get_file_identity(file_path: str) -> str:
        """Fast file fingerprint based on path, size, and mtime without full hashing."""
        try:
            stat = os.stat(file_path)
            raw = f"{os.path.abspath(file_path)}_{stat.st_size}_{stat.st_mtime_ns}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        except Exception:
            return hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:20]

    _ram_thumb_cache: Dict[str, bytes] = {}
    _ram_wave_cache: Dict[str, List[float]] = {}
    _RAM_CACHE_MAX_ITEMS = 256

    # ==================== THUMBNAIL CACHE ====================
    @classmethod
    def get_thumbnail_path(cls, file_path: str, timestamp_sec: float, thumb_h: int = 36) -> str:
        ident = cls.get_file_identity(file_path)
        t_key = f"{int(round(timestamp_sec, 2) * 100):06d}"
        filename = f"thumb_{ident}_{t_key}_{thumb_h}.jpg"
        return os.path.join(THUMBNAIL_CACHE_DIR, filename)

    @classmethod
    def load_cached_thumbnail(cls, file_path: str, timestamp_sec: float, thumb_h: int = 36) -> Optional[bytes]:
        key = f"{cls.get_file_identity(file_path)}_{round(timestamp_sec, 2)}_{thumb_h}"
        if key in cls._ram_thumb_cache:
            return cls._ram_thumb_cache[key]

        p = cls.get_thumbnail_path(file_path, timestamp_sec, thumb_h)
        if os.path.exists(p) and os.path.getsize(p) > 200:
            try:
                with open(p, "rb") as f:
                    data = f.read()
                    if len(cls._ram_thumb_cache) >= cls._RAM_CACHE_MAX_ITEMS:
                        cls._ram_thumb_cache.pop(next(iter(cls._ram_thumb_cache)))
                    cls._ram_thumb_cache[key] = data
                    return data
            except Exception:
                pass
        return None

    @classmethod
    def save_cached_thumbnail(cls, file_path: str, timestamp_sec: float, jpeg_bytes: bytes, thumb_h: int = 36):
        key = f"{cls.get_file_identity(file_path)}_{round(timestamp_sec, 2)}_{thumb_h}"
        if len(cls._ram_thumb_cache) >= cls._RAM_CACHE_MAX_ITEMS:
            cls._ram_thumb_cache.pop(next(iter(cls._ram_thumb_cache)))
        cls._ram_thumb_cache[key] = jpeg_bytes

        p = cls.get_thumbnail_path(file_path, timestamp_sec, thumb_h)
        try:
            with open(p, "wb") as f:
                f.write(jpeg_bytes)
        except Exception as e:
            logger.debug(f"Failed to save cached thumbnail: {e}")

    # ==================== WAVEFORM CACHE ====================
    @classmethod
    def get_waveform_cache_path(cls, file_path: str, samples_per_sec: int = 50) -> str:
        ident = cls.get_file_identity(file_path)
        return os.path.join(WAVEFORM_CACHE_DIR, f"wave_{ident}_{samples_per_sec}sps.json")

    @classmethod
    def load_cached_waveform(cls, file_path: str, samples_per_sec: int = 50) -> Optional[List[float]]:
        key = f"{cls.get_file_identity(file_path)}_{samples_per_sec}"
        if key in cls._ram_wave_cache:
            return cls._ram_wave_cache[key]

        p = cls.get_waveform_cache_path(file_path, samples_per_sec)
        if os.path.exists(p) and os.path.getsize(p) > 10:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        if len(cls._ram_wave_cache) >= 32:
                            cls._ram_wave_cache.pop(next(iter(cls._ram_wave_cache)))
                        cls._ram_wave_cache[key] = data
                        return data
            except Exception:
                pass
        return None

    @classmethod
    def save_cached_waveform(cls, file_path: str, peaks: List[float], samples_per_sec: int = 50):
        key = f"{cls.get_file_identity(file_path)}_{samples_per_sec}"
        if len(cls._ram_wave_cache) >= 32:
            cls._ram_wave_cache.pop(next(iter(cls._ram_wave_cache)))
        cls._ram_wave_cache[key] = peaks

        p = cls.get_waveform_cache_path(file_path, samples_per_sec)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(peaks, f)
        except Exception as e:
            logger.debug(f"Failed to save cached waveform: {e}")

    # ==================== CACHE MAINTENANCE ====================
    @classmethod
    def prune_cache(cls, max_size_mb: int = 500) -> int:
        """Prunes oldest cache files if total cache directory exceeds max_size_mb. Returns bytes freed."""
        freed = 0
        try:
            files_with_mtime = []
            total_bytes = 0
            for root, _, files in os.walk(CACHE_BASE_DIR):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        st = os.stat(fp)
                        files_with_mtime.append((st.st_mtime, st.st_size, fp))
                        total_bytes += st.st_size
                    except Exception:
                        pass

            max_bytes = max_size_mb * 1024 * 1024
            if total_bytes > max_bytes:
                # Sort oldest first
                files_with_mtime.sort(key=lambda x: x[0])
                for mtime, size, fp in files_with_mtime:
                    try:
                        os.remove(fp)
                        freed += size
                        total_bytes -= size
                        if total_bytes <= max_bytes * 0.8: # Prune down to 80% watermark
                            break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Cache pruning failed: {e}")
        return freed

    # ==================== AI TTS CACHE ====================
    @classmethod
    def get_tts_cache_key(cls, text: str, voice: str, pitch_rate: str = "") -> str:
        raw = f"{text.strip()}_{voice}_{pitch_rate}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def get_tts_cache_path(cls, text: str, voice: str, pitch_rate: str = "") -> str:
        key = cls.get_tts_cache_key(text, voice, pitch_rate)
        return os.path.join(AI_TTS_CACHE_DIR, f"tts_{key}.wav")

    # ==================== PROXY CACHE ====================
    @classmethod
    def get_proxy_path(cls, file_path: str, target_h: int = 540) -> str:
        ident = cls.get_file_identity(file_path)
        return os.path.join(PROXY_CACHE_DIR, f"proxy_{ident}_{target_h}p.mp4")

    @classmethod
    def generate_proxy(cls, file_path: str, target_h: int = 540) -> Optional[str]:
        """
        Generate lightweight preview proxy (default 540p) using ultrafast hardware downscaling.
        Returns the path to the proxy video if created or already cached.
        """
        if not file_path or not os.path.exists(file_path):
            return None
        proxy_path = cls.get_proxy_path(file_path, target_h)
        if os.path.exists(proxy_path) and os.path.getsize(proxy_path) > 1024:
            return proxy_path

        try:
            vf = f"scale=-2:{target_h}"
            v_codec = ["-c:v", "h264_videotoolbox", "-b:v", "1500k", "-color_range", "1"] if sys.platform == "darwin" else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"]
            cmd = [
                "ffmpeg", "-y", "-i", file_path,
                "-vf", vf,
                *v_codec,
                "-an",
                proxy_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(proxy_path) and os.path.getsize(proxy_path) > 1024:
                return proxy_path
        except Exception as e:
            logger.warning(f"Failed to generate proxy video: {e}")
        return None

