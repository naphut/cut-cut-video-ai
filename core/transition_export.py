"""
Transition Export Engine (Phase 3 Gate 3)
Builds deterministic FFmpeg filter graphs for multi-clip timelines with transitions.
Guarantees 100% mathematical and frame-level parity with the OpenCV Preview Engine:
  - Video: Hardware-accelerated / libx264 xfade transitions with exact handle offsets
  - Audio: Sample-accurate acrossfade with exact project timeline duration
"""
import os
import subprocess
from typing import List, Dict, Any, Optional, Tuple
from utils.logger import logger
from utils.file_utils import get_temp_path
from core.transition_audio import calculate_audio_handles, build_transition_audio_composite


# Mapping from project transition types to FFmpeg xfade transition types
XFADE_TRANSITION_MAP = {
    "cross_dissolve": "fade",
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
    "wipe_up": "wipeup",
    "wipe_down": "wipedown",
}


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Safely retrieves a field whether obj is a dataclass/object or dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_ffmpeg_transition_command(
    video_clips: List[Dict],
    transitions: Optional[List[Any]],
    output_path: str,
    target_width: int = 640,
    target_height: int = 360,
    fps: float = 30.0,
    use_videotoolbox: bool = True
) -> List[str]:
    """
    Constructs the complete FFmpeg command line for rendering multi-clip transitions.
    Ensures exact frame-level timing and audio/video synchronization.
    """
    if not video_clips:
        raise ValueError("Cannot export empty clip list.")

    # 1. First build the master composite audio track to ensure 100% parity with preview audio
    composite_audio_path, _ = build_transition_audio_composite(
        video_clips=video_clips,
        transitions=transitions
    )

    # 2. Map transitions between adjacent clips
    transition_map: Dict[int, Any] = {}
    if transitions:
        for idx in range(len(video_clips) - 1):
            clip_a = video_clips[idx]
            clip_b = video_clips[idx + 1]
            a_id = str(clip_a.get("id", idx))
            b_id = str(clip_b.get("id", idx + 1))
            for t in transitions:
                t_a = str(_get_field(t, "clip_a_id", ""))
                t_b = str(_get_field(t, "clip_b_id", ""))
                if (t_a == a_id and t_b == b_id) or _get_field(t, "cut_index", None) == idx:
                    transition_map[idx] = t
                    break

    out_tl = [0.0] * len(video_clips)
    in_tl = [0.0] * len(video_clips)
    trans_dur = [0.0] * (len(video_clips) - 1)
    trans_type = ["fade"] * (len(video_clips) - 1)

    for i in range(len(video_clips) - 1):
        if i in transition_map:
            t = transition_map[i]
            act_out, act_in, eff_dur = calculate_audio_handles(video_clips[i], video_clips[i+1], t)
            if eff_dur > 0.05:
                out_tl[i] = act_out
                in_tl[i+1] = act_in
                trans_dur[i] = eff_dur
                raw_type = str(_get_field(t, "type", "cross_dissolve"))
                trans_type[i] = XFADE_TRANSITION_MAP.get(raw_type, "fade")

    # 3. Construct inputs and per-clip video filters
    inputs = []
    filter_parts = []

    from utils.file_utils import ensure_accessible_video_file

    for i, c in enumerate(video_clips):
        raw_p = c.get("path", "")
        p = ensure_accessible_video_file(raw_p, allow_copy=True) if raw_p else raw_p
        p = os.path.abspath(p)
        inputs.extend(["-i", p])

        dur = max(0.01, float(c.get("duration", 0.0)))
        speed = max(0.01, float(c.get("speed", 1.0)))
        ext_dur = in_tl[i] + dur + out_tl[i]
        ext_s_in = max(0.0, float(c.get("source_in", 0.0)) - (in_tl[i] * speed))
        ext_s_consumed = ext_dur * speed

        # Trimming, PTS adjustment, scaling, and fps normalization
        v_filters = [
            f"trim=start={ext_s_in:.3f}:end={ext_s_in + ext_s_consumed:.3f}",
            "setpts=PTS-STARTPTS",
        ]
        if abs(speed - 1.0) > 0.001:
            v_filters.append(f"setpts=PTS/{speed:.4f}")

        v_filters.append(f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease")
        v_filters.append(f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2")
        v_filters.append(f"fps={fps}")
        v_filters.append("format=yuv420p")

        filter_parts.append(f"[{i}:v]{','.join(v_filters)}[v{i}]")

    # 4. Construct sequential xfade chain
    # In FFmpeg xfade:
    # offset is the timeline timestamp where the transition begins
    # For transition between clip i and clip i+1:
    # offset_i = (cumulative duration up to cut i) - out_tl[i]
    cum_dur = 0.0
    prev_v = "[v0]"
    for i in range(len(video_clips) - 1):
        cum_dur += float(video_clips[i].get("duration", 0.0))
        d = trans_dur[i]
        t_type = trans_type[i]
        next_v = f"[v{i+1}]"
        out_v = f"[vx{i}]" if i < len(video_clips) - 2 else "[vout]"

        if d > 0.05:
            offset = cum_dur - out_tl[i]
            filter_parts.append(
                f"{prev_v}{next_v}xfade=transition={t_type}:duration={d:.3f}:offset={offset:.3f}{out_v}"
            )
        else:
            # Hard cut: standard concat
            filter_parts.append(
                f"{prev_v}{next_v}concat=n=2:v=1:a=0{out_v}"
            )
        prev_v = out_v

    # Video encoder selection (VideoToolbox on macOS, else libx264)
    vcodec = "h264_videotoolbox" if use_videotoolbox else "libx264"
    # Fallback check will be done during execution if needed

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-i", composite_audio_path,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-map", f"{len(video_clips)}:a",
        "-c:v", vcodec,
        "-b:v", "6000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path
    ]
    return cmd


def export_timeline_with_transitions(
    video_clips: List[Dict],
    transitions: Optional[List[Any]],
    output_path: str,
    target_width: int = 640,
    target_height: int = 360,
    fps: float = 30.0,
    use_videotoolbox: bool = False
) -> bool:
    """
    Renders and exports the full timeline with all transitions to MP4.
    """
    cmd = build_ffmpeg_transition_command(
        video_clips=video_clips,
        transitions=transitions,
        output_path=output_path,
        target_width=target_width,
        target_height=target_height,
        fps=fps,
        use_videotoolbox=use_videotoolbox
    )
    logger.info(f"🚀 [EXPORT_TRANS] Running FFmpeg timeline export -> {os.path.basename(output_path)}")
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        err_tail = res.stderr[-800:] if res.stderr else "Unknown error"
        logger.warning(f"⚠️ [EXPORT_TRANS] VideoToolbox or main export failed: {err_tail}. Retrying with libx264...")
        # Retry with libx264
        cmd_fallback = build_ffmpeg_transition_command(
            video_clips=video_clips,
            transitions=transitions,
            output_path=output_path,
            target_width=target_width,
            target_height=target_height,
            fps=fps,
            use_videotoolbox=False
        )
        res_fb = subprocess.run(cmd_fallback, capture_output=True, text=True, check=False)
        if res_fb.returncode != 0:
            err_fb_tail = res_fb.stderr[-800:] if res_fb.stderr else "Unknown error"
            logger.error(f"❌ [EXPORT_TRANS] Export failed completely: {err_fb_tail}")
            return False

    return os.path.exists(output_path) and os.path.getsize(output_path) > 1000
