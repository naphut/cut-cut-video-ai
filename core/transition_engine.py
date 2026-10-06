"""
Transition Engine (Phase 3 Core Architecture)
Deterministic mathematical foundation for Non-Linear Editing transitions:
- Canonical source and handle calculations
- Alignment-aware duration clamping
- Normalized progress & monotonic easing functions
- Exact multi-clip source mapping with variable speed support
- Zero silent-freeze policy (hard validation)
"""

import math
from typing import Dict, Any, Tuple, Optional
from dataclasses import dataclass


@dataclass
class TransitionTiming:
    start_time: float
    end_time: float
    duration: float
    cut_time: float
    alignment: str


@dataclass
class HandleAvailability:
    available_out_handle_A: float  # in source seconds
    available_in_handle_B: float   # in source seconds
    max_outgoing_timeline: float   # in timeline seconds
    max_incoming_timeline: float   # in timeline seconds
    max_transition_duration: float # in timeline seconds
    status: str                    # "FULL", "CLAMPED", "REJECTED"
    allowed_duration: float        # final validated transition duration
    reason: str = ""


@dataclass
class TransitionResolutionState:
    is_active: bool
    status: str = "INACTIVE"       # "VALID", "CLAMPED", "REJECTED", "INACTIVE"
    fallback: str = "NONE"         # "NONE", "HARD_CUT"
    transition_id: Optional[str] = None
    clip_a_id: Optional[str] = None
    clip_b_id: Optional[str] = None
    progress: float = 0.0
    source_time_A: Optional[float] = None
    source_time_B: Optional[float] = None
    is_valid_media_range: bool = True
    error_message: str = ""


def validate_positive_number(val: Any, name: str) -> float:
    """Validate that val is a positive, non-zero, finite number (not NaN, not Inf)."""
    if not isinstance(val, (int, float)):
        raise TypeError(f"{name} must be a numeric type, got {type(val).__name__}")
    f_val = float(val)
    if math.isnan(f_val):
        raise ValueError(f"{name} cannot be NaN")
    if math.isinf(f_val):
        raise ValueError(f"{name} cannot be infinite")
    if f_val <= 0.0:
        raise ValueError(f"{name} must be positive (> 0), got {f_val}")
    return f_val


def calculate_source_coordinates(
    source_in: float,
    duration: float,
    speed: float,
    media_file_duration: Optional[float] = None
) -> Tuple[float, float, float]:
    """
    Canonical source math:
      source_consumed = duration * speed
      source_out = source_in + source_consumed
    Validation:
      0 <= source_in < source_out <= media_file_duration
      duration > 0, speed > 0
    Returns: (source_in, source_out, source_consumed)
    """
    dur = validate_positive_number(duration, "duration")
    spd = validate_positive_number(speed, "speed")

    if not isinstance(source_in, (int, float)) or math.isnan(source_in) or math.isinf(source_in):
        raise ValueError("source_in must be a finite number")
    s_in = float(source_in)
    if s_in < 0.0:
        raise ValueError(f"source_in cannot be negative: {s_in}")

    source_consumed = dur * spd
    source_out = s_in + source_consumed

    if media_file_duration is not None:
        mf_dur = float(media_file_duration)
        if math.isnan(mf_dur) or math.isinf(mf_dur) or mf_dur <= 0.0:
            raise ValueError(f"media_file_duration must be positive finite, got {mf_dur}")
        # Allow tiny epsilon for floating point inaccuracies
        if source_out > (mf_dur + 1e-4):
            raise ValueError(
                f"Invalid source range: source_out ({source_out:.4f}s) exceeds media_file_duration ({mf_dur:.4f}s)"
            )

    return s_in, source_out, source_consumed


def calculate_transition_timing(
    cut_time: float,
    duration: float,
    alignment: str = "center"
) -> TransitionTiming:
    """
    Calculates transition start and end bounds based on boundary cut point and alignment.
    Supported alignments:
      - "center": 50% on Outgoing Clip A, 50% on Incoming Clip B
      - "start_on_cut": 0% on A, 100% on B (transition starts at cut_time)
      - "end_on_cut": 100% on A, 0% on B (transition ends at cut_time)
    """
    cut = float(cut_time)
    if math.isnan(cut) or math.isinf(cut) or cut < 0.0:
        raise ValueError(f"cut_time must be a non-negative finite number, got {cut_time}")
    dur = validate_positive_number(duration, "duration")

    align = alignment.lower().strip()
    if align == "center":
        half = dur / 2.0
        start_t = max(0.0, cut - half)
        end_t = cut + half
    elif align == "start_on_cut":
        start_t = cut
        end_t = cut + dur
    elif align == "end_on_cut":
        start_t = max(0.0, cut - dur)
        end_t = cut
    else:
        raise ValueError(f"Unsupported transition alignment: '{alignment}'. Must be 'center', 'start_on_cut', or 'end_on_cut'.")

    return TransitionTiming(
        start_time=round(start_t, 6),
        end_time=round(end_t, 6),
        duration=dur,
        cut_time=cut,
        alignment=align
    )


