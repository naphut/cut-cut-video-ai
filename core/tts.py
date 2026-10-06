import re
import os
import wave
import time
import hashlib
import concurrent.futures
from enum import Enum
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Callable, Tuple, Any
from services.voxcpm_service import VoxCPMService, VOICE_PRESETS, EMOTION_PRESETS, STYLE_PRESETS
from core.models import Segment
from utils.file_utils import get_temp_path
from utils.logger import logger


class TTSStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCESS = "success"
    FAILED = "failed"


def validate_audio_file(file_path: Optional[str], min_size_bytes: int = 1000, min_duration_sec: float = 0.05) -> Tuple[bool, str]:
    """
    Validate that:
    1. File path is provided and exists on disk.
    2. File size >= min_size_bytes.
    3. Audio file is readable with a valid audio header (WAV or decoding with fallback).
    4. Audio duration >= min_duration_sec.
    Returns (is_valid: bool, reason: str).
    """
    if not file_path:
        return False, "File path is None or empty"
    if not os.path.exists(file_path):
        return False, f"File does not exist: {file_path}"
    
    file_size = os.path.getsize(file_path)
    if file_size < min_size_bytes:
        return False, f"File size too small ({file_size} bytes < {min_size_bytes} bytes)"

    # Try reading as WAV first
    try:
        with wave.open(file_path, 'rb') as wf:
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            if framerate <= 0:
                return False, f"Invalid framerate ({framerate})"
            dur = nframes / float(framerate)
            if dur < min_duration_sec:
                return False, f"Audio duration too short ({dur:.3f}s < {min_duration_sec}s)"
            return True, f"Valid WAV ({dur:.2f}s, {file_size:,} bytes)"
    except Exception as e_wav:
        # Fallback decoding via pydub if header differs
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(file_path)
            dur = len(audio) / 1000.0
            if dur < min_duration_sec:
                return False, f"Audio duration too short ({dur:.3f}s < {min_duration_sec}s)"
            return True, f"Valid Audio ({dur:.2f}s, {file_size:,} bytes)"
        except Exception as e_decode:
            return False, f"Cannot decode audio file: {e_decode} (WAV error: {e_wav})"


