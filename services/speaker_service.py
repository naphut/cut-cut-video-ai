import os
from typing import List, Dict
from utils.logger import logger
from services.speaker_detector import SpeakerDetector

class SpeakerService:
    """
    Dedicated Speaker Diarization & Speaker Clustering Service.
    Groups speech segments into distinct speakers (speaker_001, speaker_002, etc.).
    """
    def __init__(self):
        self.detector = SpeakerDetector()

    def identify_speaker_clusters(self, audio_path: str, subtitle_segments: List[dict]) -> List[dict]:
        """
        Assign speaker labels (Speaker 1, Speaker 2) across subtitle segments
        based on median vocal pitch F0 and segment gap analysis.
        """
        if not subtitle_segments:
            return []

        detector = SpeakerDetector(audio_path)
        updated_segments = detector.diarize_and_profile_segments(subtitle_segments)
        logger.info(f"👥 [SpeakerService] Assigned speaker labels across {len(updated_segments)} subtitle segments.")
        return updated_segments