def calculate_easing_progress(tau: float, easing: str = "linear") -> float:
    """
    Monotonic, strictly bounded easing progress computation:
      tau: Normalized linear time in [0.0, 1.0]
    Returns progress P in [0.0, 1.0].
    """
    if math.isnan(tau) or math.isinf(tau):
        raise ValueError("Progress tau cannot be NaN or infinite")
    t = max(0.0, min(1.0, float(tau)))

    ease = easing.lower().strip()
    if ease == "linear":
        p = t
    elif ease == "ease_in":
        p = t * t
    elif ease == "ease_out":
        p = 1.0 - (1.0 - t) * (1.0 - t)
    elif ease == "ease_in_out":
        if t < 0.5:
            p = 2.0 * t * t
        else:
            p = 1.0 - 2.0 * (1.0 - t) * (1.0 - t)
    else:
        # Fallback to linear for unrecognized easing
        p = t

    return max(0.0, min(1.0, float(p)))


def calculate_transition_progress(
    global_time: float,
    start_time: float,
    duration: float,
    easing: str = "linear"
) -> float:
    """
    Returns normalized eased progress P in [0.0, 1.0] for a given global timeline timestamp.
    """
    dur = validate_positive_number(duration, "duration")
    g_time = float(global_time)

    if g_time <= start_time:
        return 0.0
    if g_time >= (start_time + dur):
        return 1.0

    tau = (g_time - start_time) / dur
    return calculate_easing_progress(tau, easing)


def evaluate_transition_handles(
    media_file_duration_A: float,
    source_out_A: float,
    speed_A: float,
    source_in_B: float,
    speed_B: float,
    requested_duration: float,
    alignment: str = "center"
) -> HandleAvailability:
    """
    Alignment-aware handle engine:
      available_out_handle_A = media_file_duration_A - source_out_A
      available_in_handle_B = source_in_B
      max_outgoing_timeline = available_out_handle_A / speed_A
      max_incoming_timeline = available_in_handle_B / speed_B

    Alignment rules:
      CENTER:       max_duration = 2 * min(max_outgoing_timeline, max_incoming_timeline)
      START_ON_CUT: max_duration = max_incoming_timeline
      END_ON_CUT:   max_duration = max_outgoing_timeline

    Policy:
      If max_duration <= 0.05: REJECTED (kept as cut)
      If requested_duration <= max_duration: FULL
      Else: CLAMPED
    """
    spd_A = validate_positive_number(speed_A, "speed_A")
    spd_B = validate_positive_number(speed_B, "speed_B")
    req_dur = validate_positive_number(requested_duration, "requested_duration")

    avail_out_A = max(0.0, float(media_file_duration_A) - float(source_out_A))
    avail_in_B = max(0.0, float(source_in_B))

    max_out_tl = avail_out_A / spd_A
    max_in_tl = avail_in_B / spd_B

    align = alignment.lower().strip()
    if align == "center":
        max_dur = 2.0 * min(max_out_tl, max_in_tl)
    elif align == "start_on_cut":
        max_dur = max_in_tl
    elif align == "end_on_cut":
        max_dur = max_out_tl
    else:
        raise ValueError(f"Unknown alignment: {alignment}")

    # Validation & clamping policy
    if max_dur <= 0.05:
        return HandleAvailability(
            available_out_handle_A=round(avail_out_A, 4),
            available_in_handle_B=round(avail_in_B, 4),
            max_outgoing_timeline=round(max_out_tl, 4),
            max_incoming_timeline=round(max_in_tl, 4),
            max_transition_duration=round(max_dur, 4),
            status="REJECTED",
            allowed_duration=0.0,
            reason=f"Insufficient handles ({max_dur:.3f}s <= 0.05s). Transition rejected to avoid freeze/desync."
        )

    if req_dur <= (max_dur + 1e-4):
        return HandleAvailability(
            available_out_handle_A=round(avail_out_A, 4),
            available_in_handle_B=round(avail_in_B, 4),
            max_outgoing_timeline=round(max_out_tl, 4),
            max_incoming_timeline=round(max_in_tl, 4),
            max_transition_duration=round(max_dur, 4),
            status="FULL",
            allowed_duration=req_dur,
            reason="Full transition duration supported by available media handles."
        )
    else:
        clamped_dur = round(max_dur, 4)
        return HandleAvailability(
            available_out_handle_A=round(avail_out_A, 4),
            available_in_handle_B=round(avail_in_B, 4),
            max_outgoing_timeline=round(max_out_tl, 4),
            max_incoming_timeline=round(max_in_tl, 4),
            max_transition_duration=round(max_dur, 4),
            status="CLAMPED",
            allowed_duration=clamped_dur,
            reason=f"Requested {req_dur:.3f}s exceeds available handles. Clamped to {clamped_dur:.3f}s."
        )


