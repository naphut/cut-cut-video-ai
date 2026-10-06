"""
Timeline Audio Service
Builds unified, seamless composite audio tracks for the complete multi-clip timeline sequence (CapCut NLE Architecture).
Ensures 100% accurate global timecode alignment for STT (Whisper / Gemini AI), voice separation, and audio preview.
"""
import os
import hashlib
import subprocess
from typing import List, Dict, Tuple, Optional, Any
from utils.logger import logger
from utils.file_utils import get_temp_path


def has_audio_stream(file_path: str) -> bool:
    """Check if media file contains at least one readable audio stream."""
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return bool(res.stdout.strip())
    except Exception:
        return False


def get_audio_duration(file_path: str) -> float:
    """Measure exact duration in seconds using ffprobe."""
    if not file_path or not os.path.exists(file_path):
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"ffprobe duration error: {e}")
    return 0.0


def build_atempo_chain(speed: float) -> str:
    """
    Generate an FFmpeg atempo filter chain for arbitrary positive speed values.
    FFmpeg atempo is strictly bounded between 0.5 and 2.0.
    For speeds outside [0.5, 2.0], chains multiple atempo filters.
    """
    import math
    if not isinstance(speed, (int, float)) or math.isnan(speed) or math.isinf(speed) or speed <= 0:
        raise ValueError(f"Invalid audio playback speed: {speed}. Speed must be a positive finite number.")
    if abs(speed - 1.0) < 1e-4:
        return ""
    chain = []
    curr = float(speed)
    while curr > 2.0001:
        chain.append("atempo=2.0")
        curr /= 2.0
    while curr < 0.4999:
        chain.append("atempo=0.5")
        curr /= 0.5
    if abs(curr - 1.0) >= 1e-4:
        chain.append(f"atempo={curr:.4f}".rstrip("0").rstrip("."))
    return ",".join(chain)


def _build_clip_audio_filter(
    c: Dict,
    input_label: str,
    output_label: str,
    sample_rate: int = 44100,
    channels: int = 2
) -> str:
    """
    Builds the deterministic FFmpeg audio filter string for a single clip:
      1. atrim from source_in to source_in + source_consumed
      2. atempo chain for speed scaling (pitch preserved)
      3. volume / mute
      4. apad for short source EOF padding
      5. atrim=0:dur for exact final timeline duration normalization
      6. aformat for uniform sample rate and layout
    """
    dur = max(0.01, float(c.get("duration", 0.0)))
    s_in = max(0.0, float(c.get("source_in", 0.0)))
    speed = float(c.get("speed", 1.0))
    vol = max(0.0, float(c.get("volume", 1.0)))
    muted = bool(c.get("muted", False))

    s_consumed = dur * speed
    s_end = s_in + s_consumed
    s_out = c.get("source_out")
    if s_out is not None and float(s_out) > s_in:
        s_end = min(s_end, float(s_out))

    filters = [
        f"atrim=start={s_in:.3f}:end={s_end:.3f}",
        "asetpts=PTS-STARTPTS"
    ]

    atempo = build_atempo_chain(speed)
    if atempo:
        filters.append(atempo)

    if muted:
        filters.append("volume=0.0")
    elif abs(vol - 1.0) > 0.01:
        filters.append(f"volume={vol:.2f}")

    # apad for EOF / short source silence padding + exact duration trim
    filters.append("apad")
    filters.append(f"atrim=0:{dur:.3f}")
    filters.append("asetpts=PTS-STARTPTS")
    filters.append(f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels==2 else 'mono'}")

    return f"[{input_label}]" + ",".join(filters) + f"[{output_label}]"


