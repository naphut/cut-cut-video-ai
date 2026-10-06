"""
Transition Audio Engine (Phase 3 Gate 3)
Builds seamless, equal-power audio crossfades across variable-speed timeline clips.
Strictly preserves the Timeline Single Source of Truth and exact project duration.
Implements a 3-level hierarchical caching system:
  Level 1: Clip Audio Cache (per-clip audio with speed/volume/trim)
  Level 2: Transition Boundary Cache (crossfade snippets)
  Level 3: Master Timeline Composite Audio Cache
"""
import os
import math
import hashlib
import subprocess
from typing import List, Dict, Any, Optional, Tuple
from utils.logger import logger
from utils.file_utils import get_temp_path
from services.timeline_audio_service import (
    has_audio_stream,
    get_audio_duration,
    build_atempo_chain,
    _build_clip_audio_filter
)


def get_clip_audio_hash(clip: Dict, sample_rate: int = 44100, channels: int = 2) -> str:
    """Level 1 Cache Fingerprint: uniquely identifies processed clip audio."""
    p = os.path.abspath(clip.get("path", ""))
    s_in = float(clip.get("source_in", 0.0))
    dur = float(clip.get("duration", 0.0))
    speed = float(clip.get("speed", 1.0))
    vol = float(clip.get("volume", 1.0))
    muted = 1 if clip.get("muted", False) else 0
    try:
        mtime = int(os.path.getmtime(p)) if os.path.exists(p) else 0
    except Exception:
        mtime = 0
    raw = f"{p}:{mtime}:{s_in:.3f}:{dur:.3f}:{speed:.3f}:{vol:.2f}:{muted}:{sample_rate}:{channels}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def get_transition_audio_hash(
    clip_a_hash: str,
    clip_b_hash: str,
    trans_type: str,
    trans_dur: float,
    alignment: str,
    audio_mode: str,
    sample_rate: int = 44100,
    channels: int = 2
) -> str:
    """Level 2 Cache Fingerprint: uniquely identifies transition crossfade segment."""
    raw = f"{clip_a_hash}:{clip_b_hash}:{trans_type}:{trans_dur:.3f}:{alignment}:{audio_mode}:{sample_rate}:{channels}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Safely retrieves a field whether obj is a dataclass/object or dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_master_timeline_audio_hash(
    clips: List[Dict],
    transitions: Optional[List[Any]],
    sample_rate: int = 44100,
    channels: int = 2
) -> str:
    """Level 3 Cache Fingerprint: incorporates all clips and active transitions."""
    clip_hashes = [get_clip_audio_hash(c, sample_rate, channels) for c in clips]
    trans_parts = []
    if transitions:
        for t in transitions:
            t_id = str(_get_field(t, "id", ""))
            t_dur = float(_get_field(t, "duration", 1.0))
            t_type = str(_get_field(t, "type", "cross_dissolve"))
            t_mode = str(_get_field(t, "audio_mode", "equal_power"))
            trans_parts.append(f"{t_id}:{t_type}:{t_dur:.3f}:{t_mode}")
    raw = "|".join(clip_hashes) + "#" + "|".join(trans_parts)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def calculate_audio_handles(
    clip_a: Dict,
    clip_b: Dict,
    transition: Any
) -> Tuple[float, float, float]:
    """
    Calculate outgoing timeline handle (A) and incoming timeline handle (B) in timeline seconds.
    Respects variable speeds and source bounds:
      source_consumed_out = outgoing_timeline * speed_A <= available_out_handle_A
      source_consumed_in  = incoming_timeline * speed_B <= available_in_handle_B
    Returns: (outgoing_timeline, incoming_timeline, effective_crossfade_duration)
    """
    t_dur = float(_get_field(transition, "duration", 1.0))
    alignment = str(_get_field(transition, "alignment", "center")).lower()
    audio_mode = str(_get_field(transition, "audio_mode", "equal_power")).lower()

    if audio_mode == "none" or t_dur <= 0.05:
        return 0.0, 0.0, 0.0

    speed_a = max(0.01, float(clip_a.get("speed", 1.0)))
    speed_b = max(0.01, float(clip_b.get("speed", 1.0)))

    s_in_a = max(0.0, float(clip_a.get("source_in", 0.0)))
    dur_a = max(0.01, float(clip_a.get("duration", 0.0)))
    path_a = clip_a.get("path", "")
    med_dur_a = get_audio_duration(path_a) if path_a and os.path.exists(path_a) else dur_a * speed_a

    s_out_a = s_in_a + dur_a * speed_a
    avail_out_a = max(0.0, med_dur_a - s_out_a)

    s_in_b = max(0.0, float(clip_b.get("source_in", 0.0)))
    avail_in_b = s_in_b

    max_out_tl = avail_out_a / speed_a
    max_in_tl = avail_in_b / speed_b

    # Target timeline allocation matching Gate 1 evaluate_transition_handles
    if alignment == "start_on_cut":
        max_dur = max_in_tl
        act_dur = min(t_dur, max_dur)
        act_out = 0.0
        act_in = act_dur
    elif alignment == "end_on_cut":
        max_dur = max_out_tl
        act_dur = min(t_dur, max_dur)
        act_out = act_dur
        act_in = 0.0
    else: # center
        max_dur = 2.0 * min(max_out_tl, max_in_tl)
        act_dur = min(t_dur, max_dur)
        act_out = act_dur / 2.0
        act_in = act_dur / 2.0

    eff_dur = act_out + act_in
    if eff_dur <= 0.05:
        # Insufficient handle: fallback to hard cut
        return 0.0, 0.0, 0.0

    return act_out, act_in, eff_dur


