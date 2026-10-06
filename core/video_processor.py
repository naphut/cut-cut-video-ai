import os
import sys
import re
import math
import shutil
import subprocess
import cv2
import queue
import threading
import numpy as np
from qt_compat import QApplication, QtGui, QtCore, Qt
from utils.ffmpeg import extract_audio, combine_video_audio, get_video_info
from utils.file_utils import get_temp_path
from utils.logger import logger

_loaded_qt_fonts = set()
def ensure_qt_fonts():
    font_dir = os.path.abspath('fonts')
    if os.path.exists(font_dir):
        for f in os.listdir(font_dir):
            if f.endswith('.ttf') or f.endswith('.otf'):
                p = os.path.join(font_dir, f)
                if p not in _loaded_qt_fonts:
                    QtGui.QFontDatabase.addApplicationFont(p)
                    _loaded_qt_fonts.add(p)

def resolve_qt_font_name(requested_name: str) -> str:
    fn = str(requested_name).lower()
    if "kantumruy" in fn:
        return "Kantumruy Pro"
    elif "battambang" in fn:
        return "Battambang"
    elif "noto" in fn:
        return "Noto Sans Khmer"
    elif "sangam" in fn or "mn" in fn:
        return "Khmer Sangam MN"
    return "Kantumruy Pro"


def _get_dynamic_text_pos(cur_sec: float, zone: str, speed: str, canvas_w: int, canvas_h: int, text_w: int, text_h: int, scale_y: float = 1.0):
    speed_key = str(speed).lower()
    if 'fast' in speed_key or 'លឿន' in speed_key:
        cycle_base = 5.0
    elif 'slow' in speed_key or 'យឺត' in speed_key:
        cycle_base = 16.0
    else:
        cycle_base = 10.0
    
    z = str(zone).lower()
    if z in ['up_down', 'vertical', 'bounce', 'run_up_down']:
        # Up and Down across video (ចលនារត់ចុះឡើងការពារគេលួច)
        min_y = max(15, int(canvas_h * 0.08))
        max_y = max(min_y + 20, int(canvas_h * 0.88 - text_h))
        cycle_y = cycle_base
        
        # Gentle horizontal sway (30% center amplitude) so it doesn't stay strictly in one line
        center_x = int((canvas_w - text_w) / 2.0)
        amp_x = int(canvas_w * 0.20)
        min_x = max(15, center_x - amp_x)
        max_x = min(canvas_w - text_w - 15, center_x + amp_x)
        cycle_x = cycle_base * 1.618  # golden ratio for natural organic glide
    elif z in ['top']:
        base_y = int(canvas_h * 0.12)
        amp_y = int(canvas_h * 0.03)
        min_y, max_y = base_y - amp_y, base_y + amp_y
        cycle_y = cycle_base * 0.73
        min_x = max(14, int(canvas_w * 0.03))
        max_x = max(min_x + 20, int(canvas_w * 0.97 - text_w))
        cycle_x = cycle_base
    elif z in ['mid', 'middle']:
        base_y = int(canvas_h * 0.50 - text_h / 2.0)
        amp_y = int(canvas_h * 0.04)
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
    else: # full / 2d float / screensaver
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


def _render_text_overlay_tight_stamp(
    text: str, font_name: str, font_size_pt: int, color_rgb: tuple, scale_y: float = 1.0,
    outline_color_rgb: tuple = (0, 0, 0), outline_width: int = 2,
    shadow_color_rgb: tuple = (0, 0, 0), shadow_offset: int = 3,
    bg_color_rgb: tuple = None, opacity: float = 1.0
):
    font_size_pt = max(12, int(font_size_pt * scale_y))
    font_family = resolve_qt_font_name(font_name)
    font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
    fm = QtGui.QFontMetrics(font)
    
    text_w = fm.horizontalAdvance(text)
    text_h = fm.height()
    ascent = fm.ascent()
    
    pad = 14
    shd_off = max(0, int(float(shadow_offset) * scale_y))
    out_w = max(0, int(float(outline_width) * scale_y))
    
    stamp_w = text_w + pad * 2 + shd_off + out_w * 2
    stamp_h = text_h + pad * 2 + shd_off + out_w * 2
    
    qimg = QtGui.QImage(stamp_w, stamp_h, QtGui.QImage.Format_ARGB32_Premultiplied)
    qimg.fill(Qt.transparent)
    
    painter = QtGui.QPainter(qimg)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
    painter.setFont(font)
    
    alpha_mult = max(0.05, min(1.0, float(opacity)))
    
    origin_x = pad + out_w
    origin_y = pad + out_w + ascent
    
    if bg_color_rgb is not None and bg_color_rgb != "transparent":
        bg_alpha = int(180 * alpha_mult)
        bg_c = bg_color_rgb if (isinstance(bg_color_rgb, (tuple, list)) and len(bg_color_rgb) == 3) else (0, 0, 0)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(bg_c[0], bg_c[1], bg_c[2], bg_alpha)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QtCore.QRect(0, 0, stamp_w, stamp_h), 10, 10)
        
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
    mask = arr[:, :, 3] > 10
    return mask, text_bgr


