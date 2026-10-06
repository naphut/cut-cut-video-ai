"""
Subtitle Animation & Visual Effects Engine (CapCut Style)
High-performance SIMD/NumPy accelerated subtitle transitions and visual effects:
- Pop / Bounce (Elastic Zoom In)
- Cyber Field (Cyberpunk Chromatic Aberration & Scanlines)
- Frequency Decode (Hacker Waveform & Glitch Decrypt)
- Wiping In (Smooth Soft Wipe with Shimmer Glow Edge)
- Magical Duel (Radiant Energy Aura Glow & Appearance Flash)
- Blur Dissolve (Cinematic Blur-to-Sharp Focus)
- Karaoke Pulse (Travelling Syllable Wave Highlight)
- Glitch Shake (Impact Vibration Shake)
- None / Static (High-Contrast Clean HarfBuzz Text)
"""

import math
import cv2
import numpy as np
from typing import Tuple, Optional


def apply_subtitle_animation_effect(
    frame: np.ndarray,
    text_bgr: np.ndarray,
    alpha: np.ndarray,
    sx: int,
    sy: int,
    sw: int,
    sh: int,
    anim_type: str = "pop_bounce",
    anim_dur: float = 0.35,
    cur_sec: float = 0.0,
    seg_start: float = 0.0,
    seg_dur: float = 1.0
) -> np.ndarray:
    """
    Applies CapCut-style dynamic subtitle animations & visual effects onto a video frame.
    All operations are restricted strictly to the tight bounding box ROI for maximum speed.
    """
    h_canvas, w_canvas = frame.shape[:2]
    t = max(0.0, cur_sec - seg_start)
    anim_dur = max(0.05, float(anim_dur))
    progress = min(1.0, t / anim_dur)
    
    anim_key = str(anim_type).lower().strip()
    if anim_key in ["none", "static", "default"]:
        return _blend_static(frame, text_bgr, alpha, sx, sy, sw, sh)

    # 1. Pop / Bounce (CapCut Elastic Zoom)
    if "pop" in anim_key or "bounce" in anim_key or "zoom" in anim_key:
        if progress < 1.0:
            if progress < 0.60:
                scale = 0.65 + (progress / 0.60) * 0.45  # 0.65 -> 1.10
            else:
                p2 = (progress - 0.60) / 0.40
                scale = 1.10 - p2 * 0.10  # 1.10 -> 1.00
            
            new_w = max(10, int(sw * scale))
            new_h = max(10, int(sh * scale))
            scaled_bgr = cv2.resize(text_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            scaled_alpha = cv2.resize(alpha, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            if scaled_alpha.ndim == 2:
                scaled_alpha = scaled_alpha[:, :, np.newaxis]
            
            cx = sx + sw // 2
            cy = sy + sh // 2
            nsx = max(0, min(w_canvas - new_w, cx - new_w // 2))
            nsy = max(0, min(h_canvas - new_h, cy - new_h // 2))
            
            roi = frame[nsy:nsy+new_h, nsx:nsx+new_w]
            if roi.shape[0] == new_h and roi.shape[1] == new_w:
                roi[:] = (roi.astype(np.float32) * (1.0 - scaled_alpha) + scaled_bgr.astype(np.float32) * scaled_alpha).astype(np.uint8)
            return frame
        else:
            return _blend_static(frame, text_bgr, alpha, sx, sy, sw, sh)

    # 2. Cyber Field (Cyberpunk Chromatic Aberration + Scanlines + Flicker)
    elif "cyber" in anim_key or "matrix" in anim_key:
        cur_bgr = text_bgr.copy()
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]

        if progress < 1.0:
            shift = max(1, int(4 * (1.0 - progress)))
            # R/B Channel shift (Chromatic Aberration)
            b_chan = np.roll(cur_bgr[:, :, 0], -shift, axis=1)
            r_chan = np.roll(cur_bgr[:, :, 2], shift, axis=1)
            cur_bgr[:, :, 0] = b_chan
            cur_bgr[:, :, 2] = r_chan
            
            # Scanline modulation
            scanlines = np.ones((sh, sw, 1), dtype=np.float32)
            scanlines[::3, :, :] = 0.55
            cur_alpha = cur_alpha * scanlines
            
            # High-speed cyber flicker
            flicker = 0.85 + 0.15 * math.sin(cur_sec * 60.0)
            cur_alpha = np.clip(cur_alpha * flicker, 0.0, 1.0)
        
        # Add subtle neon cyan fringe
        cyan_glow = np.array([255, 230, 0], dtype=np.float32) # Cyan BGR
        edge_mask = np.clip(cur_alpha * (1.0 - cur_alpha) * 4.0, 0.0, 0.6)
        cur_bgr = np.clip(cur_bgr.astype(np.float32) + cyan_glow * edge_mask, 0, 255).astype(np.uint8)

        roi = frame[sy:sy+sh, sx:sx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + cur_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # 3. Frequency Decode (Hacker Decrypt Wave)
    elif "frequency" in anim_key or "decode" in anim_key or "hacker" in anim_key:
        cur_bgr = text_bgr.copy()
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]

        if progress < 1.0:
            reveal_x = int(sw * progress)
            if reveal_x < sw:
                wf_end = min(sw, reveal_x + 35)
                # Random glitch frequency matrix noise in transition band
                noise = (np.random.rand(sh, wf_end - reveal_x, 1) > 0.40).astype(np.float32)
                cur_alpha[:, reveal_x:wf_end] *= noise
                # Boost bright matrix green
                cur_bgr[:, reveal_x:wf_end, 0] = np.clip(cur_bgr[:, reveal_x:wf_end, 0] * 0.2, 0, 255)
                cur_bgr[:, reveal_x:wf_end, 1] = np.clip(cur_bgr[:, reveal_x:wf_end, 1] * 1.6 + 60, 0, 255)
                # Hide unreached text
                cur_alpha[:, wf_end:] = 0.0

        roi = frame[sy:sy+sh, sx:sx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + cur_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # 4. Wiping In (Smooth Soft Wipe with Shimmer Edge)
    elif "wip" in anim_key:
        cur_bgr = text_bgr.copy()
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]

        if progress < 1.0:
            wipe_x = int((sw + 40) * progress) - 20
            feather = 22.0
            x_coords = np.arange(sw, dtype=np.float32)
            wipe_mask = np.clip((wipe_x - x_coords) / feather + 0.5, 0.0, 1.0)[np.newaxis, :, np.newaxis]
            cur_alpha = cur_alpha * wipe_mask
            
            # Glowing moving shimmer flare at wipe edge
            glow_dist = np.abs(x_coords - wipe_x)
            glow_bar = np.clip(1.0 - (glow_dist / 14.0), 0.0, 1.0)[np.newaxis, :, np.newaxis]
            shimmer = (glow_bar * 210.0).astype(np.float32)
            cur_bgr = np.clip(cur_bgr.astype(np.float32) + shimmer, 0.0, 255.0).astype(np.uint8)
            cur_alpha = np.clip(cur_alpha + glow_bar * 0.7, 0.0, 1.0)

        roi = frame[sy:sy+sh, sx:sx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + cur_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # 5. Magical Duel (Radiant Energy Aura Glow & Flash Burst)
    elif "magic" in anim_key or "duel" in anim_key or "aura" in anim_key:
        cur_bgr = text_bgr.copy()
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]

        # Radiant dilated aura
        aura = cv2.GaussianBlur(cur_alpha, (21, 21), 6)
        if aura.ndim == 2:
            aura = aura[:, :, np.newaxis]
        pulse = 0.65 + 0.35 * math.sin(cur_sec * 8.0)
        aura_color = np.array([255, 210, 40], dtype=np.float32) # Electric Cyan-Gold

        if progress < 0.40:
            flash = 1.0 + 1.2 * (1.0 - progress / 0.40)
            cur_bgr = np.clip(cur_bgr.astype(np.float32) * flash, 0, 255).astype(np.uint8)

        roi = frame[sy:sy+sh, sx:sx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            aura_effective = np.clip(aura * pulse * 0.70, 0.0, 0.85)
            # Blend radiant aura
            roi[:] = (roi.astype(np.float32) * (1.0 - aura_effective) + aura_color * aura_effective).astype(np.uint8)
            # Blend sharp text on top
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + cur_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # 6. Blur Dissolve (Blur to Sharp Focus)
    elif "blur" in anim_key or "dissolve" in anim_key:
        cur_bgr = text_bgr.copy()
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]

        if progress < 1.0:
            k = max(1, int((1.0 - progress) * 17))
            if k % 2 == 0: k += 1
            if k > 1:
                cur_bgr = cv2.GaussianBlur(cur_bgr, (k, k), 0)
                cur_alpha = cv2.GaussianBlur(cur_alpha, (k, k), 0)
                if cur_alpha.ndim == 2:
                    cur_alpha = cur_alpha[:, :, np.newaxis]
                cur_alpha = cur_alpha * progress

        roi = frame[sy:sy+sh, sx:sx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + cur_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # 7. Karaoke Pulse (Travelling Syllable Wave Highlight)
    elif "karaoke" in anim_key or "pulse" in anim_key:
        cur_bgr = text_bgr.copy()
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]

        # Wave pulse highlighting
        wave_pos = (math.sin(cur_sec * 5.0) + 1.0) / 2.0
        wave_x = int(sw * wave_pos)
        x_coords = np.arange(sw, dtype=np.float32)
        dist = np.abs(x_coords - wave_x)
        highlight = np.clip(1.0 - (dist / 36.0), 0.0, 1.0)[np.newaxis, :, np.newaxis]
        pulse_color = np.array([40, 210, 255], dtype=np.float32) # Gold wave
        cur_bgr = np.clip(cur_bgr.astype(np.float32) + highlight * pulse_color, 0, 255).astype(np.uint8)

        roi = frame[sy:sy+sh, sx:sx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + cur_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # 8. Glitch Shake (Impact Vibration Shake)
    elif "shake" in anim_key or "shiver" in anim_key or "glitch" in anim_key:
        cur_alpha = alpha.copy()
        if cur_alpha.ndim == 2:
            cur_alpha = cur_alpha[:, :, np.newaxis]
            
        nsx, nsy = sx, sy
        if progress < 1.0:
            mag = 6.0 * (1.0 - progress)
            dx = int(math.sin(cur_sec * 90.0) * mag)
            dy = int(math.cos(cur_sec * 80.0) * (mag * 0.7))
            nsx = max(0, min(w_canvas - sw, sx + dx))
            nsy = max(0, min(h_canvas - sh, sy + dy))

        roi = frame[nsy:nsy+sh, nsx:nsx+sw]
        if roi.shape[0] == sh and roi.shape[1] == sw:
            roi[:] = (roi.astype(np.float32) * (1.0 - cur_alpha) + text_bgr.astype(np.float32) * cur_alpha).astype(np.uint8)
        return frame

    # Default fallback: Static
    return _blend_static(frame, text_bgr, alpha, sx, sy, sw, sh)


def _blend_static(frame: np.ndarray, text_bgr: np.ndarray, alpha: np.ndarray, sx: int, sy: int, sw: int, sh: int) -> np.ndarray:
    """Direct high-speed alpha compositing with safe boundary clipping."""
    h_canvas, w_canvas = frame.shape[:2]
    x1 = max(0, sx)
    y1 = max(0, sy)
    x2 = min(w_canvas, sx + sw)
    y2 = min(h_canvas, sy + sh)
    if x1 >= x2 or y1 >= y2:
        return frame

    tx1 = x1 - sx
    ty1 = y1 - sy
    tx2 = tx1 + (x2 - x1)
    ty2 = ty1 + (y2 - y1)

    if alpha.ndim == 2:
        alpha = alpha[:, :, np.newaxis]

    t_bgr_sub = text_bgr[ty1:ty2, tx1:tx2]
    alpha_sub = alpha[ty1:ty2, tx1:tx2]
    roi = frame[y1:y2, x1:x2]

    if roi.shape[:2] == t_bgr_sub.shape[:2] and roi.shape[:2] == alpha_sub.shape[:2]:
        alpha_u16 = (alpha_sub * 256.0).astype(np.uint16)
        inv_alpha_u16 = (256 - alpha_u16).astype(np.uint16)
        premul_u16 = ((t_bgr_sub.astype(np.uint16) * alpha_u16) >> 8)
        roi[:] = (((roi.astype(np.uint16) * inv_alpha_u16) >> 8) + premul_u16).astype(np.uint8)
    return frame
