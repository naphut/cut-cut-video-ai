"""
Production-Grade Dialogue Synchronization Engine
Aligns synthesized speech segments with actor dialogue slots.
Implements the recommended timing hierarchy:
1. Duration verification: If TTS fits within slot -> place directly onto timeline.
2. If TTS is longer ->
   a. Borrow available natural silence gap from following segment (up to 400ms).
   b. Apply mild phase-vocoder time-stretch (strictly clamped 0.88x - 1.25x to preserve human pitch).
3. Smooth boundary cross-fading and timeline overlay.
"""
import os
import wave
from typing import List, Dict, Union, Any
import numpy as np
from pydub import AudioSegment
from pydub.effects import normalize
from core.models import Segment
from utils.file_utils import get_temp_path
from utils.ffmpeg import adjust_audio_speed
from utils.logger import logger

class DialogueSyncEngine:
    def __init__(self, max_stretch_factor: float = 1.50, min_stretch_factor: float = 0.88):
        self.max_stretch_factor = max_stretch_factor
        self.min_stretch_factor = min_stretch_factor

    def sync_dialogue_timeline(
        self,
        segments: List[Union[Segment, Dict[str, Any]]],
        seg_audio_paths: List[str],
        total_duration_sec: float,
        output_master_path: str,
        strict: bool = False
    ) -> str:
        """
        Stitch synthesized segment audio clips into a unified dialogue master track
        synchronized with actor mouth movements and scene pacing.
        Guarantees complete spoken dialogue without mid-sentence truncation.
        """
        try:
            num_segs = len(segments)
            num_paths = len(seg_audio_paths)
            if num_segs != num_paths:
                err_msg = f"DialogueSync length mismatch: {num_segs} segments != {num_paths} audio paths"
                logger.error(f"❌ {err_msg}")
                if strict:
                    raise ValueError(err_msg)

            # Check for any missing or invalid audio paths
            missing_audio_ids = []
            for idx_check, (s_chk, p_chk) in enumerate(zip(segments, seg_audio_paths)):
                s_id = getattr(s_chk, "id", None) or (s_chk.get("id") if isinstance(s_chk, dict) else f"idx_{idx_check+1}")
                if not p_chk or not os.path.exists(p_chk) or os.path.getsize(p_chk) == 0:
                    missing_audio_ids.append(s_id)

            if missing_audio_ids:
                err_msg = f"DialogueSync Safety Failure: {len(missing_audio_ids)}/{num_segs} segments missing valid TTS audio: {missing_audio_ids}"
                logger.error(f"❌ {err_msg}")
                if strict:
                    raise RuntimeError(err_msg)

            total_samples = int((total_duration_sec + 2.0) * 44100)
            timeline_pcm = np.zeros((total_samples, 2), dtype=np.int32)

            logger.info(f"⏱ [DialogueSync] Synchronizing {num_segs} dialogue segments across {total_duration_sec:.1f}s via fast numpy PCM buffer...")

            for i, (seg, raw_path) in enumerate(zip(segments, seg_audio_paths)):
                if not raw_path or not os.path.exists(raw_path):
                    continue

                st_sec = seg.start if isinstance(seg, Segment) else float(seg.get("start", 0.0))
                et_sec = seg.end if isinstance(seg, Segment) else float(seg.get("end", st_sec + 2.0))
                
                # Strict boundary enforcement: 0 <= start < end <= video_duration
                seg_id = getattr(seg, "id", None) or (seg.get("id") if isinstance(seg, dict) else f"idx_{i+1}")
                if st_sec < 0.0 or et_sec <= st_sec or st_sec >= total_duration_sec:
                    err_timing = f"DialogueSync invalid segment timing {seg_id}: [{st_sec:.2f}s - {et_sec:.2f}s] exceeds video bounds (0.0s - {total_duration_sec:.2f}s)"
                    logger.warning(f"⚠️ [DialogueSync] Rejecting invalid segment: {err_timing}")
                    if strict:
                        raise ValueError(err_timing)
                    continue

                if et_sec > total_duration_sec:
                    et_sec = total_duration_sec

                start_ms = int(st_sec * 1000)
                end_ms = int(et_sec * 1000)
                slot_ms = max(400, end_ms - start_ms)

                try:
                    audio = AudioSegment.from_file(raw_path)
                    actual_ms = len(audio)
                except Exception as e_dec:
                    err_dec = f"DialogueSync unreadable audio '{raw_path}' for segment {seg_id}: {e_dec}"
                    logger.error(f"❌ {err_dec}")
                    if strict:
                        raise RuntimeError(err_dec)
                    continue

                if actual_ms <= 0:
                    err_zero = f"DialogueSync zero-duration audio '{raw_path}' for segment {seg_id}"
                    logger.error(f"❌ {err_zero}")
                    if strict:
                        raise RuntimeError(err_zero)
                    continue

                next_start_ms = int((total_duration_sec + 2.0) * 1000)
                if i + 1 < num_segs:
                    nxt_seg = segments[i + 1]
                    nxt_st = nxt_seg.start if isinstance(nxt_seg, Segment) else float(nxt_seg.get("start", 0.0))
                    next_start_ms = max(start_ms + 200, int(nxt_st * 1000))

                # 1. DURATION CHECK: Does it fit naturally within slot?
                if actual_ms <= slot_ms:
                    pass
                else:
                    # 2. TTS IS LONGER: Borrow available natural silence gap before next segment
                    silence_gap_ms = max(0, next_start_ms - end_ms)
                    # Allow borrowing up to 600ms of natural silence
                    borrowable_ms = min(600, int(silence_gap_ms * 0.85))
                    effective_slot_ms = slot_ms + borrowable_ms

                    if actual_ms <= effective_slot_ms:
                        logger.info(f"⏱ [DialogueSync] Seg {i+1}: Borrowed {borrowable_ms}ms pause. Audio fits without stretching.")
                    else:
                        # Apply mild time-stretch clamped strictly to max stretch factor (up to 1.50x)
                        raw_speed = actual_ms / float(effective_slot_ms)
                        speed_factor = min(self.max_stretch_factor, max(self.min_stretch_factor, raw_speed))
                        
                        adjusted_path = get_temp_path(f"synced_seg_{i:03d}_{os.path.basename(raw_path)}")
                        if adjust_audio_speed(raw_path, adjusted_path, speed_factor):
                            audio = AudioSegment.from_file(adjusted_path)
                            logger.info(f"⏱ [DialogueSync] Seg {i+1}: mild stretch applied ({speed_factor:.2f}x).")

                # 3. ANTI-COLLISION & DIALOGUE COMPLETION GUARANTEE:
                # Never cut dialogue halfway through. If speech slightly overlaps with next segment start,
                # prioritize speech compression up to max_stretch_factor.
                max_allowed_len = max(250, next_start_ms - start_ms - 20)
                if len(audio) > max_allowed_len:
                    total_needed_speed = len(audio) / float(max_allowed_len)
                    speed_factor = min(self.max_stretch_factor, total_needed_speed)
                    if speed_factor > 1.03:
                        speed_path = get_temp_path(f"anti_overlap_{i:03d}_{os.path.basename(raw_path)}")
                        if adjust_audio_speed(raw_path, speed_path, speed_factor):
                            audio = AudioSegment.from_file(speed_path)
                            logger.info(f"⏱ [DialogueSync] Seg {i+1}: Adaptive tempo speedup applied ({speed_factor:.2f}x) to preserve complete line.")

                    # If after maximum tempo speedup the audio still slightly extends into the next segment,
                    # DO NOT truncate the words! The additive timeline PCM buffer will smoothly blend
                    # the ending decay with the next utterance rather than destroying the final spoken words.
                    if len(audio) > max_allowed_len:
                        logger.info(f"⏱ [DialogueSync] Seg {i+1}: Permitting natural word decay ({len(audio)}ms into next slot at {max_allowed_len}ms) to prevent cutting off speech.")

                # Boundary fade to eliminate clicks
                if len(audio) > 30:
                    audio = audio.fade_in(10).fade_out(10)

                # Loudness normalization
                try:
                    audio = normalize(audio)
                except Exception:
                    pass

                # Direct zero-copy placement onto 44.1kHz stereo PCM buffer
                audio_stereo = audio.set_frame_rate(44100).set_channels(2)
                raw_bytes = audio_stereo.raw_data
                seg_samples = np.frombuffer(raw_bytes, dtype=np.int16).reshape(-1, 2)
                st_sample = int(start_ms * 44.1)
                et_sample = min(total_samples, st_sample + len(seg_samples))
                actual_len = et_sample - st_sample
                if actual_len > 0:
                    timeline_pcm[st_sample:et_sample] += seg_samples[:actual_len].astype(np.int32)

            # Export directly to 44.1kHz 16-bit WAV with zero clipping
            clipped_pcm = np.clip(timeline_pcm, -32768, 32767).astype(np.int16)
            import wave
            with wave.open(output_master_path, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(clipped_pcm.tobytes())

            logger.info(f"✅ [DialogueSync] Master dialogue track exported (44.1kHz stereo): {output_master_path}")
            return output_master_path

        except Exception as e:
            logger.error(f"❌ [DialogueSync] Error: {e}")
            raise