def _render_text_overlay_qt(
    text: str, font_name: str, font_size_pt: int, color_rgb: tuple, pos: tuple,
    scale_x: float, scale_y: float, width: int, height: int,
    anim_type: str = "none", p: float = 1.0, outro_factor: float = 1.0,
    bg_color_rgb: tuple = None, is_re_trigger: bool = False,
    outline_color_rgb: tuple = (0, 0, 0), outline_width: int = 2,
    shadow_color_rgb: tuple = (0, 0, 0), shadow_offset: int = 3
):
    """Render text overlay with support for drop shadow, stroke outline, static blending or dynamic animations."""
    scale = 1.0
    dx = 0
    dy = 0
    alpha_mult = 1.0
    
    if anim_type == "fade":
        if is_re_trigger:
            alpha_mult = (0.5 + 0.5 * p) * outro_factor
        else:
            alpha_mult = p * outro_factor
    elif anim_type == "slide_up":
        ease = 1.0 - (1.0 - p) ** 3
        dy = int((1.0 - ease) * (25 if is_re_trigger else 45))
        alpha_mult = (1.0 if is_re_trigger else min(1.0, p * 1.5)) * outro_factor
    elif anim_type == "slide_left":
        ease = 1.0 - (1.0 - p) ** 3
        dx = int((1.0 - ease) * (-40 if is_re_trigger else -80))
        alpha_mult = (1.0 if is_re_trigger else min(1.0, p * 1.5)) * outro_factor
    elif anim_type == "pop":
        if is_re_trigger:
            # Energetic periodic pop bounce
            pulse = math.sin(p * math.pi) * 0.30 * math.exp(-1.5 * p)
            scale = max(0.2, 1.0 + pulse)
            alpha_mult = 1.0 * outro_factor
        else:
            c1 = 1.70158
            c3 = c1 + 1.0
            ease = 1.0 + c3 * ((p - 1.0) ** 3) + c1 * ((p - 1.0) ** 2)
            scale = max(0.05, ease)
            alpha_mult = min(1.0, p * 2.0) * outro_factor
    else:
        alpha_mult = 1.0
        
    if alpha_mult <= 0.001:
        return None, None

    x = int(pos[0] * scale_x)
    y = int(pos[1] * scale_y)
    x = max(10, min(width - 30, x))
    y = max(20, min(height - 20, y))
    
    font_size_pt = max(12, int(font_size_pt * scale_y))
    font_family = resolve_qt_font_name(font_name)
    
    qimg = QtGui.QImage(width, height, QtGui.QImage.Format_ARGB32_Premultiplied)
    qimg.fill(Qt.transparent)
    
    painter = QtGui.QPainter(qimg)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
    
    font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
    painter.setFont(font)
    fm = QtGui.QFontMetrics(font)
    text_w = fm.horizontalAdvance(text)
    text_h = fm.height()
    
    pad_x, pad_y = 14, 10
    bg_x = max(0, x - pad_x)
    bg_y = max(0, y - fm.ascent() - pad_y)
    bg_w = min(width - bg_x, text_w + pad_x * 2)
    bg_h = min(height - bg_y, text_h + pad_y * 2)
    
    box_cx = bg_x + bg_w / 2.0
    box_cy = bg_y + bg_h / 2.0
    painter.save()
    painter.translate(box_cx + dx * scale_x, box_cy + dy * scale_y)
    if scale != 1.0:
        painter.scale(scale, scale)
    painter.translate(-box_cx, -box_cy)
    
    if bg_color_rgb is not None and bg_color_rgb != "transparent":
        bg_alpha = int(180 * alpha_mult)
        bg_c = bg_color_rgb if (isinstance(bg_color_rgb, (tuple, list)) and len(bg_color_rgb) == 3) else (0, 0, 0)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(bg_c[0], bg_c[1], bg_c[2], bg_alpha)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QtCore.QRect(bg_x, bg_y, bg_w, bg_h), 10, 10)
    
    path = QtGui.QPainterPath()
    path.addText(x, y, font, text)
    
    # 1. Drop shadow
    if shadow_offset > 0:
        shd_off = max(1.0, float(shadow_offset) * scale_y)
        shd_path = path.translated(shd_off, shd_off)
        shd_c = shadow_color_rgb if (shadow_color_rgb and len(shadow_color_rgb) == 3) else (0, 0, 0)
        shd_alpha = int(160 * alpha_mult)
        painter.fillPath(shd_path, QtGui.QColor(shd_c[0], shd_c[1], shd_c[2], shd_alpha))

    # 2. Stroke outline
    if outline_width > 0:
        stroke_w = max(1, int(float(outline_width) * scale_y))
        stroke_alpha = int(255 * alpha_mult)
        out_c = outline_color_rgb if (outline_color_rgb and len(outline_color_rgb) == 3) else (0, 0, 0)
        painter.strokePath(path, QtGui.QPen(QtGui.QColor(out_c[0], out_c[1], out_c[2], stroke_alpha), stroke_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    # 3. Fill text
    fill_alpha = int(255 * alpha_mult)
    painter.fillPath(path, QtGui.QColor(color_rgb[0], color_rgb[1], color_rgb[2], fill_alpha))
    
    painter.restore()
    painter.end()
    
    qimg_rgb = qimg.convertToFormat(QtGui.QImage.Format_RGB888)
    ptr = qimg_rgb.bits()
    if hasattr(ptr, 'setsize'): ptr.setsize(height * width * 3)
    arr = np.frombuffer(ptr, np.uint8).reshape((height, width, 3))
    text_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    mask = (arr > 0).any(axis=2)
    return mask, text_bgr


def _render_subtitle_overlay_qt(sub_text: str, font_name: str, font_size_pt: int, color_rgb: tuple, bg_opacity: float, width: int, height: int, scale_y: float, x_ratio: float = 0.50, y_ratio: float = 0.85):
    """Pre-render a subtitle segment overlay into a tight (mask, bgr, x1, y1, w, h) stamp for ultra-fast frame caching."""
    font_size_pt = max(14, int(font_size_pt * scale_y))
    font_family = resolve_qt_font_name(font_name)
    
    font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
    fm = QtGui.QFontMetrics(font)
    
    words = sub_text.split()
    lines = []
    curr_line = ""
    max_line_w = max(100, width - 80)
    
    for word in words:
        test_line = f"{curr_line} {word}".strip()
        if fm.horizontalAdvance(test_line) <= max_line_w or not curr_line:
            curr_line = test_line
        else:
            lines.append(curr_line)
            curr_line = word
    if curr_line:
        lines.append(curr_line)
    
    line_height = fm.height() + 6
    total_h = len(lines) * line_height
    line_widths = [fm.horizontalAdvance(l) for l in lines]
    max_w = max(line_widths) if line_widths else 100
    
    sub_box_w = max(20, min(width - 20, max_w + 44))
    sub_box_h = max(20, min(height - 20, total_h + 30))

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
    
    bg_alpha = int(bg_opacity * 255)
    painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0, bg_alpha)))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(QtCore.QRect(0, 0, sub_box_w, sub_box_h), 12, 12)
    
    curr_y = fm.ascent() + 15
    stroke_w = max(2, font_size_pt // 10)
    
    for i, l in enumerate(lines):
        lx = (sub_box_w - line_widths[i]) // 2
        path = QtGui.QPainterPath()
        path.addText(lx, curr_y, font, l)
        
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 255), stroke_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        
        painter.setBrush(QtGui.QBrush(QtGui.QColor(color_rgb[0], color_rgb[1], color_rgb[2])))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)
        
        curr_y += line_height
    
    painter.end()
    
    qimg_rgba = qimg.convertToFormat(QtGui.QImage.Format_RGBA8888)
    bpl = qimg_rgba.bytesPerLine()
    ptr = qimg_rgba.bits()
    if hasattr(ptr, 'setsize'): ptr.setsize(sub_box_h * bpl)
    arr_raw = np.frombuffer(ptr, np.uint8).reshape((sub_box_h, bpl))
    arr = arr_raw[:, :sub_box_w * 4].reshape((sub_box_h, sub_box_w, 4))
    text_bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    mask = arr[:, :, 3] > 10
    return mask, text_bgr, bg_x1, bg_y1, sub_box_w, sub_box_h