def verify_production_gate(
    whisper_count: int,
    translation_count: int,
    tts_success_count: int,
    dialoguesync_count: int,
    invalid_timestamp_count: int,
    unresolved_tts_ids: Optional[List[str]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Enforces Production Gate Check:
    Production can only report SUCCESS if:
      whisper_count == translation_count == tts_success_count == dialoguesync_count
      invalid_timestamp_count == 0
      unresolved_tts_count == 0
    """
    unresolved_tts_ids = list(unresolved_tts_ids or [])
    unresolved_tts_count = len(unresolved_tts_ids)

    is_success = (
        whisper_count > 0 and
        whisper_count == translation_count and
        translation_count == tts_success_count and
        tts_success_count == dialoguesync_count and
        invalid_timestamp_count == 0 and
        unresolved_tts_count == 0
    )
    status_str = "SUCCESS" if is_success else "FAILED / INCOMPLETE"
    summary = {
        "whisper_count": whisper_count,
        "translation_count": translation_count,
        "tts_success_count": tts_success_count,
        "dialoguesync_count": dialoguesync_count,
        "invalid_timestamp_count": invalid_timestamp_count,
        "unresolved_tts_count": unresolved_tts_count,
        "unresolved_tts_ids": unresolved_tts_ids,
        "production_status": status_str
    }
    return is_success, status_str, summary


class BaseTTSEngine(ABC):
    """Abstract Base Class for Text-to-Speech Engines."""
    @abstractmethod
    def synthesize(self, text: str, output_path: str, voice_name: str, target_duration: Optional[float] = None, emotion: str = "😐 Neutral", style: str = "Normal") -> bool:
        """Synthesize text into WAV at output_path."""
        pass


class VoxCPMEngine(BaseTTSEngine):
    """Engine using VoxCPM2 Zero-Shot Neural Voice Cloning & Stock Presets with Emotion/Style."""
    def synthesize(self, text: str, output_path: str, voice_name: str, target_duration: Optional[float] = None, emotion: str = "😐 Neutral", style: str = "Normal") -> bool:
        service = VoxCPMService(voice_name=voice_name)
        return service.synthesize(text, output_path, target_duration=target_duration, emotion=emotion, style=style)


class EdgeTTSEngine(BaseTTSEngine):
    """Engine using Microsoft Neural Edge-TTS directly with Emotion/Style."""
    def synthesize(self, text: str, output_path: str, voice_name: str, target_duration: Optional[float] = None, emotion: str = "😐 Neutral", style: str = "Normal") -> bool:
        service = VoxCPMService(voice_name=voice_name)
        return service.synthesize(text, output_path, target_duration=target_duration, emotion=emotion, style=style)


class TextToSpeech:
    """
    High-level Production TTS Orchestrator supporting:
    - Smart incremental caching via Content-Hash (preserves 100% existing valid cache)
    - Deterministic 3-attempt exponential backoff retry for transient network/socket drops
    - Explicit state tracking: pending -> running -> retry -> success / failed
    - Strict audio file validation (duration > 0, size > 0, valid WAV/audio header)
    - Fallback handling for foreign script or voice candidate failures
    - Bounded concurrency worker pool with sequential recovery pass
    """
    def __init__(self, voice_name: str = "Khmer Male - Piseth", engine_type: str = "voxcpm", max_retries: int = 3, enable_fallback: bool = True):
        self.default_voice = voice_name
        self.engine_type = engine_type
        self.max_retries = max(1, max_retries)
        self.enable_fallback = enable_fallback
        if engine_type == "edge":
            self.engine: BaseTTSEngine = EdgeTTSEngine()
        else:
            self.engine = VoxCPMEngine()

    def generate_segment_audio(self, segment: dict, index: int, force_regenerate: bool = False) -> Optional[str]:
        """
        Synthesize audio for a single segment with deterministic retry, cache validation,
        and fallback handling.
        """
        seg_id = segment.get("id") or f"seg_{index+1:04d}"
        segment["tts_status"] = TTSStatus.RUNNING.value
        segment["tts_retries"] = 0

        raw_text = segment.get("khmer_text") or segment.get("translated_text") or segment.get("original_text") or segment.get("text", "")
        # Clean any leading speaker tags like [ក្មេង], [ប្រុស], [ស្រី], [ចាស់ប្រុស], [ចាស់ស្រី], (ក្មេង), etc. so TTS never speaks brackets/tags
        from services.khmer_frontend import strip_speaker_tags
        text = strip_speaker_tags(raw_text)

        # TTS Responsibility: Only normalize harmless formatting (speaker labels, brackets, whitespace).
        # Semantic translation MUST be validated and finalized at the Translation Layer prior to TTS.
        if re.search(r'[\u0E00-\u0E7F\u4E00-\u9FFF]', text):
            logger.warning(
                f"⚠️ [TTS Foreign Script Notice] {seg_id}: Text contains foreign characters. "
                f"Ensure Translation Validation Layer is executed before TTS to guarantee subtitle/audio consistency."
            )

        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start + 2.5))
        target_duration = max(0.5, round(end - start, 2))
        
        # Use voice from segment if available, otherwise infer from persona or gender
        voice_name = segment.get("voice_id") or segment.get("voice")
        persona = segment.get("persona", "")
        gender = (segment.get("gender") or "").lower()

        from core.models import PERSONA_DEFAULT_VOICE
        if not voice_name or voice_name not in VOICE_PRESETS or (voice_name == self.default_voice and (persona or gender)):
            if persona in PERSONA_DEFAULT_VOICE:
                voice_name = PERSONA_DEFAULT_VOICE[persona]
            elif gender in ("female", "ស្រី"):
                voice_name = "Khmer Female - Sreymom"
            elif gender in ("child", "ក្មេង"):
                voice_name = "Khmer Child - Boy (Vannak)"
            elif gender in ("elder_female", "ចាស់ស្រី"):
                voice_name = "Khmer Elder - Female (Grandmother)"
            elif gender in ("elder_male", "ចាស់ប្រុស", "elder", "ចាស់", "មនុស្សចាស់"):
                voice_name = "Khmer Elder - Male (Grandfather)"
            elif voice_name not in VOICE_PRESETS:
                voice_name = self.default_voice

        emotion = segment.get("emotion") or "😐 Neutral"
        style = segment.get("speaking_style") or segment.get("style") or "Normal"
        speed = str(segment.get("speed") or segment.get("rate") or "1.0")
        pitch = str(segment.get("pitch") or "0Hz")
        language = str(segment.get("language") or "km")
        model_name = str(getattr(self.engine, 'model_name', 'edge_tts'))

        # Content hash for smart incremental caching with complete parameter coverage
        hash_seed = f"{text}|{voice_name}|{language}|{speed}|{pitch}|{emotion}|{style}|{model_name}|{target_duration}"
        cache_hash = hashlib.md5(hash_seed.encode("utf-8")).hexdigest()[:16]
        cached_wav = get_temp_path(f"tts_cache_{cache_hash}.wav")

        # ⚡ 1. CACHE VALIDATION: reuse existing audio without calling network!
        if not force_regenerate and os.path.exists(cached_wav):
            is_valid, reason = validate_audio_file(cached_wav)
            if is_valid:
                logger.debug(f"⚡ [TTS Cache Hit] Seg {index+1} ({seg_id}): Using cached voice ({cache_hash}) - {reason}")
                segment["tts_audio"] = cached_wav
                segment["audio_hash"] = cache_hash
                segment["tts_status"] = TTSStatus.SUCCESS.value
                return cached_wav
            else:
                logger.warning(f"⚠️ [TTS Cache Stale/Corrupt] Seg {index+1} ({seg_id}): Corrupt cache ({reason}). Regenerating...")
                try:
                    os.remove(cached_wav)
                except Exception:
                    pass

        # Non-vocal / pure punctuation handler: generates clean silence instead of failing network TTS
        if not re.search(r'[\u1780-\u17FF\u4E00-\u9FFFA-Za-z0-9]', text):
            logger.info(f"🔇 [TTS Non-Vocal/Pause] Seg {index+1} ({seg_id}): Subtitle has no spoken letters ('{text}'). Generating valid silent audio.")
            try:
                from services.voxcpm_service import VoxCPMService
                VoxCPMService()._generate_silent_wav(cached_wav, duration=target_duration)
                segment["tts_audio"] = cached_wav
                segment["audio_hash"] = cache_hash
                segment["tts_status"] = TTSStatus.SUCCESS.value
                return cached_wav
            except Exception as e_sil:
                logger.warning(f"Notice creating silence for {seg_id}: {e_sil}")

        # 2. DETERMINISTIC 3-ATTEMPT RETRY LOOP WITH EXPONENTIAL BACKOFF
        backoff_delays = [0.0, 1.0, 2.5]
        last_error_reason = ""

        for attempt in range(1, self.max_retries + 1):
            segment["tts_retries"] = attempt - 1
            if attempt > 1:
                segment["tts_status"] = TTSStatus.RETRY.value
                delay = backoff_delays[min(attempt - 1, len(backoff_delays) - 1)]
                logger.warning(f"🔄 [TTS Retry {attempt}/{self.max_retries}] Seg {index+1} ({seg_id}) in {delay:.1f}s... Reason: {last_error_reason}")
                time.sleep(delay)

            try:
                logger.info(f"🎙️ [TTS Gen] Seg {index+1} [{start:.2f}s -> {end:.2f}s] ({voice_name} | {emotion} | {style}): '{text[:30]}...'")
                success = self.engine.synthesize(
                    text=text,
                    output_path=cached_wav,
                    voice_name=voice_name,
                    target_duration=target_duration,
                    emotion=emotion,
                    style=style
                )

                if success:
                    is_valid, reason = validate_audio_file(cached_wav)
                    if is_valid:
                        segment["tts_audio"] = cached_wav
                        segment["audio_hash"] = cache_hash
                        segment["tts_status"] = TTSStatus.SUCCESS.value
                        return cached_wav
                    else:
                        last_error_reason = f"Synthesizer returned success but audio validation failed: {reason}"
                else:
                    last_error_reason = "Synthesizer returned False"

            except Exception as e_synth:
                last_error_reason = f"Exception: {e_synth}"
                logger.warning(f"⚠️ [TTS Exception] Seg {index+1} ({seg_id}) attempt {attempt}: {e_synth}")

        # 3. FALLBACK ENGINE: If 3 retries failed, attempt fallback voice or phonetic normalization
        if self.enable_fallback:
            logger.warning(f"⚠️ [TTS Fallback Triggered] Seg {index+1} ({seg_id}): Attempting fallback voice & phonetic synthesis...")
            fallback_voices = ["km-KH-PisethNeural", "km-KH-SreymomNeural"]
            for fb_voice in fallback_voices:
                try:
                    fb_svc = VoxCPMService(voice_name="Khmer Male - Piseth" if "Piseth" in fb_voice else "Khmer Female - Sreymom")
                    fb_ok = fb_svc.synthesize(
                        text=text,
                        output_wav_path=cached_wav,
                        target_duration=target_duration,
                        emotion=emotion,
                        style=style
                    )
                    if fb_ok:
                        is_valid, reason = validate_audio_file(cached_wav)
                        if is_valid:
                            logger.info(f"✅ [TTS Fallback Success] Seg {index+1} ({seg_id}) recovered with fallback voice: {reason}")
                            segment["tts_audio"] = cached_wav
                            segment["audio_hash"] = cache_hash
                            segment["tts_status"] = TTSStatus.SUCCESS.value
                            return cached_wav
                except Exception as e_fb:
                    logger.debug(f"Fallback attempt notice: {e_fb}")

        # 4. HARD FAILURE: Mark as failed and do not silently proceed
        segment["tts_status"] = TTSStatus.FAILED.value
        segment["tts_audio"] = None
        logger.error(f"❌ [TTS FAILED] Seg {index+1} ({seg_id}) exhausted all {self.max_retries} attempts and fallbacks! Last error: {last_error_reason}")
        return None

    def synthesize_single_line(self, segment: dict, index: int = 0) -> Optional[str]:
        """Synthesize a single line immediately with force_regenerate for 1️⃣ Line Preview."""
        return self.generate_segment_audio(segment, index, force_regenerate=True)

    def generate_all_segments(
        self,
        segments: List[dict],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        max_workers: int = 2,
        is_cancelled_fn: Optional[Callable[[], bool]] = None
    ) -> List[Optional[str]]:
        """
        Synthesize audio for all segments with:
        - Bounded concurrency (default max_workers=2) to avoid socket disconnects
        - Smart cache verification (instant return for existing valid audio)
        - Automatic sequential recovery pass for any segment that failed in the parallel pool
        - Explicit status verification for every segment
        """
        total = len(segments)
        if total == 0:
            return []

        # Bounded concurrency: Keep between 1 and 3 to ensure network reliability
        bounded_workers = max(1, min(3, max_workers))
        results: List[Optional[str]] = [None] * total
        completed_count = 0

        logger.info(f"🚀 Starting parallel TTS synthesis for {total} segments (workers={bounded_workers})...")

        # Initialize all segments with PENDING status
        for s in segments:
            if not s.get("tts_status"):
                s["tts_status"] = TTSStatus.PENDING.value

        def _worker_task(idx: int, seg: dict):
            if is_cancelled_fn and is_cancelled_fn():
                return idx, None
            wav_path = self.generate_segment_audio(seg, idx, force_regenerate=False)
            return idx, wav_path

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=bounded_workers)
        future_to_idx = {}
        try:
            for i, seg in enumerate(segments):
                if is_cancelled_fn and is_cancelled_fn():
                    break
                fut = executor.submit(_worker_task, i, seg)
                future_to_idx[fut] = i

            for future in concurrent.futures.as_completed(future_to_idx):
                if is_cancelled_fn and is_cancelled_fn():
                    logger.warning("🛑 TTS parallel synthesis cancelled by user.")
                    for fut in future_to_idx:
                        fut.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    return results

                try:
                    idx, wav_path = future.result()
                    results[idx] = wav_path
                except Exception as e:
                    logger.error(f"Error synthesizing segment: {e}")

                completed_count += 1
                if progress_callback:
                    progress_callback(completed_count, total, f"Segment {completed_count}/{total}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # -----------------------------------------------------------------
        # SEQUENTIAL RECOVERY PASS FOR TRANSIENT POOL FAILURES
        # -----------------------------------------------------------------
        unresolved_indices = [
            i for i, p in enumerate(results)
            if not p or not validate_audio_file(p)[0]
        ]

        if unresolved_indices and not (is_cancelled_fn and is_cancelled_fn()):
            logger.warning(
                f"⚠️ [TTS Recovery Pass] {len(unresolved_indices)} segments failed during parallel worker pool. "
                f"Executing deterministic sequential recovery pass..."
            )
            for idx in unresolved_indices:
                if is_cancelled_fn and is_cancelled_fn():
                    break
                seg = segments[idx]
                logger.info(f"🔄 [Sequential Recovery] Retrying Seg {idx+1} (ID: {seg.get('id')})...")
                recovered_wav = self.generate_segment_audio(seg, idx, force_regenerate=True)
                results[idx] = recovered_wav

        # Audit final results
        final_success_count = sum(1 for p in results if p and validate_audio_file(p)[0])
        failed_count = total - final_success_count
        if failed_count > 0:
            failed_ids = [
                segments[i].get("id", f"idx_{i+1}")
                for i, p in enumerate(results)
                if not p or not validate_audio_file(p)[0]
            ]
            logger.error(f"❌ [TTS Incomplete] {failed_count}/{total} segments failed TTS generation: {failed_ids}")
        else:
            logger.info(f"✅ [TTS Complete] All {total}/{total} segments successfully verified with valid audio.")

        return results
