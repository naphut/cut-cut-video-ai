"""
Core Mask Engine for Video AI Processing.
Supports 6 advanced Mask Modes:
  ├── Blur (Gaussian Blur with smooth radius)
  ├── Pixelate (Mosaic block downsampling)
  ├── Solid Cover (Color box fill with opacity)
  ├── Gradient Cover (Soft feathered edge/fade cover)
  ├── Inpaint (OpenCV Telea texture reconstruction to erase logos/text)
  └── Smart Fill (Context-aware boundary diffusion & seamless color matching)

Also supports 3 Timing Modes:
  ├── Full Video (Always on - for old logos / watermarks)
  ├── Auto-Speech (Active only during speech dialog - for old subtitles)
  └── Custom Time Range (Active between start_sec and end_sec - for old titles)
"""

import math
import cv2
import numpy as np


MASK_TYPES = [
    ("blur", "🌫️ Blur (Gaussian)"),
    ("pixelate", "🧱 Pixelate (Mosaic)"),
    ("solid", "⬛ Solid Cover"),
    ("gradient", "🌅 Gradient Cover"),
    ("inpaint", "🪄 Inpaint (Erase Logo)"),
    ("smart_fill", "🎨 Smart Fill (Blend)")
]


def check_mask_active(b_item: dict, cur_sec: float, is_speech_time: bool, is_playing: bool = True, is_active_target: bool = False) -> bool:
    """
    Determines if a mask item should be visible/applied at the current timestamp.
    """
    # If currently being edited or paused in preview, always show for positioning
    if not is_playing or is_active_target:
        return True

    mode = b_item.get("mode")
    if not mode:
        # Backward compatibility with legacy flags
        if b_item.get("auto_speech", False):
            mode = "speech"
        elif not b_item.get("full_video", True):
            mode = "range"
        else:
            mode = "full"

    if mode == "speech":
        return bool(is_speech_time)
    elif mode == "range":
        st = float(b_item.get("start_sec", 0.0))
        et = float(b_item.get("end_sec", 999999.0))
        return (st <= cur_sec <= et)
    else:  # "full"
        return True


