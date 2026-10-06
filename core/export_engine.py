import os
import sys
import re
import math
import time
import gc
import shutil
import queue
import threading
import subprocess
from typing import Optional, Callable, Dict, Any, List, Tuple
from collections import deque

import cv2
import numpy as np

from qt_compat import QApplication, QtGui, QtCore, Qt
from utils.file_utils import get_temp_path
from utils.logger import logger
from services.khmer_frontend import strip_speaker_tags
from core.subtitle_anim import apply_subtitle_animation_effect
from core.mask_engine import apply_mask_item_to_frame, check_mask_active


# ==================== QT KHMER FONT MANAGEMENT ====================

_loaded_qt_fonts = set()

def ensure_qt_fonts():
    """Register all project TrueType fonts into Qt QFontDatabase for native HarfBuzz Khmer Unicode shaping."""
    font_dir = os.path.abspath('fonts')
    if os.path.exists(font_dir):
        for f in os.listdir(font_dir):
            if f.endswith('.ttf') or f.endswith('.otf'):
                p = os.path.join(font_dir, f)
                if p not in _loaded_qt_fonts:
                    QtGui.QFontDatabase.addApplicationFont(p)
                    _loaded_qt_fonts.add(p)

_FONT_FAMILY_MAPPING = {
    "kantumruy": "Kantumruy Pro",
    "battambang": "Battambang",
    "noto": "Noto Sans Khmer",
    "moul": "Moul",
    "koulen": "Koulen",
    "bokor": "Bokor",
    "fasthand": "Fasthand",
    "bayon": "Bayon",
    "dangrek": "Dangrek",
    "siemreap": "Siemreap",
    "chenla": "Chenla",
    "koh santepheap": "Koh Santepheap",
    "santepheap": "Koh Santepheap",
    "preahvihear": "Preahvihear",
    "suwannaphum": "Suwannaphum",
    "odormeanchey": "Odor Mean Chey",
    "odor": "Odor Mean Chey",
    "content": "Content",
    "sangam": "Khmer Sangam MN",
    "mn": "Khmer Sangam MN",
}

def resolve_qt_font_name(requested_name: str) -> str:
    """Map dropdown selection to exact registered Qt font family name."""
    ensure_qt_fonts()
    req = str(requested_name).strip()
    req_lower = req.lower()
    for key, fam in _FONT_FAMILY_MAPPING.items():
        if key in req_lower:
            return fam
    return req if req else "Kantumruy Pro"


# ==================== RESOLUTION COMPUTATION HELPER ====================

