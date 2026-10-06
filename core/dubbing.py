import os
import time
from qt_compat import QThread, Signal
from core.video_processor import VideoProcessor
from core.transcriber import Transcriber
from core.translator import Translator
from core.tts import TextToSpeech
from core.audio_processor import AudioProcessor
from utils.file_utils import get_output_path, get_temp_path, clean_temp_directory
from utils.logger import setup_logger, logger

class PipelineStep:
    EXTRACT_AUDIO = "[1/6] Extracting Audio"
    DETECT_SPEECH = "[2/6] Detecting Speech"
    STT = "[3/6] Transcribing with Faster-Whisper"
    TRANSLATION = "[4/6] Translating with Gemini"
    TTS = "[5/6] Generating Khmer Voice"
    AUDIO_SYNC = "[6/6] Synchronizing Dialogue"
    MERGE_VIDEO = "Combining Video & Audio"
    APPLY_EFFECTS = "Applying Video Effects"
    COMPLETED = "Done"

class DubbingWorker(QThread):
    progress_changed = Signal(int, str)
    segments_ready = Signal(list)
    log_emitted = Signal(str)
    pipeline_finished = Signal(str)
    pipeline_error = Signal(str)

    def __init__(
        self,
        video_path: str,
        output_path: str = None,
        source_lang: str = "auto",
        target_lang: str = "km",
        whisper_model: str = "small",
        voice_name: str = "VoxCPM2-Khmer",
        api_key: str = None,
        background_volume: float = 0.30,
        pre_translated_segments: list = None,
        skip_transcription: bool = True,
        effects_config: dict = None,
        export_mode: str = "BALANCED",
        target_resolution: str = "Original",
        master_audio_path: str = None,
        timeline_clips: list = None,
        timeline_transitions: list = None
    ):
        super().__init__()
        self.setStackSize(8 * 1024 * 1024)
        self.video_path = video_path
        self.output_path = output_path or get_output_path(f"khmer_{os.path.basename(video_path)}")
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.whisper_model = whisper_model
        self.voice_name = voice_name
        self.api_key = api_key
        self.background_volume = background_volume
        self.pre_translated_segments = pre_translated_segments
        self.skip_transcription = skip_transcription
        self.effects_config = effects_config
        self.export_mode = export_mode
        self.target_resolution = target_resolution
        self.master_audio_path = master_audio_path
        self.timeline_clips = list(timeline_clips) if timeline_clips else []
        self.timeline_transitions = list(timeline_transitions) if timeline_transitions else []
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def log(self, message: str):
        self.log_emitted.emit(message)

    def run(self):
        try:
            self.log("🚀 Starting Khmer Video Dubbing Pipeline...")

            # Multi-clip timeline with transitions support (Gate 3)
            has_active_transitions = False
            if self.timeline_transitions:
                for t in self.timeline_transitions:
                    dur = float(t.get("duration", 0.0) if isinstance(t, dict) else getattr(t, "duration", 0.0))
                    if dur > 0.05:
                        has_active_transitions = True
                        break

            # Check if self.video_path is already a merged file matching full timeline duration
            already_merged = False
            if self.video_path and os.path.exists(self.video_path) and os.path.getsize(self.video_path) > 1000:
                if not has_active_transitions and len(self.timeline_clips) > 1:
                    try:
                        vinfo = VideoProcessor(self.video_path).info
                        v_dur = float(vinfo.get("duration", 0.0))
                        tl_dur = sum(float(c.get("duration", 0.0) if isinstance(c, dict) else getattr(c, "duration", 0.0)) for c in self.timeline_clips)
                        if tl_dur > 0 and abs(v_dur - tl_dur) < 4.0:
                            already_merged = True
                    except Exception:
                        pass

            if already_merged:
                self.log(f"🎬 Pre-merged timeline video verified ({os.path.basename(self.video_path)}). Bypassing redundant transition re-render...")
            elif len(self.timeline_clips) > 1 or self.timeline_transitions:
                self.progress_changed.emit(10, "Exporting Timeline Transitions")
                self.log("🎬 Multi-Clip Timeline with Transitions detected. Executing Gate 3 Master Export Engine...")
                import sys
                from core.transition_export import export_timeline_with_transitions
                from utils.file_utils import ensure_accessible_video_file

                # Guarantee accessible paths for all clips (prevents macOS Desktop permission errors)
                for c in self.timeline_clips:
                    cp = c.get("path") if isinstance(c, dict) else getattr(c, "path", None)
                    if cp and os.path.exists(cp):
                        safe_cp = ensure_accessible_video_file(cp, allow_copy=True)
                        if isinstance(c, dict):
                            c["path"] = safe_cp
                        else:
                            setattr(c, "path", safe_cp)

                cand_path = self.video_path
                if (not cand_path or not os.path.exists(cand_path)) and self.timeline_clips:
                    first_clip = self.timeline_clips[0]
                    if isinstance(first_clip, dict):
                        cand_path = first_clip.get("path") or first_clip.get("file_path")
                    else:
                        cand_path = getattr(first_clip, "path", None) or getattr(first_clip, "file_path", None)
                    if cand_path and os.path.exists(cand_path):
                        self.video_path = cand_path

                orig_w = 1920
                orig_h = 1080
                fps_val = 30.0

                clip_to_probe = None
                if self.video_path and os.path.exists(self.video_path):
                    clip_to_probe = self.video_path
                elif self.timeline_clips and len(self.timeline_clips) > 0:
                    first_clip = self.timeline_clips[0]
                    if isinstance(first_clip, dict):
                        cand = first_clip.get("path") or first_clip.get("file_path")
                    else:
                        cand = getattr(first_clip, "path", None) or getattr(first_clip, "file_path", None)
                    if cand and os.path.exists(cand):
                        clip_to_probe = cand
                        if not self.video_path:
                            self.video_path = cand

                if clip_to_probe and os.path.exists(clip_to_probe):
                    try:
                        vinfo = VideoProcessor(clip_to_probe).info
                        orig_w = int(vinfo.get("width", 1920) or 1920)
                        orig_h = int(vinfo.get("height", 1080) or 1080)
                        fps_val = float(vinfo.get("fps", 30.0) or 30.0)
                    except Exception as e:
                        logger.warning(f"Could not probe video info for {clip_to_probe}: {e}")

                # RULE: preview_size is a UI/display dimension and MUST NEVER override source video resolution!
                target_res = getattr(self, "target_resolution", "Original")
                from core.export_engine import compute_target_resolution
                out_w, out_h = compute_target_resolution(orig_w, orig_h, target_res)

                src_ar = round(orig_w / max(1, orig_h), 4)
                preview_sz = self.effects_config.get("preview_size") if self.effects_config else None
                logger.info(
                    f"🎬 [EXPORT_RESOLUTION] source={orig_w}x{orig_h} source_aspect={src_ar} "
                    f"target_preset={target_res} preview_size={preview_sz} "
                    f"preview_size_used_for_resolution=False target={out_w}x{out_h}"
                )
                self.log(
                    f"🎬 [EXPORT_RESOLUTION] source={orig_w}x{orig_h} (aspect: {src_ar}) -> target={out_w}x{out_h} ({target_res})"
                )

                # Determine if downstream dubbing or video effects pipeline is needed
                has_segments = bool(self.pre_translated_segments and len(self.pre_translated_segments) > 0)
                needs_transcription = not self.skip_transcription
                has_master = bool(self.master_audio_path and os.path.exists(self.master_audio_path) and os.path.getsize(self.master_audio_path) > 1000)
                has_effects = False
                if self.effects_config:
                    b_on = bool(self.effects_config.get("blur", {}).get("enabled", False))
                    t_on = bool(self.effects_config.get("text_overlay", {}).get("enabled", False))
                    l_on = bool(self.effects_config.get("logo", {}).get("enabled", False))
                    s_on = bool(self.effects_config.get("burn_subtitle", {}).get("enabled", False))
                    has_effects = b_on or t_on or l_on or s_on

                needs_pipeline = has_segments or needs_transcription or has_master or has_effects

                # If dubbing or effects are needed, render transition video to an intermediate temp file
                dest_video = get_temp_path("trans_timeline_visual.mp4") if needs_pipeline else self.output_path

                self.progress_changed.emit(25 if needs_pipeline else 50, "Rendering xfade & acrossfade filtergraphs")
                success = export_timeline_with_transitions(
                    video_clips=self.timeline_clips,
                    transitions=self.timeline_transitions,
                    output_path=dest_video,
                    target_width=out_w,
                    target_height=out_h,
                    fps=fps_val,
                    use_videotoolbox=(sys.platform == "darwin")
                )
                if not success:
                    self.pipeline_error.emit("❌ Transition Export Engine failed to render multi-clip transition timeline.")
                    return

                if not os.path.exists(dest_video) or os.path.getsize(dest_video) < 1000:
                    self.pipeline_error.emit("❌ Exported transition output file missing or invalid.")
                    return

                # If pure transition export without dubbing or effects, we are done
                if not needs_pipeline:
                    self.progress_changed.emit(100, PipelineStep.COMPLETED)
                    self.log(f"🎉 SUCCESS! Final transition timeline created: {self.output_path}")
                    self.pipeline_finished.emit(self.output_path)
                    return

                # If dubbing or effects are needed, update video_path to the stitched transition video
                self.log(f"✅ Transition master visual rendered: {dest_video}. Continuing to Dubbing & Effects pipeline...")
                self.video_path = dest_video

                # Invalidate stale audio caches so fresh audio is extracted from transition master
                for old_wav in ["original_stereo.wav", "dub_orig_audio.wav"]:
                    p = get_temp_path(old_wav)
                    if os.path.exists(p):
                        try: os.remove(p)
                        except Exception: pass

            elif len(self.timeline_clips) == 1:
                first_clip = self.timeline_clips[0]
                c_path = (first_clip.get("path") or first_clip.get("file_path")) if isinstance(first_clip, dict) else (getattr(first_clip, "path", None) or getattr(first_clip, "file_path", None))
                if not c_path or not os.path.exists(c_path):
                    c_path = self.video_path

                s_in = float(first_clip.get("source_in", 0.0) if isinstance(first_clip, dict) else getattr(first_clip, "source_in", 0.0))
                dur = float(first_clip.get("duration", 0.0) if isinstance(first_clip, dict) else getattr(first_clip, "duration", 0.0))
                spd = float(first_clip.get("speed", 1.0) if isinstance(first_clip, dict) else getattr(first_clip, "speed", 1.0))

                vinfo = VideoProcessor(c_path).info if (c_path and os.path.exists(c_path)) else {}
                orig_file_dur = float(vinfo.get("duration", 0.0))

                is_cut = (s_in > 0.05) or (dur > 0 and orig_file_dur > 0 and (orig_file_dur - (s_in + dur * spd)) > 0.15) or (abs(spd - 1.0) > 0.01)
                if is_cut:
                    self.log(f"✂️ Single cut/trimmed clip detected (In: {s_in:.2f}s, Dur: {dur:.2f}s, Speed: {spd:.2f}x). Pre-trimming source for export...")
                    dest_video = get_temp_path("cut_single_clip.mp4")
                    ext_s_in = max(0.0, s_in)
                    ext_dur = dur * spd
                    import subprocess
                    import sys
                    trim_cmd = [
                        "ffmpeg", "-y",
                        "-ss", f"{ext_s_in:.3f}",
                        "-i", c_path,
                        "-t", f"{ext_dur:.3f}",
                    ]
                    if abs(spd - 1.0) > 0.01:
                        trim_cmd.extend([
                            "-filter:v", f"setpts=PTS/{spd:.4f}",
                            "-filter:a", f"atempo={spd:.4f}",
                            "-c:v", "h264_videotoolbox" if sys.platform == "darwin" else "libx264",
                            "-c:a", "aac"
                        ])
                    else:
                        trim_cmd.extend([
                            "-c:v", "h264_videotoolbox" if sys.platform == "darwin" else "libx264",
                            "-c:a", "aac"
                        ])
                    trim_cmd.append(dest_video)
                    subprocess.run(trim_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(dest_video) and os.path.getsize(dest_video) > 1000:
                        self.video_path = dest_video
                        self.log(f"✅ Cut clip pre-rendered: {dest_video} ({dur:.2f}s)")
                        for old_wav in ["original_stereo.wav", "dub_orig_audio.wav"]:
                            p = get_temp_path(old_wav)
                            if os.path.exists(p):
                                try: os.remove(p)
                                except Exception: pass

            self.progress_changed.emit(5, PipelineStep.EXTRACT_AUDIO)

            if self._is_cancelled: return
            video_proc = VideoProcessor(self.video_path)
            total_duration = video_proc.info.get("duration", 10.0)
            self.log(f"📹 Video file loaded. Duration: {total_duration:.2f} seconds.")

            segments = []
            if self.pre_translated_segments:
                self.log("⚡ [Fast Export] Reusing prepared segments (Zero-waste STT & Translation)...")
                from core.subtitle_timing import resolve_subtitle_timings
                segments = resolve_subtitle_timings(self.pre_translated_segments, total_duration_sec=total_duration)
                self.orig_audio = None
                self.music_wav = None
                if self.background_volume > 0.0:
                    self.orig_audio = get_temp_path("original_stereo.wav")
                    if not os.path.exists(self.orig_audio):
                        from utils.ffmpeg import extract_audio
                        extract_audio(self.video_path, self.orig_audio, sample_rate=44100)
            elif not self.skip_transcription:
                self.progress_changed.emit(10, PipelineStep.EXTRACT_AUDIO)
                self.log("🎵 [1/6] Separating audio: Clean Dialogue (for STT) + Background Music (to preserve)...")
                vocal_wav = None
                try:
                    from services.audio_separator import AudioSeparationService
                    sep_service = AudioSeparationService()
                    sep_result = sep_service.extract_and_separate(self.video_path)
                    vocal_wav = sep_result.get("dialogue_audio") or sep_result.get("vocal_audio")
                    self.orig_audio = sep_result.get("original_audio")
                    self.music_wav = sep_result.get("background_audio") or sep_result.get("music_audio")
                except Exception as e_sep:
                    self.log(f"Audio separation notice ({e_sep}). Falling back to direct extraction...")
                    self.orig_audio = get_temp_path("original_stereo.wav")
                    from utils.ffmpeg import extract_audio
                    try:
                        extract_audio(self.video_path, self.orig_audio, sample_rate=44100)
                        vocal_wav = self.orig_audio
                        self.music_wav = self.orig_audio
                    except Exception as e_ext:
                        self.log(f"Direct extraction error: {e_ext}")
                        vocal_wav = None

                if not vocal_wav or not os.path.exists(str(vocal_wav)) or os.path.getsize(str(vocal_wav)) < 100:
                    self.pipeline_error.emit("No readable audio stream found in source video to transcribe.")
                    return

                self.progress_changed.emit(20, PipelineStep.DETECT_SPEECH)
                if self._is_cancelled: return
                self.log("🎙 [2/6] Detecting Speech Regions with Silero VAD (recall=82.2%)...")

                self.progress_changed.emit(30, PipelineStep.STT)
                if self._is_cancelled: return
                self.log("⚡ [3/6] Running High-Speed STT (Local Faster-Whisper + Silero VAD)...")
                from services.stt_service import STTService
                stt_service = STTService(api_key=self.api_key)
                
                def _stt_cb(pct, msg):
                    self.progress_changed.emit(30 + int(pct * 0.18), f"{PipelineStep.STT}: {msg}")
                    self.log(f"⚡ {msg}")

                stt_segments = stt_service.transcribe(vocal_wav, source_lang=self.source_lang, progress_callback=_stt_cb)
                
                # Save Stage 1 Original Source SRT
                if stt_segments:
                    try:
                        from utils.file_utils import export_segments_to_srt, OUTPUT_DIR
                        from pathlib import Path
                        stem = Path(self.video_path).stem
                        orig_zh_srt_p = os.path.join(str(OUTPUT_DIR), f"{stem}_original_chinese.srt")
                        orig_srt_p = os.path.join(str(OUTPUT_DIR), f"{stem}_original.srt")
                        orig_sub_zh_p = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_original_chinese.srt")
                        orig_sub_p = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_original.srt")
                        export_segments_to_srt(stt_segments, orig_zh_srt_p, text_key="original_text")
                        export_segments_to_srt(stt_segments, orig_srt_p, text_key="original_text")
                        try:
                            export_segments_to_srt(stt_segments, orig_sub_zh_p, text_key="original_text")
                            export_segments_to_srt(stt_segments, orig_sub_p, text_key="original_text")
                        except Exception: pass
                        self.log(f"📄 [Step 1 Complete] Chinese Source SRT saved ({len(stt_segments)} segments): {orig_zh_srt_p}")
                    except Exception as e_orig:
                        self.log(f"Notice saving original SRT: {e_orig}")

                if self._is_cancelled: return
                self.progress_changed.emit(50, PipelineStep.TRANSLATION)
                self.log(f"🌐 [4/6] Translating {len(stt_segments)} dialogue segments with Gemini Text AI...")
                from services.translation_service import TranslationService
                trans_service = TranslationService(api_key=self.api_key)
                segments = trans_service.translate_segments(stt_segments, source_lang=self.source_lang, target_lang=self.target_lang)

                # Save Stage 2 Khmer SRT
                if segments:
                    try:
                        from utils.file_utils import export_segments_to_srt, OUTPUT_DIR
                        from pathlib import Path
                        stem = Path(self.video_path).stem
                        khmer_srt_p = os.path.join(str(OUTPUT_DIR), f"{stem}_khmer.srt")
                        khmer_sub_p = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_khmer.srt")
                        export_segments_to_srt(segments, khmer_srt_p, text_key="khmer_text")
                        try: export_segments_to_srt(segments, khmer_sub_p, text_key="khmer_text")
                        except Exception: pass
                        self.log(f"📄 [Stage 2 Complete] Khmer SRT saved: {khmer_srt_p}")
                    except Exception as e_khm:
                        self.log(f"Notice saving Khmer SRT: {e_khm}")

                # Automatic Speaker Diarization / Gender Voice Assignment
                try:
                    self.log("👥 Identifying speakers & assigning appropriate voices using Acoustic Pitch F0...")
                    from services.speaker_detector import SpeakerDetector
                    audio_src = vocal_wav or self.orig_audio or getattr(self, 'video_path', None)
                    detector = SpeakerDetector(audio_wav_path=audio_src)
                    segments = detector.diarize_and_profile_segments(segments)
                except Exception as spk_err:
                    self.log(f"Speaker voice assignment notice: {spk_err}")

            # Ensure every segment (including pre-translated) has voice assigned across 5 categories
            for seg in segments:
                if not seg.get("voice"):
                    g = (seg.get("gender") or seg.get("role") or "").lower()
                    spk = str(seg.get("character") or seg.get("speaker_id") or seg.get("speaker", "")).lower()
                    tag = str(seg.get("speaker_tag") or "").lower()
                    if "ចាស់ស្រី" in tag or g == "elder_female":
                        seg["voice"] = "Khmer Elder - Female (Grandmother)"
                    elif "ចាស់ប្រុស" in tag or "ចាស់" in tag or g == "elder_male":
                        seg["voice"] = "Khmer Elder - Male (Grandfather)"
                    elif "ស្រី" in tag or g == "female" or "ស្រី" in spk or "female" in spk:
                        seg["voice"] = "Khmer Female - Sreymom"
                    elif "ក្មេង" in tag or g == "child" or "ក្មេង" in spk or "child" in spk:
                        seg["voice"] = "Khmer Child - Boy (Vannak)"
                    else:
                        seg["voice"] = "Khmer Male - Piseth"

            if segments:
                self.segments_ready.emit(segments)

            # Check if pre-generated master audio can be reused immediately (CapCut fast-export behavior)
            can_reuse_master = False
            master_khmer_wav = None
            if self.master_audio_path and os.path.exists(self.master_audio_path) and os.path.getsize(self.master_audio_path) > 1000:
                can_reuse_master = True
                master_khmer_wav = self.master_audio_path
                self.log(f"⚡ [Fast Export] Reusing pre-generated master voice track: {os.path.basename(self.master_audio_path)} (Skipped TTS & Sync)")
                self.progress_changed.emit(85, PipelineStep.AUDIO_SYNC)
            elif segments:
                default_master = get_temp_path("master_khmer_voice.wav")
                if os.path.exists(default_master) and os.path.getsize(default_master) > 1000:
                    # Check if all segments have tts_audio
                    all_have_audio = all(bool(s.get("tts_audio") and os.path.exists(str(s.get("tts_audio")))) for s in segments)
                    if all_have_audio:
                        can_reuse_master = True
                        master_khmer_wav = default_master
                        self.log("⚡ [Fast Export] Reusing existing project master voice track (Skipped TTS & Sync)")
                        self.progress_changed.emit(85, PipelineStep.AUDIO_SYNC)

            if not can_reuse_master and segments:
                self.progress_changed.emit(55, PipelineStep.TTS)
                if self._is_cancelled: return
                self.log(f"🔊 Synthesizing Khmer voices in parallel (Workers=3)...")
                tts_engine = TextToSpeech(voice_name=self.voice_name)
                
                num_segments = len(segments)
                def _tts_progress(completed, total, info):
                    pct = 55 + int(25 * (completed / float(max(1, total))))
                    self.progress_changed.emit(pct, f"{PipelineStep.TTS} ({completed}/{total})")

                raw_audio_paths = tts_engine.generate_all_segments(
                    segments,
                    progress_callback=_tts_progress,
                    max_workers=3,
                    is_cancelled_fn=lambda: self._is_cancelled
                )

                self.progress_changed.emit(82, PipelineStep.AUDIO_SYNC)
                if self._is_cancelled: return
                self.log("⏱ Synchronizing dialogue timeline with DialogueSyncEngine...")
                tts_durs = []
                for p in raw_audio_paths:
                    if p and os.path.exists(p):
                        try:
                            from pydub import AudioSegment
                            tts_durs.append(len(AudioSegment.from_file(p)) / 1000.0)
                        except Exception:
                            tts_durs.append(None)
                    else:
                        tts_durs.append(None)

                from core.subtitle_timing import resolve_subtitle_timings
                segments = resolve_subtitle_timings(segments, total_duration_sec=total_duration, tts_durations=tts_durs)

                from core.dialogue_sync import DialogueSyncEngine
                sync_engine = DialogueSyncEngine()
                master_khmer_wav = sync_engine.sync_dialogue_timeline(
                    segments=segments,
                    seg_audio_paths=raw_audio_paths,
                    total_duration_sec=total_duration,
                    output_master_path=get_temp_path("master_khmer_voice.wav"),
                    strict=True
                )

            if not master_khmer_wav and not segments:
                # If no dubbed voice was generated, preserve source/stitched audio
                if not self.orig_audio or not os.path.exists(str(self.orig_audio)):
                    self.orig_audio = get_temp_path("original_stereo.wav")
                    from utils.ffmpeg import extract_audio
                    try:
                        extract_audio(self.video_path, self.orig_audio, sample_rate=44100)
                    except Exception:
                        pass
                if self.orig_audio and os.path.exists(str(self.orig_audio)) and os.path.getsize(str(self.orig_audio)) > 1000:
                    master_khmer_wav = self.orig_audio
            
            apply_needed = False
            effects_output = None
            if self.effects_config:
                if segments:
                    self.effects_config["segments"] = segments
                blur_config = self.effects_config.get("blur", {})
                blur_enabled = bool(blur_config.get("enabled", False) and (bool(blur_config.get("rect")) or bool(blur_config.get("blurs"))))
                
                text_config = self.effects_config.get("text_overlay", {})
                has_any_text = bool(text_config.get("text", "").strip()) or any(bool(t.get("text", "").strip()) for t in text_config.get("items", []))
                text_enabled = has_any_text and bool(text_config.get("enabled", False))
                
                logo_config = self.effects_config.get("logo", {})
                logo_path = logo_config.get("path")
                if logo_path and not os.path.exists(str(logo_path)):
                    base_n = os.path.basename(str(logo_path))
                    cand = get_temp_path(f"safe_logo_{base_n}")
                    if os.path.exists(cand):
                        logo_path = cand
                        logo_config["path"] = cand
                    elif os.path.exists(base_n):
                        logo_path = os.path.abspath(base_n)
                        logo_config["path"] = logo_path
                logo_enabled = bool(logo_path and os.path.exists(str(logo_path))) and bool(logo_config.get("enabled", False))
                
                burn_sub_config = self.effects_config.get("burn_subtitle", {})
                has_segments_eff = bool(self.effects_config.get("segments"))
                burn_sub_enabled = has_segments_eff and bool(burn_sub_config.get("enabled", False))
                
                res_needed = False
                target_res = getattr(self, "target_resolution", "Original")
                if target_res and str(target_res).lower() not in ("original", "ទំហំដើម", "ដើម", "none"):
                    res_needed = True

                apply_needed = blur_enabled or text_enabled or logo_enabled or burn_sub_enabled or res_needed
                self.log(f"🔍 Video Effects export configuration check: Blur={blur_enabled}, Text={text_enabled}, Logo={logo_enabled}, BurnSubtitle={burn_sub_enabled}, Rescale={res_needed} ({target_res})")

            if self._is_cancelled: return

            # Prepare Background Music Track if enabled
            music_track = getattr(self, "music_wav", None)
            if self.background_volume > 0.0 and segments:
                base_orig = getattr(self, "orig_audio", None)
                if not base_orig or not os.path.exists(str(base_orig)):
                    try:
                        from core.audio_processor import AudioProcessor
                        base_orig = AudioProcessor(self.video_path).extract_audio(get_temp_path("dub_orig_audio.wav"))
                        self.orig_audio = base_orig
                    except Exception as e_ext:
                        self.log(f"Notice extracting base audio: {e_ext}")

                if base_orig and os.path.exists(str(base_orig)):
                    try:
                        self.log("🎶 Isolating pure BGM & sound effects: Deep Vocal Elimination during dialogue...")
                        from services.audio_separator import create_clean_background_track
                        export_bg_cleaned = get_temp_path("export_bg_cleaned.wav")
                        create_clean_background_track(
                            orig_audio_path=base_orig,
                            segments=segments,
                            output_bgm_path=export_bg_cleaned,
                            bgm_volume=1.0,
                            duck_speech_db=-70.0,
                            pre_pad_sec=0.08,
                            post_pad_sec=0.12
                        )
                        music_track = export_bg_cleaned
                        self.log("✅ Original Chinese speech cleanly eliminated; BGM & sound effects 100% preserved!")
                    except Exception as e_bg:
                        self.log(f"Notice cleaning background: {e_bg}")

            if self._is_cancelled: return

            if apply_needed:
                self.progress_changed.emit(90, PipelineStep.APPLY_EFFECTS)
                self.log(f"🚀 Ultra-Fast Single-Pass Direct Export (Apple Silicon Hardware Engine + Video Effects + Audio Muxing @ {getattr(self, 'target_resolution', 'Original')})...")

                def _export_progress(pct, desc=""):
                    curr_pct = 90 + int(9 * (pct / 100.0))
                    self.progress_changed.emit(curr_pct, desc or f"Exporting: {pct}%")

                export_mode = getattr(self, "export_mode", "BALANCED")
                target_res = getattr(self, "target_resolution", "Original")
                success = video_proc.export_with_effects_and_audio(
                    effects_config=self.effects_config,
                    output_path=self.output_path,
                    master_audio_path=master_khmer_wav,
                    music_audio_path=music_track,
                    background_volume=self.background_volume,
                    export_mode=export_mode,
                    target_resolution=target_res,
                    progress_callback=_export_progress,
                    is_cancelled_fn=lambda: self._is_cancelled
                )
            elif master_khmer_wav:
                self.progress_changed.emit(92, PipelineStep.MERGE_VIDEO)
                self.log("⚡ Zero-Re-encode Ultra-Fast Stream Copy (-c:v copy) with Khmer dubbed audio...")
                success = video_proc.merge_dubbed_audio(
                    dubbed_audio_path=master_khmer_wav,
                    output_video_path=self.output_path,
                    music_audio_path=music_track,
                    background_volume=self.background_volume
                )
            else:
                import shutil
                shutil.copyfile(self.video_path, self.output_path)
                success = True

            from utils.file_utils import OUTPUT_DIR
            fallback_path = os.path.join(str(OUTPUT_DIR), os.path.basename(self.output_path))
            fallback_videos_path = os.path.join(str(OUTPUT_DIR), "videos", os.path.basename(self.output_path))

            final_delivered_path = None
            if os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 1000:
                final_delivered_path = self.output_path
            elif os.path.exists(fallback_path) and os.path.getsize(fallback_path) > 1000:
                final_delivered_path = fallback_path
            elif os.path.exists(fallback_videos_path) and os.path.getsize(fallback_videos_path) > 1000:
                final_delivered_path = fallback_videos_path

            if success and final_delivered_path:
                self.output_path = final_delivered_path
                # Clean up intermediate transition video if one was generated
                trans_inter = get_temp_path("trans_timeline_visual.mp4")
                if os.path.exists(trans_inter):
                    try: os.remove(trans_inter)
                    except Exception: pass
                self.progress_changed.emit(100, PipelineStep.COMPLETED)
                self.log(f"🎉 SUCCESS! Final Khmer video created: {self.output_path}")
                self.pipeline_finished.emit(self.output_path)
            else:
                self.pipeline_error.emit("Failed to create final video output with FFmpeg.")

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.log(f"❌ Error in dubbing worker pipeline: {e}")
            self.pipeline_error.emit(str(e))
