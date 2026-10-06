import os
import subprocess
import json
from utils.logger import logger
from utils.file_utils import get_output_path

class VideoService:
    """
    Dedicated Video Operations & Multiplexing Service.
    Handles video stream metadata extraction and final FFmpeg dubbing video muxing.
    """
    def __init__(self, video_path: str = None):
        self.video_path = video_path

    def get_video_info(self, video_path: str = None) -> dict:
        """Extract video duration, resolution, fps, and audio stream existence."""
        target = video_path or self.video_path
        if not target or not os.path.exists(target):
            return {"duration": 0.0, "has_audio": False, "width": 1920, "height": 1080, "fps": 30.0}

        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=width,height,r_frame_rate,codec_type,duration:format=duration",
                "-of", "json",
                target
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                streams = data.get("streams", [])
                format_info = data.get("format", {})

                v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
                has_audio = any(s.get("codec_type") == "audio" for s in streams)

                duration = 0.0
                if "duration" in format_info:
                    try:
                        duration = float(format_info["duration"])
                    except (ValueError, TypeError):
                        pass
                if duration <= 0.0 and v_stream and "duration" in v_stream:
                    try:
                        duration = float(v_stream["duration"])
                    except (ValueError, TypeError):
                        pass

                width = int(v_stream.get("width") or 1920) if v_stream else 1920
                height = int(v_stream.get("height") or 1080) if v_stream else 1080

                fps = 30.0
                if v_stream and v_stream.get("r_frame_rate"):
                    rate_str = str(v_stream["r_frame_rate"]).strip()
                    if "/" in rate_str:
                        parts = rate_str.split("/", 1)
                        try:
                            f_num, f_den = float(parts[0]), float(parts[1])
                            if f_den > 0:
                                fps = round(f_num / f_den, 2)
                        except (ValueError, ZeroDivisionError):
                            pass
                    else:
                        try:
                            fps = float(rate_str)
                        except ValueError:
                            pass

                return {
                    "duration": duration,
                    "has_audio": has_audio,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "video_path": target
                }
        except Exception as e:
            logger.error(f"❌ [VideoService] Error probing video: {e}")

        try:
            from utils.ffmpeg import get_video_info as fallback_probe
            fb = fallback_probe(target)
            return {
                "duration": float(fb.get("duration", 0.0) or 0.0),
                "has_audio": bool(fb.get("has_audio", False)),
                "width": int(fb.get("width", 1920) or 1920),
                "height": int(fb.get("height", 1080) or 1080),
                "fps": float(fb.get("fps", 30.0) or 30.0),
                "video_path": target
            }
        except Exception:
            return {"duration": 0.0, "has_audio": False, "width": 1920, "height": 1080, "fps": 30.0, "video_path": target}

    def merge_dubbed_audio(
        self,
        video_path: str,
        master_audio_path: str,
        output_video_path: str = None,
        music_audio_path: str = None,
        background_volume: float = 0.30
    ) -> bool:
        """
        Merge dubbed audio track with original video stream using FFmpeg.
        Mixes dubbed Khmer voice (100%) with original background music/sound effects.
        """
        if not os.path.exists(video_path) or not os.path.exists(master_audio_path):
            logger.error(f"❌ [VideoService] Missing input files for video merge: {video_path}, {master_audio_path}")
            return False

        if not output_video_path:
            output_video_path = get_output_path("dubbed_khmer.mp4")

        os.makedirs(os.path.dirname(output_video_path), exist_ok=True)

        # Ensure background audio track is available if background_volume > 0
        if (not music_audio_path or not os.path.exists(music_audio_path)) and background_volume > 0.0:
            from utils.file_utils import get_temp_path
            temp_bg = get_temp_path("background.wav")
            if os.path.exists(temp_bg) and os.path.getsize(temp_bg) > 1000:
                music_audio_path = temp_bg
            else:
                try:
                    from services.audio_separator import AudioSeparationService
                    sep = AudioSeparationService().extract_and_separate(video_path)
                    music_audio_path = sep.get("background_audio") or sep.get("original_audio")
                except Exception as e_sep:
                    logger.warning(f"Could not auto-extract fallback BGM: {e_sep}")

        from utils.file_utils import get_temp_path, OUTPUT_DIR
        import shutil

        safe_temp_output = get_temp_path(f"mux_final_{os.path.basename(output_video_path)}")
        if os.path.exists(safe_temp_output):
            try: os.remove(safe_temp_output)
            except Exception: pass

        if music_audio_path and os.path.exists(music_audio_path) and background_volume > 0.0:
            bg_vol_str = f"{max(0.05, min(1.0, background_volume)):.2f}"
            logger.info(f"🎶 [VideoService] Mixing Khmer Voice (100%) with BGM ({int(float(bg_vol_str)*100)}%) + Studio Sidechain Auto-Ducking...")
            # When Khmer voice [1:a] speaks, BGM [2:a] ducks smoothly by ~6-8dB
            # When Khmer voice is silent, BGM smoothly returns to full natural volume!
            # Soft limiter prevents digital clipping
            filter_str = (
                f"[2:a]volume={bg_vol_str}[bgm_scaled];"
                f"[bgm_scaled][1:a]sidechaincompress=threshold=0.06:ratio=2.0:attack=40:release=320[ducked_bgm];"
                f"[1:a]volume=1.3[v_boost];"
                f"[v_boost][ducked_bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.99[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", master_audio_path,
                "-i", music_audio_path,
                "-filter_complex", filter_str,
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-shortest",
                safe_temp_output
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", master_audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                safe_temp_output
            ]

        try:
            logger.info(f"🎬 [VideoService] Merging video with dubbed audio -> {os.path.basename(output_video_path)}")
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and os.path.exists(safe_temp_output) and os.path.getsize(safe_temp_output) > 1000:
                delivered = False
                try:
                    target_dir = os.path.dirname(os.path.abspath(output_video_path))
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.copyfile(safe_temp_output, output_video_path)
                    if os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 1000:
                        delivered = True
                        logger.info(f"✅ [VideoService] Final Dubbed Video Created: {output_video_path}")
                except Exception as e_copy:
                    logger.warning(f"⚠️ Target folder not permitted by macOS ({e_copy}).")
                
                if not delivered:
                    fallback_path = os.path.join(str(OUTPUT_DIR), os.path.basename(output_video_path))
                    try:
                        shutil.copyfile(safe_temp_output, fallback_path)
                        logger.info(f"✅ [VideoService] Final Dubbed Video safely saved to project output: {fallback_path}")
                        delivered = True
                    except Exception as e_fall:
                        logger.error(f"❌ Fallback delivery to project output failed: {e_fall}")

                try:
                    if os.path.exists(safe_temp_output):
                        os.remove(safe_temp_output)
                except Exception:
                    pass

                return delivered
            else:
                logger.error(f"❌ [VideoService] FFmpeg multiplexing error: {res.stderr}")
                return False
        except Exception as e:
            logger.error(f"❌ [VideoService] Merge exception: {e}")
            return False