def compute_target_resolution(orig_w: int, orig_h: int, target_choice: Optional[str]) -> Tuple[int, int]:
    """
    Computes (width, height) preserving aspect ratio based on user choice:
    - 'original' or 'ទំហំដើម': (orig_w, orig_h)
    - '4k' / '3840×2160': 3840x2160 (or vertical 2160x3840)
    - '2k' / '2560×1440': 2560x1440 (or vertical 1440x2560)
    - '1080p' / '1920×1080': 1920x1080 (or vertical 1080x1920)
    - '720p' / '1280×720': 1280x720 (or vertical 720x1280)
    - '480p' / '854×480': 854x480 (or vertical 480x854)
    """
    choice = str(target_choice or "").strip().lower()
    if not choice or any(k in choice for k in ("original", "ទំហំដើម", "ដើម", "orig", "none")):
        src_ar = round(orig_w / max(1, orig_h), 4)
        logger.info(
            f"🎬 [EXPORT_RESOLUTION] source={orig_w}x{orig_h} source_aspect={src_ar} "
            f"target_preset=Original target={orig_w}x{orig_h}"
        )
        return (orig_w, orig_h)

    force_landscape = "16:9" in choice or "landscape" in choice or "horizontal" in choice
    force_portrait = "9:16" in choice or "portrait" in choice or "vertical" in choice

    if force_landscape:
        is_portrait = False
    elif force_portrait:
        is_portrait = True
    else:
        is_portrait = orig_h > orig_w

    standard_map = {
        "4k": (3840, 2160),
        "2k": (2560, 1440),
        "1080p": (1920, 1080),
        "720p": (1280, 720),
        "480p": (854, 480),
    }

    matched_dim = None
    if "4k" in choice or "3840" in choice:
        matched_dim = standard_map["4k"]
    elif "2k" in choice or "2560" in choice or "1440" in choice:
        matched_dim = standard_map["2k"]
    elif "1080" in choice or "fhd" in choice or "full hd" in choice:
        matched_dim = standard_map["1080p"]
    elif "720" in choice or "hd" in choice:
        matched_dim = standard_map["720p"]
    elif "480" in choice or "sd" in choice or "854" in choice:
        matched_dim = standard_map["480p"]

    if not matched_dim:
        return (orig_w, orig_h)

    base_w, base_h = matched_dim
    if is_portrait:
        base_w, base_h = base_h, base_w

    ar_orig = orig_w / float(max(1, orig_h))
    
    if is_portrait:
        # Standard 9:16 portrait range (0.50 <= ar_orig <= 0.60)
        if 0.50 <= ar_orig <= 0.60:
            target_w, target_h = base_w, base_h
        else:
            # Preserve non-standard portrait aspect ratio (e.g. 4:5, 3:4)
            target_h = base_h
            target_w = int(round(target_h * ar_orig))
    else:
        # Standard 16:9 landscape range (1.70 <= ar_orig <= 1.85)
        if 1.70 <= ar_orig <= 1.85:
            target_w, target_h = base_w, base_h
        else:
            # Preserve non-standard landscape aspect ratio (e.g. 4:3, 21:9)
            target_w = base_w
            target_h = int(round(target_w / ar_orig))

    target_w = (int(target_w) // 2) * 2
    target_h = (int(target_h) // 2) * 2
    res_w = max(64, target_w)
    res_h = max(64, target_h)

    logger.info(
        f"🎬 [EXPORT_RESOLUTION] source={orig_w}x{orig_h} source_aspect={ar_orig:.4f} "
        f"target_preset={target_choice} target={res_w}x{res_h}"
    )
    return res_w, res_h


# ==================== EXPORT PLANNER (DUAL-PATH) ====================

class ExportPlanner:
    """
    Determines optimal export strategy:
    - PATH A (STREAM COPY): All visual effects OFF & Original resolution -> -c:v copy (zero re-encode)
    - PATH B (HARDWARE PIPELINE): Any visual effect ON or resolution changed -> Multi-threaded VideoToolbox pipeline
    """

    @staticmethod
    def is_effects_needed(
        effects_config: Optional[Dict[str, Any]],
        target_resolution: Optional[str] = None,
        orig_w: Optional[int] = None,
        orig_h: Optional[int] = None
    ) -> Tuple[bool, Dict[str, bool]]:
        if not effects_config and not target_resolution:
            return False, {"blur": False, "text": False, "logo": False, "subtitle": False, "resolution": False}

        cfg = effects_config or {}
        blur_cfg = cfg.get("blur", {})
        blur_on = bool(blur_cfg.get("enabled", False) and (bool(blur_cfg.get("rect")) or bool(blur_cfg.get("blurs"))))

        text_cfg = cfg.get("text_overlay", {})
        has_text = bool(text_cfg.get("text", "").strip()) or any(bool(t.get("text", "").strip()) for t in text_cfg.get("items", []))
        text_on = has_text and bool(text_cfg.get("enabled", False))

        logo_cfg = cfg.get("logo", {})
        logo_path = logo_cfg.get("path")
        logo_on = bool(logo_path and os.path.exists(str(logo_path))) and bool(logo_cfg.get("enabled", False))

        sub_cfg = cfg.get("burn_subtitle", {})
        has_segs = bool(cfg.get("segments"))
        sub_on = has_segs and bool(sub_cfg.get("enabled", False))

        res_choice = target_resolution or cfg.get("target_resolution")
        res_needed = False
        if res_choice and orig_w and orig_h:
            tw, th = compute_target_resolution(orig_w, orig_h, res_choice)
            if tw != orig_w or th != orig_h:
                res_needed = True

        needed = blur_on or text_on or logo_on or sub_on or res_needed
        return needed, {"blur": blur_on, "text": text_on, "logo": logo_on, "subtitle": sub_on, "resolution": res_needed}


# ==================== PROGRESS TRACKER ====================

class ProgressTracker:
    """
    Maintains frame progress, elapsed time, rolling average FPS, and stable ETA.
    Prevents single-sample FPS spikes from destabilizing ETA estimation.
    """

    def __init__(self, total_frames: int, window_size: int = 15):
        self.total_frames = max(1, total_frames)
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.last_update_frame = 0
        self.samples = deque(maxlen=window_size)
        self.current_fps = 0.0
        self.average_fps = 0.0

    def update(self, current_frame: int) -> Dict[str, Any]:
        now = time.time()
        dt = now - self.last_update_time
        df = current_frame - self.last_update_frame

        if dt >= 0.5 and df > 0:
            inst_fps = df / dt
            self.samples.append(inst_fps)
            self.current_fps = inst_fps
            self.average_fps = sum(self.samples) / len(self.samples)
            self.last_update_time = now
            self.last_update_frame = current_frame

        elapsed_sec = max(0.1, now - self.start_time)
        overall_fps = current_frame / elapsed_sec
        effective_fps = self.average_fps if self.average_fps > 0 else overall_fps

        pct = min(100, int((current_frame / float(self.total_frames)) * 100))
        remaining_frames = max(0, self.total_frames - current_frame)
        eta_sec = remaining_frames / effective_fps if effective_fps > 1.0 else 0.0

        elapsed_m, elapsed_s = divmod(int(elapsed_sec), 60)
        eta_m, eta_s = divmod(int(eta_sec), 60)

        elapsed_str = f"{elapsed_m:02d}:{elapsed_s:02d}"
        eta_str = f"{eta_m:02d}:{eta_s:02d}" if eta_sec > 0 else "--:--"

        status_text = (
            f"Rendering Video: {current_frame:,} / {self.total_frames:,} ({pct}%) | "
            f"{self.current_fps:.0f} FPS (Avg: {effective_fps:.0f}) | "
            f"Elapsed: {elapsed_str} | ETA: {eta_str}"
        )

        return {
            "progress_pct": pct,
            "current_frame": current_frame,
            "total_frames": self.total_frames,
            "current_fps": self.current_fps,
            "average_fps": effective_fps,
            "elapsed_str": elapsed_str,
            "eta_str": eta_str,
            "status_text": status_text
        }


# ==================== TEXT & SUBTITLE OVERLAY HELPERS ====================

def render_text_overlay_tight_stamp(
    text: str, font_name: str, font_size_pt: int, color_rgb: tuple, scale_y: float = 1.0,
    outline_color_rgb: tuple = (0, 0, 0), outline_width: int = 2,
    shadow_color_rgb: tuple = (0, 0, 0), shadow_offset: int = 3,
    bg_color_rgb: tuple = None, opacity: float = 1.0
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], int, int]:
    """Render text into a tight bounding box mask & BGR stamp (zero full-canvas memory allocation)."""
    scale_factor = max(1.0, float(scale_y))
    font_size_pt = max(4, int(round(font_size_pt * scale_factor)))
    font_family = resolve_qt_font_name(font_name)
    font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
    fm = QtGui.QFontMetrics(font)

    text_w = fm.horizontalAdvance(text)
    text_h = fm.height()
    ascent = fm.ascent()

    pad_x = max(2, int(round(14 * scale_factor)))
    pad_y = max(2, int(round(10 * scale_factor)))
    shd_off = max(0, int(round(float(shadow_offset) * scale_factor)))
    out_w = max(0, int(round(float(outline_width) * scale_factor)))

    stamp_w = text_w + pad_x * 2 + shd_off + out_w * 2
    stamp_h = text_h + pad_y * 2 + shd_off + out_w * 2

    qimg = QtGui.QImage(stamp_w, stamp_h, QtGui.QImage.Format_ARGB32_Premultiplied)
    qimg.fill(Qt.transparent)

    painter = QtGui.QPainter(qimg)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
    painter.setFont(font)

    alpha_mult = max(0.05, min(1.0, float(opacity)))

    origin_x = pad_x + out_w
    origin_y = pad_y + out_w + ascent

    if bg_color_rgb is not None and bg_color_rgb != "transparent":
        bg_alpha = int(180 * alpha_mult)
        bg_c = bg_color_rgb if (isinstance(bg_color_rgb, (tuple, list)) and len(bg_color_rgb) == 3) else (0, 0, 0)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(bg_c[0], bg_c[1], bg_c[2], bg_alpha)))
        painter.setPen(Qt.NoPen)
        radius = max(4, int(round(10 * scale_factor)))
        painter.drawRoundedRect(QtCore.QRect(0, 0, stamp_w, stamp_h), radius, radius)

    path = QtGui.QPainterPath()
    path.addText(origin_x, origin_y, font, text)

    if shd_off > 0:
        shd_path = path.translated(shd_off, shd_off)
        shd_c = shadow_color_rgb if (shadow_color_rgb and len(shadow_color_rgb) == 3) else (0, 0, 0)
        painter.fillPath(shd_path, QtGui.QColor(shd_c[0], shd_c[1], shd_c[2], int(160 * alpha_mult)))

    if out_w > 0:
        out_c = outline_color_rgb if (outline_color_rgb and len(outline_color_rgb) == 3) else (0, 0, 0)
        painter.strokePath(path, QtGui.QPen(QtGui.QColor(out_c[0], out_c[1], out_c[2], int(255 * alpha_mult)), out_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    painter.fillPath(path, QtGui.QColor(color_rgb[0], color_rgb[1], color_rgb[2], int(255 * alpha_mult)))
    painter.end()

    qimg_rgba = qimg.convertToFormat(QtGui.QImage.Format_RGBA8888)
    bpl = qimg_rgba.bytesPerLine()
    ptr = qimg_rgba.bits()
    if hasattr(ptr, 'setsize'): ptr.setsize(stamp_h * bpl)
    arr_raw = np.frombuffer(ptr, np.uint8).reshape((stamp_h, bpl))
    arr = arr_raw[:, :stamp_w * 4].reshape((stamp_h, stamp_w, 4))
    text_bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    mask = (arr[:, :, 3] > 10).astype(np.uint8) * 255
    return mask, text_bgr, origin_x, origin_y


def render_subtitle_overlay_tight(
    sub_text: str, font_name: str, font_size_pt: int, color_rgb: tuple, bg_opacity: float,
    width: int, height: int, scale_y: float, x_ratio: float = 0.50, y_ratio: float = 0.85,
    template: Optional[dict] = None
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], int, int, int, int]:
    """Pre-render a subtitle segment overlay into a tight (mask, bgr, x1, y1, w, h) stamp for ultra-fast frame caching."""
    # Scale font size from preview UI points into full export resolution using scale_y
    eff_scale = max(1.0, float(scale_y))
    font_size_pt = max(16, min(140, int(round(font_size_pt * eff_scale))))
    font_family = resolve_qt_font_name(font_name)
    font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
    fm = QtGui.QFontMetrics(font)

    max_line_w = max(100, int(width * 0.85))
    from services.khmer_frontend import wrap_khmer_subtitle_lines
    lines = wrap_khmer_subtitle_lines(sub_text, fm, max_line_w)
    if not lines:
        lines = [sub_text]

    line_height = fm.height() + 6
    total_h = len(lines) * line_height
    line_widths = [fm.horizontalAdvance(l) for l in lines]
    max_w = max(line_widths) if line_widths else 100

    tpl = template or {}
    stroke_color_hex = tpl.get("stroke_color", "#000000")
    raw_stroke = tpl.get("stroke_width", max(2, font_size_pt // 8))
    stroke_w = max(2, int(round(float(raw_stroke) * eff_scale)))
    has_3d = bool(tpl.get("has_3d", False))
    shadow_color_hex = tpl.get("shadow_color", "#000000")
    glow_color_hex = tpl.get("glow_color", None)

    margin = max(16, stroke_w + 14)
    sub_box_w = max(20, min(width - 20, max_w + margin * 2))
    sub_box_h = max(20, min(height - 20, total_h + margin * 2))

    center_x = int(width * x_ratio)
    center_y = int(height * y_ratio)

    bg_x1 = max(10, min(width - sub_box_w - 10, center_x - sub_box_w // 2))
    bg_y1 = max(10, min(height - sub_box_h - 10, center_y - sub_box_h // 2))

    qimg = QtGui.QImage(sub_box_w, sub_box_h, QtGui.QImage.Format_ARGB32_Premultiplied)
    qimg.fill(Qt.transparent)

    painter = QtGui.QPainter(qimg)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
    painter.setFont(font)

    # Only draw background box if bg_opacity > 0.05
    if bg_opacity > 0.05:
        bg_alpha = int(bg_opacity * 255)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0, bg_alpha)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QtCore.QRect(0, 0, sub_box_w, sub_box_h), 10, 10)

    curr_y = margin + fm.ascent()
    paths = []
    for i, l in enumerate(lines):
        lx = max(margin, (sub_box_w - line_widths[i]) // 2)
        p = QtGui.QPainterPath()
        p.addText(lx, curr_y, font, l)
        paths.append(p)
        curr_y += line_height

    # Layer 1: Neon Glow
    if glow_color_hex:
        gc = QtGui.QColor(glow_color_hex)
        for gw_add, ga in [(12, 30), (8, 65), (4, 120)]:
            g_pen = QtGui.QPen(QtGui.QColor(gc.red(), gc.green(), gc.blue(), ga), stroke_w + gw_add, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(g_pen)
            painter.setBrush(Qt.NoBrush)
            for p in paths:
                painter.drawPath(p)

    # Layer 2: 3D Extrusion Shadow
    if has_3d and shadow_color_hex:
        sc = QtGui.QColor(shadow_color_hex)
        for off in (4, 3, 2, 1):
            for p in paths:
                shd_p = p.translated(off, off)
                painter.fillPath(shd_p, sc)
                painter.strokePath(shd_p, QtGui.QPen(sc, stroke_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    elif shadow_color_hex and bg_opacity <= 0.05:
        sc = QtGui.QColor(shadow_color_hex)
        for p in paths:
            shd_p = p.translated(3, 3)
            painter.fillPath(shd_p, QtGui.QColor(sc.red(), sc.green(), sc.blue(), 180))

    # Layer 3: Outline Stroke
    st_color = QtGui.QColor(stroke_color_hex)
    painter.setPen(QtGui.QPen(st_color, stroke_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    for p in paths:
        painter.drawPath(p)

    # Layer 4: Fill Text
    fill_color = QtGui.QColor(color_rgb[0], color_rgb[1], color_rgb[2])
    painter.setBrush(QtGui.QBrush(fill_color))
    painter.setPen(Qt.NoPen)
    for p in paths:
        painter.drawPath(p)

    painter.end()

    qimg_rgba = qimg.convertToFormat(QtGui.QImage.Format_RGBA8888)
    bpl = qimg_rgba.bytesPerLine()
    ptr = qimg_rgba.bits()
    if hasattr(ptr, 'setsize'): ptr.setsize(sub_box_h * bpl)
    arr_raw = np.frombuffer(ptr, np.uint8).reshape((sub_box_h, bpl))
    arr = arr_raw[:, :sub_box_w * 4].reshape((sub_box_h, sub_box_w, 4))
    text_bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    alpha = (arr[:, :, 3].astype(np.float32) / 255.0)[:, :, np.newaxis]
    return text_bgr, alpha, bg_x1, bg_y1, sub_box_w, sub_box_h


def get_dynamic_text_pos(cur_sec: float, zone: str, speed: str, canvas_w: int, canvas_h: int, text_w: int, text_h: int, scale_y: float = 1.0) -> Tuple[int, int]:
    speed_key = str(speed).lower()
    if 'fast' in speed_key or 'លឿន' in speed_key:
        cycle_base = 5.0
    elif 'slow' in speed_key or 'យឺត' in speed_key:
        cycle_base = 16.0
    else:
        cycle_base = 10.0

    z = str(zone).lower()
    if z in ['up_down', 'vertical', 'bounce', 'run_up_down']:
        min_y = max(15, int(canvas_h * 0.08))
        max_y = max(min_y + 20, int(canvas_h * 0.88 - text_h))
        cycle_y = cycle_base

        center_x = int((canvas_w - text_w) / 2.0)
        amp_x = int(canvas_w * 0.20)
        min_x = max(15, center_x - amp_x)
        max_x = min(canvas_w - text_w - 15, center_x + amp_x)
        cycle_x = cycle_base * 1.618
    elif z in ['top']:
        base_y = int(canvas_h * 0.12)
        amp_y = int(canvas_h * 0.03)
        min_y, max_y = base_y - amp_y, base_y + amp_y
        cycle_y = cycle_base * 0.73
        min_x = max(14, int(canvas_w * 0.03))
        max_x = max(min_x + 20, int(canvas_w * 0.97 - text_w))
        cycle_x = cycle_base
    elif z in ['bot', 'bottom']:
        base_y = int(canvas_h * 0.85 - text_h)
        amp_y = int(canvas_h * 0.03)
        min_y, max_y = base_y - amp_y, base_y + amp_y
        cycle_y = cycle_base * 0.73
        min_x = max(14, int(canvas_w * 0.03))
        max_x = max(min_x + 20, int(canvas_w * 0.97 - text_w))
        cycle_x = cycle_base
    else:
        min_y = max(20, int(canvas_h * 0.08))
        max_y = max(min_y + 20, int(canvas_h * 0.92 - text_h))
        cycle_y = cycle_base * 1.41421356
        min_x = max(14, int(canvas_w * 0.03))
        max_x = max(min_x + 20, int(canvas_w * 0.97 - text_w))
        cycle_x = cycle_base

    tx = (cur_sec / cycle_x) % 2.0
    fx = tx if tx <= 1.0 else 2.0 - tx
    smooth_x = 0.5 - 0.5 * math.cos(fx * math.pi)
    x = int(min_x + smooth_x * (max_x - min_x))

    ty = (cur_sec / cycle_y) % 2.0
    fy = ty if ty <= 1.0 else 2.0 - ty
    smooth_y = 0.5 - 0.5 * math.cos(fy * math.pi)
    y = int(min_y + smooth_y * (max_y - min_y))
    return x, y


# ==================== EFFECTS PROCESSOR ====================

class EffectsProcessor:
    """
    Manages pre-calculated assets and applies active effects per frame:
    - Blur & Mosaic (ROI cropped & feathered)
    - Text Overlays & Dynamic Watermarks (Tight stamp ROI)
    - Logo Overlay (Pre-scaled alpha blend ROI)
    - Burn Subtitles (HarfBuzz Khmer Shaped tight stamps with O(1) rolling pointer)
    """

    def __init__(self, effects_config: dict, width: int, height: int, fps: float, orig_w: Optional[int] = None, orig_h: Optional[int] = None):
        self.config = effects_config or {}
        self.width = width
        self.height = height
        self.fps = max(1.0, fps)

        preview_w, preview_h = self.config.get("preview_size", (280, 260))
        preview_w = max(1, int(preview_w))
        preview_h = max(1, int(preview_h))
        self.preview_w = preview_w
        self.preview_h = preview_h

        # Content rectangle of the video within the export canvas (width, height)
        # CRITICAL: video_ar must come from real source media, NEVER preview widget dimensions!
        src_w = orig_w or self.config.get("orig_w") or self.config.get("source_w") or width
        src_h = orig_h or self.config.get("orig_h") or self.config.get("source_h") or height

        video_ar = src_w / float(max(1, src_h))
        canvas_ar = width / float(max(1, height))

        if abs(video_ar - canvas_ar) < 0.04:
            self.video_rect_x = 0
            self.video_rect_y = 0
            self.video_rect_w = width
            self.video_rect_h = height
        elif video_ar < canvas_ar:
            # Narrower than canvas -> Pillarboxed (black bars left and right)
            self.video_rect_h = height
            self.video_rect_w = int(round(height * video_ar))
            self.video_rect_x = (width - self.video_rect_w) // 2
            self.video_rect_y = 0
        else:
            # Wider than canvas -> Letterboxed (black bars top and bottom)
            self.video_rect_w = width
            self.video_rect_h = int(round(width / video_ar))
            self.video_rect_x = 0
            self.video_rect_y = (height - self.video_rect_h) // 2

        self.scale_x = self.video_rect_w / float(preview_w)
        self.scale_y = self.video_rect_h / float(preview_h)

        self.blur_config = self.config.get("blur", {})
        self.text_config = self.config.get("text_overlay", {})
        self.logo_config = self.config.get("logo", {})
        self.burn_sub_config = self.config.get("burn_subtitle", {})

        raw_segments = self.config.get("segments", [])
        if raw_segments:
            from core.subtitle_timing import resolve_subtitle_timings
            total_dur_hint = float(self.config.get("total_duration_sec", 999999.0))
            self.sorted_segs = resolve_subtitle_timings(raw_segments, total_duration_sec=total_dur_hint)
        else:
            self.sorted_segs = []
        self.active_seg_idx = 0

        self.subtitle_cache: Dict[int, Tuple[Any, ...]] = {}
        self.logo_blend_data: Optional[Tuple[int, int, int, int, np.ndarray, np.ndarray]] = None
        self.text_render_items: List[Dict[str, Any]] = []

        self._init_logo()
        self._init_text_overlays()

    def _init_logo(self):
        logo_path = self.logo_config.get("path")
        if not (logo_path and os.path.exists(str(logo_path)) and bool(self.logo_config.get("enabled", False))):
            return

        target_w = max(10, int(self.logo_config.get("width", 100) * self.scale_x))
        target_h = max(10, int(self.logo_config.get("height", 100) * self.scale_y))

        norm_x = float(self.logo_config.get("x", 233)) / float(self.preview_w)
        norm_y = float(self.logo_config.get("y", 6)) / float(self.preview_h)

        logo_x = self.video_rect_x + int(norm_x * self.video_rect_w)
        logo_y = self.video_rect_y + int(norm_y * self.video_rect_h)

        v_min_x = self.video_rect_x
        v_max_x = self.video_rect_x + self.video_rect_w
        v_min_y = self.video_rect_y
        v_max_y = self.video_rect_y + self.video_rect_h

        x1 = max(v_min_x, logo_x)
        y1 = max(v_min_y, logo_y)
        x2 = min(v_max_x, logo_x + target_w)
        y2 = min(v_max_y, logo_y + target_h)

        if x1 >= x2 or y1 >= y2:
            return

        try:
            with open(str(logo_path), 'rb') as f:
                bytes_data = np.frombuffer(f.read(), np.uint8)
                img = cv2.imdecode(bytes_data, cv2.IMREAD_UNCHANGED)
            if img is None:
                return

            orig_h, orig_w = img.shape[:2]
            if target_w < orig_w and target_h < orig_h:
                logo_resized = cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_AREA)
                blurred = cv2.GaussianBlur(logo_resized, (0, 0), 1.0)
                logo_resized = cv2.addWeighted(logo_resized, 1.25, blurred, -0.25, 0)
            else:
                logo_resized = cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

            crop_x1 = x1 - logo_x
            crop_y1 = y1 - logo_y
            crop_x2 = crop_x1 + (x2 - x1)
            crop_y2 = crop_y1 + (y2 - y1)
            sub_logo = logo_resized[crop_y1:crop_y2, crop_x1:crop_x2]

            remove_green = self.logo_config.get("remove_green", False)
            if remove_green:
                hsv = cv2.cvtColor(sub_logo if sub_logo.shape[2] == 3 else sub_logo[:, :, :3], cv2.COLOR_BGR2HSV)
                lower_green = np.array([35, 40, 40])
                upper_green = np.array([85, 255, 255])
                mask = cv2.inRange(hsv, lower_green, upper_green)
                if sub_logo.shape[2] == 3:
                    sub_logo = cv2.cvtColor(sub_logo, cv2.COLOR_BGR2BGRA)
                soft_mask = cv2.GaussianBlur(mask, (3, 3), 0)
                sub_logo[:, :, 3] = np.where(soft_mask > 200, 0, np.where(soft_mask > 50, 255 - soft_mask, sub_logo[:, :, 3]))
            elif sub_logo.shape[2] == 3:
                sub_logo = cv2.cvtColor(sub_logo, cv2.COLOR_BGR2BGRA)

            if sub_logo.shape[2] == 4:
                alpha_f = (sub_logo[:, :, 3:4].astype(np.float32) / 255.0)
                logo_rgb_f = sub_logo[:, :, :3].astype(np.float32)
            else:
                alpha_f = np.ones((y2 - y1, x2 - x1, 1), dtype=np.float32)
                logo_rgb_f = sub_logo[:, :, :3].astype(np.float32)

            l_alpha_u16 = (alpha_f * 256.0).astype(np.uint16)
            inv_alpha_u16 = (256 - l_alpha_u16).astype(np.uint16)
            l_premul_u16 = (logo_rgb_f * alpha_f).astype(np.uint16)
            self.logo_blend_data = (x1, y1, x2 - x1, y2 - y1, inv_alpha_u16, l_premul_u16)
            logger.info(f"✅ [EffectsProcessor] Pre-cached Logo for export: {os.path.basename(str(logo_path))} at ({x1}, {y1}) [{x2-x1}x{y2-y1}]")
        except Exception as e:
            logger.error(f"Error pre-loading logo for export: {e}")

    def _init_text_overlays(self):
        if not bool(self.text_config.get("enabled", False)):
            return

        raw_items = self.text_config.get("items", [])
        if not raw_items and bool(self.text_config.get("text")):
            anim_cfg = self.text_config.get("animation", {})
            if not isinstance(anim_cfg, dict):
                anim_cfg = {"type": str(anim_cfg)} if anim_cfg else {}

            raw_items = [{
                "id": "text_1",
                "text": self.text_config.get("text", ""),
                "font_name": self.text_config.get("font_name", "Kantumruy Pro"),
                "size_pt": self.text_config.get("size_pt", 12),
                "color_rgb": self.text_config.get("color_rgb", (255, 255, 255)),
                "bg_color_rgb": self.text_config.get("bg_color_rgb", (0, 0, 0)),
                "outline_color_rgb": self.text_config.get("outline_color_rgb", (0, 0, 0)),
                "outline_width": self.text_config.get("outline_width", 2),
                "shadow_color_rgb": self.text_config.get("shadow_color_rgb", (0, 0, 0)),
                "shadow_offset": self.text_config.get("shadow_offset", 3),
                "position": self.text_config.get("position", (50, 80)),
                "anim_type": anim_cfg.get("type", "none"),
                "anim_speed": anim_cfg.get("speed", 0.5),
                "anim_repeat_sec": anim_cfg.get("repeat_sec", 2.0),
                "anim_mode": anim_cfg.get("mode", "always"),
                "start_sec": anim_cfg.get("start_sec", 0.0),
                "duration_sec": anim_cfg.get("duration_sec", 0.0),
            }]

        for it in raw_items:
            txt = it.get("text", "").strip()
            if not txt:
                continue

            font_name = it.get("font_name") or it.get("font", "Kantumruy Pro")
            size_pt = int(it.get("size") or it.get("size_pt") or self.text_config.get("size_pt") or self.text_config.get("size") or 12)
            color_rgb = it.get("color_rgb", (255, 255, 255))
            bg_color_rgb = it.get("bg_color_rgb", None)
            if bg_color_rgb is None and it.get("bg_color") and it.get("bg_color") != "transparent":
                hex_c = str(it.get("bg_color")).lstrip('#')
                if len(hex_c) == 6:
                    bg_color_rgb = (int(hex_c[0:2], 16), int(hex_c[2:4], 16), int(hex_c[4:6], 16))

            outline_color_rgb = it.get("outline_color_rgb", (0, 0, 0))
            outline_width = int(it.get("outline_width", 2))
            shadow_color_rgb = it.get("shadow_color_rgb", (0, 0, 0))
            shadow_offset = int(it.get("shadow_offset", 3))
            pos = it.get("position", (int(it.get("x", 50)), int(it.get("y", 80))))
            anim_type = it.get("anim_type", "none")
            anim_speed = max(0.1, float(it.get("anim_speed", 0.5)))
            anim_repeat_sec = max(0.0, float(it.get("anim_repeat_sec", 2.0)))
            anim_mode = it.get("anim_mode", "always")
            start_sec = float(it.get("start_sec") if "start_sec" in it else it.get("start", 0.0))
            duration_sec = float(it.get("duration_sec") if "duration_sec" in it else it.get("duration", 0.0))
            full_video = bool(it.get("full_video", anim_mode == "always" or duration_sec <= 0.0))
            opacity = float(it.get("opacity", 1.0))
            if it.get("is_watermark") and "opacity" not in it:
                opacity = 0.6
            pos_zone = str(it.get("pos_zone", "mid"))
            speed_str = str(it.get("speed_str", "medium"))

            stamp_mask, stamp_bgr = None, None
            pos_clamped = (0, 0)
            try:
                stamp_mask, stamp_bgr, origin_x, origin_y = render_text_overlay_tight_stamp(
                    text=txt,
                    font_name=font_name,
                    font_size_pt=size_pt,
                    color_rgb=color_rgb,
                    scale_y=self.scale_y,
                    outline_color_rgb=outline_color_rgb,
                    outline_width=outline_width,
                    shadow_color_rgb=shadow_color_rgb,
                    shadow_offset=shadow_offset,
                    bg_color_rgb=bg_color_rgb,
                    opacity=opacity
                )
                if stamp_mask is not None:
                    sh, sw = stamp_mask.shape[:2]
                    if isinstance(pos, str):
                        p_low = pos.lower()
                        if "top" in p_low:
                            tx = max(self.video_rect_x, self.video_rect_x + (self.video_rect_w - sw) // 2)
                            ty = max(self.video_rect_y, self.video_rect_y + int(self.video_rect_h * 0.08))
                        elif "bottom" in p_low:
                            tx = max(self.video_rect_x, self.video_rect_x + (self.video_rect_w - sw) // 2)
                            ty = max(self.video_rect_y, self.video_rect_y + int(self.video_rect_h * 0.85) - sh)
                        else:
                            tx = max(self.video_rect_x, self.video_rect_x + (self.video_rect_w - sw) // 2)
                            ty = max(self.video_rect_y, self.video_rect_y + (self.video_rect_h - sh) // 2)
                    elif isinstance(pos, (tuple, list)) and len(pos) >= 2:
                        norm_tx = float(pos[0]) / float(self.preview_w)
                        norm_ty = float(pos[1]) / float(self.preview_h)
                        base_x = self.video_rect_x + int(norm_tx * self.video_rect_w)
                        base_y = self.video_rect_y + int(norm_ty * self.video_rect_h)
                        tx = max(self.video_rect_x, min(self.video_rect_x + self.video_rect_w - sw, base_x - origin_x))
                        ty = max(self.video_rect_y, min(self.video_rect_y + self.video_rect_h - sh, base_y - origin_y))
                    else:
                        tx = max(self.video_rect_x, self.video_rect_x + (self.video_rect_w - sw) // 2)
                        ty = max(self.video_rect_y, self.video_rect_y + int(self.video_rect_h * 0.08))
                    pos_clamped = (tx, ty)
            except Exception as e:
                logger.error(f"Error pre-rendering tight text stamp '{txt}': {e}")

            self.text_render_items.append({
                "text": txt,
                "anim_type": anim_type,
                "start_sec": start_sec,
                "duration_sec": duration_sec,
                "full_video": full_video,
                "pos_zone": pos_zone,
                "speed_str": speed_str,
                "stamp_mask": stamp_mask,
                "stamp_bgr": stamp_bgr,
                "pos_clamped": pos_clamped
            })

    def process_frame(self, frame: np.ndarray, cur_sec: float) -> np.ndarray:
        """Applies all active effects (Blur, Text, Logo, Subtitles) to the given frame in-place."""
        # 1. Update O(1) rolling pointer for active subtitle segment
        while self.active_seg_idx < len(self.sorted_segs) and self.sorted_segs[self.active_seg_idx].get("end", 0.0) < cur_sec:
            self.active_seg_idx += 1

        active_seg = None
        if self.active_seg_idx < len(self.sorted_segs):
            cand = self.sorted_segs[self.active_seg_idx]
            if cand.get("start", 0.0) <= cur_sec <= cand.get("end", 0.0):
                active_seg = cand

        is_speech_time = (active_seg is not None)

        # 2. Multi-Mask & Cover (Blur, Pixelate, Solid Cover, Gradient Cover, Inpaint, Smart Fill)
        if self.blur_config.get("enabled", False):
            blurs = self.blur_config.get("blurs", [])
            if not blurs and self.blur_config.get("rect"):
                r = self.blur_config["rect"]
                if hasattr(r, 'x'):
                    rx, ry, rw, rh = r.x(), r.y(), r.width(), r.height()
                elif isinstance(r, (list, tuple)) and len(r) >= 4:
                    rx, ry, rw, rh = r[0], r[1], r[2], r[3]
                elif isinstance(r, dict):
                    rx, ry, rw, rh = r.get("x", 0), r.get("y", 0), r.get("width", 50), r.get("height", 50)
                else:
                    rx, ry, rw, rh = 0, 0, 50, 50
                blurs = [{
                    "x": rx, "y": ry, "width": rw, "height": rh,
                    "rotation": 0.0, "intensity": self.blur_config.get("intensity", 35),
                    "type": "blur", "start_sec": 0.0, "end_sec": 999999.0, "mode": "full"
                }]

            for b_item in blurs:
                if not check_mask_active(b_item, cur_sec, is_speech_time, is_playing=True):
                    continue
                try:
                    bx = b_item.get("x", 0)
                    by = b_item.get("y", 0)
                    bw = b_item.get("width", b_item.get("w", 100))
                    bh = b_item.get("height", b_item.get("h", 80))

                    norm_bx = bx / float(self.preview_w)
                    norm_by = by / float(self.preview_h)
                    norm_bw = bw / float(self.preview_w)
                    norm_bh = bh / float(self.preview_h)

                    mapped_b = dict(b_item)
                    mapped_b["x"] = self.video_rect_x + int(norm_bx * self.video_rect_w)
                    mapped_b["y"] = self.video_rect_y + int(norm_by * self.video_rect_h)
                    mapped_b["width"] = max(10, int(norm_bw * self.video_rect_w))
                    mapped_b["height"] = max(10, int(norm_bh * self.video_rect_h))

                    frame = apply_mask_item_to_frame(frame, mapped_b, 1.0, 1.0)
                except Exception as e_m:
                    logger.error(f"Error applying mask item: {e_m}")

        # 3. Logo Overlay (High-Speed Integer Vectorized Blending on ROI)
        if self.logo_blend_data is not None:
            logo_full = bool(self.logo_config.get("full_video", True))
            logo_st = float(self.logo_config.get("start_sec", 0.0))
            logo_dur = float(self.logo_config.get("duration_sec", 0.0))
            apply_logo = True if logo_full or logo_dur <= 0.0 else (logo_st <= cur_sec <= logo_st + logo_dur)

            if apply_logo:
                lx, ly, lw, lh, inv_alpha_u16, l_premul_u16 = self.logo_blend_data
                roi = frame[ly:ly+lh, lx:lx+lw]
                if roi.shape[0] == lh and roi.shape[1] == lw:
                    roi_u16 = roi.astype(np.uint16)
                    blended = ((roi_u16 * inv_alpha_u16) >> 8) + l_premul_u16
                    frame[ly:ly+lh, lx:lx+lw] = blended.astype(np.uint8)

        # 4. Text Overlays & Dynamic Watermark (Tight ROI Stamps via C++ SIMD)
        for t_item in self.text_render_items:
            t_start = t_item["start_sec"]
            t_dur = t_item["duration_sec"]
            if not t_item["full_video"] and t_dur > 0.0 and (cur_sec < t_start or cur_sec > t_start + t_dur):
                continue

            stamp_mask = t_item.get("stamp_mask")
            stamp_bgr = t_item.get("stamp_bgr")
            if stamp_mask is None or stamp_bgr is None:
                continue

            sh, sw = stamp_mask.shape[:2]
            if t_item["anim_type"] in ["dynamic", "dynamic_watermark", "watermark"]:
                x_dyn, y_dyn = get_dynamic_text_pos(
                    cur_sec=cur_sec, zone=t_item["pos_zone"], speed=t_item["speed_str"],
                    canvas_w=self.video_rect_w, canvas_h=self.video_rect_h, text_w=sw, text_h=sh, scale_y=self.scale_y
                )
                x_dyn = max(self.video_rect_x, min(self.video_rect_x + self.video_rect_w - sw, self.video_rect_x + x_dyn))
                y_dyn = max(self.video_rect_y, min(self.video_rect_y + self.video_rect_h - sh, self.video_rect_y + y_dyn))
                roi = frame[y_dyn:y_dyn+sh, x_dyn:x_dyn+sw]
                if roi.shape[0] == sh and roi.shape[1] == sw:
                    cv2.copyTo(stamp_bgr, stamp_mask, roi)
            else:
                tx, ty = t_item["pos_clamped"]
                roi = frame[ty:ty+sh, tx:tx+sw]
                if roi.shape[0] == sh and roi.shape[1] == sw:
                    cv2.copyTo(stamp_bgr, stamp_mask, roi)

        # 5. Burn Subtitle (HarfBuzz Khmer Shaped Tight Stamp via C++ SIMD)
        if self.burn_sub_config.get("enabled", False) and active_seg is not None:
            sub_text = active_seg.get("_cached_clean_text")
            if sub_text is None:
                raw_sub = active_seg.get("khmer_text") or active_seg.get("original_text") or active_seg.get("text", "")
                sub_text = strip_speaker_tags(raw_sub)
                active_seg["_cached_clean_text"] = sub_text

            if sub_text:
                seg_idx = self.active_seg_idx
                if seg_idx not in self.subtitle_cache:
                    try:
                        text_bgr, alpha, sx, sy, sw, sh = render_subtitle_overlay_tight(
                            sub_text=sub_text,
                            font_name=self.burn_sub_config.get("font_name", "Kantumruy Pro"),
                            font_size_pt=self.burn_sub_config.get("font_size", 20),
                            color_rgb=self.burn_sub_config.get("color_rgb", (255, 255, 255)),
                            bg_opacity=self.burn_sub_config.get("bg_opacity", 0.0),
                            width=self.video_rect_w,
                            height=self.video_rect_h,
                            scale_y=self.scale_y,
                            x_ratio=self.burn_sub_config.get("x_ratio", 0.50),
                            y_ratio=self.burn_sub_config.get("y_ratio", 0.85),
                            template=self.burn_sub_config.get("template")
                        )
                        sx += self.video_rect_x
                        sy += self.video_rect_y
                        self.subtitle_cache[seg_idx] = (text_bgr, alpha, sx, sy, sw, sh)
                        # Bounded eviction: Keep RAM flat under 20MB for long videos
                        if len(self.subtitle_cache) > 60:
                            min_keep = max(0, seg_idx - 10)
                            for old_k in [k for k in list(self.subtitle_cache.keys()) if k < min_keep]:
                                del self.subtitle_cache[old_k]
                    except Exception as e_sub:
                        logger.error(f"Error caching burn subtitle for segment {seg_idx}: {e_sub}")
                        self.subtitle_cache[seg_idx] = (None, None, 0, 0, 0, 0)

                cached_sub = self.subtitle_cache.get(seg_idx)
                if cached_sub is not None and len(cached_sub) == 6:
                    text_bgr, alpha, sx, sy, sw, sh = cached_sub
                    if text_bgr is not None and sw > 0 and sh > 0:
                        seg_st = float(active_seg.get("start", 0.0))
                        seg_dur = max(0.1, float(active_seg.get("end", 0.0)) - seg_st)
                        anim_type = self.burn_sub_config.get("anim_type", "pop_bounce")
                        anim_dur = float(self.burn_sub_config.get("anim_dur", 0.35))
                        frame = apply_subtitle_animation_effect(
                            frame=frame,
                            text_bgr=text_bgr,
                            alpha=alpha,
                            sx=sx,
                            sy=sy,
                            sw=sw,
                            sh=sh,
                            anim_type=anim_type,
                            anim_dur=anim_dur,
                            cur_sec=cur_sec,
                            seg_start=seg_st,
                            seg_dur=seg_dur
                        )

        return frame


# ==================== VIDEO EXPORT PIPELINE ====================

class VideoExportPipeline:
    """
    Orchestrates the entire Single-Pass Video Export with Audio Muxing:
    - Reader Worker Thread (bounded queue)
    - Effects Processor Engine (Tight ROI & O(1) rolling pointer)
    - Writer Worker Thread (Zero Intermediate Video Disk I/O pipe)
    - Apple Silicon VideoToolbox Hardware Acceleration with FAST / BALANCED / QUALITY profiles
    - Responsive ProgressTracker & Clean Cancellation
    """

    @staticmethod
    def get_encoder_params(mode: str = "BALANCED", width: int = 1920, height: int = 1080) -> List[str]:
        m = str(mode).upper()
        use_videotoolbox = (sys.platform == "darwin")

        max_dim = max(width, height)
        min_dim = min(width, height)

        if use_videotoolbox:
            if max_dim >= 3840 or min_dim >= 2160:  # 4K
                bitrates = {"FAST": ("18000k", "24000k", "32000k"), "QUALITY": ("40000k", "55000k", "70000k"), "BALANCED": ("25000k", "35000k", "45000k")}
            elif max_dim >= 2560 or min_dim >= 1440: # 2K
                bitrates = {"FAST": ("10000k", "14000k", "18000k"), "QUALITY": ("22000k", "30000k", "40000k"), "BALANCED": ("14000k", "20000k", "25000k")}
            elif max_dim >= 1920 or min_dim >= 1080: # 1080p
                bitrates = {"FAST": ("6000k", "10000k", "14000k"), "QUALITY": ("16000k", "24000k", "32000k"), "BALANCED": ("9000k", "15000k", "20000k")}
            elif max_dim >= 1280 or min_dim >= 720:  # 720p
                bitrates = {"FAST": ("3000k", "4500k", "6000k"), "QUALITY": ("7000k", "10000k", "14000k"), "BALANCED": ("4500k", "7000k", "9000k")}
            else:  # SD 480p or smaller
                bitrates = {"FAST": ("1200k", "1800k", "2500k"), "QUALITY": ("3500k", "5000k", "7000k"), "BALANCED": ("2000k", "3000k", "4000k")}

            b_target, b_max, b_buf = bitrates.get(m, bitrates["BALANCED"])
            prio = "0" if m == "QUALITY" else "1"

            params = [
                "-c:v", "h264_videotoolbox",
                "-prio_speed", prio,
                "-realtime", "0",
                "-b:v", b_target,
                "-maxrate", b_max,
                "-bufsize", b_buf,
                "-pix_fmt", "yuv420p",
                "-color_range", "1",
                "-tag:v", "avc1"
            ]
            if m == "FAST":
                params.extend(["-profile:v", "main"])
            return params
        else:
            if m == "FAST":
                return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
            elif m == "QUALITY":
                return ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p"]
            else:
                return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]

    @classmethod
    def execute(
        cls,
        video_path: str,
        output_path: str,
        effects_config: Optional[dict] = None,
        master_audio_path: Optional[str] = None,
        music_audio_path: Optional[str] = None,
        background_volume: float = 0.30,
        export_mode: str = "BALANCED",
        target_resolution: str = "Original",
        progress_callback: Optional[Callable[[int, str], None]] = None,
        is_cancelled_fn: Optional[Callable[[], bool]] = None
    ) -> bool:
        """Runs the complete Single-Pass export process."""
        app = QApplication.instance() or QApplication(sys.argv)
        ensure_qt_fonts()

        if not os.path.exists(video_path):
            logger.error(f"❌ Input video does not exist: {video_path}")
            return False

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"❌ Cannot open video file with OpenCV: {video_path}")
            return False

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            total_frames = 1

        res_choice = target_resolution
        if (not res_choice or res_choice == "Original") and effects_config and "target_resolution" in effects_config:
            res_choice = effects_config.get("target_resolution")

        target_w, target_h = compute_target_resolution(orig_w, orig_h, res_choice)
        need_resize = (target_w != orig_w or target_h != orig_h)
        logger.info(f"🎬 Video Export Resolution: Source {orig_w}x{orig_h} -> Target {target_w}x{target_h} ({res_choice}) [Resize: {need_resize}]")

        # Check visual effects to select export mode (MODE A vs MODE B vs MODE C)
        has_blur = bool(effects_config and effects_config.get("blur", {}).get("enabled", False))
        has_text = bool(effects_config and effects_config.get("text_overlay", {}).get("enabled", False))
        has_logo = bool(effects_config and effects_config.get("logo", {}).get("enabled", False))
        has_sub = bool(effects_config and effects_config.get("burn_subtitle", {}).get("enabled", False) and effects_config.get("segments"))
        has_visual_effects = has_blur or has_text or has_logo or has_sub

        has_audio = bool(master_audio_path and os.path.exists(master_audio_path))
        safe_temp_output = get_temp_path(f"fast_export_{os.path.basename(output_path)}")
        if os.path.exists(safe_temp_output):
            try: os.remove(safe_temp_output)
            except Exception: pass

        # ==================== MODE A: ZERO-RE-ENCODE STREAM COPY (-c:v copy) ====================
        if not need_resize and not has_visual_effects:
            logger.info("🚀 [EXPORT ENGINE] Selecting MODE A: Zero-Re-encode Stream Copy (-c:v copy)")
            logger.info(f"📋 [HARDWARE VERIFICATION] Mode: MODE_A_STREAM_COPY | Input: {orig_w}x{orig_h} @ {fps:.1f}fps | Codec: -c:v copy (0% quality loss)")
            if progress_callback:
                progress_callback(10, "⚡ Executing Mode A (Instant Stream Copy)...")

            stream_cmd = ["ffmpeg", "-y", "-i", video_path]
            if has_audio:
                stream_cmd.extend(["-i", master_audio_path])
                if music_audio_path and os.path.exists(music_audio_path) and background_volume > 0.0:
                    stream_cmd.extend(["-i", music_audio_path])
                    bg_vol_str = f"{max(0.05, min(1.0, background_volume)):.2f}"
                    filter_str = (
                        f"[2:a]volume={bg_vol_str}[bgm_scaled];"
                        f"[bgm_scaled][1:a]sidechaincompress=threshold=0.035:ratio=3.5:attack=35:release=280[ducked_bgm];"
                        f"[1:a]volume=2.2[v_boost];"
                        f"[v_boost][ducked_bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.99[aout]"
                    )
                    stream_cmd.extend(["-filter_complex", filter_str, "-map", "0:v:0", "-map", "[aout]"])
                else:
                    stream_cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
                stream_cmd.extend(["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-shortest", "-movflags", "+faststart", safe_temp_output])
            else:
                stream_cmd.extend(["-c:v", "copy", "-movflags", "+faststart", safe_temp_output])

            res = subprocess.run(stream_cmd, capture_output=True, text=True)
            if res.returncode == 0 and os.path.exists(safe_temp_output) and os.path.getsize(safe_temp_output) > 1000:
                if os.path.exists(output_path):
                    try: os.remove(output_path)
                    except Exception: pass
                shutil.move(safe_temp_output, output_path)
                if progress_callback: progress_callback(100, "✅ Export Completed (Mode A Stream Copy)!")
                logger.info(f"✅ [EXPORT ENGINE] Mode A succeeded! Output: {output_path} ({os.path.getsize(output_path):,} bytes)")
                return True
            else:
                logger.warning(f"Mode A stream copy fallback to VideoToolbox: {res.stderr[:200] if res.stderr else 'Unknown'}")

        # ==================== MODE B / C: HARDWARE ACCELERATED EFFECTS PIPELINE ====================
        use_vtb = (sys.platform == "darwin")
        enc_name = "h264_videotoolbox" if use_vtb else "libx264"
        hw_status = "Apple VideoToolbox (ENABLED)" if use_vtb else "Software libx264"
        logger.info(f"🚀 [EXPORT ENGINE] Selecting MODE B: Hardware Acceleration Pipeline")
        logger.info(f"📋 [HARDWARE VERIFICATION] Mode: MODE_B_HARDWARE | Resolution: {orig_w}x{orig_h} -> {target_w}x{target_h} | FPS: {fps:.1f}")
        logger.info(f"📋 [HARDWARE VERIFICATION] Video Encoder: {enc_name} | HW Status: {hw_status} | Pixel Format: yuv420p | Profile: {export_mode}")

        # Memory Safety: Bounded queue size based on target frame resolution
        if target_w >= 3840 or target_h >= 2160:
            queue_size = 24  # 4K (~590MB)
        elif target_w >= 1920 or target_h >= 1080:
            queue_size = 64  # 1080p: smooth pipelined execution (~390MB RAM)
        elif target_w >= 1280 or target_h >= 720:
            queue_size = 96  # 720p (~260MB)
        else:
            queue_size = 128 # SD

        # Safe multi-threaded decoding utilizing all available CPU cores
        try:
            cv2.setNumThreads(0)
        except Exception:
            pass

        # 1. Build FFmpeg command with native hardware multi-threading
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-threads", "0",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{target_w}x{target_h}",
            "-pix_fmt", "bgr24",
            "-r", str(fps),
            "-i", "-"
        ]

        has_audio = bool(master_audio_path and os.path.exists(master_audio_path))
        if has_audio:
            ffmpeg_cmd.extend(["-i", master_audio_path])
            if music_audio_path and os.path.exists(music_audio_path) and background_volume > 0.0:
                ffmpeg_cmd.extend(["-i", music_audio_path])
                bg_vol_str = f"{max(0.05, min(1.0, background_volume)):.2f}"
                filter_str = (
                    f"[2:a]volume={bg_vol_str}[bgm_scaled];"
                    f"[bgm_scaled][1:a]sidechaincompress=threshold=0.035:ratio=3.5:attack=35:release=280[ducked_bgm];"
                    f"[1:a]volume=2.2[v_boost];"
                    f"[v_boost][ducked_bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.99[aout]"
                )
                ffmpeg_cmd.extend([
                    "-filter_complex", filter_str,
                    "-map", "0:v:0",
                    "-map", "[aout]"
                ])
            else:
                ffmpeg_cmd.extend([
                    "-map", "0:v:0",
                    "-map", "1:a:0"
                ])
        else:
            ffmpeg_cmd.extend(["-map", "0:v:0"])

        # Hardware Encoding Profile scaled to target resolution
        ffmpeg_cmd.extend(cls.get_encoder_params(export_mode, target_w, target_h))

        if has_audio:
            ffmpeg_cmd.extend([
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-shortest"
            ])

        ffmpeg_cmd.extend(["-movflags", "+faststart"])
        ffmpeg_cmd.append(safe_temp_output)

        # Redirect FFmpeg stderr to log file to completely eliminate OS pipe buffer deadlock
        stderr_log_path = get_temp_path("ffmpeg_export_stderr.log")
        try:
            if os.path.exists(stderr_log_path):
                os.remove(stderr_log_path)
        except Exception:
            pass

        stderr_file = None
        pipe_proc = None
        try:
            stderr_file = open(stderr_log_path, "wb")
            pipe_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                bufsize=16 * 1024 * 1024
            )
            logger.info(f"🚀 [VideoExportPipeline] Started FFmpeg hardware pipe ({export_mode} mode @ {target_w}x{target_h}) for {total_frames:,} frames")
        except Exception as e_pipe:
            logger.error(f"❌ Failed to spawn FFmpeg pipe process: {e_pipe}")
            if stderr_file and not stderr_file.closed:
                try: stderr_file.close()
                except Exception: pass
            cap.release()
            return False

        # 2. Producer-Consumer Thread Queues
        frame_in_queue = queue.Queue(maxsize=queue_size)
        frame_out_queue = queue.Queue(maxsize=queue_size)
        stop_event = threading.Event()

        def reader_worker():
            fc = 0
            while not stop_event.is_set():
                if is_cancelled_fn and is_cancelled_fn():
                    break
                ret, f = cap.read()
                if not ret or f is None:
                    break
                if need_resize:
                    interp = cv2.INTER_AREA if (target_w < orig_w and target_h < orig_h) else cv2.INTER_LINEAR
                    f = cv2.resize(f, (target_w, target_h), interpolation=interp)
                # Put frame without dropping: responsive retry until placed or stopped
                while not stop_event.is_set():
                    try:
                        frame_in_queue.put((fc, f), timeout=0.002)
                        fc += 1
                        break
                    except queue.Full:
                        if is_cancelled_fn and is_cancelled_fn():
                            break
                        continue

            # Signal reader done
            while not stop_event.is_set():
                try:
                    frame_in_queue.put((None, None), timeout=0.002)
                    break
                except queue.Full:
                    continue

        def writer_worker():
            while True:
                try:
                    f = frame_out_queue.get(timeout=0.002)
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    continue

                if f is None:
                    break

                try:
                    if pipe_proc and pipe_proc.stdin:
                        pipe_proc.stdin.write(memoryview(f))
                except Exception as e_w:
                    logger.error(f"Pipe write error: {e_w}")
                    break

        reader_thread = threading.Thread(target=reader_worker, daemon=True)
        writer_thread = threading.Thread(target=writer_worker, daemon=True)
        reader_thread.start()
        writer_thread.start()

        effects_engine = EffectsProcessor(
            effects_config or {}, target_w, target_h, fps,
            orig_w=orig_w, orig_h=orig_h
        )
        tracker = ProgressTracker(total_frames)

        frame_count = 0
        cancelled = False

        try:
            while True:
                if is_cancelled_fn and is_cancelled_fn():
                    cancelled = True
                    logger.info("⚠️ [VideoExportPipeline] Export cancelled by user request.")
                    break

                try:
                    fc, frame = frame_in_queue.get(timeout=0.002)
                except queue.Empty:
                    if not reader_thread.is_alive() and frame_in_queue.empty():
                        break
                    continue

                if frame is None:
                    break

                cur_sec = fc / fps
                processed_frame = effects_engine.process_frame(frame, cur_sec)

                while not stop_event.is_set():
                    try:
                        frame_out_queue.put(processed_frame, timeout=0.002)
                        break
                    except queue.Full:
                        if is_cancelled_fn and is_cancelled_fn():
                            cancelled = True
                            break
                        continue

                frame_count += 1
                if frame_count % 3000 == 0:
                    gc.collect()

                if frame_count % 30 == 0 or frame_count == total_frames:
                    info = tracker.update(frame_count)
                    if progress_callback:
                        try:
                            progress_callback(info["progress_pct"], info["status_text"])
                        except Exception:
                            pass

        finally:
            # Signal writer thread that no more frames are coming
            try:
                frame_out_queue.put(None, timeout=0.2)
            except Exception:
                pass

            stop_event.set()
            writer_thread.join(timeout=15.0)
            reader_thread.join(timeout=3.0)
            cap.release()

            # 2. Crucial: Flush and Close stdin to signal clean EOF to FFmpeg
            if pipe_proc is not None:
                if pipe_proc.stdin:
                    try:
                        pipe_proc.stdin.flush()
                    except Exception:
                        pass
                    try:
                        pipe_proc.stdin.close()
                    except Exception:
                        pass

                if cancelled:
                    try:
                        pipe_proc.terminate()
                        pipe_proc.wait(timeout=2.0)
                    except Exception:
                        try: pipe_proc.kill()
                        except Exception: pass
                else:
                    # Give FFmpeg reasonable time to write headers and finish cleanly
                    try:
                        pipe_proc.wait(timeout=30.0)
                    except subprocess.TimeoutExpired:
                        logger.warning("FFmpeg did not exit within 30s after EOF, terminating...")
                        try:
                            pipe_proc.terminate()
                            pipe_proc.wait(timeout=5.0)
                        except Exception:
                            try: pipe_proc.kill()
                            except Exception: pass

            # 3. Close stderr log file safely
            if stderr_file is not None and not stderr_file.closed:
                try:
                    stderr_file.close()
                except Exception:
                    pass

        if cancelled:
            if os.path.exists(safe_temp_output):
                try: os.remove(safe_temp_output)
                except Exception: pass
            return False

        if pipe_proc is not None and pipe_proc.returncode != 0:
            logger.error(f"❌ FFmpeg export process failed with return code {pipe_proc.returncode}")
            if os.path.exists(stderr_log_path):
                try:
                    with open(stderr_log_path, "r", encoding="utf-8", errors="replace") as ef:
                        err_tail = "".join(ef.readlines()[-25:])
                        logger.error(f"FFmpeg stderr tail:\n{err_tail}")
                except Exception:
                    pass
            if os.path.exists(safe_temp_output):
                try: os.remove(safe_temp_output)
                except Exception: pass
            return False

        if os.path.exists(safe_temp_output) and os.path.getsize(safe_temp_output) > 1000:
            delivered = False

            # 1. Try to deliver to requested output_path
            try:
                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                if os.path.exists(output_path):
                    try: os.remove(output_path)
                    except Exception: pass
                shutil.move(safe_temp_output, output_path)
                logger.info(f"🎉 [VideoExportPipeline] Export successfully delivered: {output_path}")
                delivered = True
                return True
            except Exception as e_mv:
                logger.warning(f"⚠️ Primary move to '{output_path}' failed ({e_mv}). Trying raw byte stream copy...")
                try:
                    with open(safe_temp_output, "rb") as fsrc, open(output_path, "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                        try: os.remove(safe_temp_output)
                        except Exception: pass
                        logger.info(f"🎉 [VideoExportPipeline] Export successfully delivered via stream copy: {output_path}")
                        delivered = True
                        return True
                except Exception as e_cp:
                    logger.warning(f"⚠️ Direct write to '{output_path}' blocked by OS permissions ({e_cp}).")

            # 2. Resilient Fallback: If destination is blocked (e.g., macOS ~/Desktop without TCC access)
            # Deliver safely to project's own OUTPUT_DIR which is always writable
            if not delivered:
                from utils.file_utils import OUTPUT_DIR, ensure_directories
                ensure_directories()
                fallback_path = os.path.join(str(OUTPUT_DIR), os.path.basename(output_path))
                fallback_videos = os.path.join(str(OUTPUT_DIR), "videos", os.path.basename(output_path))
                for target_fb in [fallback_path, fallback_videos]:
                    try:
                        if os.path.exists(target_fb):
                            try: os.remove(target_fb)
                            except Exception: pass
                        shutil.move(safe_temp_output, target_fb)
                        logger.info(f"🎉 [VideoExportPipeline] Export safely saved to project output fallback: {target_fb}")
                        return True
                    except Exception as e_fb:
                        try:
                            with open(safe_temp_output, "rb") as fsrc, open(target_fb, "wb") as fdst:
                                shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
                            if os.path.exists(target_fb) and os.path.getsize(target_fb) > 1000:
                                try: os.remove(safe_temp_output)
                                except Exception: pass
                                logger.info(f"🎉 [VideoExportPipeline] Export safely streamed to project output fallback: {target_fb}")
                                return True
                        except Exception:
                            continue

            return delivered

        return False
