"""
Dual Decoder Manager (Phase 3 Gate 2 Core Architecture)
Manages the lifecycle of primary and secondary OpenCV decoders for transition rendering:
- Primary decoder (cap_A) for outgoing clip
- Secondary decoder (cap_B) for incoming clip (pre-rolled ~500ms before transition)
- Decodes both frames during transition window [T_start, T_end)
- Reuses decoders across rapid scrubbing to prevent decoder thrashing and memory leaks
- Strictly guarantees: 0.0 <= source_time <= media_file_duration (No negative or out-of-bounds seeks)
- Exposes diagnostic counters (open_count, release_count, active_decoder_count) for verification
"""

import os
import cv2
import numpy as np
from typing import Dict, Any, Optional, Tuple
from utils.logger import logger


class SafeDecoder:
    """
    Wrapper around cv2.VideoCapture guaranteeing strict boundary validation,
    leak-free lifecycle, and timestamp safety.
    """
    def __init__(self, path: str, media_file_duration: float):
        self.path = os.path.abspath(path)
        self.media_file_duration = max(0.01, float(media_file_duration))
        self.cap: Optional[cv2.VideoCapture] = None
        self.is_opened = False
        self._open()

    def _open(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = cv2.VideoCapture(self.path)
        self.is_opened = bool(self.cap and self.cap.isOpened())

    def seek_and_read(self, source_time_sec: float) -> Tuple[bool, Optional[np.ndarray]]:
        """
        Safely seeks to source_time_sec with strict floating-point boundary clamping
        and retrieves the decoded frame.
        """
        if not self.is_opened or self.cap is None:
            return False, None

        # Critical Safety Rule: Strictly clamp between 0.0 and media_file_duration
        safe_time = max(0.0, min(self.media_file_duration, float(source_time_sec)))
        safe_time = round(safe_time, 6)

        # OpenCV expects milliseconds
        pos_msec = safe_time * 1000.0
        self.cap.set(cv2.CAP_PROP_POS_MSEC, pos_msec)
        ret, frame = self.cap.read()
        
        # EOF edge protection: if at end of container, clamp to last frame before EOF
        if (not ret or frame is None) and safe_time >= (self.media_file_duration - 0.1):
            fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 30.0)
            edge_time = max(0.0, self.media_file_duration - (1.0 / max(1.0, fps)))
            self.cap.set(cv2.CAP_PROP_POS_MSEC, edge_time * 1000.0)
            ret, frame = self.cap.read()

        return bool(ret and frame is not None), frame

    def release(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self.is_opened = False


class DualDecoderManager:
    """
    Dual-Decoder Manager for Seamless Transition Playback.
    Maintains at most 2 active decoders (Primary cap_A and Secondary cap_B).
    """
    def __init__(self):
        self._cap_a: Optional[SafeDecoder] = None
        self._cap_b: Optional[SafeDecoder] = None
        self._path_a: Optional[str] = None
        self._path_b: Optional[str] = None
        self._clip_a_id: Optional[str] = None
        self._clip_b_id: Optional[str] = None

        # Diagnostic counters for testing and health monitoring
        self.open_count = 0
        self.release_count = 0

    @property
    def active_decoder_count(self) -> int:
        count = 0
        if self._cap_a and self._cap_a.is_opened:
            count += 1
        if self._cap_b and self._cap_b.is_opened:
            count += 1
        return count

    def get_primary_decoder(self) -> Optional[SafeDecoder]:
        return self._cap_a

    def get_secondary_decoder(self) -> Optional[SafeDecoder]:
        return self._cap_b

    def _ensure_primary(self, clip_a: Dict[str, Any]) -> bool:
        """Ensure primary decoder matches clip_a, reusing if already opened."""
        path = clip_a.get("path", "")
        clip_id = clip_a.get("id")
        mf_dur = float(clip_a.get("media_file_duration", clip_a.get("duration", 10.0)))

        if self._cap_a and self._cap_a.is_opened and self._path_a == path:
            self._clip_a_id = clip_id
            return True

        # Need to open new primary
        if self._cap_a:
            self._cap_a.release()
            self.release_count += 1
            self._cap_a = None

        if path and (os.path.exists(path) or path.startswith("synthetic://")):
            self._cap_a = SafeDecoder(path, mf_dur)
            self._path_a = path
            self._clip_a_id = clip_id
            self.open_count += 1
            return self._cap_a.is_opened
        return False

    def _ensure_secondary(self, clip_b: Dict[str, Any]) -> bool:
        """Ensure secondary decoder matches clip_b, reusing if already opened."""
        path = clip_b.get("path", "")
        clip_id = clip_b.get("id")
        mf_dur = float(clip_b.get("media_file_duration", clip_b.get("duration", 10.0)))

        if self._cap_b and self._cap_b.is_opened and self._path_b == path:
            self._clip_b_id = clip_id
            return True

        if self._cap_b:
            self._cap_b.release()
            self.release_count += 1
            self._cap_b = None

        if path and (os.path.exists(path) or path.startswith("synthetic://")):
            self._cap_b = SafeDecoder(path, mf_dur)
            self._path_b = path
            self._clip_b_id = clip_id
            self.open_count += 1
            return self._cap_b.is_opened
        return False

    def release_secondary(self):
        """Release secondary decoder when exiting transition window."""
        if self._cap_b:
            self._cap_b.release()
            self.release_count += 1
            self._cap_b = None
            self._path_b = None
            self._clip_b_id = None

    def promote_secondary_to_primary(self, new_clip_data: Dict[str, Any]):
        """
        Transition exit: Promote secondary decoder (clip B) to primary,
        releasing the old primary decoder (clip A).
        """
        if self._cap_b and self._cap_b.is_opened and self._path_b == new_clip_data.get("path"):
            if self._cap_a:
                self._cap_a.release()
                self.release_count += 1
            self._cap_a = self._cap_b
            self._path_a = self._path_b
            self._clip_a_id = new_clip_data.get("id")

            self._cap_b = None
            self._path_b = None
            self._clip_b_id = None
        else:
            self.release_secondary()
            self._ensure_primary(new_clip_data)

    def prepare_preroll(self, clip_b: Dict[str, Any]):
        """Pre-roll: Initialize secondary decoder ~500ms before transition."""
        self._ensure_secondary(clip_b)

    def decode_transition_pair(
        self,
        clip_a: Dict[str, Any],
        clip_b: Dict[str, Any],
        source_time_A: float,
        source_time_B: float
    ) -> Tuple[bool, Optional[np.ndarray], bool, Optional[np.ndarray]]:
        """
        Decodes both frames simultaneously during transition window.
        Returns: (ret_A, frame_A, ret_B, frame_B)
        """
        ok_a = self._ensure_primary(clip_a)
        ok_b = self._ensure_secondary(clip_b)

        if not ok_a or not ok_b:
            return False, None, False, None

        ret_a, frame_a = self._cap_a.seek_and_read(source_time_A)
        ret_b, frame_b = self._cap_b.seek_and_read(source_time_B)

        return ret_a, frame_a, ret_b, frame_b

    def close(self):
        """Deterministic cleanup of all active decoders."""
        if self._cap_a:
            self._cap_a.release()
            self.release_count += 1
            self._cap_a = None
        if self._cap_b:
            self._cap_b.release()
            self.release_count += 1
            self._cap_b = None
        self._path_a = None
        self._path_b = None
        self._clip_a_id = None
        self._clip_b_id = None
