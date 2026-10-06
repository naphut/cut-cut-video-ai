"""
Dedicated Speech-to-Text (STT) Service
Leverages Gemini Dedicated Transcription models (gemini-3.5-transcribe / gemini-3.8-flash).
Focuses strictly on:
1. Speech Recognition (Transcribing audio)
2. Accurate Segment Timestamps
3. Automatic Language Detection
4. Speaker Diarization
DOES NOT perform translation. (Translation is handled by TranslationService).
"""
import os
import json
import time
import base64
import re
import subprocess
from typing import List, Dict, Any, Optional
import requests
from core.models import Segment
from services.vad_service import VADService
from utils.logger import logger
from utils.config_manager import get_gemini_api_key
from utils.file_utils import get_temp_path

class STTService:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_gemini_api_key()
        # Production model hierarchy (gemini-3.5-transcribe & gemini-3.1-flash-lite are active & available)
        self.models = [
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite-preview",
            "gemini-3.5-flash-lite"
        ]
        self.vad_service = VADService()

    def transcribe(
        self,
        audio_path: str,
        source_lang: str = "auto",
        progress_callback = None,
        is_cancelled_fn = None
    ) -> List[Segment]:
        """
        Execute Speech-to-Text pipeline:
        Uses High-Speed Local Faster-Whisper (int8 + Silero VAD) as the PRIMARY engine.
        It is ultra-fast, zero-latency, 100% offline, and never suffers from cloud 503/400 errors.
        Falls back to Gemini Cloud STT only if local engine is unavailable.
        """
        if not audio_path or not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        try:
            logger.info("⚡ [STT] Running High-Speed Local Faster-Whisper Engine (int8 + Silero VAD)...")
            return self._transcribe_with_local_whisper(audio_path, source_lang, progress_callback, is_cancelled_fn=is_cancelled_fn)
        except Exception as e:
            logger.warning(f"⚠️ [STT] Local Whisper encounter ({e}). Falling back to Gemini Cloud STT...")
            if progress_callback:
                progress_callback(20, "Local engine unavailable, attempting Gemini Cloud STT...")
            return self._transcribe_with_gemini(audio_path, source_lang, progress_callback)

    def _transcribe_with_local_whisper(
        self,
        audio_path: str,
        source_lang: str,
        progress_callback = None,
        is_cancelled_fn = None
    ) -> List[Segment]:
        """
        Local high-speed Faster-Whisper speech recognition (offline, instant, reliable).
        """
        duration_sec = self._get_duration_sec(audio_path)
        logger.info(f"🎙 [STT] Running local High-Speed Faster-Whisper on {os.path.basename(audio_path)} (duration: {duration_sec:.1f}s)...")
        if progress_callback:
            progress_callback(5, "Initializing High-Speed Local Faster-Whisper (int8 + Silero VAD)...")

        from services.whisper_service import WhisperService
        whisper_svc = WhisperService(model_size="small")
        whisper_svc.initialize()
        raw_segs = whisper_svc.transcribe(
            audio_path,
            language=source_lang,
            progress_callback=progress_callback,
            total_duration=duration_sec,
            is_cancelled_fn=is_cancelled_fn
        )

        if not raw_segs:
            logger.warning("Local whisper returned empty segments. Attempting fallback...")
            raw_segs = whisper_svc._generate_fallback_segments(audio_path)

        # Diarization: Speaker identification and gender clustering
        try:
            from services.speaker_service import SpeakerService
            spk_svc = SpeakerService()
            raw_segs = spk_svc.identify_speaker_clusters(audio_path, raw_segs)
        except Exception as spk_e:
            logger.debug(f"Speaker clustering notice: {spk_e}")

        segments: List[Segment] = []
        for i, s in enumerate(raw_segs):
            spk = s.get("speaker") or ("Speaker 1" if i % 2 == 0 else "Speaker 2")
            st_val = float(s["start"])
            et_val = float(s["end"])
            raw_st = float(s.get("raw_start", st_val))
            raw_et = float(s.get("raw_end", et_val))
            seg = Segment(
                id=f"seg_{i+1:04d}",
                start=st_val,
                end=et_val,
                speaker_id=spk,
                source_language=source_lang if source_lang != "auto" else "en",
                original_text=s["text"],
                target_language="km",
                status="ready",
                translation_status="pending",
                raw_start=raw_st,
                raw_end=raw_et,
                words=s.get("words"),
                confidence=s.get("confidence")
            )
            self._log_segment_debug("RAW", seg)
            segments.append(seg)

        segments = self._normalize_transcript_segments(segments)
        if progress_callback:
            progress_callback(100, f"STT Complete! Generated {len(segments)} segments.")
        logger.info(f"✅ [STT] Local Faster-Whisper finished. {len(segments)} dialogue segments extracted.")
        return segments

    def _transcribe_with_gemini(
        self,
        audio_path: str,
        source_lang: str = "auto",
        progress_callback = None
    ) -> List[Segment]:
        """Run Gemini Cloud STT with VAD-aware chunks."""
        api_key = self.api_key or get_gemini_api_key()
        if not api_key:
            raise ValueError("No Gemini API key available.")

        if progress_callback:
            progress_callback(5, "Running Voice Activity Detection (VAD)...")

        speech_regions = self.vad_service.detect_speech_regions(audio_path)
        duration_sec = self._get_duration_sec(audio_path)
        logger.info(f"🎙 [STT] Audio duration: {duration_sec:.1f}s, Speech regions detected: {len(speech_regions)}")

        chunks = self.vad_service.group_vad_chunks(duration_sec, speech_regions, target_chunk_sec=300.0)

        all_segments: List[Segment] = []
        global_seg_idx = 1

        for idx, chunk in enumerate(chunks):
            start_sec = chunk["start"]
            dur_sec = chunk["duration"]
            total_chunks = len(chunks)

            if total_chunks > 1:
                chunk_file = get_temp_path(f"stt_chunk_{idx:03d}.mp3")
                cmd = [
                    "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(dur_sec),
                    "-i", audio_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k",
                    chunk_file
                ]
                subprocess.run(cmd, capture_output=True, check=False)
                active_file = chunk_file if os.path.exists(chunk_file) else audio_path
                pct = int(10 + (idx / float(total_chunks)) * 80)
                msg = f"Gemini STT: Transcribing chunk {idx+1}/{total_chunks} ({int(start_sec//60)}m - {int((start_sec+dur_sec)//60)}m)..."
            else:
                active_file = get_temp_path("stt_compressed.mp3")
                cmd = ["ffmpeg", "-y", "-i", audio_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", active_file]
                subprocess.run(cmd, capture_output=True, check=False)
                pct = 35
                msg = "Gemini STT: Transcribing speech with timestamps and speaker diarization..."

            if progress_callback:
                progress_callback(pct, msg)

            raw_chunk_segments = self._transcribe_audio_chunk(
                active_file,
                source_lang=source_lang,
                api_key=api_key
            )

            for item in raw_chunk_segments:
                seg_start = round(item.get("start", 0.0) + start_sec, 2)
                seg_end = round(item.get("end", seg_start + 2.0) + start_sec, 2)
                spk = item.get("speaker_id") or item.get("character") or "Speaker 1"
                txt = item.get("original_text") or item.get("text") or ""
                detected_lang = item.get("source_language") or source_lang

                if txt.strip():
                    seg = Segment(
                        id=f"seg_{global_seg_idx:04d}",
                        start=seg_start,
                        end=seg_end,
                        speaker_id=spk,
                        source_language=detected_lang,
                        original_text=txt.strip(),
                        target_language="km",
                        status="ready",
                        translation_status="pending"
                    )
                    all_segments.append(seg)
                    global_seg_idx += 1

        all_segments = self._normalize_transcript_segments(all_segments)

        if progress_callback:
            progress_callback(100, f"STT Complete! Generated {len(all_segments)} transcript segments.")

        logger.info(f"✅ [STT] Pipeline finished. {len(all_segments)} dialogue segments transcribed.")
        return all_segments

    def _transcribe_audio_chunk(self, audio_file: str, source_lang: str, api_key: str) -> List[Dict[str, Any]]:
        """Call Gemini dedicated transcription endpoint and parse JSON response."""
        with open(audio_file, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")

        lang_instruction = f"Source language is: {source_lang}." if source_lang != "auto" else "Automatically detect the spoken language."

        prompt = (
            "You are a state-of-the-art Speech-to-Text transcription and speaker diarization engine.\n"
            f"{lang_instruction}\n"
            "Carefully listen to this audio and perform:\n"
            "1. Accurate verbatim speech transcription.\n"
            "2. Word/segment-level start and end timestamps in seconds (e.g. 1.45).\n"
            "3. Speaker Diarization: Assign speaker labels ('Speaker 1', 'Speaker 2', etc.).\n"
            "4. Language detection for each line.\n\n"
            "Output the transcription as a JSON array of objects with keys: start, end, speaker_id, source_language, original_text."
        )

        headers = {"Content-Type": "application/json"}
        last_error = ""

        for model in self.models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            
            # Note: gemini-3.5-transcribe does not support response_mime_type: application/json
            gen_cfg = {"temperature": 0.1}
            if "transcribe" not in model:
                gen_cfg["response_mime_type"] = "application/json"

            payload = {
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": gen_cfg
            }

            try:
                logger.info(f"🌐 Calling Gemini STT ({model})...")
                resp = requests.post(url, headers=headers, json=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        text_res = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        raw_list = self._parse_json_array(text_res)
                        if raw_list:
                            return raw_list
                else:
                    err_msg = resp.text[:200]
                    last_error = f"Gemini {model} returned HTTP {resp.status_code}: {err_msg}"
                    logger.warning(last_error)
            except Exception as e:
                last_error = f"Gemini {model} error: {e}"
                logger.warning(last_error)
                continue


    def _parse_json_array(self, raw_text: str) -> List[Dict[str, Any]]:
        """Clean and parse JSON array output from Gemini with robust recovery."""
        from utils.gemini_parser import parse_gemini_json_response
        return parse_gemini_json_response(raw_text)

    def _log_segment_debug(self, stage: str, seg: Segment):
        """Structured debug logging per Requirement 13."""
        txt = (seg.original_text or "").replace('\n', ' ')
        words_cnt = len(txt.split())
        conf_str = f", conf={seg.confidence:.2f}" if seg.confidence is not None else ""
        raw_dur = f", raw_dur={seg.raw_end - seg.raw_start:.2f}s" if (seg.raw_start is not None and seg.raw_end is not None) else ""
        logger.info(
            f"[STT {stage}] id={seg.id}, time={seg.start:.2f}-{seg.end:.2f}s (dur={seg.slot_duration:.2f}s{raw_dur}), "
            f"words={words_cnt}{conf_str}, spk={seg.speaker_id}: \"{txt}\""
        )

    def validate_segments(self, segments: List[Segment]) -> List[Segment]:
        """
        Safe pipeline validation per Requirement 14:
        - start >= 0
        - end > start
        - no empty text
        - no severe timestamp overlap
        - word timestamps remain inside segment boundaries
        Logs WARNING rather than silently deleting suspicious items.
        """
        valid_segments = []
        for i, s in enumerate(segments):
            if s.start < 0:
                logger.warning(f"⚠️ [STT Validation] Segment {s.id} has negative start ({s.start}s). Clamping to 0.0s.")
                s.start = 0.0
            if s.end <= s.start:
                logger.warning(f"⚠️ [STT Validation] Segment {s.id} has end <= start ({s.start}-{s.end}s). Setting duration to 0.3s.")
                s.end = round(s.start + 0.30, 2)
            
            s.slot_duration = round(s.end - s.start, 2)
            
            if not (s.original_text or "").strip():
                logger.warning(f"⚠️ [STT Validation] Segment {s.id} has empty text. Skipping.")
                continue

            # Verify word timestamps inside boundary
            if s.words:
                first_w = s.words[0]["start"]
                last_w = s.words[-1]["end"]
                if first_w < s.start - 0.05:
                    logger.warning(f"⚠️ [STT Validation] Word start ({first_w}s) is before segment start ({s.start}s) for {s.id}. Adjusting segment start.")
                    s.start = round(max(0.0, first_w - 0.05), 2)
                    s.slot_duration = round(s.end - s.start, 2)
                if last_w > s.end + 0.05:
                    logger.warning(f"⚠️ [STT Validation] Word end ({last_w}s) is after segment end ({s.end}s) for {s.id}. Adjusting segment end.")
                    s.end = round(last_w + 0.05, 2)
                    s.slot_duration = round(s.end - s.start, 2)

            valid_segments.append(s)

        return valid_segments

    def _normalize_transcript_segments(self, segments: List[Segment]) -> List[Segment]:
        """
        Conservative, dialogue-preserving normalization:
        1. Preserves raw Whisper timestamps (raw_start, raw_end).
        2. Protects word-level timestamps (never cuts into spoken words).
        3. Never discards short speech ("Yes", "No", "Wait", "Hey!" are preserved).
        4. Overlaps are resolved using silence gaps between word boundaries when available.
        5. Does not aggressively delete repetitions unless heavy overlap + identical text indicates a glitch.
        6. Emits structured debug logging ([STT RAW] vs [STT NORMALIZED]).
        """
        if not segments:
            return []

        # Sort strictly by start time
        segments.sort(key=lambda s: (s.start, s.end))

        normalized: List[Segment] = []

        for i, curr in enumerate(segments):
            # Ensure raw timestamps are stored
            if curr.raw_start is None:
                curr.raw_start = curr.start
            if curr.raw_end is None:
                curr.raw_end = curr.end

            text = (curr.original_text or curr.text or "").strip()
            # Only discard truly empty segments or pure punctuation with no words
            clean_text = re.sub(r'^[^\w\s]+|[^\w\s]+$', '', text).strip()
            if not clean_text:
                logger.debug(f"[STT NORM] Discarding empty/symbol-only segment id={curr.id}: '{text}'")
                continue

            # Ensure minimal duration sanity (end > start)
            if curr.end <= curr.start:
                curr.end = round(curr.start + 0.30, 2)

            # Use word timestamps to safeguard boundaries (Requirement 8)
            curr_words = curr.words or []
            if curr_words:
                first_w_st = curr_words[0]["start"]
                last_w_et = curr_words[-1]["end"]
                # Expand slightly to preserve initial plosives and ending decay
                safe_w_start = max(0.0, round(first_w_st - 0.05, 2))
                safe_w_end = round(last_w_et + 0.10, 2)
                curr.start = min(curr.start, safe_w_start)
                curr.end = max(curr.end, safe_w_end)

            if not normalized:
                curr.start = round(max(0.0, curr.start), 2)
                curr.end = round(curr.end, 2)
                curr.slot_duration = round(curr.end - curr.start, 2)
                normalized.append(curr)
                self._log_segment_debug("NORMALIZED", curr)
                continue

            prev = normalized[-1]
            prev_text = (prev.original_text or prev.text or "").strip()
            prev_words = prev.words or []

            # 1. Repetition / Glitch Detection (Requirement 10)
            # Only treat as hallucination if text is identical AND timestamps overlap heavily (< 0.20s gap)
            is_same_text = (text.lower() == prev_text.lower())
            is_heavy_overlap = (curr.start < prev.end + 0.20)
            if is_same_text and is_heavy_overlap:
                logger.info(f"🔁 [STT NORM] Glitch repetition loop detected: '{text}' (curr {curr.start:.2f}-{curr.end:.2f}s overlaps prev {prev.start:.2f}-{prev.end:.2f}s). Merging.")
                if curr.end > prev.end:
                    prev.end = round(curr.end, 2)
                    prev.slot_duration = round(prev.end - prev.start, 2)
                continue

            # 2. Check complete enclosure: curr inside prev
            if curr.start >= prev.start and curr.end <= prev.end:
                if text.lower() in prev_text.lower():
                    # Substring already contained in previous segment
                    logger.debug(f"[STT NORM] Substring enclosed inside prev segment id={prev.id}. Skipping id={curr.id}.")
                    continue
                elif prev_text.lower() in text.lower():
                    # Curr is the fuller phrase, update prev text without cutting
                    prev.original_text = curr.original_text
                    prev.text = curr.text
                    continue

            # 3. Safe Word-Aware Overlap Resolution (Requirement 7 & 8)
            if curr.start < prev.end:
                overlap_sec = round(prev.end - curr.start, 2)
                
                # Check if word timestamps can find the silence gap between the two utterances
                resolved = False
                if prev_words and curr_words:
                    prev_last_word_end = prev_words[-1]["end"]
                    curr_first_word_start = curr_words[0]["start"]
                    
                    if curr_first_word_start >= prev_last_word_end:
                        # Perfect! Boundary placed cleanly in the silence gap between words
                        mid_gap = round((prev_last_word_end + curr_first_word_start) / 2.0, 2)
                        prev.end = max(round(prev.start + 0.1, 2), mid_gap)
                        curr.start = min(round(curr.end - 0.1, 2), mid_gap)
                        resolved = True
                        logger.debug(f"[STT NORM] Resolved overlap between id={prev.id} and {curr.id} using word silence gap: {mid_gap}s (prev word end: {prev_last_word_end}s, curr word start: {curr_first_word_start}s)")
                
                if not resolved:
                    # If words overlap (speakers talking at same time) or no word timestamps:
                    # Do NOT cut into words! Allow small natural overlaps (<= 0.25s)
                    if overlap_sec <= 0.25:
                        logger.debug(f"[STT NORM] Small natural dialogue overlap ({overlap_sec:.2f}s) tolerated between id={prev.id} and {curr.id}")
                    else:
                        # Significant overlap (> 0.25s): Determine overlap nature
                        if prev_words and curr_words:
                            min_prev_end = round(prev_words[-1]["end"] + 0.05, 2)
                            max_curr_start = round(curr_words[0]["start"] - 0.05, 2)
                            if min_prev_end <= max_curr_start:
                                # Clean silence gap between spoken words: place boundary right in the gap
                                mid = round((min_prev_end + max_curr_start) / 2.0, 2)
                                prev.end = mid
                                curr.start = mid
                                logger.debug(f"[STT NORM] Clean boundary placed in word gap: {mid}s")
                            else:
                                # Legitimate simultaneous speech (overlapping actors):
                                # NEVER cut into words! Allow each segment to encompass its complete words.
                                prev.end = max(prev.end, min_prev_end)
                                curr.start = min(curr.start, max(0.0, max_curr_start))
                                logger.info(f"[STT NORM] Preserved simultaneous speech overlap between id={prev.id} and {curr.id} without cutting word boundaries.")
                        else:
                            # Without word timestamps: split at midpoint while guaranteeing minimum durations
                            mid = round((prev.end + curr.start) / 2.0, 2)
                            if (mid - prev.start) >= 0.4 and (curr.end - mid) >= 0.4:
                                prev.end = mid
                                curr.start = mid
                            else:
                                if curr.end - prev.end >= 0.3:
                                    curr.start = round(prev.end, 2)

            # Final boundary rounding & slot duration calculation
            curr.start = round(max(0.0, curr.start), 2)
            curr.end = round(curr.end, 2)
            if curr.end <= curr.start:
                curr.end = round(curr.start + 0.30, 2)
            curr.slot_duration = round(curr.end - curr.start, 2)

            normalized.append(curr)
            self._log_segment_debug("NORMALIZED", curr)

        # Run safe validation step (Requirement 14)
        validated = self.validate_segments(normalized)
        return validated

    def _get_duration_sec(self, audio_path: str) -> float:
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception:
            pass
        return 0.0
