"""
Batch Dubbing Worker
Sequentially processes multiple videos through the complete end-to-end pipeline:
1. Extract & Normalize 24kHz vocal audio with filter
2. Smart 120s chunking with 5s overlap
3. Google Gemini Flash 3-in-1 Multimodal Translation
4. Merge & Deduplicate segments into clean Khmer
5. Standard SRT generation
6. TTS Dubbing & LipSync Audio Synchronization
7. Final Video Export with Apple Silicon VideoToolbox
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from qt_compat import QThread, Signal
from services.gemini_audio_service import GeminiAudioService
from core.tts import TextToSpeech
from core.dialogue_sync import DialogueSyncEngine
from core.video_processor import VideoProcessor
from services.audio_separator import create_clean_background_track
from utils.file_utils import OUTPUT_DIR, ensure_directories, get_temp_path
from utils.logger import logger


class BatchDubbingWorker(QThread):
    item_status_changed = Signal(str, str, int)  # (video_path, status_text, progress_pct)
    overall_progress = Signal(int, str)          # (overall_pct, status_text)
    log_message = Signal(str)
    item_completed = Signal(str, str)            # (video_path, final_output_mp4)
    batch_finished = Signal(int, int)            # (success_count, total_count)

    def __init__(
        self,
        video_paths: List[str],
        effects_config: Optional[Dict] = None,
        background_volume: float = 0.20,
        voice_name: str = "Khmer Male - Piseth",
        target_resolution: str = "Match Source",
        parent=None
    ):
        super().__init__(parent)
        self.video_paths = list(video_paths)
        self.effects_config = effects_config or {}
        self.background_volume = background_volume
        self.voice_name = voice_name
        self.target_resolution = target_resolution
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        self.log_message.emit("⏹ Batch processing cancellation requested...")

    def run(self):
        total_videos = len(self.video_paths)
        if total_videos == 0:
            self.batch_finished.emit(0, 0)
            return

        ensure_directories()
        success_count = 0
        gemini_service = GeminiAudioService()

        self.log_message.emit(f"🎬 Starting Batch Dubbing for {total_videos} videos...")

        for idx, video_path in enumerate(self.video_paths):
            if self._is_cancelled:
                self.log_message.emit("⏹ Batch stopped by user.")
                break

            base_name = Path(video_path).stem
            curr_overall_pct = int((idx / float(total_videos)) * 100)
            self.overall_progress.emit(curr_overall_pct, f"Processing {idx+1}/{total_videos}: {base_name}")
            self.log_message.emit(f"\n=======================================================")
            self.log_message.emit(f"🎬 [{idx+1}/{total_videos}] Processing: {os.path.basename(video_path)}")
            self.log_message.emit(f"=======================================================")

            self.item_status_changed.emit(video_path, "1/5 Extracting 24k...", 10)

            try:
                # ---------------- 1 & 2 & 3 & 4. PRODUCTION PIPELINE (Faster-Whisper + Gemini Text) ----------------
                def _pipeline_progress(pct, msg):
                    if self._is_cancelled:
                        return
                    self.item_status_changed.emit(video_path, f"{msg}", pct)
                    self.log_message.emit(f"  [{base_name}] {msg}")

                out_srt_path = os.path.join(str(OUTPUT_DIR), f"{base_name}_khmer.srt")
                if os.path.exists(out_srt_path) and os.path.getsize(out_srt_path) > 100:
                    self.log_message.emit(f"⚡ [{base_name}] Reusing already generated SRT ({os.path.getsize(out_srt_path):,} bytes)...")
                    with open(out_srt_path, "r", encoding="utf-8") as f:
                        srt_content = f.read()
                    from core.srt_translator import SRTTranslator
                    st = SRTTranslator()
                    sub_segs = st.parse_srt(srt_content)
                    segments = [{"start": s.start_seconds, "end": s.end_seconds, "original_text": s.text, "khmer_text": s.text} for s in sub_segs]
                else:
                    _pipeline_progress(10, "1/6 Extracting Audio...")
                    from services.audio_separator import AudioSeparationService
                    sep_service = AudioSeparationService()
                    sep_result = sep_service.extract_and_separate(video_path)
                    vocal_wav = sep_result.get("dialogue_audio") or sep_result.get("vocal_audio") or sep_result.get("original_audio")

                    if self._is_cancelled:
                        break

                    _pipeline_progress(25, "2/6 Detecting Speech...")
                    _pipeline_progress(35, "3/6 Transcribing with Faster-Whisper...")
                    from services.stt_service import STTService
                    stt_service = STTService()
                    stt_segments = stt_service.transcribe(
                        vocal_wav,
                        source_lang="zh",
                        progress_callback=lambda p, m: _pipeline_progress(int(25 + 0.25 * p), f"3/6 Whisper: {m}")
                    )

                    if self._is_cancelled:
                        break

                    if not stt_segments:
                        self.item_status_changed.emit(video_path, "No speech detected", 100)
                        self.log_message.emit(f"⚠️ [{base_name}] No speech detected by Faster-Whisper.")
                        continue

                    # Save Stage 1 Original Source SRT
                    try:
                        from utils.file_utils import export_segments_to_srt
                        orig_srt_p = os.path.join(str(OUTPUT_DIR), f"{base_name}_original.srt")
                        orig_sub_p = os.path.join(str(OUTPUT_DIR), "subtitles", f"{base_name}_original.srt")
                        export_segments_to_srt(stt_segments, orig_srt_p, text_key="original_text")
                        try: export_segments_to_srt(stt_segments, orig_sub_p, text_key="original_text")
                        except Exception: pass
                        self.log_message.emit(f"📄 [{base_name}] Original Source SRT saved: {orig_srt_p}")
                    except Exception as e_s:
                        self.log_message.emit(f"Notice saving original SRT: {e_s}")

                    _pipeline_progress(50, f"4/6 Translating {len(stt_segments)} lines with Gemini Text...")
                    from services.translation_service import TranslationService
                    trans_service = TranslationService()
                    segments = trans_service.translate_segments(
                        stt_segments,
                        source_lang="zh",
                        target_lang="km",
                        progress_callback=lambda p, m: _pipeline_progress(int(50 + 0.15 * p), f"4/6 Translating: {m}")
                    )

                    for s in segments:
                        if "khmer_text" not in s:
                            s["khmer_text"] = s.get("translated_text") or s.get("translation") or s.get("original_text") or ""
                        if "original_text" not in s:
                            s["original_text"] = s.get("text", "")

                    # Save generated SRT
                    from utils.gemini_parser import seconds_to_srt_time
                    srt_lines = []
                    for i_srt, seg_item in enumerate(segments, 1):
                        st_str = seconds_to_srt_time(float(seg_item["start"]))
                        et_str = seconds_to_srt_time(float(seg_item["end"]))
                        kh_str = seg_item.get("khmer_text", "")
                        srt_lines.append(f"{i_srt}\n{st_str} --> {et_str}\n{kh_str}\n")
                    with open(out_srt_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(srt_lines))

                if self._is_cancelled:
                    break

                if not segments:
                    self.item_status_changed.emit(video_path, "No speech detected", 100)
                    self.log_message.emit(f"⚠️ [{base_name}] No dialogue segments generated.")
                    continue

                self.log_message.emit(f"✅ [{base_name}] {len(segments)} Khmer dialogue lines generated & saved to SRT.")

                # ---------------- 6. TTS DUBBING & LIP-SYNC ----------------
                self.item_status_changed.emit(video_path, "🎙️ 6/7 TTS Dubbing...", 60)
                self.log_message.emit(f"🎙️ [{base_name}] Synthesizing Khmer voices with LipSync alignment...")

                # Assign voices based on detected speaker tag or gender
                for s in segments:
                    g = (s.get("gender") or "").lower()
                    spk = str(s.get("speaker_tag") or s.get("character") or s.get("khmer_text") or "").lower()
                    if "ចាស់ស្រី" in spk or g == "elder_female":
                        s["voice"] = "Khmer Elder - Female (Grandmother)"
                    elif "ចាស់ប្រុស" in spk or "ចាស់" in spk or g in ("elder", "elder_male"):
                        s["voice"] = "Khmer Elder - Male (Grandfather)"
                    elif "ក្មេង" in spk or g == "child":
                        s["voice"] = "Khmer Child - Boy (Vannak)"
                    elif "ស្រី" in spk or g == "female":
                        s["voice"] = "Khmer Female - Sreymom"
                    elif not s.get("voice"):
                        s["voice"] = "Khmer Male - Piseth"

                tts_engine = TextToSpeech(voice_name=self.voice_name)
                raw_audio_paths = tts_engine.generate_all_segments(
                    segments,
                    progress_callback=lambda c, t, inf: self.item_status_changed.emit(
                        video_path, f"🎙️ TTS ({c}/{t})", int(60 + 20 * (c / float(max(1, t))))
                    ),
                    max_workers=3,
                    is_cancelled_fn=lambda: self._is_cancelled
                )

                if self._is_cancelled:
                    break

                # Measure video duration
                video_proc = VideoProcessor(video_path)
                total_duration = video_proc.get_duration() or 60.0

                # Synchronize timeline
                self.item_status_changed.emit(video_path, "⏱ Aligning timeline...", 82)
                sync_engine = DialogueSyncEngine()
                master_khmer_wav = sync_engine.sync_dialogue_timeline(
                    segments=segments,
                    seg_audio_paths=raw_audio_paths,
                    total_duration_sec=total_duration,
                    output_master_path=get_temp_path(f"master_khmer_{base_name}.wav")
                )

                # Save copy of dubbed MP3
                out_mp3_path = os.path.join(str(OUTPUT_DIR), f"{base_name}_khmer_dubbed.mp3")
                from services.voxcpm_service import export_mp3
                export_mp3(master_khmer_wav, out_mp3_path)

                # Prepare BGM track with vocal ducking
                music_track = None
                if self.background_volume > 0.0:
                    try:
                        orig_audio = get_temp_path(f"orig_audio_{base_name}.wav")
                        from core.audio_processor import AudioProcessor
                        AudioProcessor(video_path).extract_audio(orig_audio)
                        if os.path.exists(orig_audio):
                            bgm_clean = get_temp_path(f"bgm_clean_{base_name}.wav")
                            create_clean_background_track(
                                orig_audio_path=orig_audio,
                                segments=segments,
                                output_bgm_path=bgm_clean,
                                bgm_volume=self.background_volume,
                                duck_speech_db=-80.0,
                                pre_pad_sec=0.08,
                                post_pad_sec=0.12
                            )
                            music_track = bgm_clean
                    except Exception as e_bg:
                        self.log_message.emit(f"Notice on BGM cleaning: {e_bg}")

                # ---------------- 7. EXPORT FINAL MP4 ----------------
                self.item_status_changed.emit(video_path, "⚡ 7/7 Exporting Video...", 90)
                self.log_message.emit(f"⚡ [{base_name}] Exporting final dubbed video with Apple Silicon VideoToolbox...")

                final_output_mp4 = os.path.join(str(OUTPUT_DIR), f"{base_name}_khmer_dubbed.mp4")

                if self.effects_config and any(self.effects_config.values()):
                    export_success = video_proc.export_with_effects_and_audio(
                        effects_config=self.effects_config,
                        output_path=final_output_mp4,
                        master_audio_path=master_khmer_wav,
                        music_audio_path=music_track,
                        background_volume=self.background_volume,
                        target_resolution=self.target_resolution,
                        progress_callback=lambda pct, msg: self.item_status_changed.emit(
                            video_path, f"Export {pct}%", int(90 + 0.1 * pct)
                        )
                    )
                else:
                    export_success = video_proc.merge_dubbed_audio(
                        dubbed_audio_path=master_khmer_wav,
                        output_video_path=final_output_mp4,
                        music_audio_path=music_track,
                        background_volume=self.background_volume
                    )

                if export_success and os.path.exists(final_output_mp4) and os.path.getsize(final_output_mp4) > 1000:
                    success_count += 1
                    self.item_status_changed.emit(video_path, "Completed ✅", 100)
                    self.item_completed.emit(video_path, final_output_mp4)
                    self.log_message.emit(f"🎉 [{base_name}] COMPLETED! Saved to: {final_output_mp4}")
                else:
                    self.item_status_changed.emit(video_path, "Export Failed ❌", 100)
                    self.log_message.emit(f"❌ [{base_name}] Export failed.")

            except Exception as e_proc:
                logger.error(f"Error processing video {video_path}: {e_proc}", exc_info=True)
                self.item_status_changed.emit(video_path, f"Error: {str(e_proc)[:30]}", 0)
                self.log_message.emit(f"❌ [{base_name}] Error: {e_proc}")

        self.overall_progress.emit(100, f"Batch Finished: {success_count}/{total_videos} videos exported.")
        self.log_message.emit(f"\n🏁 Batch Processing Finished! {success_count}/{total_videos} videos successfully exported to: {OUTPUT_DIR}")
        self.batch_finished.emit(success_count, total_videos)