class TimelineAudioService:
    """
    Dedicated Service for assembling Timeline Composite Audio Tracks.
    Treats the Timeline as the Single Source of Truth.
    Supports variable-speed playback (atempo pitch preservation),
    deterministic EOF silence padding, volume, and mute.
    """
    def __init__(self):
        pass

    def build_timeline_composite_audio(
        self,
        video_clips: List[Dict],
        output_path: Optional[str] = None,
        sample_rate: int = 44100,
        channels: int = 2,
        force_rebuild: bool = False,
        return_duration: bool = False,
        transitions: Optional[List[Any]] = None
    ):
        """
        Concatenates all timeline video clips into a single continuous, gapless audio WAV file.
        Respects:
          - Exact timeline order (video_clips[0], video_clips[1], ...)
          - source_in & duration (trimming)
          - speed multiplier (pitch-preserved via atempo)
          - deterministic EOF padding (short source files padded with silence to exact timeline duration)
          - volume and mute
          - Silent clips (replaces missing audio streams with silence to preserve timing)
          - Phase 3 Audio Transitions (equal-power / linear crossfade across variable speed clips)
        Returns:
          composite_wav_path (str) by default, or (composite_wav_path, total_duration) if return_duration=True.
        """
        def _ret(p, d):
            self.last_duration = float(d or 0.0)
            return (p, self.last_duration) if return_duration else p

        if not video_clips:
            return _ret(None, 0.0)

        # Route to Gate 3 Transition Audio Engine if active transitions are present
        if transitions:
            has_active = any(
                getattr(t, "audio_mode", t.get("audio_mode", "equal_power") if isinstance(t, dict) else "equal_power") != "none"
                and float(getattr(t, "duration", t.get("duration", 0.0) if isinstance(t, dict) else 0.0)) > 0.05
                for t in transitions
            )
            if has_active:
                from core.transition_audio import build_transition_audio_composite
                p, d = build_transition_audio_composite(
                    video_clips=video_clips,
                    transitions=transitions,
                    output_path=output_path,
                    sample_rate=sample_rate,
                    channels=channels,
                    force_rebuild=force_rebuild
                )
                return _ret(p, d)

        # Calculate expected total duration
        total_duration = sum(max(0.0, float(c.get("duration", 0.0))) for c in video_clips)
        if total_duration <= 0.0:
            return _ret(None, 0.0)

        # Build deterministic cache key
        cache_parts = []
        for c in video_clips:
            p = os.path.abspath(c.get("path", ""))
            st = float(c.get("start", 0.0))
            dur = float(c.get("duration", 0.0))
            s_in = float(c.get("source_in", 0.0))
            s_out = float(c.get("source_out", s_in + dur))
            speed = float(c.get("speed", 1.0))
            vol = float(c.get("volume", 1.0))
            muted = 1 if c.get("muted", False) else 0
            try:
                mtime = int(os.path.getmtime(p)) if os.path.exists(p) else 0
            except Exception:
                mtime = 0
            cache_parts.append(f"{p}:{mtime}:{st:.3f}:{dur:.3f}:{s_in:.3f}:{s_out:.3f}:{speed:.2f}:{vol:.2f}:{muted}")

        cache_str = "|".join(cache_parts) + f"_sr{sample_rate}_ch{channels}"
        hash_id = hashlib.md5(cache_str.encode()).hexdigest()[:16]

        if not output_path:
            output_path = get_temp_path(f"timeline_composite_{hash_id}.wav")

        if not force_rebuild and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            logger.info(f"⚡ [TIMELINE_AUDIO] clips={len(video_clips)} duration={total_duration:.3f}s cache=HIT path={os.path.basename(output_path)}")
            return _ret(output_path, total_duration)

        logger.info(f"🎙️ [TIMELINE_AUDIO] clips={len(video_clips)} duration={total_duration:.3f}s cache=MISS path={os.path.basename(output_path)}")

        # Handle Single Clip fast-path
        if len(video_clips) == 1:
            c = video_clips[0]
            p = c.get("path", "")
            dur = max(0.01, float(c.get("duration", 0.0)))
            s_in = max(0.0, float(c.get("source_in", 0.0)))
            speed = float(c.get("speed", 1.0))
            s_consumed = dur * speed

            logger.info(f"🎚️ [AUDIO_SPEED] clip=0 speed={speed:.2f}x source_consumed={s_consumed:.3f}s timeline_dur={dur:.3f}s")
            if p and os.path.exists(p):
                src_dur = get_audio_duration(p)
                if src_dur > 0 and (s_in + s_consumed) > src_dur:
                    avail = max(0.0, src_dur - s_in)
                    logger.info(f"⚠️ [AUDIO_EOF] clip=0 required={s_consumed:.3f}s available={avail:.3f}s padding={s_consumed - avail:.3f}s")

            if not has_audio_stream(p) or not os.path.exists(p):
                # Generate exact silence
                logger.info("🔇 [TimelineAudio] Single clip has no audio, generating silence track...")
                cmd_silence = [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'stereo' if channels==2 else 'mono'}",
                    "-t", f"{dur:.3f}",
                    "-c:a", "pcm_s16le",
                    "-ar", str(sample_rate),
                    "-ac", str(channels),
                    output_path
                ]
                subprocess.run(cmd_silence, capture_output=True, check=False)
            else:
                # Process audio with speed scaling, volume, apad, and exact duration trim
                filt = _build_clip_audio_filter(c, "0:a", "aout", sample_rate, channels)
                cmd_single = [
                    "ffmpeg", "-y",
                    "-i", p,
                    "-filter_complex", filt,
                    "-map", "[aout]",
                    "-c:a", "pcm_s16le",
                    "-ar", str(sample_rate),
                    "-ac", str(channels),
                    output_path
                ]
                res = subprocess.run(cmd_single, capture_output=True, check=False)
                if res.returncode != 0 or not os.path.exists(output_path):
                    # Safe fallback
                    cmd_single_fallback = [
                        "ffmpeg", "-y",
                        "-i", p,
                        "-ss", f"{s_in:.3f}",
                        "-t", f"{s_consumed:.3f}",
                        "-vn",
                        "-c:a", "pcm_s16le",
                        "-ar", str(sample_rate),
                        "-ac", str(channels),
                        output_path
                    ]
                    subprocess.run(cmd_single_fallback, capture_output=True, check=False)

            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return _ret(output_path, total_duration)

        # Multi-Clip Assembly: Build inputs & filter graph
        inputs = []
        filter_parts = []
        stream_labels = []

        curr_input_idx = 0
        for i, c in enumerate(video_clips):
            p = c.get("path", "")
            dur = max(0.01, float(c.get("duration", 0.0)))
            s_in = max(0.0, float(c.get("source_in", 0.0)))
            speed = float(c.get("speed", 1.0))
            s_consumed = dur * speed

            logger.info(f"🎚️ [AUDIO_SPEED] clip={i} speed={speed:.2f}x source_consumed={s_consumed:.3f}s timeline_dur={dur:.3f}s")

            if p and os.path.exists(p) and has_audio_stream(p):
                src_dur = get_audio_duration(p)
                if src_dur > 0 and (s_in + s_consumed) > src_dur:
                    avail = max(0.0, src_dur - s_in)
                    logger.info(f"⚠️ [AUDIO_EOF] clip={i} required={s_consumed:.3f}s available={avail:.3f}s padding={s_consumed - avail:.3f}s")

                inputs.extend(["-i", p])
                filt = _build_clip_audio_filter(c, f"{curr_input_idx}:a", f"a{i}", sample_rate, channels)
                filter_parts.append(filt)
                curr_input_idx += 1
            else:
                # Silent clip: synthesize silence of exact duration
                logger.info(f"🔇 [TimelineAudio] Clip {i} has no audio stream. Generating {dur:.2f}s silence to preserve timing.")
                inputs.extend(["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'stereo' if channels==2 else 'mono'}"])
                filter_parts.append(
                    f"[{curr_input_idx}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]"
                )
                curr_input_idx += 1

            stream_labels.append(f"[a{i}]")

        n = len(video_clips)
        concat_str = "".join(stream_labels) + f"concat=n={n}:v=0:a=1[aout]"
        filter_str = ";".join(filter_parts) + ";" + concat_str

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "[aout]",
            "-c:a", "pcm_s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            output_path
        ]

        logger.debug(f"⚡ [TimelineAudio] Running ffmpeg multi-clip concat command...")
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            actual_dur = get_audio_duration(output_path) or total_duration
            logger.info(f"✅ [TimelineAudio] Timeline composite audio built successfully: {output_path} ({actual_dur:.1f}s)")
            return _ret(output_path, actual_dur)
        else:
            logger.warning(f"⚠️ [TimelineAudio] Concat filter error: {res.stderr[:300] if res.stderr else 'Unknown'}. Trying intermediate WAV fallback...")
            # Intermediate extracted WAV fallback
            temp_wavs = []
            try:
                for idx, c in enumerate(video_clips):
                    p = c.get("path", "")
                    s_in = max(0.0, float(c.get("source_in", 0.0)))
                    dur = max(0.01, float(c.get("duration", 0.0)))
                    part_wav = get_temp_path(f"part_{hash_id}_{idx}.wav")
                    if p and os.path.exists(p) and has_audio_stream(p):
                        filt = _build_clip_audio_filter(c, "0:a", "aout", sample_rate, channels)
                        cmd_part = [
                            "ffmpeg", "-y",
                            "-i", p,
                            "-filter_complex", filt,
                            "-map", "[aout]",
                            "-c:a", "pcm_s16le",
                            "-ar", str(sample_rate),
                            "-ac", str(channels),
                            part_wav
                        ]
                    else:
                        cmd_part = [
                            "ffmpeg", "-y",
                            "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'stereo' if channels==2 else 'mono'}",
                            "-t", f"{dur:.3f}",
                            "-c:a", "pcm_s16le",
                            "-ar", str(sample_rate),
                            "-ac", str(channels),
                            part_wav
                        ]
                    subprocess.run(cmd_part, capture_output=True, check=False)
                    if os.path.exists(part_wav):
                        temp_wavs.append(part_wav)

                if temp_wavs:
                    list_txt = get_temp_path(f"concat_list_{hash_id}.txt")
                    with open(list_txt, "w") as f:
                        for tw in temp_wavs:
                            f.write(f"file '{os.path.abspath(tw)}'\n")

                    cmd_concat = [
                        "ffmpeg", "-y",
                        "-f", "concat", "-safe", "0",
                        "-i", list_txt,
                        "-c:a", "pcm_s16le",
                        "-ar", str(sample_rate),
                        "-ac", str(channels),
                        output_path
                    ]
                    subprocess.run(cmd_concat, capture_output=True, check=False)

                    # Cleanup temporary parts
                    for tw in temp_wavs:
                        try: os.remove(tw)
                        except Exception: pass
                    try: os.remove(list_txt)
                    except Exception: pass

                    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                        actual_dur = get_audio_duration(output_path) or total_duration
                        logger.info(f"✅ [TimelineAudio] Fallback concat built composite audio: {output_path} ({actual_dur:.1f}s)")
                        return _ret(output_path, actual_dur)
            except Exception as e_fb:
                logger.error(f"❌ [TimelineAudio] Fallback concat failed: {e_fb}")

        return _ret(None, 0.0)

    def build_timeline_composite_audio_with_duration(
        self,
        video_clips: List[Dict],
        output_path: Optional[str] = None,
        sample_rate: int = 44100,
        channels: int = 2,
        force_rebuild: bool = False,
        transitions: Optional[List[Any]] = None
    ) -> Tuple[Optional[str], float]:
        """Convenience method returning (composite_wav_path, total_duration)."""
        return self.build_timeline_composite_audio(
            video_clips=video_clips,
            output_path=output_path,
            sample_rate=sample_rate,
            channels=channels,
            force_rebuild=force_rebuild,
            return_duration=True,
            transitions=transitions
        )
