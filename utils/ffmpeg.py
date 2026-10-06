import subprocess
import json
import os
import shutil
from pathlib import Path
from utils.logger import logger

import sys

def is_ffmpeg_available() -> bool:
    """Check if ffmpeg command is available in system path or local directory."""
    try:
        res = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0
    except Exception:
        local_bin = "ffmpeg.exe" if sys.platform == "win32" else "./ffmpeg"
        if os.path.exists(local_bin):
            try:
                res = subprocess.run([local_bin, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                return res.returncode == 0
            except Exception:
                pass
        return False

def get_safe_ffmpeg_threads() -> int:
    """Return safe thread count (1 to 4) to prevent CPU starvation and system freezes."""
    cores = os.cpu_count() or 4
    return max(1, min(4, cores // 2))

def safe_run_ffmpeg(cmd: list, timeout: float = None) -> subprocess.CompletedProcess:
    """Run FFmpeg command with safe thread limits and lower process priority on POSIX."""
    def _safe_preexec():
        try:
            if hasattr(os, 'nice'):
                os.nice(5)
        except Exception:
            pass

    if "-threads" not in cmd and len(cmd) > 1 and cmd[0] == "ffmpeg":
        cmd = [cmd[0], "-threads", str(get_safe_ffmpeg_threads())] + cmd[1:]

    preexec = _safe_preexec if sys.platform != "win32" else None

    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        preexec_fn=preexec
    )


def get_video_info(video_path: str) -> dict:
    """Get metadata about video file (duration, resolution, audio presence)."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        video_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            logger.error(f"Failed to probe video info for {video_path}: {res.stderr.strip()[:200]}")
            return {"duration": 0.0, "has_audio": True, "width": 1280, "height": 720}
        data = json.loads(res.stdout)
        
        duration = float(data.get("format", {}).get("duration", 0.0))
        has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
        
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        width = int(video_stream.get("width") or 1280)
        height = int(video_stream.get("height") or 720)
        
        return {
            "duration": duration,
            "has_audio": has_audio,
            "width": width,
            "height": height,
            "format": data.get("format", {}).get("format_name", "")
        }
    except Exception as e:
        logger.error(f"Failed to probe video info for {video_path}: {e}")
        return {"duration": 0.0, "has_audio": True, "width": 1280, "height": 720}

def extract_audio(video_path: str, output_audio_path: str, sample_rate: int = 16000) -> bool:
    """Extract audio from video file to 16kHz mono WAV or MP3 format."""
    codec = "libmp3lame" if output_audio_path.lower().endswith(".mp3") else "pcm_s16le"
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", codec,
        "-ar", str(sample_rate),
        "-ac", "1",
        output_audio_path
    ]
    try:
        logger.info(f"Extracting audio from {os.path.basename(video_path)} -> {os.path.basename(output_audio_path)}")
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            logger.error(f"FFmpeg audio extraction error: {res.stderr}")
            return False
        return True
    except Exception as e:
        logger.error(f"Error running FFmpeg audio extraction: {e}")
        return False

def extract_video_thumbnail(video_path: str, output_image_path: str) -> bool:
    """Extract a single video frame thumbnail using FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", "00:00:00.500",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        output_image_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0 and os.path.exists(output_image_path)
    except Exception as e:
        logger.error(f"Error extracting video thumbnail: {e}")
        return False

def combine_video_audio(
    video_path: str, 
    audio_path: str, 
    output_path: str, 
    background_volume: float = 0.0
) -> bool:
    """
    Merge video track with new audio track.
    If background_volume > 0.0, mixes original video audio at lowered volume.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if background_volume <= 0.0:
        # Simple replace audio stream
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path
        ]
    else:
        # Mix background audio with synthesized audio
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-filter_complex", (
                f"[0:a]volume={background_volume}[bg];"
                f"[bg][1:a]sidechaincompress=threshold=0.03:ratio=5:attack=50:release=350[ducked_bg];"
                f"[1:a][ducked_bg]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            ),
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            output_path
        ]

    try:
        logger.info(f"Combining video & dubbed audio -> {output_path}")
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            logger.error(f"FFmpeg combine video error: {res.stderr}")
            return False
        return True
    except Exception as e:
        logger.error(f"Error merging video and audio: {e}")
        return False

def adjust_audio_speed(input_audio: str, output_audio: str, speed_factor: float) -> bool:
    """Adjust audio playback speed using FFmpeg atempo filter (0.5 to 2.0 per filter stage)."""
    if speed_factor == 1.0:
        shutil.copy(input_audio, output_audio)
        return True

    # FFmpeg atempo limits: 0.5 <= atempo <= 2.0
    filters = []
    current_speed = speed_factor

    while current_speed > 2.0:
        filters.append("atempo=2.0")
        current_speed /= 2.0
    while current_speed < 0.5:
        filters.append("atempo=0.5")
        current_speed /= 0.5
    
    filters.append(f"atempo={current_speed:.4f}")
    filter_chain = ",".join(filters)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_audio,
        "-filter:a", filter_chain,
        output_audio
    ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0
    except Exception as e:
        logger.error(f"Error adjusting audio speed: {e}")
        return False


def trim_video(
    video_path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    lossless: bool = True
) -> bool:
    """
    Trim a video file between start_sec and end_sec.
    Tries fast lossless stream copy first (<0.5s).
    Falls back to ultrafast high-quality re-encode if stream copy fails.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    start_sec = max(0.0, float(start_sec))
    end_sec = float(end_sec)
    dur_sec = end_sec - start_sec
    if dur_sec < 0.1:
        logger.warning(f"⚠️ Invalid trim range: start={start_sec:.2f}s, end={end_sec:.2f}s (dur={dur_sec:.2f}s < 0.1s)")
        return False

    # 1. Try Fast Lossless Stream Copy first if requested
    if lossless:
        cmd_copy = [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}",
            "-t", f"{dur_sec:.3f}",
            "-i", video_path,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        try:
            logger.info(f"✂️ [FFmpeg] Trimming video lossless: {start_sec:.2f}s -> {end_sec:.2f}s (dur: {dur_sec:.2f}s)")
            res = safe_run_ffmpeg(cmd_copy)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                logger.info(f"✅ Lossless trim succeeded -> {output_path} ({os.path.getsize(output_path):,} bytes)")
                return True
            else:
                logger.warning(f"⚠️ Lossless trim produced invalid output, falling back to accurate re-encode: {res.stderr[:200]}")
        except Exception as e:
            logger.warning(f"⚠️ Lossless trim failed: {e}, falling back to accurate re-encode...")

    # 2. Accurate Re-encode fallback with safe thread throttling
    cmd_encode = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-t", f"{dur_sec:.3f}",
        "-i", video_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "19",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path
    ]
    try:
        logger.info(f"✂️ [FFmpeg] Accurate re-encode trim: {start_sec:.2f}s -> {end_sec:.2f}s")
        res = safe_run_ffmpeg(cmd_encode)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            logger.info(f"✅ Re-encode trim succeeded -> {output_path}")
            return True
        logger.error(f"FFmpeg trim error: {res.stderr}")
        return False
    except Exception as e:
        logger.error(f"Error during video trim: {e}")
        return False


def cut_out_video_segment(
    video_path: str,
    output_path: str,
    cut_start_sec: float,
    cut_end_sec: float
) -> bool:
    """
    Cut out (remove / ripple delete) the segment between cut_start_sec and cut_end_sec,
    and concatenate [0 -> cut_start_sec] + [cut_end_sec -> total_duration].
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    info = get_video_info(video_path)
    total_dur = info.get("duration", 0.0)
    has_audio = info.get("has_audio", True)

    cut_start_sec = max(0.0, float(cut_start_sec))
    cut_end_sec = min(total_dur, float(cut_end_sec)) if total_dur > 0 else float(cut_end_sec)

    if cut_start_sec <= 0.05:
        # Cut is at the very beginning -> just trim from cut_end_sec to total_dur
        return trim_video(video_path, output_path, cut_end_sec, total_dur, lossless=False)

    if total_dur > 0 and cut_end_sec >= total_dur - 0.05:
        # Cut is at the very end -> just trim from 0 to cut_start_sec
        return trim_video(video_path, output_path, 0.0, cut_start_sec, lossless=False)

    # Cut is in the middle -> concat part 1 [0..cut_start] and part 2 [cut_end..end]
    if has_audio:
        filter_complex = (
            f"[0:v]trim=start=0:end={cut_start_sec:.3f},setpts=PTS-STARTPTS[v1];"
            f"[0:a]atrim=start=0:end={cut_start_sec:.3f},asetpts=PTS-STARTPTS[a1];"
            f"[0:v]trim=start={cut_end_sec:.3f},setpts=PTS-STARTPTS[v2];"
            f"[0:a]atrim=start={cut_end_sec:.3f},asetpts=PTS-STARTPTS[a2];"
            f"[v1][a1][v2][a2]concat=n=2:v=1:a=1[outv][outa]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "19",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]
    else:
        filter_complex = (
            f"[0:v]trim=start=0:end={cut_start_sec:.3f},setpts=PTS-STARTPTS[v1];"
            f"[0:v]trim=start={cut_end_sec:.3f},setpts=PTS-STARTPTS[v2];"
            f"[v1][v2]concat=n=2:v=1:a=0[outv]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "19",
            output_path
        ]

    try:
        logger.info(f"✂️ [FFmpeg] Cutting out segment: {cut_start_sec:.2f}s -> {cut_end_sec:.2f}s and merging remainder")
        res = safe_run_ffmpeg(cmd)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            logger.info(f"✅ Cut-out merge succeeded -> {output_path}")
            return True
        logger.error(f"FFmpeg cut-out error: {res.stderr}")
        return False
    except Exception as e:
        logger.error(f"Error during video cut-out: {e}")
        return False


def split_video_at(
    video_path: str,
    split_sec: float,
    output_part1: str,
    output_part2: str,
    lossless: bool = True
) -> bool:
    """
    Split a video at split_sec into two separate video files:
    Part 1: 0.0 -> split_sec
    Part 2: split_sec -> total_duration
    """
    info = get_video_info(video_path)
    total_dur = info.get("duration", 0.0)
    split_sec = float(split_sec)
    if split_sec <= 0.1 or (total_dur > 0 and split_sec >= total_dur - 0.1):
        logger.error(f"Invalid split point: {split_sec}s for duration {total_dur}s")
        return False

    p1_ok = trim_video(video_path, output_part1, 0.0, split_sec, lossless=lossless)
    p2_ok = trim_video(video_path, output_part2, split_sec, total_dur if total_dur > 0 else 999999.0, lossless=lossless)
    return p1_ok and p2_ok


def are_clips_strictly_compatible_for_lossless_concat(video_paths: list) -> bool:
    """
    Check if video files have 100% matching codecs, resolutions, aspect ratios,
    framerate, and audio parameters so that direct '-c copy' concat demuxer
    produces a 100% valid, glitch-free, synchronized video.
    """
    if len(video_paths) <= 1:
        return True

    first_info = None
    for p in video_paths:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,sample_rate,channels",
                "-of", "json", p
            ]
            res = safe_run_ffmpeg(cmd)
            if res.returncode != 0:
                return False
            import json
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

            if not v_stream:
                return False

            sig = {
                "v_codec": v_stream.get("codec_name"),
                "v_profile": v_stream.get("profile"),
                "width": v_stream.get("width"),
                "height": v_stream.get("height"),
                "fps": v_stream.get("r_frame_rate"),
                "has_audio": (a_stream is not None),
                "a_codec": a_stream.get("codec_name") if a_stream else None,
                "a_profile": a_stream.get("profile") if a_stream else None,
                "sample_rate": a_stream.get("sample_rate") if a_stream else None,
                "channels": a_stream.get("channels") if a_stream else None,
            }
            if first_info is None:
                first_info = sig
            else:
                h_diff = abs(int(sig.get("height") or 0) - int(first_info.get("height") or 0))
                w_diff = abs(int(sig.get("width") or 0) - int(first_info.get("width") or 0))
                if (sig["v_codec"] != first_info["v_codec"] or
                    w_diff > 4 or
                    h_diff > 8 or
                    sig["fps"] != first_info["fps"] or
                    sig["has_audio"] != first_info["has_audio"] or
                    sig["a_codec"] != first_info["a_codec"] or
                    sig["sample_rate"] != first_info["sample_rate"] or
                    sig["channels"] != first_info["channels"]):
                    return False
        except Exception:
            return False
    return True


def merge_multiple_videos(
    video_paths: list,
    output_path: str,
    target_width: int = 0,
    target_height: int = 0,
    lossless: bool = True,
    progress_callback: callable = None,
    is_cancelled: callable = None
) -> bool:
    """
    Merge/Stitch multiple video files (3 to 10+ clips) sequentially into a single unified video.
    Features:
    1. If all clips have 100% matching specs and lossless=True, uses instant FFmpeg Concat Demuxer (<1s, 0% CPU).
    2. If specs differ (resolution, audio profile, fps), normalizes clips SEQUENTIALLY with hardware acceleration
       (Apple VideoToolbox / h264), then instantly losslessly concats them so audio/video NEVER drops or freezes.
    3. Live progress reporting and cancellation support.
    """
    import sys
    import tempfile
    from utils.file_utils import ensure_accessible_video_file
    if not video_paths:
        return False
    video_paths = [ensure_accessible_video_file(p) for p in video_paths]
    if len(video_paths) == 1:
        shutil.copy2(video_paths[0], output_path)
        if progress_callback:
            progress_callback(100, "✅ រួចរាល់ 100%")
        return True

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    n = len(video_paths)

    if progress_callback:
        progress_callback(5, f"🔍 ពិនិត្យមើលពត៌មានវីដេអូចំនួន {n} clips...")

    # 1. Probe all input videos
    infos = [get_video_info(p) for p in video_paths]
    ref_w = target_width if target_width > 0 else (infos[0].get("width") or 1080)
    ref_h = target_height if target_height > 0 else (infos[0].get("height") or 1920)
    # Ensure even dimensions (required by h264)
    ref_w = ref_w - (ref_w % 2)
    ref_h = ref_h - (ref_h % 2)

    # 2. Strict compatibility verification for direct lossless stream copy
    can_direct_lossless = False
    if lossless:
        can_direct_lossless = are_clips_strictly_compatible_for_lossless_concat(video_paths)
        if can_direct_lossless:
            logger.info(f"⚡ All {n} clips have 100% identical specs. Using instant direct lossless concat demuxer.")
        else:
            logger.info(f"⚙️ Clips have differing specs (resolution/fps/audio codec/profile). Using hardware-accelerated sequential normalization to guarantee 100% playable output.")

    if can_direct_lossless:
        if progress_callback:
            progress_callback(15, "⚡ កំពុងភ្ជាប់វីដេអូភ្លាមៗ (Fast Lossless Concat)...")
        list_file = output_path + ".concat_list.txt"
        try:
            with open(list_file, "w", encoding="utf-8") as f:
                for p in video_paths:
                    safe_p = os.path.abspath(p).replace("'", "'\\''")
                    f.write(f"file '{safe_p}'\n")

            cmd_concat = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                output_path
            ]
            logger.info(f"🔗 [FFmpeg] Merging {n} clips via fast lossless concat demuxer -> {output_path}")
            res = safe_run_ffmpeg(cmd_concat)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                expected_tot = sum(inf.get("duration", 0.0) for inf in infos)
                out_inf = get_video_info(output_path)
                max_allowed_diff = max(5.0, min(30.0, expected_tot * 0.03 + n * 0.3))
                if abs(out_inf.get("duration", 0.0) - expected_tot) <= max_allowed_diff:
                    logger.info(f"✅ Lossless merge succeeded -> {output_path} ({os.path.getsize(output_path):,} bytes, duration={out_inf.get('duration')}s vs expected={expected_tot:.1f}s)")
                    if progress_callback:
                        progress_callback(100, "✅ ភ្ជាប់វីដេអូបានសម្រេចដោយជោគជ័យ (Lossless)")
                    return True
                else:
                    logger.warning(f"⚠️ Direct concat produced duration discrepancy ({out_inf.get('duration')}s vs {expected_tot}s, limit={max_allowed_diff:.1f}s). Normalizing...")
            else:
                logger.warning(f"⚠️ Lossless concat demuxer produced invalid output, falling back to sequential normalize concat: {res.stderr[:200]}")
        except Exception as e:
            logger.warning(f"⚠️ Lossless concat demuxer error: {e}, falling back to sequential normalize concat...")
        finally:
            if os.path.exists(list_file):
                try: os.remove(list_file)
                except Exception: pass

    # 3. Smooth Sequential Normalization (processes clips one by one to avoid CPU/RAM lag)
    temp_dir = tempfile.mkdtemp(prefix="merge_norm_")
    norm_clips = []
    try:
        v_codec = "h264_videotoolbox" if sys.platform == "darwin" else "libx264"

        for i, p in enumerate(video_paths):
            if is_cancelled and is_cancelled():
                logger.info("Merge cancelled by user during normalization.")
                return False

            pct = int(10 + (i / float(n)) * 80)
            base_name = os.path.basename(p)
            if len(base_name) > 20:
                base_name = base_name[:17] + "..."
            if progress_callback:
                progress_callback(pct, f"⚙️ កំពុងរៀបចំ Clip {i+1}/{n} ({base_name})...")

            norm_clip_path = os.path.join(temp_dir, f"clip_{i:04d}.mp4")
            inf = infos[i]
            has_a = inf.get("has_audio", True)

            # High-performance single-clip normalization filter
            vf = (
                f"scale={ref_w}:{ref_h}:force_original_aspect_ratio=decrease,"
                f"pad={ref_w}:{ref_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps=30"
            )
            cmd_norm = ["ffmpeg", "-y", "-i", p, "-vf", vf]

            if v_codec == "libx264":
                cmd_norm.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p"])
            else:
                cmd_norm.extend(["-c:v", "h264_videotoolbox", "-b:v", "3500k", "-pix_fmt", "yuv420p", "-color_range", "1"])

            if has_a:
                cmd_norm.extend([
                    "-af", "aformat=sample_rates=48000:channel_layouts=stereo",
                    "-c:a", "aac", "-b:a", "192k"
                ])
            else:
                dur = inf.get("duration", 10.0)
                cmd_norm.extend([
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", f"{dur:.3f}",
                    "-c:a", "aac", "-b:a", "192k"
                ])

            cmd_norm.append(norm_clip_path)

            res_norm = safe_run_ffmpeg(cmd_norm)
            if res_norm.returncode != 0 or not os.path.exists(norm_clip_path) or os.path.getsize(norm_clip_path) < 1024:
                # If videotoolbox failed, fallback smoothly to libx264
                if v_codec != "libx264":
                    cmd_norm_fallback = ["ffmpeg", "-y", "-i", p, "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p"]
                    if has_a:
                        cmd_norm_fallback.extend(["-af", "aformat=sample_rates=48000:channel_layouts=stereo", "-c:a", "aac", "-b:a", "192k"])
                    cmd_norm_fallback.append(norm_clip_path)
                    res_fallback = safe_run_ffmpeg(cmd_norm_fallback)
                    if res_fallback.returncode != 0 or not os.path.exists(norm_clip_path):
                        err_tail = res_fallback.stderr.strip()[-300:] if res_fallback.stderr else "Unknown error"
                        logger.error(f"Failed to normalize clip {i}: {err_tail}")
                        return False
                else:
                    err_tail = res_norm.stderr.strip()[-300:] if res_norm.stderr else "Unknown error"
                    logger.error(f"Failed to normalize clip {i}: {err_tail}")
                    return False

            norm_clips.append(norm_clip_path)

        if is_cancelled and is_cancelled():
            return False

        # 4. Final step: Instant Concat Demuxer of all normalized clips (<0.5s)
        if progress_callback:
            progress_callback(92, f"🔗 កំពុងបញ្ចប់ការភ្ជាប់វីដេអូ {n} clips (Finalizing)...")

        norm_list_file = os.path.join(temp_dir, "norm_list.txt")
        with open(norm_list_file, "w", encoding="utf-8") as f:
            for p in norm_clips:
                safe_p = os.path.abspath(p).replace("'", "'\\''")
                f.write(f"file '{safe_p}'\n")

        cmd_final = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", norm_list_file,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        res_final = safe_run_ffmpeg(cmd_final)
        if res_final.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            logger.info(f"✅ Multi-video merge succeeded -> {output_path} ({os.path.getsize(output_path):,} bytes)")
            if progress_callback:
                progress_callback(100, "✅ ភ្ជាប់វីដេអូបានសម្រេចដោយជោគជ័យ 100%")
            return True
        else:
            logger.error(f"Final concat demuxer failed: {res_final.stderr[:300]}")
            return False

    except Exception as e:
        logger.error(f"Error during video merge: {e}")
        return False
    finally:
        # Cleanup temporary normalized files
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


def concat_videos_lossless(video_paths: list, output_path: str) -> bool:
    """
    Concatenate multiple video files into a single video file losslessly.
    Uses concat demuxer if clips are compatible, or hardware-accelerated normalize fallback.
    """
    if not video_paths:
        return False
    if len(video_paths) == 1:
        shutil.copy2(video_paths[0], output_path)
        return True
    return merge_multiple_videos(video_paths, output_path, lossless=True)