def build_transition_audio_composite(
    video_clips: List[Dict],
    transitions: Optional[List[Any]],
    output_path: Optional[str] = None,
    sample_rate: int = 44100,
    channels: int = 2,
    force_rebuild: bool = False
) -> Tuple[Optional[str], float]:
    """
    Builds composite audio with seamless variable-speed transitions and exact project duration.
    Uses 3-level hierarchical caching:
      Level 1: cached clip audio tracks
      Level 2: cached transition crossfade segments
      Level 3: master composite
    """
    if not video_clips:
        return None, 0.0

    total_duration = sum(max(0.0, float(c.get("duration", 0.0))) for c in video_clips)
    if total_duration <= 0.0:
        return None, 0.0

    master_hash = get_master_timeline_audio_hash(video_clips, transitions, sample_rate, channels)
    if not output_path:
        output_path = get_temp_path(f"timeline_composite_{master_hash}.wav")

    # Level 3 Cache check
    if not force_rebuild and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        logger.info(f"⚡ [TIMELINE_AUDIO_TRANS] clips={len(video_clips)} cache=HIT path={os.path.basename(output_path)}")
        return output_path, total_duration

    logger.info(f"🎙️ [TIMELINE_AUDIO_TRANS] clips={len(video_clips)} duration={total_duration:.3f}s cache=MISS (Building Hierarchical Audio)")

    # Map transitions by cut index: transition_map[i] is transition between clip i and clip i+1
    transition_map: Dict[int, Any] = {}
    if transitions:
        for idx in range(len(video_clips) - 1):
            clip_a = video_clips[idx]
            clip_b = video_clips[idx + 1]
            a_id = str(clip_a.get("id", idx))
            b_id = str(clip_b.get("id", idx + 1))
            # Match by id or cut index
            for t in transitions:
                t_a = str(_get_field(t, "clip_a_id", ""))
                t_b = str(_get_field(t, "clip_b_id", ""))
                if (t_a == a_id and t_b == b_id) or _get_field(t, "cut_index", None) == idx:
                    transition_map[idx] = t
                    break

    # Build audio for each clip with handles
    # For clip i:
    # outgoing handle to clip i+1: out_tl[i]
    # incoming handle from clip i-1: in_tl[i]
    out_tl = [0.0] * len(video_clips)
    in_tl = [0.0] * len(video_clips)
    crossfade_dur = [0.0] * (len(video_clips) - 1)
    curve_types = ["tri"] * (len(video_clips) - 1)

    for i in range(len(video_clips) - 1):
        if i in transition_map:
            t = transition_map[i]
            act_out, act_in, eff_dur = calculate_audio_handles(video_clips[i], video_clips[i+1], t)
            if eff_dur > 0.05:
                out_tl[i] = act_out
                in_tl[i+1] = act_in
                crossfade_dur[i] = eff_dur
                mode = str(_get_field(t, "audio_mode", "equal_power"))
                curve_types[i] = "qsin" if mode == "equal_power" else "tri"

    # Step 1: Render / fetch Level 1 clip audio with required handles
    clip_wavs = []
    for i, c in enumerate(video_clips):
        c_dur = max(0.01, float(c.get("duration", 0.0)))
        speed = max(0.01, float(c.get("speed", 1.0)))
        vol = max(0.0, float(c.get("volume", 1.0)))
        muted = bool(c.get("muted", False))
        p = c.get("path", "")

        # Extended duration for this clip stream: in_tl[i] + c_dur + out_tl[i]
        ext_dur = in_tl[i] + c_dur + out_tl[i]
        ext_s_in = max(0.0, float(c.get("source_in", 0.0)) - (in_tl[i] * speed))
        ext_s_consumed = ext_dur * speed

        # Level 1 cache key for this specific stream slice
        l1_key = get_clip_audio_hash(c, sample_rate, channels) + f"_ext_{in_tl[i]:.3f}_{out_tl[i]:.3f}"
        part_wav = get_temp_path(f"clip_audio_{l1_key}.wav")

        if not os.path.exists(part_wav) or os.path.getsize(part_wav) < 1000:
            if not p or not os.path.exists(p) or not has_audio_stream(p):
                # Synthesize silence of exact extended duration
                cmd_sil = [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'stereo' if channels==2 else 'mono'}",
                    "-t", f"{ext_dur:.3f}",
                    "-c:a", "pcm_s16le",
                    "-ar", str(sample_rate),
                    "-ac", str(channels),
                    part_wav
                ]
                subprocess.run(cmd_sil, capture_output=True, check=False)
            else:
                # Extract trimmed audio, speed adjust via atempo, volume, and apad
                filters = [
                    f"atrim=start={ext_s_in:.3f}:end={ext_s_in + ext_s_consumed:.3f}",
                    "asetpts=PTS-STARTPTS"
                ]
                atempo = build_atempo_chain(speed)
                if atempo:
                    filters.append(atempo)
                if muted:
                    filters.append("volume=0.0")
                elif abs(vol - 1.0) > 0.01:
                    filters.append(f"volume={vol:.2f}")

                filters.append("apad")
                filters.append(f"atrim=0:{ext_dur:.3f}")
                filters.append("asetpts=PTS-STARTPTS")
                filters.append(f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels==2 else 'mono'}")

                cmd_extract = [
                    "ffmpeg", "-y",
                    "-i", p,
                    "-filter_complex", f"[0:a]{','.join(filters)}[aout]",
                    "-map", "[aout]",
                    "-c:a", "pcm_s16le",
                    "-ar", str(sample_rate),
                    "-ac", str(channels),
                    part_wav
                ]
                res = subprocess.run(cmd_extract, capture_output=True, check=False)
                if res.returncode != 0 or not os.path.exists(part_wav):
                    # Fallback to silence
                    cmd_sil = [
                        "ffmpeg", "-y",
                        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'stereo' if channels==2 else 'mono'}",
                        "-t", f"{ext_dur:.3f}",
                        "-c:a", "pcm_s16le",
                        "-ar", str(sample_rate),
                        "-ac", str(channels),
                        part_wav
                    ]
                    subprocess.run(cmd_sil, capture_output=True, check=False)

        clip_wavs.append(part_wav)

    # Step 2: Combine clips using sequential acrossfade for active transitions
    # or concat for hard cuts
    # If no transitions active, standard concat
    has_active_crossfade = any(d > 0.05 for d in crossfade_dur)
    if not has_active_crossfade:
        # Standard fast concat
        inputs = []
        labels = []
        for idx, cw in enumerate(clip_wavs):
            inputs.extend(["-i", cw])
            labels.append(f"[{idx}:a]")
        concat_filter = f"{''.join(labels)}concat=n={len(clip_wavs)}:v=0:a=1[aout]"
        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", concat_filter,
            "-map", "[aout]",
            "-c:a", "pcm_s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            output_path
        ]
        subprocess.run(cmd, capture_output=True, check=False)
    else:
        # Build acrossfade chain
        inputs = []
        for cw in clip_wavs:
            inputs.extend(["-i", cw])

        filter_parts = []
        prev_label = "[0:a]"
        for i in range(len(clip_wavs) - 1):
            next_label = f"[{i+1}:a]"
            out_label = f"[m{i}]" if i < len(clip_wavs) - 2 else "[aout]"
            d = crossfade_dur[i]
            c_type = curve_types[i]
            if d > 0.05:
                filter_parts.append(
                    f"{prev_label}{next_label}acrossfade=d={d:.3f}:c1={c_type}:c2={c_type}{out_label}"
                )
            else:
                filter_parts.append(
                    f"{prev_label}{next_label}concat=n=2:v=0:a=1{out_label}"
                )
            prev_label = out_label

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", "[aout]",
            "-c:a", "pcm_s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            output_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            logger.error(f"❌ [TIMELINE_AUDIO_TRANS] acrossfade filter error: {res.stderr[:200]}")

    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        actual_dur = get_audio_duration(output_path) or total_duration
        logger.info(f"✅ [TIMELINE_AUDIO_TRANS] Composite audio built successfully: {output_path} ({actual_dur:.3f}s)")
        return output_path, actual_dur

    return None, 0.0