def apply_mask_to_roi(roi: np.ndarray, full_frame: np.ndarray,
                       x1: int, y1: int, x2: int, y2: int,
                       mask_type: str, intensity: int = 35,
                       color_bgr: tuple = (0, 0, 0)) -> np.ndarray:
    """
    Processes an axis-aligned ROI using one of the 6 mask modes.
    """
    if roi.size == 0:
        return roi

    h, w = roi.shape[:2]
    m_type = str(mask_type).lower().strip()
    # Normalize aliases
    if "mosaic" in m_type or "pixel" in m_type:
        m_type = "pixelate"
    elif "solid" in m_type:
        m_type = "solid"
    elif "gradient" in m_type:
        m_type = "gradient"
    elif "inpaint" in m_type:
        m_type = "inpaint"
    elif "smart" in m_type or "fill" in m_type:
        m_type = "smart_fill"
    else:
        m_type = "blur"

    # 1. BLUR (Gaussian Blur)
    if m_type == "blur":
        max_k = min(w, h)
        if max_k % 2 == 0:
            max_k -= 1
        ksize = max(3, int((intensity / 100.0) * 45.0) | 1)
        ksize = min(ksize, max(3, max_k))
        if ksize >= 3:
            if h > 120 and w > 120:
                down = cv2.resize(roi, (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_LINEAR)
                down_k = max(3, (ksize // 2) | 1)
                down_b = cv2.GaussianBlur(down, (down_k, down_k), 0)
                return cv2.resize(down_b, (w, h), interpolation=cv2.INTER_LINEAR)
            return cv2.GaussianBlur(roi, (ksize, ksize), 0)
        return roi

    # 2. PIXELATE (Mosaic)
    elif m_type == "pixelate":
        block = max(3, int(max(4, intensity / 3.0)))
        mw = max(1, w // block)
        mh = max(1, h // block)
        small = cv2.resize(roi, (mw, mh), interpolation=cv2.INTER_NEAREST)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

    # 3. SOLID COVER
    elif m_type == "solid":
        solid_box = np.full_like(roi, color_bgr, dtype=np.uint8)
        alpha = min(1.0, max(0.1, intensity / 100.0))
        if alpha >= 0.98:
            return solid_box
        return cv2.addWeighted(solid_box, alpha, roi, 1.0 - alpha, 0)

    # 4. GRADIENT COVER
    elif m_type == "gradient":
        ramp = np.linspace(0.0, 1.0, h, dtype=np.float32)
        # Soft bell curve: 0 at top -> 1 at center/bottom -> 0 at bottom
        grad_1d = np.maximum(0.0, np.sin(np.pi * ramp)) ** 1.3
        grad_2d = np.repeat(grad_1d[:, np.newaxis], w, axis=1)[:, :, np.newaxis]
        alpha = min(1.0, max(0.1, intensity / 100.0))
        grad_2d = grad_2d * alpha

        col_arr = np.array(color_bgr, dtype=np.float32)
        blended = (col_arr * grad_2d + roi.astype(np.float32) * (1.0 - grad_2d))
        return np.clip(blended, 0, 255).astype(np.uint8)

    # 5. INPAINT (Texture diffusion)
    elif m_type == "inpaint":
        margin = max(4, min(12, int(min(w, h) * 0.15)))
        fh, fw = full_frame.shape[:2]
        my1 = max(0, y1 - margin)
        my2 = min(fh, y2 + margin)
        mx1 = max(0, x1 - margin)
        mx2 = min(fw, x2 + margin)

        crop = full_frame[my1:my2, mx1:mx2].copy()
        if crop.size == 0:
            return roi

        mask = np.zeros(crop.shape[:2], dtype=np.uint8)
        iy1 = y1 - my1
        iy2 = iy1 + h
        ix1 = x1 - mx1
        ix2 = ix1 + w
        mask[iy1:iy2, ix1:ix2] = 255

        inpaint_radius = max(3, min(margin, 7))
        inpainted = cv2.inpaint(crop, mask, inpaintRadius=inpaint_radius, flags=cv2.INPAINT_TELEA)
        return inpainted[iy1:iy2, ix1:ix2]

    # 6. SMART FILL (Boundary interpolation & edge diffusion)
    elif m_type == "smart_fill":
        fh, fw = full_frame.shape[:2]
        top_y = max(0, y1 - 1)
        bot_y = min(fh - 1, y2)
        left_x = max(0, x1 - 1)
        right_x = min(fw - 1, x2)

        top_row = full_frame[top_y, x1:x2].astype(np.float32)
        bot_row = full_frame[bot_y, x1:x2].astype(np.float32)
        left_col = full_frame[y1:y2, left_x].astype(np.float32)
        right_col = full_frame[y1:y2, right_x].astype(np.float32)

        if top_row.shape[0] != w:
            top_row = cv2.resize(top_row, (w, 1))[0]
        if bot_row.shape[0] != w:
            bot_row = cv2.resize(bot_row, (w, 1))[0]
        if left_col.shape[0] != h:
            left_col = cv2.resize(left_col, (1, h))[:, 0]
        if right_col.shape[0] != h:
            right_col = cv2.resize(right_col, (1, h))[:, 0]

        u = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
        v = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :, None]

        tb = (1.0 - u) * top_row[None, :, :] + u * bot_row[None, :, :]
        lr = (1.0 - v) * left_col[:, None, :] + v * right_col[:, None, :]
        smart = ((tb + lr) * 0.5)

        # Smooth internal transitions
        smart_u8 = np.clip(smart, 0, 255).astype(np.uint8)
        k_smooth = max(3, min(21, (min(w, h) // 4) | 1))
        smart_smooth = cv2.GaussianBlur(smart_u8, (k_smooth, k_smooth), 0)

        alpha = min(1.0, max(0.3, intensity / 100.0))
        if alpha >= 0.98:
            return smart_smooth
        return cv2.addWeighted(smart_smooth, alpha, roi, 1.0 - alpha, 0)

    return roi


def apply_mask_item_to_frame(frame: np.ndarray, b_item: dict, scale_x: float, scale_y: float) -> np.ndarray:
    """
    Renders a single mask item onto frame taking rotation and scale into account.
    """
    h, w = frame.shape[:2]
    bx = b_item.get("x", 0)
    by = b_item.get("y", 0)
    bw = b_item.get("width", b_item.get("w", 100))
    bh = b_item.get("height", b_item.get("h", 80))
    rot = float(b_item.get("rotation", 0.0))
    intensity = int(b_item.get("intensity", 35))
    b_type = str(b_item.get("type", "blur")).lower().strip()

    # Color parsing
    raw_col = b_item.get("solid_color", (0, 0, 0))
    if isinstance(raw_col, str) and raw_col.startswith("#"):
        hex_c = raw_col.lstrip("#")
        if len(hex_c) == 6:
            r = int(hex_c[0:2], 16)
            g = int(hex_c[2:4], 16)
            b = int(hex_c[4:6], 16)
            color_bgr = (b, g, r)
        else:
            color_bgr = (0, 0, 0)
    elif isinstance(raw_col, (list, tuple)) and len(raw_col) >= 3:
        color_bgr = tuple(raw_col[:3])
    else:
        color_bgr = (0, 0, 0)

    fcx = (bx + bw / 2.0) * scale_x
    fcy = (by + bh / 2.0) * scale_y
    fw = bw * scale_x
    fh = bh * scale_y

    if abs(rot) < 0.5:
        x1 = max(0, int(fcx - fw / 2.0))
        y1 = max(0, int(fcy - fh / 2.0))
        x2 = min(w, int(fcx + fw / 2.0))
        y2 = min(h, int(fcy + fh / 2.0))

        if x1 < x2 and y1 < y2:
            roi = frame[y1:y2, x1:x2]
            if roi.size > 0:
                frame[y1:y2, x1:x2] = apply_mask_to_roi(roi, frame, x1, y1, x2, y2, b_type, intensity, color_bgr)
    else:
        rad = math.radians(rot)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        local_corners = [
            (-fw / 2.0, -fh / 2.0),
            (fw / 2.0, -fh / 2.0),
            (fw / 2.0, fh / 2.0),
            (-fw / 2.0, fh / 2.0)
        ]
        pts = []
        for lx, ly in local_corners:
            px = fcx + (lx * cos_r - ly * sin_r)
            py = fcy + (lx * sin_r + ly * cos_r)
            pts.append([px, py])
        pts = np.array(pts, dtype=np.int32)

        bx1 = max(0, int(np.min(pts[:, 0])))
        by1 = max(0, int(np.min(pts[:, 1])))
        bx2 = min(w, int(np.max(pts[:, 0])))
        by2 = min(h, int(np.max(pts[:, 1])))

        if bx1 < bx2 and by1 < by2:
            sub = frame[by1:by2, bx1:bx2]
            if sub.size > 0:
                poly_mask = np.zeros(sub.shape[:2], dtype=np.uint8)
                rel_pts = pts - np.array([bx1, by1], dtype=np.int32)
                cv2.fillPoly(poly_mask, [rel_pts], 255)

                processed_sub = apply_mask_to_roi(sub.copy(), frame, bx1, by1, bx2, by2, b_type, intensity, color_bgr)
                sub[poly_mask == 255] = processed_sub[poly_mask == 255]
                frame[by1:by2, bx1:bx2] = sub

    return frame
