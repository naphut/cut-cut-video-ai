"""
Voice Activity Detection (VAD) Service
Detects precise speech regions, eliminates silence/music intervals,
and groups contiguous speech into VAD-aware chunks without cutting words or sentences.
"""
import os
from typing import List, Dict, Any
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
from utils.logger import logger

class VADService:
    def __init__(
        self,
        min_silence_len_ms: int = 700,
        silence_thresh_offset_db: float = -18.0,
        min_speech_len_ms: int = 150,
        speech_padding_ms: int = 450
    ):
        self.min_silence_len_ms = min_silence_len_ms
        self.silence_thresh_offset_db = silence_thresh_offset_db
        self.min_speech_len_ms = min_speech_len_ms
        self.speech_padding_ms = speech_padding_ms

    def detect_speech_regions(self, audio_path: str) -> List[Dict[str, float]]:
        """
        Scan audio file and return list of exact speech intervals.
        Output: [{'start': 4.21, 'end': 8.93}, {'start': 11.42, 'end': 16.70}, ...]
        """
        if not os.path.exists(audio_path):
            logger.error(f"❌ VAD: Audio file not found: {audio_path}")
            return []

        try:
            audio = AudioSegment.from_file(audio_path)
            total_duration_sec = len(audio) / 1000.0

            # Dynamic thresholding based on audio dBFS
            silence_thresh = max(-45.0, audio.dBFS + self.silence_thresh_offset_db)
            logger.info(f"🎙 [VAD] Scanning speech regions (dBFS: {audio.dBFS:.1f} dB, silence threshold: {silence_thresh:.1f} dB)...")

            nonsilent_ranges_ms = detect_nonsilent(
                audio,
                min_silence_len=self.min_silence_len_ms,
                silence_thresh=silence_thresh
            )

            speech_regions = []
            for start_ms, end_ms in nonsilent_ranges_ms:
                # Apply padding to preserve initial plosives and ending consonants
                pad_start = max(0, start_ms - self.speech_padding_ms)
                pad_end = min(len(audio), end_ms + self.speech_padding_ms)

                dur_ms = pad_end - pad_start
                if dur_ms >= self.min_speech_len_ms:
                    st_sec = round(pad_start / 1000.0, 2)
                    et_sec = round(pad_end / 1000.0, 2)
                    speech_regions.append({
                        "start": st_sec,
                        "end": et_sec,
                        "duration": round(et_sec - st_sec, 2)
                    })

            # Merge overlapping speech intervals if padding caused collisions
            merged_regions = self._merge_overlapping_regions(speech_regions)
            logger.info(f"✅ [VAD] Detected {len(merged_regions)} speech regions across {total_duration_sec:.1f}s audio.")
            return merged_regions

        except Exception as e:
            logger.error(f"❌ [VAD] Error analyzing audio: {e}")
            return []

    def _merge_overlapping_regions(self, regions: List[Dict[str, float]]) -> List[Dict[str, float]]:
        """Merge adjacent speech regions that overlap or are separated by less than 200ms."""
        if not regions:
            return []

        merged = [regions[0]]
        for curr in regions[1:]:
            prev = merged[-1]
            if curr["start"] <= prev["end"] + 0.20:
                prev["end"] = max(prev["end"], curr["end"])
                prev["duration"] = round(prev["end"] - prev["start"], 2)
            else:
                merged.append(curr)
        return merged

    def group_vad_chunks(
        self,
        audio_duration_sec: float,
        speech_regions: List[Dict[str, float]],
        target_chunk_sec: float = 300.0
    ) -> List[Dict[str, Any]]:
        """
        Group speech regions into VAD-aware chunks (default: ~5 minutes).
        Guarantees that chunk boundaries ONLY fall on verified silence gaps,
        never cutting speech in the middle of a sentence.
        """
        if not speech_regions or audio_duration_sec <= target_chunk_sec + 60.0:
            return [{
                "index": 1,
                "start": 0.0,
                "end": round(audio_duration_sec, 2),
                "duration": round(audio_duration_sec, 2)
            }]

        chunks = []
        chunk_start = 0.0
        current_chunk_regions = []

        for region in speech_regions:
            current_chunk_regions.append(region)
            current_duration = region["end"] - chunk_start

            # If approaching target chunk duration, find the natural silence gap to cut
            if current_duration >= target_chunk_sec:
                chunk_end = region["end"] + 0.25
                chunks.append({
                    "index": len(chunks) + 1,
                    "start": round(chunk_start, 2),
                    "end": round(chunk_end, 2),
                    "duration": round(chunk_end - chunk_start, 2)
                })
                chunk_start = chunk_end
                current_chunk_regions = []

        # Remaining tail
        if chunk_start < audio_duration_sec:
            chunks.append({
                "index": len(chunks) + 1,
                "start": round(chunk_start, 2),
                "end": round(audio_duration_sec, 2),
                "duration": round(audio_duration_sec - chunk_start, 2)
            })

        logger.info(f"🧩 [VAD] Partitioned audio into {len(chunks)} VAD-aligned chunks.")
        return chunks