def resolve_transition_at_time(
    global_time: float,
    transition: Dict[str, Any],
    clip_a: Dict[str, Any],
    clip_b: Dict[str, Any]
) -> TransitionResolutionState:
    """
    Pure resolver:
      Determines if global_time T falls within the transition window.
      Maps exact source timestamps:
        source_time_A = source_in_A + (T - clip_a.start) * speed_A
        source_time_B = source_in_B + (T - clip_b.start) * speed_B
      Ensures strict safety:
        0.0 <= source_time_A <= media_file_duration_A
        0.0 <= source_time_B <= media_file_duration_B
        Never returns negative source time or silent extrapolations.
    """
    t_id = transition.get("id")
    cut_t = float(transition.get("cut_time", 0.0))
    dur = float(transition.get("duration", 0.0))
    align = transition.get("alignment", "center")
    easing = transition.get("easing", "linear")

    timing = calculate_transition_timing(cut_t, dur, align)
    g_time = float(global_time)

    # Check if global_time is within transition window [start_time, end_time)
    if g_time < timing.start_time or g_time >= timing.end_time:
        return TransitionResolutionState(
            is_active=False,
            transition_id=t_id,
            clip_a_id=clip_a.get("id"),
            clip_b_id=clip_b.get("id"),
            progress=0.0
        )

    # Compute normalized progress
    progress = calculate_transition_progress(g_time, timing.start_time, timing.duration, easing)

    # Resolve Outgoing Clip A source coordinate
    s_in_A = float(clip_a.get("source_in", 0.0))
    spd_A = float(clip_a.get("speed", 1.0))
    st_A = float(clip_a.get("start", 0.0))
    mf_dur_A = float(clip_a.get("media_file_duration", clip_a.get("duration", 0.0) * spd_A))

    source_time_A = s_in_A + (g_time - st_A) * spd_A

    # Resolve Incoming Clip B source coordinate
    s_in_B = float(clip_b.get("source_in", 0.0))
    spd_B = float(clip_b.get("speed", 1.0))
    st_B = float(clip_b.get("start", 0.0))
    mf_dur_B = float(clip_b.get("media_file_duration", clip_b.get("duration", 0.0) * spd_B))

    source_time_B = s_in_B + (g_time - st_B) * spd_B

    # Strict bounds validation
    is_valid = True
    err_msg = ""

    if source_time_A < -1e-4:
        is_valid = False
        err_msg = f"Clip A source_time is negative: {source_time_A:.4f}s"
    elif source_time_A > (mf_dur_A + 1e-3):
        is_valid = False
        err_msg = f"Clip A source_time ({source_time_A:.4f}s) exceeds media duration ({mf_dur_A:.4f}s)"

    if source_time_B < -1e-4:
        is_valid = False
        err_msg = f"Clip B source_time is negative: {source_time_B:.4f}s"
    elif source_time_B > (mf_dur_B + 1e-3):
        is_valid = False
        err_msg = f"Clip B source_time ({source_time_B:.4f}s) exceeds media duration ({mf_dur_B:.4f}s)"

    # Hard clamp to media limits to avoid crash, but flag validity state
    safe_src_A = max(0.0, min(mf_dur_A, source_time_A))
    safe_src_B = max(0.0, min(mf_dur_B, source_time_B))

    return TransitionResolutionState(
        is_active=True,
        transition_id=t_id,
        clip_a_id=clip_a.get("id"),
        clip_b_id=clip_b.get("id"),
        progress=round(progress, 6),
        source_time_A=round(safe_src_A, 6),
        source_time_B=round(safe_src_B, 6),
        is_valid_media_range=is_valid,
        error_message=err_msg
    )
