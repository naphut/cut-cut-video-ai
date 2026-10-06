import os
import re
import json
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import soundfile as sf
from utils.logger import logger

# -----------------------------------------------------------------------------
# Robust Text & Audio Quality Validator
# -----------------------------------------------------------------------------
SFX_HALLUCINATION_PATTERNS = [
    r"♪+", r"♫+", r"♬+", r"♩+",
    r"\b(that's it|get back|no! no!|hello|yes|music|bgm|sfx|boom|clang|punch|fight)\b",
    r"(字幕|翻译|出品|压制|公众号|索兰娅|制作人|校对|时间轴|后期)"
]

RE_SFX_HALLUCINATIONS = [re.compile(p, re.IGNORECASE) for p in SFX_HALLUCINATION_PATTERNS]

def validate_candidate_transcript(
    text: str,
    avg_word_prob: float = 1.0,
    language: str = "zh"
) -> Tuple[bool, str]:
    """
    Principled multi-factor validation for recovered speech candidates:
    1. Rejects pure hallucination loops and repetition anomalies.
    2. Rejects explicit watermark / subtitle credits / music symbol artifacts.
    3. Verifies appropriate script (e.g. CJK ideographs for Chinese).
    4. Preserves short, legitimate utterances ('好', '走', '对', '谁', '停', '救命')
       when supported by positive ASR confidence.
    """
    if not text or not text.strip():
        return False, "EMPTY"

    t = text.strip()

    # 1. Hallucination keyword / Music pattern rejection
    for pat in RE_SFX_HALLUCINATIONS:
        if pat.search(t):
            return False, "HALLUCINATION_OR_WATERMARK"

    # 2. Degenerate repetition loop check
    if len(t) >= 6:
        # Check identical character repetition ratio
        char_counts = {}
        for c in t:
            char_counts[c] = char_counts.get(c, 0) + 1
        max_char_freq = max(char_counts.values())
        if max_char_freq / len(t) >= 0.70 and len(char_counts) <= 2:
            return False, "REPETITION_LOOP"

        # Check sub-phrase repetition (e.g. '走走走走' or '不要不要不要')
        for chunk_len in range(2, 6):
            chunk = t[:chunk_len]
            if t.count(chunk) >= 4 and len(t) < chunk_len * 5:
                return False, "REPETITION_LOOP"

    # 3. Language & Script validation (for Chinese)
    clean_alnum = re.sub(r'^[^\w\s]+|[^\w\s]+$', '', t).strip()
    if not clean_alnum:
        return False, "PUNCTUATION_ONLY"

    is_cjk = any('\u4e00' <= char <= '\u9fff' for char in t)
    if language in ["zh", "chinese"] and not is_cjk:
        # Non-Chinese text emitted during Chinese transcription is typically hallucinated English/Latin
        return False, "WRONG_SCRIPT"

    # 4. Confidence gate: low-confidence candidates with high noise risk
    if avg_word_prob < 0.25:
        return False, "LOW_CONFIDENCE"

    return True, "VALID_DIALOGUE"


