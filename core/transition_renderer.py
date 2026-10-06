"""
Transition Preview Renderer (Phase 3 Gate 2 Core Architecture)
Pure visual blending component for desktop OpenCV preview:
- Cross Dissolve (SIMD cv2.addWeighted)
- Fade to Black / Color Dip (exact color midpoint at P=0.5)
- Directional Wipes (Left, Right, Up, Down) with normalized masks
- Zero timeline state, zero audio ownership, zero decoder lifecycle dependency
"""

import cv2
import numpy as np
from typing import Optional, Dict, Any


def render_cross_dissolve(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    progress: float
) -> np.ndarray:
    """
    Standard linear cross-dissolve:
      alpha = clamp(progress, 0.0, 1.0)
      output = frame_a * (1.0 - alpha) + frame_b * alpha
    """
    alpha = float(max(0.0, min(1.0, progress)))
    if alpha <= 0.0:
        return frame_a.copy()
    if alpha >= 1.0:
        return frame_b.copy()

    # Hardware-accelerated OpenCV SIMD blend
    return cv2.addWeighted(frame_a, 1.0 - alpha, frame_b, alpha, 0.0)


def render_color_dip(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    progress: float,
    color: tuple = (0, 0, 0)
) -> np.ndarray:
    """
    Fade to color (default: black):
      First half (P in [0.0, 0.5]):  Clip A fades to color
      Second half (P in [0.5, 1.0]): Color fades to Clip B
      At P = 0.5: Exact solid color frame
    """
    p = float(max(0.0, min(1.0, progress)))
    h, w = frame_a.shape[:2]
    ch = frame_a.shape[2] if len(frame_a.shape) > 2 else 1

    # Create solid color frame matching target dtype & shape
    if ch > 1:
        color_frame = np.full((h, w, ch), color[:ch], dtype=frame_a.dtype)
    else:
        color_frame = np.full((h, w), color[0], dtype=frame_a.dtype)

    if p <= 0.5:
        # Scale 0.0 -> 0.5 to 0.0 -> 1.0
        alpha = p * 2.0
        return cv2.addWeighted(frame_a, 1.0 - alpha, color_frame, alpha, 0.0)
    else:
        # Scale 0.5 -> 1.0 to 0.0 -> 1.0
        beta = (p - 0.5) * 2.0
        return cv2.addWeighted(color_frame, 1.0 - beta, frame_b, beta, 0.0)


def render_directional_wipe(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    progress: float,
    direction: str = "left",
    feather: float = 0.0
) -> np.ndarray:
    """
    Directional wipe transition:
      Supported directions: 'left', 'right', 'up', 'down'
      progress in [0.0, 1.0]
      feather: edge softness in [0.0, 0.2]
    """
    p = float(max(0.0, min(1.0, progress)))
    if p <= 0.0:
        return frame_a.copy()
    if p >= 1.0:
        return frame_b.copy()

    h, w = frame_a.shape[:2]
    d = direction.lower().strip()

    if d in ("left", "wipe_left"):
        # Boundary moves from right (1.0) to left (0.0)
        # At p=0: split=1.0 (all A). At p=1: split=0.0 (all B)
        split_x = int((1.0 - p) * w)
        split_x = max(0, min(w, split_x))
        out = frame_a.copy()
        out[:, split_x:] = frame_b[:, split_x:]
        return out

    elif d in ("right", "wipe_right"):
        # Boundary moves from left (0.0) to right (1.0)
        split_x = int(p * w)
        split_x = max(0, min(w, split_x))
        out = frame_a.copy()
        out[:, :split_x] = frame_b[:, :split_x]
        return out

    elif d in ("up", "wipe_up"):
        # Boundary moves from bottom (1.0) to top (0.0)
        split_y = int((1.0 - p) * h)
        split_y = max(0, min(h, split_y))
        out = frame_a.copy()
        out[split_y:, :] = frame_b[split_y:, :]
        return out

    elif d in ("down", "wipe_down"):
        # Boundary moves from top (0.0) to bottom (1.0)
        split_y = int(p * h)
        split_y = max(0, min(h, split_y))
        out = frame_a.copy()
        out[:split_y, :] = frame_b[:split_y, :]
        return out

    # Fallback to cross dissolve if unknown direction
    return render_cross_dissolve(frame_a, frame_b, p)


def render_transition_frame(
    frame_a: Optional[np.ndarray],
    frame_b: Optional[np.ndarray],
    transition_type: str,
    progress: float,
    parameters: Optional[Dict[str, Any]] = None
) -> Optional[np.ndarray]:
    """
    Unified entry point for transition frame rendering.
    Safely resolves dimensions and routes to the appropriate visual renderer.
    """
    if frame_a is None and frame_b is None:
        return None
    if frame_a is None:
        return frame_b.copy()
    if frame_b is None:
        return frame_a.copy()

    # Safety: Ensure dimensions match exactly
    if frame_a.shape != frame_b.shape:
        h, w = frame_a.shape[:2]
        frame_b_matched = cv2.resize(frame_b, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        frame_b_matched = frame_b

    params = parameters or {}
    t_type = (transition_type or "cross_dissolve").lower().strip()

    if t_type in ("cross_dissolve", "dissolve"):
        return render_cross_dissolve(frame_a, frame_b_matched, progress)

    elif t_type in ("fade_black", "fade_to_black", "dip_to_black", "color_dip"):
        dip_col = params.get("color", (0, 0, 0))
        return render_color_dip(frame_a, frame_b_matched, progress, color=dip_col)

    elif t_type.startswith("wipe_") or t_type in ("left", "right", "up", "down"):
        direction = params.get("direction", t_type.replace("wipe_", ""))
        feather = float(params.get("feather", 0.0))
        return render_directional_wipe(frame_a, frame_b_matched, progress, direction=direction, feather=feather)

    # Default fallback
    return render_cross_dissolve(frame_a, frame_b_matched, progress)