class VideoProcessor:
    def __init__(self, video_path: str):
        self.video_path = video_path
        from services.video_service import VideoService
        self.service = VideoService(video_path)
        self.info = self.service.get_video_info(video_path)

    def get_duration(self) -> float:
        """Return video duration in seconds."""
        try:
            dur = float(self.info.get("duration", 0.0) or 0.0)
            if dur > 0.0:
                return dur
        except Exception:
            pass
        from utils.ffmpeg import get_video_info
        return float(get_video_info(self.video_path).get("duration", 0.0) or 0.0)

    def get_resolution(self) -> tuple:
        """Return (width, height) tuple."""
        return int(self.info.get("width", 1920) or 1920), int(self.info.get("height", 1080) or 1080)

    def get_fps(self) -> float:
        """Return video framerate."""
        return float(self.info.get("fps", 30.0) or 30.0)

    def extract_source_audio(self) -> str:
        """Extract audio stream from source video to 16kHz WAV."""
        from services.audio_extractor import AudioExtractor
        extractor = AudioExtractor()
        audio_temp_path = get_temp_path("original_audio.wav")
        result = extractor.extract_audio(self.video_path, audio_temp_path)
        if result and os.path.exists(result):
            return result
        raise RuntimeError(f"Failed to extract audio from video: {self.video_path}")

    def merge_dubbed_audio(
        self,
        dubbed_audio_path: str,
        output_video_path: str,
        music_audio_path: str = None,
        background_volume: float = 0.30
    ) -> bool:
        """Merge dubbed Khmer audio track with original video stream, preserving background music."""
        return self.service.merge_dubbed_audio(
            video_path=self.video_path,
            master_audio_path=dubbed_audio_path,
            output_video_path=output_video_path,
            music_audio_path=music_audio_path,
            background_volume=background_volume
        )

    def apply_effects_to_video(self, effects_config: dict, output_path: str) -> bool:
        """
        Apply effects (blur, text overlay, logo, burn subtitle) to video using ultra-fast 10x cached rendering.
        Provides 100% native HarfBuzz Khmer Unicode shaping during export.
        """
        return self.export_with_effects_and_audio(
            effects_config=effects_config,
            output_path=output_path,
            master_audio_path=None
        )

    def export_with_effects_and_audio(
        self,
        effects_config: dict,
        output_path: str,
        master_audio_path: str = None,
        music_audio_path: str = None,
        background_volume: float = 0.30,
        export_mode: str = "BALANCED",
        target_resolution: str = "Original",
        progress_callback = None,
        is_cancelled_fn = None
    ) -> bool:
        """
        Unified Ultra-Fast Single-Pass Video Pipeline:
        - Multi-Threaded Frame Reader & Pipe Writer (Producer-Consumer Queue)
        - Apple Silicon M-Series Hardware Video Acceleration (h264_videotoolbox with -prio_speed 1)
        - Tight ROI Bounding-Box Overlay Slicing (zero full-canvas memory allocations)
        - O(1) Amortized Rolling Pointer for Subtitles and Speech Detection
        - Single-Pass Direct Video + Audio Muxing (Zero intermediate video disk I/O)
        - Dynamic Resolution Scaling (4K, 2K, 1080p, 720p, 480p, Original)
        - Responsive progress tracking and clean cancellation
        """
        from core.export_engine import VideoExportPipeline, ExportPlanner

        orig_w = self.info.get("width")
        orig_h = self.info.get("height")
        is_needed, _ = ExportPlanner.is_effects_needed(
            effects_config,
            target_resolution=target_resolution,
            orig_w=orig_w,
            orig_h=orig_h
        )
        if not is_needed:
            if master_audio_path and os.path.exists(master_audio_path):
                return self.merge_dubbed_audio(
                    dubbed_audio_path=master_audio_path,
                    output_video_path=output_path,
                    music_audio_path=music_audio_path,
                    background_volume=background_volume
                )
            import subprocess
            cmd = ['ffmpeg', '-y', '-i', self.video_path, '-c', 'copy', output_path]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0

        return VideoExportPipeline.execute(
            video_path=self.video_path,
            output_path=output_path,
            effects_config=effects_config,
            master_audio_path=master_audio_path,
            music_audio_path=music_audio_path,
            background_volume=background_volume,
            export_mode=export_mode,
            target_resolution=target_resolution,
            progress_callback=progress_callback,
            is_cancelled_fn=is_cancelled_fn
        )