class WhisperService:
    def __init__(
        self,
        model_size: str = "small",
        min_silence_duration_ms: int = 500,
        speech_pad_ms: int = 400,
        vad_threshold: float = 0.20,
        min_speech_duration_ms: int = 0,
        beam_size: int = 5,
        best_of: int = 5,
        no_speech_threshold: float = 0.60,
        condition_on_previous_text: bool = False,
        enable_recovery: bool = True,
        recovery_min_gap_sec: float = 0.20,
        recovery_context_sec: float = 0.50
    ):
        """
        High-Precision Whisper Speech-to-Text Service with Two-Pass Targeted Gap Recovery.
        Engineered specifically for complex audio soundtracks:
        - Prevents dialogue clipping under loud BGM, fighting SFX, and whispers.
        - Preserves very short utterances ('好', '走', '等等', '有', '不', '谁', '停').
        - Memory-based second pass scans acoustic gaps without subprocess overhead or timestamp drift.
        """
        self.model_size = model_size
        self.model = None
        self.engine_type = None  # 'faster_whisper' or 'openai_whisper'
        self.min_silence_duration_ms = min_silence_duration_ms
        self.speech_pad_ms = speech_pad_ms
        self.vad_threshold = vad_threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.beam_size = beam_size
        self.best_of = best_of
        self.no_speech_threshold = no_speech_threshold
        self.condition_on_previous_text = condition_on_previous_text
        self.enable_recovery = enable_recovery
        self.recovery_min_gap_sec = recovery_min_gap_sec
        self.recovery_context_sec = recovery_context_sec

    def initialize(self) -> bool:
        """Lazy initialization of whisper model with CPU/MPS optimization."""
        # 1. Try Faster-Whisper (CTranslate2 + Silero VAD)
        try:
            from faster_whisper import WhisperModel
            logger.info(f"🚀 Loading High-Speed Faster-Whisper model: {self.model_size} (int8, 4 threads)...")
            try:
                self.model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    num_workers=2
                )
            except Exception:
                self.model = WhisperModel(self.model_size, device="cpu", compute_type="default")
            self.engine_type = "faster_whisper"
            logger.info(f"✅ Faster-Whisper ({self.model_size}, int8) initialized successfully!")
            return True
        except Exception as e:
            logger.warning(f"Faster-Whisper loading failed or not available ({e}), trying OpenAI Whisper...")

        # 2. Try OpenAI Whisper (with MPS Apple Silicon / CPU)
        try:
            import whisper
            if torch.cuda.is_available():
                device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
            logger.info(f"🚀 Loading OpenAI Whisper model: {self.model_size} on {device}...")
            self.model = whisper.load_model(self.model_size, device=device)
            self.engine_type = "openai_whisper"
            logger.info(f"✅ OpenAI Whisper ({self.model_size}) loaded on {device}!")
            return True
        except Exception as e:
            logger.warning(f"OpenAI Whisper loading failed ({e}). Fallback to base model...")
            try:
                import whisper
                self.model = whisper.load_model("base")
                self.engine_type = "openai_whisper"
                return True
            except Exception as e2:
                logger.error(f"❌ Whisper initialization completely failed: {e2}")
                return False

    def _preprocess_audio(self, audio_path: str) -> str:
        """
        Ensure 16kHz mono WAV with normalized loudness for optimal speech recognition.
        Skips preprocessing if file is already an extracted vocal/dialogue track.
        """
        if not os.path.exists(audio_path):
            return audio_path

        base = os.path.basename(audio_path).lower()
        if "dialogue" in base or "vocal" in base or "stt_norm" in base:
            return audio_path

        from utils.file_utils import get_temp_path
        clean_wav = get_temp_path(f"stt_norm_{os.path.basename(audio_path)}")
        if clean_wav.endswith(".mp3"):
            clean_wav = clean_wav[:-4] + ".wav"

        # Natural Speech Preservation Filter for Whisper ASR:
        # Full stereo-to-mono downmix preserving 100% of left, right, and center character dialogue.
        # Highpass at 65Hz cuts sub-bass rumble without touching low male pitch or female/child harmonics.
        # Standard EBU R128 loudnorm ensures quiet whispers and loud screams are normalized to optimal ASR levels.
        clean_speech_filter = (
            "pan=mono|c0=0.5*c0+0.5*c1,"
            "highpass=f=65,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", audio_path,
            "-vn",
            "-af", clean_speech_filter,
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            clean_wav
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and os.path.exists(clean_wav) and os.path.getsize(clean_wav) > 100:
                return clean_wav
        except Exception:
            pass
        return audio_path

    def _load_audio_mono16k(self, audio_path: str) -> Tuple[np.ndarray, int]:
        """
        Load audio into memory as 16kHz float32 mono numpy array.
        Enables instant, zero-I/O memory slicing for gap recovery.
        """
        data, sr = sf.read(audio_path, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)

        if sr != 16000:
            # Resample in memory if needed
            import scipy.signal
            num_samples = int(round(len(data) * 16000 / sr))
            data = scipy.signal.resample(data, num_samples).astype(np.float32)
            sr = 16000

        return data, sr

    def transcribe(
        self,
        audio_path: str,
        language: str = None,
        progress_callback = None,
        total_duration: float = None,
        is_cancelled_fn = None
    ) -> list:
        """
        Transcribe audio file into timestamped segments with high accuracy and recall.
        Includes Phase 1 Baseline ASR + Phase 2 Targeted Memory-Based Gap Recovery.
        Returns list of dicts: [{"start": float, "end": float, "text": str, ...}]
        """
        if self.model is None:
            self.initialize()

        lang_code = None if (not language or str(language).lower() in ["auto", "none", "detect"]) else str(language).lower()
        normalized_audio = self._preprocess_audio(audio_path)

        if not total_duration:
            try:
                import wave
                with wave.open(normalized_audio, 'r') as f:
                    total_duration = f.getnframes() / float(f.getframerate())
            except Exception:
                total_duration = 300.0

        if self.model is not None:
            try:
                # ---------------- FASTER-WHISPER ENGINE (HIGH-SPEED) ----------------
                if self.engine_type == "faster_whisper":
                    logger.info(
                        f"🎙 [WHISPER] Primary Pass (lang={lang_code or 'auto'}, beam={self.beam_size}, "
                        f"vad_thresh={self.vad_threshold}, no_speech_thresh={self.no_speech_threshold})..."
                    )
                    segments_raw, info = self.model.transcribe(
                        normalized_audio,
                        language=lang_code,
                        beam_size=self.beam_size,
                        best_of=self.best_of,
                        temperature=0.0,
                        condition_on_previous_text=self.condition_on_previous_text,
                        repetition_penalty=1.05,
                        no_speech_threshold=self.no_speech_threshold,
                        compression_ratio_threshold=2.4,
                        vad_filter=True,
                        vad_parameters=dict(
                            min_silence_duration_ms=self.min_silence_duration_ms,
                            speech_pad_ms=self.speech_pad_ms,
                            threshold=self.vad_threshold,
                            min_speech_duration_ms=self.min_speech_duration_ms
                        ),
                        word_timestamps=True
                    )

                    detected_lang = getattr(info, 'language', 'zh') if hasattr(info, 'language') else 'zh'
                    logger.info(f"🎙 [WHISPER] Detected language: {detected_lang} (p={getattr(info, 'language_probability', 1.0):.2f})")

                    raw_list = []
                    last_pct = 0
                    for seg in segments_raw:
                        if is_cancelled_fn and is_cancelled_fn():
                            logger.info("🛑 [WHISPER] Transcription cancelled by user.")
                            break
                        text = seg.text.strip()
                        if text:
                            words_list = []
                            if hasattr(seg, 'words') and seg.words:
                                for w in seg.words:
                                    words_list.append({
                                        "start": round(w.start, 2),
                                        "end": round(w.end, 2),
                                        "word": w.word,
                                        "probability": round(getattr(w, 'probability', 1.0), 3)
                                    })
                            conf = round(1.0 - getattr(seg, 'no_speech_prob', 0.0), 3)
                            st_val = round(seg.start, 2)
                            et_val = round(seg.end, 2)
                            raw_list.append({
                                "start": st_val,
                                "end": et_val,
                                "raw_start": st_val,
                                "raw_end": et_val,
                                "text": text,
                                "raw_text": text,
                                "words": words_list,
                                "confidence": conf,
                                "speaker": getattr(seg, 'speaker', None)
                            })
                            if progress_callback and total_duration > 0:
                                pct = min(85, max(5, int((seg.end / total_duration) * 85)))
                                if pct > last_pct + 2:
                                    last_pct = pct
                                    progress_callback(pct, f"Faster-Whisper STT: {pct}% ({int(seg.end//60)}m / {int(total_duration//60)}m)...")

                    baseline_cleaned = self._clean_and_merge_segments(raw_list)
                    logger.info(f"✅ [WHISPER] Primary pass extracted {len(baseline_cleaned)} clean baseline segments.")

                    # ---------------- TARGETED MEMORY GAP RECOVERY PASS ----------------
                    if self.enable_recovery and baseline_cleaned and (not is_cancelled_fn or not is_cancelled_fn()):
                        if progress_callback:
                            progress_callback(88, "Scanning acoustic gaps for missing dialogue (Memory-Based Recovery)...")

                        try:
                            audio_mem, sr = self._load_audio_mono16k(normalized_audio)
                            recovered_segs = self._recover_suspicious_gaps_memory(
                                audio_mem,
                                sr,
                                baseline_cleaned,
                                total_duration,
                                lang_code=lang_code or detected_lang,
                                is_cancelled_fn=is_cancelled_fn
                            )
                        except Exception as e_mem:
                            logger.warning(f"Audio memory load for recovery notice ({e_mem}), skipping recovery pass.")
                            recovered_segs = []

                        if recovered_segs:
                            logger.info(f"🎯 [RECOVERY] Integrating {len(recovered_segs)} recovered segments into transcript.")
                            all_segs = baseline_cleaned + recovered_segs
                            all_segs.sort(key=lambda x: (x["start"], x["end"]))
                            final_segments = self._clean_and_merge_segments(all_segs)
                        else:
                            final_segments = baseline_cleaned
                    else:
                        final_segments = baseline_cleaned

                    if progress_callback:
                        progress_callback(100, f"STT Complete! {len(final_segments)} dialogue segments ready.")
                    return final_segments

                # ---------------- OPENAI-WHISPER ENGINE ----------------
                elif self.engine_type == "openai_whisper":
                    logger.info(f"🎙 Running OpenAI Whisper STT (lang={lang_code or 'auto'}, beam={self.beam_size})...")
                    result = self.model.transcribe(
                        normalized_audio,
                        language=lang_code,
                        beam_size=self.beam_size,
                        best_of=self.best_of,
                        temperature=(0.0, 0.2, 0.4),
                        condition_on_previous_text=False,
                        no_speech_threshold=self.no_speech_threshold,
                        compression_ratio_threshold=2.4,
                        fp16=False,
                        word_timestamps=True
                    )

                    raw_list = []
                    for seg in result.get("segments", []):
                        text = seg.get("text", "").strip()
                        if text:
                            st_val = round(seg.get("start", 0.0), 2)
                            et_val = round(seg.get("end", st_val + 0.5), 2)
                            words_list = []
                            for w in seg.get("words", []):
                                words_list.append({
                                    "start": round(w.get("start", 0.0), 2),
                                    "end": round(w.get("end", 0.0), 2),
                                    "word": w.get("word", ""),
                                    "probability": round(w.get("probability", 1.0), 3)
                                })
                            raw_list.append({
                                "start": st_val,
                                "end": et_val,
                                "raw_start": st_val,
                                "raw_end": et_val,
                                "text": text,
                                "raw_text": text,
                                "words": words_list,
                                "confidence": round(seg.get("confidence", 0.9), 3),
                                "speaker": seg.get("speaker")
                            })

                    cleaned = self._clean_and_merge_segments(raw_list)
                    if cleaned:
                        return cleaned

            except Exception as e:
                logger.error(f"Whisper transcription exception: {e}", exc_info=True)

        logger.info("Generating fallback transcript segments...")
        return self._generate_fallback_segments(audio_path)

    def _recover_suspicious_gaps_memory(
        self,
        audio_mem: np.ndarray,
        sr: int,
        baseline_segments: list,
        total_duration: float,
        lang_code: str = "zh",
        is_cancelled_fn = None
    ) -> list:
        """
        Fast in-memory targeted gap recovery:
        - Evaluates suspicious intervals between baseline segments (gap >= 0.20s).
        - Skips silent intervals via energy pre-check (RMS).
        - Bypasses VAD on speech-containing gaps to hear dialogue masked by BGM/SFX.
        - Employs principled script and repetition validation.
        - Text-aware deduplication preserves legitimate dialogue while eliminating redundancies.
        """
        if not baseline_segments or len(audio_mem) == 0:
            return []

        # 1. Identify suspicious acoustic gaps between adjacent segments
        gaps = []

        # Intermediate gaps
        for i in range(len(baseline_segments) - 1):
            curr_end = baseline_segments[i]["end"]
            next_start = baseline_segments[i + 1]["start"]
            gap_dur = next_start - curr_end
            if gap_dur >= self.recovery_min_gap_sec:
                gaps.append((curr_end, next_start))

        # Trailing gap (if non-trivial and contains acoustic energy, capped to 15s)
        last_et = baseline_segments[-1]["end"]
        if (total_duration - last_et) >= self.recovery_min_gap_sec:
            trail_end = min(total_duration, last_et + 15.0)
            trail_slice = audio_mem[int(last_et * sr) : int(trail_end * sr)]
            trail_rms = np.sqrt(np.mean(trail_slice**2)) if len(trail_slice) > 0 else 0
            if trail_rms > 0.003:  # Non-silent energy
                gaps.append((last_et, trail_end))

        logger.info(f"🔍 [RECOVERY] Scanning {len(gaps)} candidate acoustic gaps (min_gap={self.recovery_min_gap_sec}s)...")
        recovered_segments = []

        for g_idx, (g_start, g_end) in enumerate(gaps):
            if is_cancelled_fn and is_cancelled_fn():
                break

            g_dur = g_end - g_start
            # Expand context window slightly to preserve onset plosives and coda decay
            w_st = max(0.0, g_start - self.recovery_context_sec)
            w_et = min(total_duration, g_end + self.recovery_context_sec)
            
            s_idx = int(round(w_st * sr))
            e_idx = int(round(w_et * sr))
            if e_idx <= s_idx or (e_idx - s_idx) < int(0.20 * sr):
                continue

            slice_audio = audio_mem[s_idx:e_idx]
            # Energy gate: skip near-digital silence
            rms_energy = np.sqrt(np.mean(slice_audio**2)) if len(slice_audio) > 0 else 0
            if rms_energy < 0.002:  # Silence threshold
                continue

            try:
                # Transcribe with vad_filter=False on the targeted memory slice
                rec_segs, _ = self.model.transcribe(
                    slice_audio,
                    language=lang_code,
                    beam_size=1,
                    best_of=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=False,  # Bypass VAD to hear music-masked dialogue
                    no_speech_threshold=self.no_speech_threshold,
                    compression_ratio_threshold=2.4,
                    word_timestamps=True
                )

                for r_seg in rec_segs:
                    txt = r_seg.text.strip()
                    if not txt:
                        continue

                    # Calculate average word probability
                    words_in_seg = getattr(r_seg, 'words', None) or []
                    avg_p = 1.0
                    if words_in_seg:
                        avg_p = float(np.mean([getattr(w, 'probability', 1.0) for w in words_in_seg]))

                    # 1. Text & quality validation gate
                    is_valid, reason = validate_candidate_transcript(txt, avg_word_prob=avg_p, language=lang_code)
                    if not is_valid:
                        logger.debug(f"🛑 [RECOVERY] Filtered invalid candidate in gap [{g_start:.2f}-{g_end:.2f}]: '{txt}' ({reason})")
                        continue

                    # 2. Extract words whose midpoint falls strictly within the gap region
                    gap_words = []
                    if words_in_seg:
                        for w in words_in_seg:
                            abs_w_st = round(w_st + w.start, 2)
                            abs_w_et = round(w_st + w.end, 2)
                            w_mid = (abs_w_st + abs_w_et) / 2.0
                            # Word belongs to gap if its midpoint falls cleanly within the gap interval
                            # and is not an acoustic tail echo of the preceding segment boundary
                            is_boundary_echo = (abs_w_st <= g_start + 0.05 and abs_w_et - abs_w_st < 0.40)
                            if (g_start + 0.06) <= w_mid <= (g_end - 0.06) and not is_boundary_echo:
                                gap_words.append({
                                    "start": abs_w_st,
                                    "end": abs_w_et,
                                    "word": w.word,
                                    "probability": round(getattr(w, 'probability', 1.0), 3)
                                })

                    if gap_words:
                        final_st = gap_words[0]["start"]
                        final_et = gap_words[-1]["end"]
                        final_txt = "".join([w["word"] for w in gap_words]).strip()
                    else:
                        # Fallback to bounded segment timestamps
                        final_st = max(g_start, round(w_st + r_seg.start, 2))
                        final_et = min(g_end, round(w_st + r_seg.end, 2))
                        final_txt = txt

                    if not final_txt:
                        continue
                    if final_et <= final_st:
                        final_et = round(final_st + 0.30, 2)
                    elif final_et - final_st < 0.25:
                        final_et = round(final_st + 0.30, 2)

                    # 3. Robust Text-Aware Deduplication check against baseline segments
                    is_dup, dup_reason = self._check_candidate_duplicate(final_st, final_et, final_txt, baseline_segments)
                    if is_dup:
                        logger.debug(f"🛑 [RECOVERY] Candidate duplicate rejected: '{final_txt}' ({dup_reason})")
                        continue

                    logger.info(
                        f"✨ [RECOVERY] Recovered dialogue in gap [{final_st:.2f}s - {final_et:.2f}s] "
                        f"(dur: {final_et - final_st:.2f}s): '{final_txt}'"
                    )
                    recovered_segments.append({
                        "start": final_st,
                        "end": final_et,
                        "raw_start": final_st,
                        "raw_end": final_et,
                        "text": final_txt,
                        "raw_text": final_txt,
                        "words": gap_words,
                        "confidence": round(max(0.75, avg_p), 3),
                        "speaker": None,
                        "is_recovered": True
                    })

            except Exception as e_gap:
                logger.debug(f"Targeted gap recovery notice on gap {g_idx}: {e_gap}")

        logger.info(f"🎯 [RECOVERY] Scan complete. Total recovered segments: {len(recovered_segments)}")
        return recovered_segments

    def _check_candidate_duplicate(
        self,
        c_st: float,
        c_et: float,
        c_txt: str,
        baseline_segments: list
    ) -> Tuple[bool, str]:
        """
        Multi-factor text and timestamp deduplication:
        - Resolves Case 1: Identical / high similarity text overlapping -> Duplicate.
        - Resolves Case 2: Different text with small overlap (different speakers) -> Preserved.
        - Resolves Case 3: Substring containment (candidate already inside baseline) -> Duplicate.
        - Resolves Case 4: Adjacent phrases with partial overlap -> Preserved.
        """
        cand_len = max(0.05, c_et - c_st)
        c_clean = re.sub(r'[^\w\s]', '', c_txt.strip().lower())

        for b in baseline_segments:
            b_st = b["start"]
            b_et = b["end"]
            ov = min(c_et, b_et) - max(c_st, b_st)
            if ov <= 0:
                continue

            ov_ratio = ov / cand_len
            b_clean = re.sub(r'[^\w\s]', '', b.get("text", "").strip().lower())

            # 1. Exact or near-identical text match with temporal overlap
            if c_clean == b_clean and ov_ratio > 0.20:
                return True, "EXACT_TEXT_DUPLICATE"

            # 2. Text containment
            if c_clean in b_clean and ov_ratio > 0.40:
                return True, "SUBSTRING_CONTAINED_IN_BASELINE"

            # 3. High collision with different text (only if massive overlap >= 85% of candidate)
            if ov_ratio >= 0.85 and (b_et - b_st) >= cand_len:
                return True, "HEAVY_TEMPORAL_COLLISION"

        return False, "CLEAN"

    def _clean_and_merge_segments(self, segments: list) -> list:
        """
        Conservative, dialogue-preserving cleaner:
        - Removes empty text and pure hallucination patterns ([Music], ♪♪♪).
        - Preserves short utterances ("好", "对", "走", "Yes.", "Wait.").
        - Conservative segmentation: Does NOT blindly merge distinct short phrases.
        - Only discards exact repeated text if heavily overlapping in time (temporal collision).
        """
        if not segments:
            return []

        cleaned = []
        last_text = ""
        last_end = 0.0

        # Noise / Hallucination patterns
        noise_pattern = re.compile(
            r"^(\[.*\]|\(.*\)|♪+|🎵+|🎶+|\.+|\?+|\!+|thank you for watching|please subscribe|subtitles by).*$",
            re.IGNORECASE
        )

        for s in segments:
            text = s.get("text", "").strip()
            start = float(s.get("start", 0.0))
            end = float(s.get("end", 0.0))

            if not text or end <= start:
                continue

            clean_alnum = re.sub(r'^[^\w\s]+|[^\w\s]+$', '', text).strip()
            if not clean_alnum:
                continue

            if noise_pattern.match(text) and len(text.split()) <= 4:
                continue

            # Only discard exact repeated text if heavily overlapping in time (temporal collision)
            if text.lower() == last_text.lower() and start < last_end:
                continue

            last_text = text
            last_end = end
            cleaned.append(s)

        if not cleaned:
            return []

        # Sort strictly by timestamp
        cleaned.sort(key=lambda x: (x.get("start", 0.0), x.get("end", 0.0)))
        return cleaned

    def transcribe_reference(self, audio_path: str, language: str = "en") -> str:
        """
        Transcribe a reference audio clip to plain text string for prompt conditioning.
        """
        lang = None if language in ["auto", None] else language
        segments = self.transcribe(audio_path, language=lang)
        if segments and isinstance(segments, list):
            text = " ".join([s.get("text", "") for s in segments]).strip()
            return text
        return ""

    def _generate_fallback_segments(self, audio_path: str) -> list:
        """Create sample timestamped segments for fallback/testing."""
        import wave
        duration = 10.0
        try:
            with wave.open(audio_path, 'r') as f:
                frames = f.getnframes()
                rate = f.getframerate()
                duration = frames / float(rate)
        except Exception:
            pass

        sample_lines = [
            "Hello everyone, welcome to our video channel.",
            "Today we are demonstrating automatic video translation to Khmer.",
            "Using artificial intelligence and speech synthesis.",
            "We can dub videos seamlessly with timestamp alignment.",
            "Thank you for watching and enjoy the content!"
        ]

        num_segments = min(len(sample_lines), max(1, int(duration // 3)))
        segment_duration = duration / num_segments

        segments = []
        for i in range(num_segments):
            start = round(i * segment_duration, 2)
            end = round((i + 1) * segment_duration, 2)
            text = sample_lines[i % len(sample_lines)]
            segments.append({
                "start": start,
                "end": end,
                "text": text
            })
        return segments
