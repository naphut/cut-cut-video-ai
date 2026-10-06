"""
Production-Grade Subtitle Timing Resolver
Ensures subtitle intervals meet target minimum duration (1.2s - 1.5s)
without overlapping consecutive subtitles, violating clip boundaries,
or breaking existing global synchronization.
"""

from typing import List, Dict, Any, Union, Optional
from core.models import Segment

TARGET_MIN_DURATION = 1.2    # Minimum duration for comfortable human reading
PREFERRED_DURATION = 1.5     # Preferred duration when natural silence gap is available
DEFAULT_SAFETY_GAP = 0.05    # 50ms buffer between consecutive subtitles to prevent visual merging


def resolve_subtitle_timings(
    segments: List[Union[Segment, Dict[str, Any]]],
    total_duration_sec: float = 999999.0,
    target_min_duration: float = TARGET_MIN_DURATION,
    preferred_duration: float = PREFERRED_DURATION,
    safety_gap: float = DEFAULT_SAFETY_GAP,
    tts_durations: Optional[List[float]] = None
) -> List[Dict[str, Any]]:
    """
    Resolve and optimize subtitle timing intervals:
    1. Preserves original STT start time whenever possible.
    2. If duration < target_min_duration, attempts to extend it up to preferred_duration.
    3. Strictly prevents overlapping with the next subtitle (maintains safety_gap).
    4. Never extends beyond total_duration_sec.
    5. If TTS duration is longer than STT duration, extends subtitle duration into available gap
       to stay synchronized with the spoken audio.
    6. Does not alter the global timeline clock or drop segments.
    """
    if not segments:
        return []

    # Convert all inputs to dictionary copies with preserved attributes
    resolved: List[Dict[str, Any]] = []
    for idx, s in enumerate(segments):
        if isinstance(s, Segment):
            item = {
                "id": s.id,
                "start": float(s.start),
                "end": float(s.end),
                "text": getattr(s, "original_text", getattr(s, "text", "")),
                "khmer_text": getattr(s, "translated_text", getattr(s, "khmer_text", "")),
                "speaker": getattr(s, "speaker_id", getattr(s, "speaker", "Speaker 1")),
                "voice": getattr(s, "voice", ""),
                "tts_audio": getattr(s, "tts_audio", None)
            }
        else:
            item = dict(s)
            item["start"] = float(item.get("start", 0.0))
            item["end"] = float(item.get("end", item["start"] + 1.0))

        # Check if tts_duration is provided via segment or parameter
        if tts_durations and idx < len(tts_durations) and tts_durations[idx] is not None:
            item["_tts_dur"] = float(tts_durations[idx])
        elif "tts_duration" in item and item["tts_duration"]:
            item["_tts_dur"] = float(item["tts_duration"])

        resolved.append(item)

    # Sort strictly by start time to maintain causal order
    resolved.sort(key=lambda x: x["start"])
    num_segs = len(resolved)

    for i in range(num_segs):
        seg = resolved[i]
        st = seg["start"]
        et = seg["end"]
        orig_dur = max(0.01, et - st)

        # Determine next boundary limit
        if i + 1 < num_segs:
            nxt_st = resolved[i + 1]["start"]
            # Maximum allowed end is next start minus safety gap
            max_allowed_end = max(st + 0.1, min(total_duration_sec, nxt_st - safety_gap))
        else:
            max_allowed_end = total_duration_sec

        # Handle overlapping input timestamps gracefully
        if et > max_allowed_end:
            et = max(st + 0.05, max_allowed_end)

        current_dur = et - st
        tts_dur = seg.get("_tts_dur", None)

        # 1. Coordinate with TTS duration if speech is longer than current interval
        if tts_dur is not None and tts_dur > current_dur:
            desired_end = st + tts_dur
            et = min(desired_end, max_allowed_end)
            current_dur = et - st

        # 2. Check if duration is below target minimum
        if current_dur < target_min_duration:
            # Attempt to extend to preferred_duration (1.5s) if space allows, otherwise up to target_min (1.2s)
            desired_end = st + preferred_duration
            new_et = min(desired_end, max_allowed_end)
            if new_et > et:
                et = new_et

        # Store resolved timings
        seg["start"] = round(st, 3)
        seg["end"] = round(et, 3)
        
        # Clean up temporary fields
        seg.pop("_tts_dur", None)

    return resolved
