import os
import re
import wave
import subprocess
import tempfile
import numpy as np
from typing import List, Dict, Optional
from utils.logger import logger

class SpeakerDetector:
    """
    Advanced Acoustic-First Speaker Diarization & Profiling Engine for AI Video Dubbing.
    Categorizes speech accurately across 5 core voice categories:
    1. Child [ក្មេង] (Vannak / Sreyka) - Acoustic F0 >= 225 Hz
    2. Female [ស្រី] (Sreymom) - Acoustic F0 168 Hz - 225 Hz
    3. Male [ប្រុស] (Piseth) - Acoustic F0 70 Hz - 168 Hz
    4. Elderly Male [ចាស់ប្រុស] (Grandfather) - Deep Male F0 < 112 Hz + Elder context
    5. Elderly Female [ចាស់ស្រី] (Grandmother) - Female F0 + Elder context
    """
    def __init__(self, audio_wav_path: Optional[str] = None):
        self.audio_wav_path = audio_wav_path
        self._temp_extracted_wav = None
        self._prepare_audio_source()

    def _prepare_audio_source(self):
        """Ensure audio source is accessible as a 16kHz PCM WAV for reliable pitch estimation."""
        if not self.audio_wav_path or not os.path.exists(self.audio_wav_path):
            return

        is_valid_wav = False
        try:
            with wave.open(self.audio_wav_path, 'rb') as wf:
                if wf.getnframes() > 0:
                    is_valid_wav = True
        except Exception:
            is_valid_wav = False

        if not is_valid_wav:
            try:
                temp_file = tempfile.NamedTemporaryFile(suffix="_spk_16k.wav", delete=False)
                temp_path = temp_file.name
                temp_file.close()

                cmd = [
                    "ffmpeg", "-y", "-i", self.audio_wav_path,
                    "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                    "-threads", "2", temp_path
                ]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 100:
                    self._temp_extracted_wav = temp_path
                    self.audio_wav_path = temp_path
                    logger.info(f"🎤 [SpeakerDetector] Converted source to 16kHz PCM audio: {temp_path}")
            except Exception as e:
                logger.debug(f"[SpeakerDetector] Audio extraction failed: {e}")

    def __del__(self):
        if self._temp_extracted_wav and os.path.exists(self._temp_extracted_wav):
            try:
                os.remove(self._temp_extracted_wav)
            except Exception:
                pass

    def analyze_audio_segment_pitch(self, start_sec: float, end_sec: float) -> float:
        """Estimate fundamental vocal pitch frequency (F0) using autocorrelation pitch analysis."""
        if not self.audio_wav_path or not os.path.exists(self.audio_wav_path):
            return 0.0

        try:
            with wave.open(self.audio_wav_path, 'rb') as wf:
                framerate = wf.getframerate()
                nchannels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                nframes = wf.getnframes()

                start_frame = max(0, int(start_sec * framerate))
                end_frame = min(nframes, int(end_sec * framerate))
                if end_frame <= start_frame:
                    return 0.0
                num_frames = end_frame - start_frame

                wf.setpos(start_frame)
                raw_bytes = wf.readframes(num_frames)

                if not raw_bytes or len(raw_bytes) < 256:
                    return 0.0

                dtype = np.int16 if sampwidth == 2 else np.int8
                samples = np.frombuffer(raw_bytes, dtype=dtype)

                if nchannels > 1:
                    samples = samples[::nchannels]

                if len(samples) < 512:
                    return 0.0

                samples = samples.astype(np.float32)

                # Sliding window frame analysis (80ms windows, 40ms hop)
                win_len = int(framerate * 0.080)
                hop_len = int(framerate * 0.040)
                min_lag = max(1, int(framerate / 420.0)) # Max 420Hz
                max_lag = int(framerate / 65.0)          # Min 65Hz

                if len(samples) <= win_len:
                    win_len = len(samples)
                    hop_len = win_len

                pitches = []
                for i in range(0, len(samples) - win_len + 1, hop_len):
                    w = samples[i : i + win_len]
                    if np.std(w) < 100.0:  # Skip silent / noise floor frames
                        continue

                    w_centered = w - np.mean(w)
                    corr = np.correlate(w_centered, w_centered, mode='full')
                    corr = corr[len(corr)//2:]

                    m_lag = min(max_lag, len(corr) - 1)
                    if min_lag >= m_lag:
                        continue

                    peak_idx = np.argmax(corr[min_lag:m_lag]) + min_lag
                    # Require strong periodicity peak > 35% of zero-lag energy
                    if peak_idx > 0 and corr[0] > 0 and (corr[peak_idx] / corr[0]) > 0.35:
                        f0 = framerate / float(peak_idx)
                        if 65.0 <= f0 <= 420.0:
                            pitches.append(f0)

                if not pitches:
                    return 0.0

                median_f0 = float(np.median(pitches))
                return median_f0

        except Exception as e:
            logger.debug(f"Audio pitch extraction exception: {e}")
            return 0.0

    def classify_text_speaker(self, khmer_text: str, original_text: str) -> dict:
        """
        Analyze text semantics WITHOUT confusing addressees (e.g. child calling 'Mom' or 'Dad')
        with the speaking character.
        """
        combined = f"{original_text} {khmer_text}".lower()

        # Explicit tags
        has_child_tag = any(t in combined for t in ['[ក្មេង]', '[ក្មេងប្រុស]', '[ក្មេងស្រី]'])
        has_elder_f_tag = '[ចាស់ស្រី]' in combined
        has_elder_m_tag = any(t in combined for t in ['[ចាស់ប្រុស]', '[ចាស់]'])
        has_female_tag = '[ស្រី]' in combined
        has_male_tag = '[ប្រុស]' in combined

        # Self-referential cues (speaker talking about themselves)
        child_self = any(w in combined for t in ['កូនឈ្មោះ', 'នៀននៀន', 'nian nian', 'niannian', 'កូនឃ្លាន', 'កូនខ្លាច', 'កូនចង់'] for w in [t])
        elder_m_self = any(w in combined for t in ['លោកតាខ្ញុំ', 'តានិយាយ', 'តាប្រាប់', 'តាដឹង'] for w in [t])
        elder_f_self = any(w in combined for t in ['លោកយាយខ្ញុំ', 'យាយនិយាយ', 'យាយប្រាប់', 'យាយដឹង'] for w in [t])

        # Female indicators
        female_name_cues = any(w in combined for w in ['sreymom', 'sreyka', 'ស្រីកា', 'នារី', 'នាង', 'អ្នកនាង', 'លោកស្រី', 'អ្នកស្រី'])
        male_name_cues = any(w in combined for w in ['piseth', 'vannak', 'បុរស', 'លោកប្រធាន', 'លោកម្ចាស់', 'ពូ'])

        return {
            "has_child_tag": has_child_tag,
            "has_elder_f_tag": has_elder_f_tag,
            "has_elder_m_tag": has_elder_m_tag,
            "has_female_tag": has_female_tag,
            "has_male_tag": has_male_tag,
            "child_self": child_self,
            "elder_m_self": elder_m_self,
            "elder_f_self": elder_f_self,
            "female_name": female_name_cues,
            "male_name": male_name_cues
        }

    def diarize_and_profile_segments(self, segments: List[Dict]) -> List[Dict]:
        """
        Perform Acoustic-First Speaker Diarization with Conversational Continuity.
        Guarantees consecutive lines by the same character maintain voice stability.
        """
        processed_segments = []
        last_role = None
        last_voice = None
        last_tag = None
        last_persona = None
        last_end_sec = 0.0
        last_pitch = 0.0
        all_text = " ".join([str(s.get("khmer_text", "")) + " " + str(s.get("original_text", "")) for s in segments]).lower()
        has_girl_context = any(w in all_text for w in ['នៀននៀន', 'nian nian', 'niannian', 'ក្មេងស្រី', 'កូនស្រី', 'sreyka', 'girl'])
        has_boy_context = any(w in all_text for w in ['ក្មេងប្រុស', 'កូនប្រុស', 'vannak', 'boy'])
        default_child_voice = "Khmer Child - Girl (Sreyka)" if (has_girl_context and not has_boy_context) else "Khmer Child - Boy (Vannak)"
        default_child_persona = "Girl / Child" if default_child_voice == "Khmer Child - Girl (Sreyka)" else "Boy / Child"

        for i, seg in enumerate(segments):
            st = float(seg.get("start", i * 3.0))
            et = float(seg.get("end", (i + 1) * 3.0))
            orig_text = str(seg.get("original_text", seg.get("text", "")))
            khmer_text = str(seg.get("khmer_text", seg.get("translated_text", "")))

            # Strip existing bracket tags if present in text
            clean_khmer = khmer_text.strip()
            explicit_tag = None
            tag_m = re.match(r"^\[(ប្រុស|ស្រី|ក្មេង|ក្មេងប្រុស|ក្មេងស្រី|ចាស់|ចាស់ប្រុស|ចាស់ស្រី)\]\s*", clean_khmer)
            if tag_m:
                explicit_tag = f"[{tag_m.group(1)}]"
                clean_khmer = clean_khmer[len(tag_m.group(0)):].strip()

            # Measure acoustic pitch F0 directly from vocal audio
            pitch_f0 = self.analyze_audio_segment_pitch(st, et)
            cues = self.classify_text_speaker(clean_khmer, orig_text)

            time_gap = max(0.0, st - last_end_sec)
            is_immediate_continuation = (i > 0 and time_gap < 1.6)

            # --- DECISION LOGIC: ACOUSTIC PITCH IS PRIMARY GROUND TRUTH ---
            role = None
            voice = None
            tag = None
            persona = None

            # Case 1: High Pitch >= 225 Hz -> Physical Child voice
            if pitch_f0 >= 225.0:
                role = "child"
                tag = "[ក្មេង]"
                if is_immediate_continuation and last_role == "child" and last_voice:
                    voice = last_voice
                    persona = last_persona
                elif cues["female_name"] or "sreyka" in orig_text.lower() or "នៀននៀន" in clean_khmer or "girl" in orig_text.lower():
                    voice = "Khmer Child - Girl (Sreyka)"
                    persona = "Girl / Child"
                else:
                    voice = default_child_voice
                    persona = default_child_persona

            # Case 2: Adult Female Pitch (168 Hz - 225 Hz)
            elif 168.0 <= pitch_f0 < 225.0:
                if cues["has_elder_f_tag"] or cues["elder_f_self"]:
                    role = "elder_female"
                    tag = "[ចាស់ស្រី]"
                    voice = "Khmer Elder - Female (Grandmother)"
                    persona = "Elderly Female"
                else:
                    role = "female"
                    tag = "[ស្រី]"
                    voice = "Khmer Female - Sreymom"
                    persona = "Female Adult"

            # Case 3: Adult Male Pitch (65 Hz - 168 Hz)
            elif 65.0 <= pitch_f0 < 168.0:
                if (pitch_f0 < 112.0 and (cues["has_elder_m_tag"] or cues["elder_m_self"])) or explicit_tag == "[ចាស់ប្រុស]":
                    role = "elder_male"
                    tag = "[ចាស់ប្រុស]"
                    voice = "Khmer Elder - Male (Grandfather)"
                    persona = "Elderly Male"
                else:
                    role = "male"
                    tag = "[ប្រុស]"
                    voice = "Khmer Male - Piseth"
                    persona = "Male Adult"

            # Case 4: Unvoiced / Whisper / Missing Audio (pitch_f0 == 0.0)
            else:
                # If conversational continuation (< 1.6s gap) and previous speaker was established, inherit it
                if is_immediate_continuation and last_role:
                    role = last_role
                    voice = last_voice
                    tag = last_tag
                    persona = last_persona
                # Check explicit tags
                elif explicit_tag:
                    if explicit_tag in ("[ក្មេង]", "[ក្មេងប្រុស]", "[ក្មេងស្រី]"):
                        role = "child"
                        tag = "[ក្មេង]"
                        voice = "Khmer Child - Girl (Sreyka)" if cues["female_name"] else "Khmer Child - Boy (Vannak)"
                        persona = "Girl / Child" if cues["female_name"] else "Boy / Child"
                    elif explicit_tag == "[ចាស់ស្រី]":
                        role = "elder_female"
                        tag = "[ចាស់ស្រី]"
                        voice = "Khmer Elder - Female (Grandmother)"
                        persona = "Elderly Female"
                    elif explicit_tag in ("[ចាស់ប្រុស]", "[ចាស់]"):
                        role = "elder_male"
                        tag = "[ចាស់ប្រុស]"
                        voice = "Khmer Elder - Male (Grandfather)"
                        persona = "Elderly Male"
                    elif explicit_tag == "[ស្រី]":
                        role = "female"
                        tag = "[ស្រី]"
                        voice = "Khmer Female - Sreymom"
                        persona = "Female Adult"
                    else:
                        role = "male"
                        tag = "[ប្រុស]"
                        voice = "Khmer Male - Piseth"
                        persona = "Male Adult"
                elif cues["child_self"]:
                    role = "child"
                    tag = "[ក្មេង]"
                    voice = "Khmer Child - Girl (Sreyka)"
                    persona = "Girl / Child"
                elif cues["female_name"]:
                    role = "female"
                    tag = "[ស្រី]"
                    voice = "Khmer Female - Sreymom"
                    persona = "Female Adult"
                else:
                    role = "male"
                    tag = "[ប្រុស]"
                    voice = "Khmer Male - Piseth"
                    persona = "Male Adult"

            # Conversational Smoothing: If previous speaker was child and pitch is high or unvoiced, keep child
            if is_immediate_continuation and last_role == "child" and (pitch_f0 >= 210.0 or pitch_f0 == 0.0):
                role = "child"
                tag = "[ក្មេង]"
                voice = last_voice or "Khmer Child - Girl (Sreyka)"
                persona = last_persona or "Girl / Child"

            # Update speaker memory
            last_role = role
            last_voice = voice
            last_tag = tag
            last_persona = persona
            last_end_sec = et
            last_pitch = pitch_f0

            # Store updated segment fields
            updated = dict(seg)
            spk_id = seg.get("speaker") or seg.get("character") or f"Speaker 1"
            updated["speaker"] = spk_id
            updated["character"] = spk_id
            updated["khmer_text"] = clean_khmer
            updated["voice"] = voice
            updated["role"] = role
            updated["speaker_tag"] = tag
            updated["persona"] = persona
            updated["pitch_f0"] = pitch_f0

            processed_segments.append(updated)

        return processed_segments

    def detect_speaker_for_segment(self, khmer_text: str, original_text: str, start_sec: float, end_sec: float) -> dict:
        """Single segment speaker profile guidance across all 5 voice categories."""
        dummy_seg = {
            "start": start_sec,
            "end": end_sec,
            "khmer_text": khmer_text,
            "original_text": original_text
        }
        res = self.diarize_and_profile_segments([dummy_seg])
        if res:
            r = res[0]
            v = r.get("voice", "Khmer Male - Piseth")
            role = r.get("role", "male")
            tag = r.get("speaker_tag", "[ប្រុស]")
            persona = r.get("persona", "Male Adult")

            # Map to standard category name
            if role == "child":
                spk_type = "Child"
            elif role == "elder_female":
                spk_type = "Elderly Female"
            elif role == "elder_male":
                spk_type = "Elderly Male"
            elif role == "female":
                spk_type = "Female"
            else:
                spk_type = "Male"

            return {
                "speaker_name": r.get("speaker", "Speaker 1"),
                "clean_khmer_text": r.get("khmer_text", khmer_text),
                "voice": v,
                "speaker_type": spk_type,
                "role": role,
                "tag": tag,
                "persona": persona,
                "pitch_f0": r.get("pitch_f0", 0.0)
            }

        return {
            "speaker_name": "Speaker 1",
            "clean_khmer_text": khmer_text,
            "voice": "Khmer Male - Piseth",
            "speaker_type": "Male",
            "role": "male",
            "tag": "[ប្រុស]",
            "persona": "Male Adult",
            "pitch_f0": 0.0
        }
