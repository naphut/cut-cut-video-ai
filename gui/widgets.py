import os
import sys
import time
import re
import shutil
import wave
import subprocess
import cv2
import math
import bisect
import numpy as np
from qt_compat import (
    Qt, Signal, Slot, QThread, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QPlainTextEdit, QLineEdit, QAbstractItemView, QSlider, QCheckBox, QRadioButton, QSpinBox,
    QDoubleSpinBox, QComboBox, QGroupBox, QScrollArea, QMediaPlayer, QAudioOutput, QProgressBar,
    QUrl, create_media_content, QtGui, QtCore, QScrollBar, QTabWidget, QColorDialog,
    QMenu, QAction, QToolButton, QStyledItemDelegate, QStyleOptionViewItem,
    QDialog, QDialogButtonBox, QMessageBox, QSizePolicy, QApplication, QButtonGroup
)
from services.voxcpm_service import (
    VOICE_PRESETS, VOICE_NAMES, VOICE_CATEGORIES, VoxCPMService, 
    add_custom_voice_preset, export_mp3, preprocess_reference_audio,
    extract_audio_from_video, extract_best_speech_segment, extract_segment_audio
)
from utils.file_utils import get_temp_path
from utils.logger import logger
from gui.svg_icons import get_svg_icon
from core.models import (
    PERSONA_CHOICES, EMOTION_CHOICES, STYLE_CHOICES, PERSONA_DEFAULT_VOICE,
    normalize_persona, normalize_emotion,
    SpeakerProfile, Segment, ProjectData
)

# ==================== QT KHMER FONT REGISTRATION ====================
_loaded_qt_fonts = set()
_fonts_ensured = False

def ensure_qt_fonts():
    """Register all project TrueType fonts into Qt QFontDatabase for native HarfBuzz Khmer Unicode shaping."""
    global _fonts_ensured
    if _fonts_ensured:
        return
    font_dir = os.path.abspath('fonts')
    if os.path.exists(font_dir):
        for f in os.listdir(font_dir):
            if f.endswith('.ttf') or f.endswith('.otf'):
                p = os.path.join(font_dir, f)
                if p not in _loaded_qt_fonts:
                    QtGui.QFontDatabase.addApplicationFont(p)
                    _loaded_qt_fonts.add(p)
    _fonts_ensured = True

from core.subtitle_anim import apply_subtitle_animation_effect
from core.mask_engine import apply_mask_item_to_frame, check_mask_active, MASK_TYPES

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

AVAILABLE_KHMER_FONTS = [
    "Kantumruy Pro",
    "Noto Sans Khmer",
    "Battambang",
    "Moul",
    "Koulen",
    "Bokor",
    "Fasthand",
    "Bayon",
    "Dangrek",
    "Siemreap",
    "Chenla",
    "Koh Santepheap",
    "Preahvihear",
    "Suwannaphum",
    "Odor Mean Chey",
    "Content",
    "Khmer Sangam MN"
]

def resolve_qt_font_name(requested_name: str) -> str:
    """Map dropdown selection to exact registered Qt font family name."""
    ensure_qt_fonts()
    req = str(requested_name).strip()
    req_lower = req.lower()
    for key, fam in _FONT_FAMILY_MAPPING.items():
        if key in req_lower:
            return fam
    return req if req else "Kantumruy Pro"


def format_capcut_timecode(seconds: float, fps: float = 30.0) -> str:
    """Format seconds into SMPTE timecode (HH:MM:SS:FF) matching CapCut."""
    total_frames = int(max(0.0, seconds) * fps)
    frames = total_frames % int(fps)
    total_secs = int(max(0.0, seconds))
    secs = total_secs % 60
    mins = (total_secs // 60) % 60
    hours = total_secs // 3600
    return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"


# ==================== FIXED LEFT TRACK HEADERS WIDGET ====================
class TimelineTrackHeaderWidget(QWidget):
    """
    Fixed Left-Side Track Headers Column for Timeline (Track Names & Icons).
    Anchored to the left of the scroll area so track titles never jitter or stamp onto the canvas.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(116)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setStyleSheet("background-color: #0b0f19; border-right: 1px solid #1a2238;")

    def _get_canvas(self):
        te = self.parent()
        if hasattr(te, 'waveform_canvas'):
            return te.waveform_canvas
        return None

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        
        w = self.width()
        h = max(200, self.height())
        
        canvas = self._get_canvas()
        if canvas and hasattr(canvas, 'compute_track_layout'):
            layout = canvas.compute_track_layout(h)
        else:
            layout = {
                'vid': (24, 68),
                'text': None,
                'logo': None,
                'sub': (96, h - 98),
                'blur': None
            }
        
        vid_top, vid_h = layout['vid']
        sub_top, sub_h = layout['sub']
        
        # 1. Background
        painter.fillRect(0, 0, w, h, QtGui.QColor("#080c16"))
        
        # 2. Time Ruler Corner (y: 0 -> 22)
        painter.fillRect(0, 0, w, 22, QtGui.QColor("#0f1526"))
        painter.setPen(QtGui.QColor("#1f2a44"))
        painter.drawLine(0, 22, w, 22)
        painter.setPen(QtGui.QColor("#64748b"))
        painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
        painter.drawText(QtCore.QRect(8, 0, w - 16, 22), Qt.AlignLeft | Qt.AlignVCenter, "TRACKS")
        
        # 3. Track 0: 🎬 Video (V1) (y: vid_top, h: vid_h)
        painter.fillRect(0, vid_top, w, vid_h, QtGui.QColor("#081c22"))
        painter.setPen(QtGui.QColor("#0e3c45"))
        painter.drawLine(0, vid_top + vid_h, w, vid_top + vid_h)
        
        # Video Header Badge (CapCut Dark Teal Style)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 78, 87, 230)))
        painter.setPen(QtGui.QPen(QtGui.QColor("#00a3ad"), 1.2))
        painter.drawRoundedRect(QtCore.QRect(6, vid_top + 4, w - 12, vid_h - 8), 4, 4)
        painter.setPen(QtGui.QColor("#f8fafc"))
        painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
        painter.drawText(QtCore.QRect(12, vid_top + 6, w - 24, 20), Qt.AlignLeft | Qt.AlignVCenter, "🎬 Video V1")
        
        # Sub-badge showing CapCut track type
        painter.setPen(QtGui.QColor("#38bdf8"))
        painter.setFont(QtGui.QFont("Menlo" if sys.platform == "darwin" else "Courier New", 7))
        painter.drawText(QtCore.QRect(12, vid_top + 34, w - 24, 16), Qt.AlignLeft | Qt.AlignVCenter, "Main Media")
        
        # 4. Optional Track 1: 📝 Text Overlay
        if layout.get('text'):
            t_top, t_h = layout['text']
            painter.fillRect(0, t_top, w, t_h, QtGui.QColor("#0a0f1d"))
            painter.setPen(QtGui.QColor("#161f36"))
            painter.drawLine(0, t_top + t_h, w, t_top + t_h)
            
            painter.setBrush(QtGui.QBrush(QtGui.QColor(6, 78, 59, 230)))
            painter.setPen(QtGui.QPen(QtGui.QColor("#10b981"), 1.2))
            painter.drawRoundedRect(QtCore.QRect(6, t_top + 1, w - 12, t_h - 2), 4, 4)
            painter.setPen(QtGui.QColor("#34d399"))
            painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
            painter.drawText(QtCore.QRect(12, t_top + 1, w - 24, t_h - 2), Qt.AlignLeft | Qt.AlignVCenter, "📝 Text")
        
        # 5. Optional Track 2: 🖼️ Logo Overlay
        if layout.get('logo'):
            l_top, l_h = layout['logo']
            painter.fillRect(0, l_top, w, l_h, QtGui.QColor("#0c111e"))
            painter.setPen(QtGui.QColor("#161f36"))
            painter.drawLine(0, l_top + l_h, w, l_top + l_h)
            
            painter.setBrush(QtGui.QBrush(QtGui.QColor(120, 53, 15, 230)))
            painter.setPen(QtGui.QPen(QtGui.QColor("#f59e0b"), 1.2))
            painter.drawRoundedRect(QtCore.QRect(6, l_top + 1, w - 12, l_h - 2), 4, 4)
            painter.setPen(QtGui.QColor("#fbbf24"))
            painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
            painter.drawText(QtCore.QRect(12, l_top + 1, w - 24, l_h - 2), Qt.AlignLeft | Qt.AlignVCenter, "🖼 Logo")
        
        # 6. Track 3: 💬 Subtitle / Audio Waveform (Dynamic Height!)
        painter.fillRect(0, sub_top, w, sub_h, QtGui.QColor("#080c16"))
        painter.setPen(QtGui.QColor("#161f36"))
        painter.drawLine(0, sub_top + sub_h, w, sub_top + sub_h)
        
        # Subtitle Header Badge
        painter.setBrush(QtGui.QBrush(QtGui.QColor(7, 89, 133, 230)))
        painter.setPen(QtGui.QPen(QtGui.QColor("#0ea5e9"), 1.2))
        painter.drawRoundedRect(QtCore.QRect(6, sub_top + 4, w - 12, 22), 4, 4)
        painter.setPen(QtGui.QColor("#38bdf8"))
        painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
        painter.drawText(QtCore.QRect(12, sub_top + 4, w - 24, 22), Qt.AlignLeft | Qt.AlignVCenter, "💬 Subtitle")
        
        # 7. Optional Track 4: 🔍 Blur Mask (Pinned to bottom)
        if layout.get('blur'):
            b_top, b_h = layout['blur']
            painter.fillRect(0, b_top, w, b_h, QtGui.QColor("#0c111e"))
            painter.setPen(QtGui.QColor("#161f36"))
            painter.drawLine(0, b_top + b_h, w, b_top + b_h)
            
            painter.setBrush(QtGui.QBrush(QtGui.QColor(136, 19, 55, 230)))
            painter.setPen(QtGui.QPen(QtGui.QColor("#f43f5e"), 1.2))
            painter.drawRoundedRect(QtCore.QRect(6, b_top + 2, w - 12, b_h - 4), 4, 4)
            painter.setPen(QtGui.QColor("#fb7185"))
            painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
            painter.drawText(QtCore.QRect(12, b_top + 2, w - 24, b_h - 4), Qt.AlignLeft | Qt.AlignVCenter, "🔍 Blur")
        
        # Vertical divider line to separate header from scrollable tracks
        painter.setPen(QtGui.QColor("#1f2a44"))
        painter.drawLine(w - 1, 0, w - 1, h)


# ==================== ASYNCHRONOUS VIDEO THUMBNAIL LOADER ====================
class ThumbnailLoaderThread(QThread):
    thumbnail_ready = Signal(str, float, float, QtGui.QImage)  # (clip_path, t_rel, t_global, image)
    all_finished = Signal()

    def __init__(self, video_path: str = None, clips: list = None, duration_sec: float = 0.0, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.clips = clips or []
        self.duration_sec = duration_sec
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            import cv2
            from services.cache_service import CacheService
            work_list = []
            if self.clips:
                for c in self.clips:
                    c_p = c.get("path")
                    c_st = c.get("start", 0.0)
                    c_dur = c.get("duration", 0.0)
                    if c_p and os.path.exists(c_p) and c_dur > 0:
                        work_list.append((c_p, c_st, c_dur))
            if not work_list and self.video_path and os.path.exists(self.video_path):
                work_list.append((self.video_path, 0.0, max(1.0, self.duration_sec)))
                
            if not work_list:
                return
                
            thumb_h = 36
            # Fast Pass 1: Decode Frame 0 for EVERY clip immediately (<3ms)!
            for path, start_offset, clip_dur in work_list:
                if self._is_cancelled:
                    break
                try:
                    cached_bytes = CacheService.load_cached_thumbnail(path, 0.0, thumb_h)
                    if cached_bytes:
                        qimg = QtGui.QImage.fromData(cached_bytes, "JPG")
                        if not qimg.isNull():
                            self.thumbnail_ready.emit(path, 0.0, start_offset, qimg)
                            continue
                    cap0 = cv2.VideoCapture(path)
                    if cap0.isOpened():
                        ret0, frame0 = cap0.read()
                        if ret0 and frame0 is not None:
                            fh0, fw0 = frame0.shape[:2]
                            aspect0 = fw0 / max(1, fh0)
                            thumb_w0 = max(20, min(75, int(thumb_h * aspect0)))
                            small0 = cv2.resize(frame0, (thumb_w0, thumb_h), interpolation=cv2.INTER_AREA)
                            _, buf0 = cv2.imencode(".jpg", small0, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                            CacheService.save_cached_thumbnail(path, 0.0, buf0.tobytes(), thumb_h)
                            rgb0 = cv2.cvtColor(small0, cv2.COLOR_BGR2RGB)
                            qimg0 = QtGui.QImage(rgb0.data, thumb_w0, thumb_h, thumb_w0 * 3, QtGui.QImage.Format_RGB888).copy()
                            self.thumbnail_ready.emit(path, 0.0, start_offset, qimg0)
                        cap0.release()
                except Exception:
                    pass

            # Fast Pass 2: Sequential keyframe decodes across full duration
            for path, start_offset, clip_dur in work_list:
                if self._is_cancelled:
                    break
                try:
                    step_sec = max(1.8, min(6.0, clip_dur / 30.0))
                    t = step_sec
                    cap = None
                    while t < clip_dur and not self._is_cancelled:
                        cur_t = round(start_offset + t, 2)
                        t_rel = round(t, 2)
                        # 1. Check persistent disk cache first (instant <0.1ms!)
                        cached_bytes = CacheService.load_cached_thumbnail(path, t_rel, thumb_h)
                        if cached_bytes:
                            qimg = QtGui.QImage.fromData(cached_bytes, "JPG")
                            if not qimg.isNull():
                                self.thumbnail_ready.emit(path, t_rel, cur_t, qimg)
                                t += step_sec
                                continue

                        # 2. Decode missing frame with OpenCV lazily
                        if cap is None:
                            cap = cv2.VideoCapture(path)
                            if not cap.isOpened():
                                break

                        cap.set(cv2.CAP_PROP_POS_MSEC, t_rel * 1000.0)
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            fh, fw = frame.shape[:2]
                            aspect = fw / max(1, fh)
                            thumb_w = int(thumb_h * aspect)
                            thumb_w = max(20, min(75, thumb_w))
                            small = cv2.resize(frame, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)
                            # Persist to disk cache
                            _, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                            CacheService.save_cached_thumbnail(path, t_rel, buf.tobytes(), thumb_h)
                            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                            qimg = QtGui.QImage(rgb.data, thumb_w, thumb_h, thumb_w * 3, QtGui.QImage.Format_RGB888).copy()
                            self.thumbnail_ready.emit(path, t_rel, cur_t, qimg)
                        t += step_sec
                    if cap:
                        cap.release()
                except Exception:
                    pass
            self.all_finished.emit()
        except Exception:
            pass


# ==================== ASYNCHRONOUS REAL WAVEFORM LOADER ====================
class WaveformLoaderThread(QThread):
    waveform_ready = Signal(list)

    def __init__(self, media_path: str, samples_per_sec: int = 50, parent=None):
        super().__init__(parent)
        self.path = media_path
        self.samples_per_sec = samples_per_sec
        self._is_cancelled = False
        self._proc = None

    def cancel(self):
        self._is_cancelled = True
        if self._proc is not None:
            try:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=0.05)
                except Exception:
                    self._proc.poll()
            except Exception:
                pass

    def run(self):
        if not self.path or not os.path.exists(self.path):
            return
        from services.cache_service import CacheService
        # Check disk cache first (<0.005s)
        cached = CacheService.load_cached_waveform(self.path, self.samples_per_sec)
        if cached:
            self.waveform_ready.emit(cached)
            return

        if self._is_cancelled:
            return

        try:
            import subprocess, numpy as np
            if self._is_cancelled:
                return
            cmd = [
                'ffmpeg', '-y', '-i', self.path,
                '-vn', '-ac', '1', '-ar', '4000',
                '-f', 's16le', '-'
            ]
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            raw, _ = self._proc.communicate()
            if self._proc is not None:
                try:
                    self._proc.poll()
                except Exception:
                    pass
            if self._is_cancelled:
                return
            samples = np.frombuffer(raw, dtype=np.int16)
            dur = len(samples) / 4000.0
            block_size = max(1, int(len(samples) / max(1.0, dur * self.samples_per_sec)))
            peaks = []
            for i in range(0, len(samples), block_size):
                if self._is_cancelled:
                    return
                chunk = samples[i:i + block_size]
                if len(chunk) > 0:
                    val = float(np.max(np.abs(chunk))) / 32768.0
                    peaks.append(round(min(1.0, val), 3))

            CacheService.save_cached_waveform(self.path, peaks, self.samples_per_sec)
            if not self._is_cancelled:
                self.waveform_ready.emit(peaks)
        except Exception as e:
            logger.debug(f"Waveform extraction notice: {e}")


# ==================== MULTI-TRACK INTERACTIVE TIMELINE CANVAS ====================
class AudioWaveformCanvas(QWidget):
    """
    Developer-Grade 4-Track Dynamic Visual Timeline Canvas & Time Ruler.
    Renders 4 discrete tracks:
      Track 0: 📝 Text Overlay (Draggable clip range & resize handles)
      Track 1: 🖼️ Logo Overlay (Draggable clip range & resize handles)
      Track 2: 💬 Subtitles (Dynamic speech waveforms, Khmer text & trim handles)
      Track 3: 🔍 Blur Mask (Auto-speech synchronized blur blocks)
    Features:
      • Dynamic vertical expansion (Waveform grows as splitter is pulled taller)
      • Smooth Hand Tool Panning (Right-click drag / Middle-click drag / Space + Left drag)
      • Interactive Subtitle Trim & Move (Drag edges to trim, body to slide timing)
      • Ctrl/Cmd + Mouse Wheel Zoom
      • Pinned Sticky Badges & Precision Scrubbing Playhead
    """
    seek_requested = Signal(float)
    text_clip_changed = Signal(float, float)            # (start_sec, duration_sec)
    text_selected = Signal(str)                         # text_id
    text_item_timing_changed = Signal(str, float, float) # (text_id, start_sec, duration_sec)
    text_item_delete_requested = Signal(str)            # text_id
    text_item_duplicate_requested = Signal(str)         # text_id
    logo_clip_changed = Signal(float, float)            # (start_sec, duration_sec)
    subtitle_segment_adjusted = Signal(int, float, float) # (seg_idx, start_sec, end_sec)
    blur_item_selected = Signal(str)
    blur_item_timing_changed = Signal(str, float, float)
    blur_item_delete_requested = Signal(str)
    blur_item_duplicate_requested = Signal(str)
    blur_item_reset_rotation_requested = Signal(str)
    blur_item_extend_end_requested = Signal(str)
    in_out_changed = Signal(object, object)              # (in_sec or None, out_sec or None)
    split_requested = Signal(float)                      # (playhead_pos_sec)
    cut_requested = Signal()                             # request video cutter dialog
    video_clip_selected = Signal(int)                   # (clip_idx)
    video_clip_delete_requested = Signal(int)          # (clip_idx)
    video_clip_speed_changed = Signal(int, float)      # (clip_idx, speed)
    in_out_delete_requested = Signal(float, float)     # (in_sec, out_sec)
    trim_left_requested = Signal(float)                 # (sec)
    trim_right_requested = Signal(float)                # (sec)
    clip_dropped_on_timeline = Signal(str, float)       # (video_path, drop_sec) - CapCut Drag & Drop
    video_clips_reordered = Signal(list)                # (reordered_video_clips) - CapCut Drag & Drop Reorder
    transition_clicked = Signal(int)                   # (cut_idx) - Phase 3 Gate 4 Transitions

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._is_drag_hovering = False
        self._drop_indicator_sec = None
        self._drop_insert_idx = None
        self._drag_curr_x = None
        self.segments = []
        self.zoom_factor = 1.0
        self.playhead_pos_sec = 0.0
        self.total_duration_sec = 60.0
        self.selected_clip_idx = None
        
        # In / Out trim markers
        self.in_point_sec = None
        self.out_point_sec = None
        
        # Track items configuration
        self.video_clips = []
        self.video_path = None
        self._thumb_loader = None
        self._wave_loader = None

        self.text_clip = {"start": 0.0, "duration": 5.0, "text": "សង្សារមនុស្សល្អ...", "enabled": True}
        self.text_items = []
        self.active_text_id = None
        self.logo_clip = {"start": 0.0, "duration": 60.0, "name": "Logo", "enabled": False, "full_video": True}
        self.blur_auto_speech = True
        self.blur_enabled = True
        self.blur_items = []
        self.active_blur_id = None
        
        # Dragging & Panning state
        self._drag_target = None  # None or ('playhead', None, None), ('text', handle, None), ('logo', handle, None), ('sub', handle, idx), ('in_point', 'move', None), ('out_point', 'move', None)
        self._drag_start_x = 0
        self._drag_orig_start = 0.0
        self._drag_orig_dur = 0.0
        self._drag_moved = False
        
        self._is_panning = False
        self._pan_start_x = 0
        self._pan_start_scroll = 0
        self._space_pressed = False
        
        # Real Video Filmstrip Thumbnails cache
        self._thumbnails = {}  # {sec: QPixmap}
        self._thumb_loader = None
        
        # Adaptive Track Visibility (Clean CapCut mode by default!)
        self.show_text = False
        self.show_logo = False
        self.show_blur = False

        # Debounced canvas refresh for smooth asynchronous thumbnail loading
        self._thumb_timer = QtCore.QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.timeout.connect(self.update)

        # Real Audio Waveform Peaks Cache
        self.waveform_peaks = []
        self._wave_loader = None

        self.setMinimumHeight(200)
        self.setMinimumWidth(800)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def cleanup(self):
        """Immediately cancel background loaders and detach resources."""
        if hasattr(self, '_thumb_loader') and self._thumb_loader:
            try:
                self._thumb_loader.cancel()
                self._thumb_loader.wait(300)
                self._thumb_loader.deleteLater()
            except Exception:
                pass
            self._thumb_loader = None
        if hasattr(self, '_wave_loader') and self._wave_loader:
            try:
                self._wave_loader.cancel()
                self._wave_loader.wait(300)
                self._wave_loader.deleteLater()
            except Exception:
                pass
            self._wave_loader = None

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def compute_track_layout(self, total_height: int):
        vid_top = 24
        vid_h = 68
        vid_bottom = vid_top + vid_h
        curr_y = vid_bottom + 2
        
        show_t = getattr(self, 'show_text', False)
        show_l = getattr(self, 'show_logo', False)
        show_b = getattr(self, 'show_blur', False)
        
        if show_t:
            text_top = curr_y
            text_h = 22
            curr_y += text_h + 2
        else:
            text_top = None
            text_h = 0
            
        if show_l:
            logo_top = curr_y
            logo_h = 22
            curr_y += logo_h + 2
        else:
            logo_top = None
            logo_h = 0
            
        if show_b:
            blur_h = 24
            blur_top = total_height - blur_h - 2
        else:
            blur_top = None
            blur_h = 0
            
        sub_top = curr_y
        sub_bottom = (blur_top - 2) if show_b else (total_height - 2)
        sub_h = max(50, sub_bottom - sub_top)
        
        return {
            'vid': (vid_top, vid_h),
            'text': (text_top, text_h) if show_t else None,
            'logo': (logo_top, logo_h) if show_l else None,
            'sub': (sub_top, sub_h),
            'blur': (blur_top, blur_h) if show_b else None,
        }

    def _start_thumbnail_loader(self, force_reload: bool = False):
        clips = getattr(self, 'video_clips', None) or []
        target_path = getattr(self, 'video_path', None)
        if not target_path and clips:
            target_path = clips[0].get("path")
            
        if (not target_path and not clips) or self.total_duration_sec <= 0:
            return

        if not hasattr(self, '_thumbnails') or self._thumbnails is None:
            self._thumbnails = {}
        if not hasattr(self, '_clip_thumbnails') or self._clip_thumbnails is None:
            self._clip_thumbnails = {}

        # Immediately pre-seed thumbnails from MediaBin cache for 0ms visual feedback
        try:
            from gui.media_bin_widget import _THUMBNAIL_CACHE
            for c in clips:
                cp = c.get("path")
                if cp and cp in _THUMBNAIL_CACHE and _THUMBNAIL_CACHE[cp]:
                    pix = _THUMBNAIL_CACHE[cp]
                    self._clip_thumbnails.setdefault(cp, {})[0.0] = pix
                    self._thumbnails[c.get("start", 0.0)] = pix
        except Exception:
            pass

        if hasattr(self, '_thumb_loader') and self._thumb_loader and self._thumb_loader.isRunning():
            self._thumb_loader.cancel()
            self._thumb_loader.wait(100)
            self._thumb_loader.deleteLater()
            
        self._thumb_loader = ThumbnailLoaderThread(
            video_path=target_path,
            clips=clips,
            duration_sec=self.total_duration_sec,
            parent=self
        )
        self._thumb_loader.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._thumb_loader.start()

    def _on_thumbnail_ready(self, path: str, t_rel: float, t_global: float, qimg: QtGui.QImage):
        pix = QtGui.QPixmap.fromImage(qimg)
        if not hasattr(self, '_clip_thumbnails'):
            self._clip_thumbnails = {}
        self._clip_thumbnails.setdefault(path, {})[t_rel] = pix
        self._thumbnails[t_global] = pix
        if hasattr(self, '_thumb_timer') and not self._thumb_timer.isActive():
            self._thumb_timer.start(50)
        elif not hasattr(self, '_thumb_timer'):
            self.update()

    def set_video_clips(self, clips: list, video_path: str = None):
        """Set loaded video clips to render on the V1 Video Track."""
        if video_path is not None:
            self.video_path = video_path
        self.video_clips = list(clips or [])
        if not hasattr(self, '_clip_thumbnails'):
            self._clip_thumbnails = {}
        if not hasattr(self, '_thumbnails'):
            self._thumbnails = {}

        if self.video_clips:
            tot = sum(c.get("duration", 0.0) for c in self.video_clips)
            if tot > 0:
                self.total_duration_sec = tot
                pixels_per_sec = 40.0 * getattr(self, 'zoom_factor', 1.0)
                self.setMinimumWidth(int(max(800, self.total_duration_sec * pixels_per_sec)))

            # Instant 0ms Frame 0 extraction for any clip lacking thumbnail so timeline is never blank
            try:
                import cv2
                from gui.media_bin_widget import _THUMBNAIL_CACHE
                for c in self.video_clips:
                    cp = c.get("path")
                    if not cp or not os.path.exists(cp):
                        continue
                    if cp in _THUMBNAIL_CACHE and _THUMBNAIL_CACHE[cp]:
                        self._clip_thumbnails.setdefault(cp, {})[0.0] = _THUMBNAIL_CACHE[cp]
                        self._thumbnails[c.get("start", 0.0)] = _THUMBNAIL_CACHE[cp]
                    elif cp not in self._clip_thumbnails or not self._clip_thumbnails[cp]:
                        cap_init = cv2.VideoCapture(cp)
                        if cap_init.isOpened():
                            ret_i, f_init = cap_init.read()
                            if ret_i and f_init is not None:
                                fh_i, fw_i = f_init.shape[:2]
                                asp_i = fw_i / max(1, fh_i)
                                tw_i = max(20, min(75, int(36 * asp_i)))
                                sm_i = cv2.resize(f_init, (tw_i, 36), interpolation=cv2.INTER_AREA)
                                rgb_i = cv2.cvtColor(sm_i, cv2.COLOR_BGR2RGB)
                                qi_i = QtGui.QImage(rgb_i.data, tw_i, 36, tw_i * 3, QtGui.QImage.Format_RGB888).copy()
                                px_i = QtGui.QPixmap.fromImage(qi_i)
                                self._clip_thumbnails.setdefault(cp, {})[0.0] = px_i
                                self._thumbnails[c.get("start", 0.0)] = px_i
                                _THUMBNAIL_CACHE[cp] = px_i
                            cap_init.release()
            except Exception:
                pass
        else:
            self.video_path = None
            self._thumbnails = {}
            self._clip_thumbnails = {}
            if hasattr(self, '_clip_waveforms'):
                self._clip_waveforms = {}
            self.waveform_peaks = []
            self.selected_clip_idx = None
            self.total_duration_sec = 60.0
            pixels_per_sec = 40.0 * getattr(self, 'zoom_factor', 1.0)
            self.setMinimumWidth(int(max(800, self.total_duration_sec * pixels_per_sec)))
        self._start_thumbnail_loader(force_reload=True)
        self._start_waveform_loader()
        self.update()

    def _start_waveform_loader(self):
        clips = getattr(self, 'video_clips', None) or []
        target_path = getattr(self, 'video_path', None)
        if not clips and target_path:
            clips = [{"path": target_path, "start": 0.0, "duration": self.total_duration_sec}]
        if not clips:
            return

        if not hasattr(self, '_clip_waveforms'):
            self._clip_waveforms = {}

        from services.cache_service import CacheService
        need_bg_load = []
        for c in clips:
            c_p = c.get("path")
            if c_p and os.path.exists(c_p):
                if c_p in self._clip_waveforms and self._clip_waveforms[c_p]:
                    continue
                cached = CacheService.load_cached_waveform(c_p, 50)
                if cached:
                    self._clip_waveforms[c_p] = cached
                else:
                    need_bg_load.append(c_p)

        if self._clip_waveforms:
            first_peaks = list(self._clip_waveforms.values())[0]
            if not getattr(self, 'waveform_peaks', None):
                self.waveform_peaks = first_peaks

        if not need_bg_load:
            self.update()
            return

        if hasattr(self, '_wave_loader') and self._wave_loader and self._wave_loader.isRunning():
            self._wave_loader.cancel()
            self._wave_loader.wait(100)
            self._wave_loader.deleteLater()

        load_path = need_bg_load[0]
        self._wave_loader = WaveformLoaderThread(load_path, samples_per_sec=50, parent=self)
        def _on_wave_ready(peaks, p=load_path):
            if not hasattr(self, '_clip_waveforms'):
                self._clip_waveforms = {}
            self._clip_waveforms[p] = peaks
            if not getattr(self, 'waveform_peaks', None):
                self.waveform_peaks = peaks
            self.update()
        self._wave_loader.waveform_ready.connect(_on_wave_ready)
        self._wave_loader.start()

    def _on_waveform_ready(self, peaks: list):
        self.waveform_peaks = peaks
        self.update()

    def _get_scroll_area(self):
        p = self.parent()
        while p:
            if isinstance(p, QScrollArea):
                return p
            p = p.parent()
        return None

    def set_data(self, segments: list = None, playhead_sec: float = None, zoom: float = None, total_duration: float = None):
        size_changed = False
        if segments is not None:
            self.segments = segments
            if segments:
                last_end = max(seg.get("end", 0.0) for seg in segments)
                self.total_duration_sec = max(30.0, last_end + 5.0)
                if self.logo_clip.get("full_video", True):
                    self.logo_clip["start"] = 0.0
                    self.logo_clip["duration"] = self.total_duration_sec
                size_changed = True
        if total_duration is not None and total_duration > 0:
            self.total_duration_sec = total_duration
            if self.logo_clip.get("full_video", True):
                self.logo_clip["start"] = 0.0
                self.logo_clip["duration"] = self.total_duration_sec
            size_changed = True
        if playhead_sec is not None:
            self.playhead_pos_sec = max(0.0, playhead_sec)
        if zoom is not None:
            self.zoom_factor = max(0.1, min(6.0, zoom))
            size_changed = True
            
        if size_changed or not hasattr(self, '_cached_canvas_w'):
            pixels_per_sec = 40.0 * self.zoom_factor
            calc_w = int(max(800, self.total_duration_sec * pixels_per_sec))
            if getattr(self, '_cached_canvas_w', None) != calc_w:
                self._cached_canvas_w = calc_w
                self.setMinimumWidth(calc_w)
        self.update()

    def set_text_clip(self, start_sec: float, duration_sec: float, text: str = None):
        self.text_clip["start"] = max(0.0, start_sec)
        self.text_clip["duration"] = max(0.5, duration_sec)
        if text:
            self.text_clip["text"] = text
        self.update()

    def set_text_items(self, items: list, active_id: str = None):
        self.text_items = items or []
        self.active_text_id = active_id
        if self.text_items:
            cur = next((t for t in self.text_items if t.get("id") == active_id), self.text_items[0])
            self.text_clip["start"] = cur.get("start_sec", 0.0)
            self.text_clip["duration"] = max(0.5, cur.get("duration_sec", 5.0))
            self.text_clip["text"] = cur.get("text", "")
        self.update()

    def set_in_point(self, sec: float = None):
        if sec is None:
            sec = self.playhead_pos_sec
        sec = max(0.0, float(sec))
        if self.out_point_sec is not None and sec >= self.out_point_sec:
            sec = max(0.0, self.out_point_sec - 0.1)
        self.in_point_sec = round(sec, 2)
        self.update()
        self.in_out_changed.emit(self.in_point_sec, self.out_point_sec)

    def set_out_point(self, sec: float = None):
        if sec is None:
            sec = self.playhead_pos_sec
        sec = min(self.total_duration_sec, float(sec))
        if self.in_point_sec is not None and sec <= self.in_point_sec:
            sec = min(self.total_duration_sec, self.in_point_sec + 0.1)
        self.out_point_sec = round(sec, 2)
        self.update()
        self.in_out_changed.emit(self.in_point_sec, self.out_point_sec)

    def clear_in_out(self):
        self.in_point_sec = None
        self.out_point_sec = None
        self.update()
        self.in_out_changed.emit(None, None)

    def _show_ruler_context_menu(self, sec_at_x: float, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0b1329;
                color: #f1f5f9;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item {
                padding: 7px 22px 7px 12px;
                border-radius: 5px;
                font-size: 12px;
                font-family: 'Kantumruy Pro', sans-serif;
                font-weight: 500;
            }
            QMenu::item:selected {
                background-color: #0284c7;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #1e293b;
                margin: 5px 6px;
            }
        """)
        split_act = menu.addAction(f"✂️ កាត់ត្រង់នេះ (Split at {sec_at_x:.2f}s)\tCtrl+B")
        trim_l_act = menu.addAction(f"⇤ Trim Left (កាត់ផ្នែកឆ្វេងដល់ {sec_at_x:.2f}s)\tQ")
        trim_r_act = menu.addAction(f"Trim Right ⇥ (កាត់ផ្នែកស្តាំពី {sec_at_x:.2f}s)\tW")
        
        del_range_act = None
        if self.in_point_sec is not None and self.out_point_sec is not None:
            del_range_act = menu.addAction(f"🗑️ លុបចន្លោះ In-Out ({self.in_point_sec:.2f}s ➔ {self.out_point_sec:.2f}s)\tDel")

        menu.addSeparator()
        in_act = menu.addAction(f"[ កំណត់ Mark In ត្រង់នេះ ({sec_at_x:.2f}s)\tI")
        out_act = menu.addAction(f"] កំណត់ Mark Out ត្រង់នេះ ({sec_at_x:.2f}s)\tO")
        if self.in_point_sec is not None or self.out_point_sec is not None:
            clear_act = menu.addAction("✕ សម្អាត In/Out (Clear Selection)")
        else:
            clear_act = None
            
        menu.addSeparator()
        cut_dialog_act = menu.addAction("✂️ បើកផ្ទាំងកាត់តវីដេអូ... (Video Cutter Studio)\tCtrl+K")

        act = menu.exec_(global_pos) if hasattr(menu, 'exec_') else menu.exec(global_pos)
        if act == split_act:
            self.split_requested.emit(sec_at_x)
        elif act == trim_l_act:
            self.trim_left_requested.emit(sec_at_x)
        elif act == trim_r_act:
            self.trim_right_requested.emit(sec_at_x)
        elif del_range_act and act == del_range_act:
            self.in_out_delete_requested.emit(self.in_point_sec, self.out_point_sec)
        elif act == in_act:
            self.set_in_point(sec_at_x)
        elif act == out_act:
            self.set_out_point(sec_at_x)
        elif act == cut_dialog_act:
            self.cut_requested.emit()
        elif clear_act and act == clear_act:
            self.clear_in_out()

    def _show_video_context_menu(self, sec_at_x: float, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0b1329;
                color: #f1f5f9;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item {
                padding: 7px 22px 7px 12px;
                border-radius: 5px;
                font-size: 12px;
                font-family: 'Kantumruy Pro', sans-serif;
                font-weight: 500;
            }
            QMenu::item:selected {
                background-color: #0284c7;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #1e293b;
                margin: 5px 6px;
            }
        """)

        # Identify which clip is at sec_at_x
        target_clip_idx = None
        v_clips = getattr(self, 'video_clips', [])
        for c_i, c in enumerate(v_clips):
            c_st = c.get("start", 0.0)
            c_dur = c.get("duration", 0.0)
            if c_st <= sec_at_x <= (c_st + c_dur):
                target_clip_idx = c_i
                break

        if target_clip_idx is not None:
            self.selected_clip_idx = target_clip_idx
            self.update()

        split_act = menu.addAction(f"✂️ កាត់/បំបែកត្រង់នេះ (Split at {sec_at_x:.2f}s)\tCtrl+B")

        del_clip_act = None
        if target_clip_idx is not None and len(v_clips) > 0:
            c = v_clips[target_clip_idx]
            c_label = c.get("name", f"Clip {target_clip_idx + 1}")
            del_clip_act = menu.addAction(f"🗑️ លុប '{c_label}' (Delete Clip)\tDel")
            
            cur_speed = float(c.get("speed", 1.0))
            speed_menu = menu.addMenu(f"⚡ ល្បឿន / Speed ({cur_speed:.2f}x)")
            for spd_val, spd_lbl in [
                (0.50, "0.50x (យឺត / Slow)"),
                (0.75, "0.75x"),
                (1.00, "1.00x (ធម្មតា / Normal)"),
                (1.25, "1.25x"),
                (1.50, "1.50x"),
                (2.00, "2.00x (លឿន / Fast)")
            ]:
                act_spd = speed_menu.addAction(spd_lbl)
                act_spd.setCheckable(True)
                act_spd.setChecked(abs(cur_speed - spd_val) < 0.05)
                act_spd.triggered.connect(lambda chk, s=spd_val, idx=target_clip_idx: self.video_clip_speed_changed.emit(idx, s))

        del_range_act = None
        if self.in_point_sec is not None and self.out_point_sec is not None:
            del_range_act = menu.addAction(f"🗑️ លុបចន្លោះ In-Out ({self.in_point_sec:.2f}s ➔ {self.out_point_sec:.2f}s)\tDel")

        menu.addSeparator()
        trim_l_act = menu.addAction(f"⇤ Trim Left (កាត់ផ្នែកឆ្វេងដល់ {sec_at_x:.2f}s)\tQ")
        trim_r_act = menu.addAction(f"Trim Right ⇥ (កាត់ផ្នែកស្តាំពី {sec_at_x:.2f}s)\tW")

        menu.addSeparator()
        in_act = menu.addAction(f"[ កំណត់ Mark In ត្រង់នេះ ({sec_at_x:.2f}s)\tI")
        out_act = menu.addAction(f"] កំណត់ Mark Out ត្រង់នេះ ({sec_at_x:.2f}s)\tO")
        if self.in_point_sec is not None or self.out_point_sec is not None:
            clear_act = menu.addAction("✕ សម្អាត In/Out (Clear Selection)")
        else:
            clear_act = None

        menu.addSeparator()
        cut_dialog_act = menu.addAction("✂️ បើកផ្ទាំងកាត់តវីដេអូ... (Video Cutter Studio)\tCtrl+K")

        act = menu.exec_(global_pos) if hasattr(menu, 'exec_') else menu.exec(global_pos)
        if act == split_act:
            self.split_requested.emit(sec_at_x)
        elif del_clip_act and act == del_clip_act:
            self.video_clip_delete_requested.emit(target_clip_idx)
        elif del_range_act and act == del_range_act:
            self.in_out_delete_requested.emit(self.in_point_sec, self.out_point_sec)
        elif act == trim_l_act:
            self.trim_left_requested.emit(sec_at_x)
        elif act == trim_r_act:
            self.trim_right_requested.emit(sec_at_x)
        elif act == in_act:
            self.set_in_point(sec_at_x)
        elif act == out_act:
            self.set_out_point(sec_at_x)
        elif clear_act and act == clear_act:
            self.clear_in_out()
        elif act == cut_dialog_act:
            self.cut_requested.emit()

    def _show_text_context_menu(self, t_id: str, global_pos):
        menu = QMenu(self)
        sel_act = menu.addAction("Select Text")
        dup_act = menu.addAction("Duplicate Text")
        del_act = menu.addAction("Delete Text")
        extend_act = menu.addAction("Extend to Video End")
        
        act = menu.exec_(global_pos) if hasattr(menu, 'exec_') else menu.exec(global_pos)
        if act == sel_act:
            self.active_text_id = t_id
            self.text_selected.emit(t_id)
            self.update()
        elif act == del_act:
            self.text_item_delete_requested.emit(t_id)
        elif act == dup_act:
            self.text_item_duplicate_requested.emit(t_id)
        elif act == extend_act:
            tot = getattr(self, 'total_duration_sec', 60.0)
            t_item = next((t for t in getattr(self, 'text_items', []) if t.get("id") == t_id), None)
            st = t_item.get("start_sec", 0.0) if t_item else self.text_clip["start"]
            new_dur = max(0.5, tot - st)
            if t_item: t_item["duration_sec"] = new_dur
            self.text_clip["duration"] = new_dur
            self.text_clip_changed.emit(st, new_dur)
            self.text_item_timing_changed.emit(t_id, st, new_dur)
            self.update()

    def _show_logo_context_menu(self, global_pos):
        menu = QMenu(self)
        extend_act = menu.addAction("Extend Full Video (ពេញមួយវីដេអូ)")
        reset_act = menu.addAction("Reset Timing (0s - 10s)")
        
        act = menu.exec_(global_pos) if hasattr(menu, 'exec_') else menu.exec(global_pos)
        if act == extend_act:
            self.set_logo_clip(0.0, self.total_duration_sec, full_video=True)
            self.logo_clip_changed.emit(0.0, self.total_duration_sec)
        elif act == reset_act:
            self.set_logo_clip(0.0, 10.0, full_video=False)
            self.logo_clip_changed.emit(0.0, 10.0)

    def set_logo_clip(self, start_sec: float, duration_sec: float, name: str = None, full_video: bool = None):
        if full_video is not None:
            self.logo_clip["full_video"] = full_video
        
        if self.logo_clip.get("full_video", False) or duration_sec <= 0:
            self.logo_clip["start"] = 0.0
            self.logo_clip["duration"] = max(1.0, getattr(self, "total_duration_sec", 60.0))
        else:
            self.logo_clip["start"] = max(0.0, start_sec)
            self.logo_clip["duration"] = max(0.5, duration_sec)
            
        if name:
            self.logo_clip["name"] = name
        self.update()

    def set_blur_auto_speech(self, enabled: bool):
        self.blur_auto_speech = enabled
        self.update()

    def set_blur_items(self, items: list, active_id: str = None):
        self.blur_items = items or []
        self.active_blur_id = active_id
        self.update()

    def _show_blur_context_menu(self, b_id: str, global_pos):
        menu = QMenu(self)
        del_act = menu.addAction("Delete Blur")
        dup_act = menu.addAction("Duplicate Blur")
        reset_rot_act = menu.addAction("Reset Rotation (0°)")
        extend_act = menu.addAction("Extend to Video End")
        
        act = menu.exec_(global_pos) if hasattr(menu, 'exec_') else menu.exec(global_pos)
        if act == del_act:
            self.blur_item_delete_requested.emit(b_id)
        elif act == dup_act:
            self.blur_item_duplicate_requested.emit(b_id)
        elif act == reset_rot_act:
            self.blur_item_reset_rotation_requested.emit(b_id)
        elif act == extend_act:
            self.blur_item_extend_end_requested.emit(b_id)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        
        width = self.width()
        height = max(172, self.height())
        pixels_per_sec = 40.0 * self.zoom_factor
        # Visible horizontal bounds from the parent QScrollArea viewport
        sa = self._get_scroll_area()
        if sa and sa.viewport():
            v_left = sa.horizontalScrollBar().value()
            v_right = v_left + sa.viewport().width()
        else:
            visible_rect = event.rect()
            v_left = visible_rect.left()
            v_right = visible_rect.right()
        
        # Calculate dynamic track heights based on adaptive track visibility
        layout = self.compute_track_layout(height)
        vid_top, vid_h = layout['vid']
        sub_top, sub_h = layout['sub']
        center_y = sub_top + sub_h / 2.0
        max_amp = max(8.0, sub_h / 2.0 - 7.0)
        
        # 1. Canvas Background
        painter.fillRect(0, 0, width, height, QtGui.QColor("#0a0e1a"))
        
        # 2. Time Ruler Bar (y: 0 -> 22)
        painter.fillRect(0, 0, width, 22, QtGui.QColor("#0f1526"))
        painter.setPen(QtGui.QColor("#1f2a44"))
        painter.drawLine(0, 22, width, 22)
        
        # 2. Time Ruler Bar: CapCut Adaptive Multi-Level Tick & Timestamp System
        if pixels_per_sec >= 160:       # High zoom (frame / sub-second level)
            ruler_step = 0.5
            sub_step = 0.1
        elif pixels_per_sec >= 75:      # Medium-high zoom
            ruler_step = 1.0
            sub_step = 0.2
        elif pixels_per_sec >= 30:      # Standard 1x zoom
            ruler_step = 2.0
            sub_step = 0.5
        elif pixels_per_sec >= 12:      # Zoomed out (~0.3x)
            ruler_step = 5.0
            sub_step = 1.0
        elif pixels_per_sec >= 5:       # Multi-minute sequence
            ruler_step = 15.0
            sub_step = 5.0
        elif pixels_per_sec >= 2:       # Long video (30-60 min)
            ruler_step = 30.0
            sub_step = 10.0
        else:                           # Very long sequence (> 1 hour)
            ruler_step = 60.0
            sub_step = 30.0

        # Draw subtle minor sub-ticks first
        if sub_step > 0:
            start_sub = max(0.0, math.floor((v_left - 30) / (pixels_per_sec * sub_step)) * sub_step)
            end_sub = min(self.total_duration_sec, math.ceil((v_right + 30) / (pixels_per_sec * sub_step)) * sub_step)
            painter.setPen(QtGui.QColor("#24314c"))
            cur_sub = start_sub
            while cur_sub <= end_sub + 0.0001:
                sub_x = int(cur_sub * pixels_per_sec)
                painter.drawLine(sub_x, 16, sub_x, 22)
                cur_sub += sub_step

        # Draw major ticks and CapCut time labels
        start_maj = max(0.0, math.floor((v_left - 60) / (pixels_per_sec * ruler_step)) * ruler_step)
        end_maj = min(self.total_duration_sec, math.ceil((v_right + 60) / (pixels_per_sec * ruler_step)) * ruler_step)
        cur_maj = start_maj
        font_major = QtGui.QFont("Menlo" if sys.platform == "darwin" else "Courier New", 8, QtGui.QFont.Bold)
        font_normal = QtGui.QFont("Menlo" if sys.platform == "darwin" else "Courier New", 8, QtGui.QFont.Normal)

        while cur_maj <= end_maj + 0.0001:
            maj_x = int(cur_maj * pixels_per_sec)
            is_primary = (abs(cur_maj % (ruler_step * 2)) < 0.0001) or (cur_maj == 0.0)
            painter.setPen(QtGui.QColor("#00f0ff" if is_primary else "#4a5a7a"))
            painter.drawLine(maj_x, 7 if is_primary else 12, maj_x, 22)

            m = int(cur_maj // 60)
            s = cur_maj % 60
            if ruler_step < 1.0:
                txt = f"{m:02d}:{s:04.1f}"
            else:
                txt = f"{m:02d}:{int(s):02d}"

            painter.setFont(font_major if is_primary else font_normal)
            painter.setPen(QtGui.QColor("#cbd5e1" if is_primary else "#94a3b8"))
            painter.drawText(maj_x + 3, 16, txt)
            cur_maj += ruler_step

        # 3. Track Lane Backgrounds & Boundaries
        # Lane 0: 🎬 Video (V1) (y: vid_top, h: vid_h)
        painter.fillRect(0, vid_top, width, vid_h, QtGui.QColor("#0c1022"))
        painter.setPen(QtGui.QColor("#1e293b"))
        painter.drawLine(0, vid_top + vid_h, width, vid_top + vid_h)
        
        # Lane 1: 📝 Text (V2) (if enabled)
        if layout.get('text'):
            t_top, t_h = layout['text']
            painter.fillRect(0, t_top, width, t_h, QtGui.QColor("#0a0f1d"))
            painter.setPen(QtGui.QColor("#161f36"))
            painter.drawLine(0, t_top + t_h, width, t_top + t_h)
            
        # Lane 2: 🖼️ Logo (V3) (if enabled)
        if layout.get('logo'):
            l_top, l_h = layout['logo']
            painter.fillRect(0, l_top, width, l_h, QtGui.QColor("#0c111e"))
            painter.setPen(QtGui.QColor("#161f36"))
            painter.drawLine(0, l_top + l_h, width, l_top + l_h)
            
        # Lane 3: 💬 Subtitle (Dynamic Height! y: sub_top, h: sub_h)
        painter.fillRect(0, sub_top, width, sub_h, QtGui.QColor("#090d18"))
        painter.setPen(QtGui.QColor("#161f36"))
        painter.drawLine(0, sub_top + sub_h, width, sub_top + sub_h)
        # Centerline for waveform in Track 3
        painter.setPen(QtGui.QPen(QtGui.QColor("#182238"), 1, Qt.DashLine))
        painter.drawLine(0, int(center_y), width, int(center_y))

        # Lane 4: 🔍 Blur Mask (if enabled)
        if layout.get('blur'):
            b_top, b_h = layout['blur']
            painter.fillRect(0, b_top, width, b_h, QtGui.QColor("#0c111e"))
            painter.setPen(QtGui.QColor("#161f36"))
            painter.drawLine(0, b_top + b_h, width, b_top + b_h)

        # 4. RENDER TRACK 0: 🎬 Video (V1) Clips with Authentic CapCut Design
        v_clips = getattr(self, 'video_clips', None)
        if v_clips is None and getattr(self, 'total_duration_sec', 0.0) > 0 and getattr(self, 'video_path', None) and os.path.exists(self.video_path):
            c_name = os.path.basename(self.video_path)
            v_clips = [{
                "name": c_name,
                "start": 0.0,
                "duration": self.total_duration_sec,
                "path": self.video_path
            }]

        if not v_clips:
            painter.setPen(QtGui.QColor("#475569"))
            painter.setFont(QtGui.QFont("Kantumruy Pro", 8))
            empty_rect = QtCore.QRect(12, vid_top, max(100, width - 24), vid_h)
            painter.drawText(empty_rect, Qt.AlignVCenter | Qt.AlignLeft, "🎬 គ្មាន Clip ទេ (អូសទម្លាក់វីដេអូ ឬចុច '➕ Timeline' ពី Media Bin)")
        else:
            header_h = 18
            wave_h = 18
            thumb_h = max(16, vid_h - header_h - wave_h)
            thumb_y = vid_top + header_h
            wave_y = vid_top + vid_h - wave_h

            for idx, clip in enumerate(v_clips):
                c_name = clip.get("name", f"Clip {idx + 1}")
                c_st = clip.get("start", 0.0)
                c_dur = max(0.1, clip.get("duration", self.total_duration_sec))
                c_x1 = int(c_st * pixels_per_sec)
                c_w = max(16, int(c_dur * pixels_per_sec))
                c_x2 = c_x1 + c_w

                if c_x2 < v_left or c_x1 > v_right:
                    continue

                clip_rect = QtCore.QRect(c_x1, vid_top, c_w, vid_h)

                painter.save()
                painter.setClipRect(clip_rect)

                # CapCut deep teal-black clip base
                painter.fillRect(clip_rect, QtGui.QColor("#082025"))

                # 1. CapCut Solid Teal Header Bar (Top 18px)
                header_rect = QtCore.QRect(c_x1, vid_top, c_w, header_h)
                painter.fillRect(header_rect, QtGui.QColor("#004e57"))
                painter.setPen(QtGui.QColor("#00b4c8"))
                painter.drawLine(c_x1, vid_top, c_x2, vid_top)

                # File Name Label (Left of Header with Video Icon)
                name_rect = QtCore.QRect(c_x1 + 6, vid_top, max(10, c_w - 92), header_h)
                painter.setPen(QtGui.QColor("#ffffff"))
                painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
                painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, f"🎬 {c_name}")

                # SMPTE Timecode Badge (Right of Header: e.g. 00:04:33:23)
                if c_w >= 85:
                    tc_str = format_capcut_timecode(c_dur, fps=30.0)
                    tc_w = 78
                    tc_x = c_x2 - tc_w - 4
                    if tc_x < c_x1 + 20:
                        tc_x = c_x1 + 20
                    tc_badge = QtCore.QRect(tc_x, vid_top + 2, tc_w, header_h - 4)
                    painter.setBrush(QtGui.QBrush(QtGui.QColor("#00252b")))
                    painter.setPen(QtGui.QPen(QtGui.QColor("#007380"), 1))
                    painter.drawRoundedRect(tc_badge, 3, 3)
                    painter.setPen(QtGui.QColor("#38bdf8"))
                    painter.setFont(QtGui.QFont("Menlo" if sys.platform == "darwin" else "Courier New", 7, QtGui.QFont.Bold))
                    painter.drawText(tc_badge, Qt.AlignCenter, tc_str)

                # 2. Middle Continuous Filmstrip Thumbnails
                c_p = clip.get("path")
                clip_thumbs = getattr(self, '_clip_thumbnails', {}).get(c_p)
                if not clip_thumbs:
                    clip_thumbs = {t: pix for t, pix in getattr(self, '_thumbnails', {}).items() if (c_st - 2.0) <= t <= (c_st + c_dur + 2.0)}
                if not clip_thumbs and hasattr(self, '_clip_thumbnails'):
                    for th_dict in self._clip_thumbnails.values():
                        if th_dict:
                            clip_thumbs = th_dict
                            break

                if clip_thumbs:
                    sorted_times = sorted(clip_thumbs.keys())
                    sample_pix = clip_thumbs[sorted_times[0]]
                    
                    fw = sample_pix.width()
                    fh = max(1, sample_pix.height())
                    aspect = fw / float(fh)
                    tile_w = max(24, min(75, int(thumb_h * aspect)))
                    
                    render_start = max(c_x1, v_left - tile_w)
                    render_end = min(c_x2, v_right + tile_w)
                    first_idx = max(0, int((render_start - c_x1) // tile_w))
                    curr_x = c_x1 + first_idx * tile_w
                    
                    while curr_x < render_end:
                        t_tile = (curr_x + tile_w * 0.5 - c_x1) / max(0.001, pixels_per_sec)
                        b_idx = bisect.bisect_left(sorted_times, t_tile)
                        if b_idx == 0:
                            best_t = sorted_times[0]
                        elif b_idx >= len(sorted_times):
                            best_t = sorted_times[-1]
                        else:
                            t0 = sorted_times[b_idx - 1]
                            t1 = sorted_times[b_idx]
                            best_t = t0 if abs(t0 - t_tile) <= abs(t1 - t_tile) else t1
                            
                        pix = clip_thumbs[best_t]
                        if pix.height() != thumb_h:
                            draw_pix = pix.scaledToHeight(thumb_h, Qt.SmoothTransformation)
                        else:
                            draw_pix = pix
                            
                        painter.drawPixmap(curr_x, thumb_y, draw_pix)
                        
                        # Frame separator line between film frames
                        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 160), 1))
                        painter.drawLine(curr_x + tile_w - 1, thumb_y, curr_x + tile_w - 1, thumb_y + thumb_h)
                        
                        curr_x += tile_w

                # Subtle cinema shade over thumbnails for unified tone
                painter.fillRect(QtCore.QRect(c_x1, thumb_y, c_w, thumb_h), QtGui.QColor(8, 20, 28, 40))

                # 3. Bottom Integrated Audio Waveform Bar (CapCut style cyan bars + amber peak dots)
                wave_rect = QtCore.QRect(c_x1, wave_y, c_w, wave_h)
                painter.fillRect(wave_rect, QtGui.QColor("#00242a"))
                painter.setPen(QtGui.QColor("#003840"))
                painter.drawLine(c_x1, wave_y, c_x2, wave_y)
                
                center_wave = wave_y + wave_h // 2
                painter.setPen(QtGui.QColor("#004d56"))
                painter.drawLine(c_x1, center_wave, c_x2, center_wave)
                
                bar_step = 3
                w_start = max(c_x1, v_left - 10)
                w_end = min(c_x2, v_right + 10)
                peaks_arr = getattr(self, '_clip_waveforms', {}).get(c_p) or getattr(self, 'waveform_peaks', None)
                for bx in range(w_start, w_end, bar_step):
                    t_rel = (bx - c_x1) / max(0.001, pixels_per_sec)
                    if peaks_arr:
                        p_idx = int(t_rel * 50)
                        amp_val = peaks_arr[p_idx] if (0 <= p_idx < len(peaks_arr)) else 0.05
                    else:
                        amp_val = abs(math.sin(t_rel * 4.3) * 0.5 + math.sin(t_rel * 11.2) * 0.35 + math.cos(t_rel * 1.9) * 0.15)
                    bar_len = max(2, int(amp_val * (wave_h - 4)))
                    
                    painter.setPen(QtGui.QColor("#00a8b5"))
                    painter.drawLine(bx, center_wave - bar_len // 2, bx, center_wave + bar_len // 2)
                    
                    # Amber / Orange peak tip matching CapCut screenshot
                    if amp_val > 0.62:
                        painter.setPen(QtGui.QColor("#f97316"))
                        painter.drawPoint(bx, center_wave - bar_len // 2)

                painter.restore()

                # 4. CapCut Outer White Border around selected clip with signature trim grab handles
                is_selected = (idx == getattr(self, 'selected_clip_idx', None))
                if is_selected:
                    # Solid 2px bright white border around selected clip
                    painter.setBrush(Qt.NoBrush)
                    painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 2.0))
                    painter.drawRoundedRect(clip_rect, 4, 4)

                    # White rounded vertical grab handles on left and right edges (CapCut signature)
                    handle_w = 4
                    handle_h = 24
                    handle_y = vid_top + (vid_h - handle_h) // 2
                    painter.setBrush(QtGui.QBrush(QtGui.QColor("#ffffff")))
                    painter.setPen(Qt.NoPen)
                    painter.drawRoundedRect(QtCore.QRect(c_x1, handle_y, handle_w, handle_h), 2, 2)
                    painter.drawRoundedRect(QtCore.QRect(c_x2 - handle_w, handle_y, handle_w, handle_h), 2, 2)
                else:
                    painter.setBrush(Qt.NoBrush)
                    painter.setPen(QtGui.QPen(QtGui.QColor("#00a3ad"), 1.2))
                    painter.drawRoundedRect(clip_rect, 4, 4)

                # 5. Distinct Cut Seam / Split Divider between sequential clips
                if idx > 0 and c_x1 >= v_left - 10:
                    # Dark gap separator
                    painter.setPen(QtGui.QPen(QtGui.QColor("#060a12"), 3))
                    painter.drawLine(c_x1, vid_top - 2, c_x1, vid_top + vid_h + 2)
                    
                    # Bright Cut Line
                    painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.5))
                    painter.drawLine(c_x1, vid_top, c_x1, vid_top + vid_h)
                    
                    # Cut notch indicators at top and bottom
                    painter.setBrush(QtGui.QBrush(QtGui.QColor("#00e5ff")))
                    painter.setPen(Qt.NoPen)
                    notch_t = QtGui.QPolygon([
                        QtCore.QPoint(c_x1 - 3, vid_top),
                        QtCore.QPoint(c_x1 + 3, vid_top),
                        QtCore.QPoint(c_x1 + 3, vid_top),
                        QtCore.QPoint(c_x1, vid_top + 5)
                    ])
                    notch_b = QtGui.QPolygon([
                        QtCore.QPoint(c_x1 - 3, vid_top + vid_h),
                        QtCore.QPoint(c_x1 + 3, vid_top + vid_h),
                        QtCore.QPoint(c_x1, vid_top + vid_h - 5)
                    ])
                    painter.drawPolygon(notch_t)
                    painter.drawPolygon(notch_b)

                    # Phase 3 CapCut-style Transition Indicator Badge
                    t_list = getattr(self, 'transitions', [])
                    active_t = None
                    cut_idx = idx - 1
                    for t in t_list:
                        t_cut = getattr(t, 'cut_index', None)
                        t_a = str(getattr(t, 'clip_a_id', ''))
                        t_b = str(getattr(t, 'clip_b_id', ''))
                        ca_id = str(v_clips[cut_idx].get('id', cut_idx))
                        cb_id = str(v_clips[idx].get('id', idx))
                        if t_cut == cut_idx or (t_a == ca_id and t_b == cb_id):
                            active_t = t
                            break

                    b_w = 26
                    b_h = 22
                    b_x = c_x1 - b_w // 2
                    b_y = vid_top + (vid_h - b_h) // 2
                    badge_rect = QtCore.QRect(b_x, b_y, b_w, b_h)

                    if active_t:
                        painter.setBrush(QtGui.QBrush(QtGui.QColor("#4f46e5")))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#c7d2fe"), 1.2))
                        painter.drawRoundedRect(badge_rect, 4, 4)
                        painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff")))
                        font = painter.font()
                        font.setBold(True)
                        font.setPointSize(9)
                        painter.setFont(font)
                        painter.drawText(badge_rect, Qt.AlignCenter, "⧓")
                    else:
                        painter.setBrush(QtGui.QBrush(QtGui.QColor("#1e293b")))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#475569"), 1))
                        painter.drawRoundedRect(badge_rect, 4, 4)
                        painter.setPen(QtGui.QPen(QtGui.QColor("#94a3b8")))
                        font = painter.font()
                        font.setBold(True)
                        font.setPointSize(8)
                        painter.setFont(font)
                        painter.drawText(badge_rect, Qt.AlignCenter, "+")

            # CapCut Visual Drop Indicator for Video Clip Reordering
            if self._drag_target and self._drag_target[0] == 'video_clip' and getattr(self, '_drag_moved', False):
                target_idx = getattr(self, '_drop_insert_idx', None)
                orig_idx = self._drag_target[2]
                if target_idx is not None and v_clips and 0 <= target_idx < len(v_clips) and target_idx != orig_idx:
                    if target_idx > orig_idx:
                        # Dropping after target clip
                        tc = v_clips[target_idx]
                        ins_x = int((tc.get("start", 0.0) + tc.get("duration", 0.0)) * pixels_per_sec)
                    else:
                        # Dropping before target clip
                        tc = v_clips[target_idx]
                        ins_x = int(tc.get("start", 0.0) * pixels_per_sec)

                    # 1. Neon Cyan Drop Insertion Line
                    painter.setPen(QtGui.QPen(QtGui.QColor("#00f2fe"), 3.0))
                    painter.drawLine(ins_x, vid_top - 4, ins_x, vid_top + vid_h + 4)

                    # Top and Bottom Triangular Pointer Arrows
                    painter.setBrush(QtGui.QBrush(QtGui.QColor("#00f2fe")))
                    painter.setPen(Qt.NoPen)
                    arrow_t = QtGui.QPolygon([
                        QtCore.QPoint(ins_x - 6, vid_top - 7),
                        QtCore.QPoint(ins_x + 6, vid_top - 7),
                        QtCore.QPoint(ins_x, vid_top)
                    ])
                    arrow_b = QtGui.QPolygon([
                        QtCore.QPoint(ins_x - 6, vid_top + vid_h + 7),
                        QtCore.QPoint(ins_x + 6, vid_top + vid_h + 7),
                        QtCore.QPoint(ins_x, vid_top + vid_h)
                    ])
                    painter.drawPolygon(arrow_t)
                    painter.drawPolygon(arrow_b)

                # 2. Floating semi-transparent Drag Ghost under cursor
                c_idx = self._drag_target[2]
                if c_idx is not None and 0 <= c_idx < len(v_clips):
                    cur_x = getattr(self, '_drag_curr_x', None)
                    if cur_x is not None:
                        ghost_w = 140
                        ghost_h = 30
                        ghost_x = cur_x - ghost_w // 2
                        ghost_y = vid_top + (vid_h - ghost_h) // 2
                        painter.setBrush(QtGui.QBrush(QtGui.QColor(8, 47, 73, 235)))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#00f2fe"), 1.5))
                        painter.drawRoundedRect(ghost_x, ghost_y, ghost_w, ghost_h, 6, 6)
                        painter.setPen(QtGui.QColor("#ffffff"))
                        painter.setFont(QtGui.QFont("Inter", 8, QtGui.QFont.Bold))
                        c_name = v_clips[c_idx].get("name", "Clip")
                        disp_name = c_name if len(c_name) <= 16 else c_name[:13] + "..."
                        painter.drawText(ghost_x + 8, ghost_y + 19, f"↔ {disp_name}")

        # 5. RENDER TRACK 1: 📝 Text Overlay Clips (if active)
        if layout.get('text'):
            t_top, t_h = layout['text']
            t_items = getattr(self, 'text_items', None)
            if not t_items:
                t_items = [{
                    "id": "text_1",
                    "start_sec": self.text_clip.get("start", 0.0),
                    "duration_sec": self.text_clip.get("duration", 5.0),
                    "text": self.text_clip.get("text", "Text Overlay")
                }]
                
            for t_item in t_items:
                t_id = t_item.get("id")
                t_st = t_item.get("start_sec", t_item.get("start", 0.0))
                t_dur = max(0.5, t_item.get("duration_sec", t_item.get("duration", 5.0)))
                t_x1 = int(t_st * pixels_per_sec)
                t_w = max(24, int(t_dur * pixels_per_sec))
                is_active = (t_id == getattr(self, 'active_text_id', None))
                
                if t_x1 + t_w >= v_left and t_x1 <= v_right:
                    t_rect = QtCore.QRect(t_x1, t_top + 1, t_w, t_h - 2)
                    if is_active:
                        painter.setBrush(QtGui.QBrush(QtGui.QColor(16, 185, 129, 230)))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#6ee7b7"), 2.0))
                    else:
                        painter.setBrush(QtGui.QBrush(QtGui.QColor(16, 185, 129, 140)))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#059669"), 1.0))
                    painter.drawRoundedRect(t_rect, 4, 4)
                    # Left & Right Drag Handles
                    handle_pen = QtGui.QPen(QtGui.QColor("#ffffff" if is_active else "#a7f3d0"), 2)
                    painter.setPen(handle_pen)
                    painter.drawLine(t_x1 + 3, t_top + 4, t_x1 + 3, t_top + t_h - 4)
                    painter.drawLine(t_x1 + t_w - 4, t_top + 4, t_x1 + t_w - 4, t_top + t_h - 4)
                    # Text inside clip
                    txt_label = t_item.get("text", "Text Overlay")
                    painter.setPen(QtGui.QColor("#ffffff"))
                    painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold if is_active else QtGui.QFont.Normal))
                    painter.drawText(QtCore.QRect(t_x1 + 8, t_top + 1, max(10, t_w - 16), t_h - 2), Qt.AlignLeft | Qt.AlignVCenter, txt_label)

        # 6. RENDER TRACK 2: 🖼️ Logo Overlay Clip (if active)
        if layout.get('logo'):
            l_top, l_h = layout['logo']
            l_st = self.logo_clip["start"]
            l_dur = self.logo_clip["duration"]
            l_x1 = int(l_st * pixels_per_sec)
            l_w = max(24, int(l_dur * pixels_per_sec))
            if l_x1 + l_w >= v_left and l_x1 <= v_right:
                l_rect = QtCore.QRect(l_x1, l_top + 1, l_w, l_h - 2)
                painter.setBrush(QtGui.QBrush(QtGui.QColor(245, 158, 11, 170)))  # Amber
                painter.setPen(QtGui.QPen(QtGui.QColor("#fbbf24"), 1.5))
                painter.drawRoundedRect(l_rect, 4, 4)
                # Left & Right Drag Handles
                painter.setPen(QtGui.QPen(QtGui.QColor("#fef3c7"), 2))
                painter.drawLine(l_x1 + 3, l_top + 4, l_x1 + 3, l_top + l_h - 4)
                painter.drawLine(l_x1 + l_w - 4, l_top + 4, l_x1 + l_w - 4, l_top + l_h - 4)
                # Name inside clip
                logo_label = self.logo_clip.get("name", "Logo Overlay")
                painter.setPen(QtGui.QColor("#ffffff"))
                painter.setFont(QtGui.QFont("sans-serif", 8, QtGui.QFont.Bold))
                painter.drawText(QtCore.QRect(l_x1 + 8, l_top + 1, max(10, l_w - 16), l_h - 2), Qt.AlignLeft | Qt.AlignVCenter, logo_label)

        # 7. RENDER TRACK 3: 💬 Subtitles (Waveforms & Speech Text) & TRACK 4: 🔍 Blur Mask Blocks
        for idx, seg in enumerate(self.segments):
            st = seg.get("start", 0.0)
            et = seg.get("end", 0.0)
            x_start = int(st * pixels_per_sec)
            x_end = int(et * pixels_per_sec)

            # Skip if completely off-screen (saves CPU on 1000+ segments)
            if x_end < v_left - 30 or x_start > v_right + 30:
                continue

            seg_w = max(8, x_end - x_start)

            # Track 3: Subtitle Segment Block
            is_cyan = (idx % 2 == 0)
            bg_color = QtGui.QColor("#00f0ff" if is_cyan else "#7c4dff")
            bg_color.setAlpha(50)
            border_color = QtGui.QColor("#00f0ff" if is_cyan else "#9166ff")
            
            painter.setBrush(QtGui.QBrush(bg_color))
            painter.setPen(QtGui.QPen(border_color, 1.2))
            s_rect = QtCore.QRect(x_start, sub_top + 2, seg_w, sub_h - 4)
            painter.drawRoundedRect(s_rect, 4, 4)

            # Trimming visual handles on left and right edges
            painter.setPen(QtGui.QPen(border_color.lighter(140), 2))
            painter.drawLine(x_start + 2, sub_top + 5, x_start + 2, sub_top + sub_h - 7)
            painter.drawLine(x_end - 3, sub_top + 5, x_end - 3, sub_top + sub_h - 7)

            # Audio Waveform lines (adapt amplitude dynamically to available sub_h)
            painter.setPen(QtGui.QPen(border_color.lighter(120), 1))
            num_bars = int(seg_w / 5)
            for b in range(num_bars):
                bx = x_start + b * 5
                amp = (math.sin(b * 0.4) * 0.4 + math.sin(b * 0.95) * 0.35 + 0.25) * max_amp
                painter.drawLine(bx, int(center_y - amp), bx, int(center_y + amp))

            # Dialogue text preview
            text_str = seg.get("khmer_text", seg.get("original_text", seg.get("text", "")))
            if seg_w > 35:
                painter.setPen(QtGui.QColor("#ffffff"))
                painter.setFont(QtGui.QFont("Kantumruy Pro", 8, QtGui.QFont.Bold))
                painter.drawText(QtCore.QRect(x_start + 5, sub_top + 3, seg_w - 10, 16), Qt.AlignLeft | Qt.AlignVCenter, text_str)

            # Track 4: 🔍 Blur Mask Blocks (Auto-Speech Synchronized Fallback) (if blur enabled)
            if layout.get('blur') and not self.blur_items and self.blur_auto_speech:
                b_top, b_h = layout['blur']
                b_st = max(0.0, st - 0.15)
                b_et = et + 0.15
                bx_start = int(b_st * pixels_per_sec)
                bx_end = int(b_et * pixels_per_sec)
                bw = max(6, bx_end - bx_start)

                painter.setBrush(QtGui.QBrush(QtGui.QColor(234, 179, 8, 150)))  # Yellow/Gold
                painter.setPen(QtGui.QPen(QtGui.QColor("#facc15"), 1.2))
                b_rect = QtCore.QRect(bx_start, b_top + 2, bw, b_h - 4)
                painter.drawRoundedRect(b_rect, 3, 3)

                if bw > 55:
                    painter.setPen(QtGui.QColor("#000000"))
                    painter.setFont(QtGui.QFont("sans-serif", 7, QtGui.QFont.Bold))
                    painter.drawText(b_rect, Qt.AlignCenter, "Auto-Blur")

        # 8. Track 4: 🔍 Multi-Blur Mask Interactive Blocks (if blur enabled)
        if layout.get('blur') and self.blur_items:
            b_top, b_h = layout['blur']
            for b_idx, b_item in enumerate(self.blur_items):
                b_id = b_item.get("id", f"blur_{b_idx}")
                b_name = b_item.get("name", f"Blur {b_idx+1}")
                is_active = (b_id == self.active_blur_id)
                b_st = b_item.get("start_sec", 0.0)
                b_et = b_item.get("end_sec", b_st + 5.0)
                
                bx_start = int(b_st * pixels_per_sec)
                bx_end = int(b_et * pixels_per_sec)
                bw = max(24, bx_end - bx_start)
                
                if is_active:
                    bg_color = QtGui.QColor("#eab308")
                    bg_color.setAlpha(210)
                    border_color = QtGui.QColor("#fef08a")
                    pen_w = 2.0
                else:
                    bg_color = QtGui.QColor("#b45309")
                    bg_color.setAlpha(150)
                    border_color = QtGui.QColor("#f59e0b")
                    pen_w = 1.0
                    
                painter.setBrush(QtGui.QBrush(bg_color))
                painter.setPen(QtGui.QPen(border_color, pen_w))
                b_rect = QtCore.QRect(bx_start, b_top + 2, bw, b_h - 4)
                painter.drawRoundedRect(b_rect, 4, 4)
                
                # Visual trimming handles on left and right edges
                painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.8))
                painter.drawLine(bx_start + 2, b_top + 5, bx_start + 2, b_top + b_h - 5)
                painter.drawLine(bx_end - 3, b_top + 5, bx_end - 3, b_top + b_h - 5)
                
                # Dialogue / Blur Name label
                if bw > 30:
                    painter.setPen(QtGui.QColor("#000000" if is_active else "#ffffff"))
                    painter.setFont(QtGui.QFont("sans-serif", 8, QtGui.QFont.Bold))
                    rot = int(b_item.get("rotation", 0.0))
                    rot_str = f" ({rot}°)" if rot != 0 else ""
                    painter.drawText(b_rect, Qt.AlignCenter, f"🔍 {b_name}{rot_str}")

        # 6.5. Interactive Video In / Out Trim Selection & Excluded Regions Dimming
        has_in = self.in_point_sec is not None
        has_out = self.out_point_sec is not None
        if has_in or has_out:
            x_in = int((self.in_point_sec if has_in else 0.0) * pixels_per_sec)
            x_out = int((self.out_point_sec if has_out else self.total_duration_sec) * pixels_per_sec)

            # Dim excluded regions outside [In, Out]
            if has_in and x_in > 0:
                painter.fillRect(0, 0, x_in, height, QtGui.QColor(0, 0, 0, 150))
            if has_out and x_out < width:
                painter.fillRect(x_out, 0, max(0, width - x_out), height, QtGui.QColor(0, 0, 0, 150))

            # Active selection highlight on time ruler (y: 0..22)
            sel_w = max(2, x_out - x_in)
            painter.fillRect(x_in, 0, sel_w, 22, QtGui.QColor(0, 240, 255, 30))
            painter.fillRect(x_in, 0, sel_w, 3, QtGui.QColor("#00f0ff"))

            # Draw In-Point Bracket [
            if has_in:
                painter.setPen(QtGui.QPen(QtGui.QColor("#00f0ff"), 2))
                painter.drawLine(x_in, 0, x_in, height)
                painter.drawLine(x_in, 0, x_in + 8, 0)
                painter.drawLine(x_in, 22, x_in + 8, 22)
                painter.fillRect(x_in, 0, 8, 22, QtGui.QColor(0, 240, 255, 80))
                painter.setFont(QtGui.QFont("sans-serif", 8, QtGui.QFont.Bold))
                painter.setPen(QtGui.QColor("#00f0ff"))
                painter.drawText(x_in + 4, 16, "[")

            # Draw Out-Point Bracket ]
            if has_out:
                painter.setPen(QtGui.QPen(QtGui.QColor("#00f0ff"), 2))
                painter.drawLine(x_out, 0, x_out, height)
                painter.drawLine(x_out - 8, 0, x_out, 0)
                painter.drawLine(x_out - 8, 22, x_out, 22)
                painter.fillRect(x_out - 8, 0, 8, 22, QtGui.QColor(0, 240, 255, 80))
                painter.setFont(QtGui.QFont("sans-serif", 8, QtGui.QFont.Bold))
                painter.setPen(QtGui.QColor("#00f0ff"))
                painter.drawText(x_out - 8, 16, "]")

        # 7. Interactive Red Playhead Vertical Indicator
        playhead_x = int(self.playhead_pos_sec * pixels_per_sec)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ff1744"), 2))
        painter.drawLine(playhead_x, 0, playhead_x, height)
        
        # Red Playhead Top Handle Polygon
        playhead_head = QtGui.QPolygon([
            QtCore.QPoint(playhead_x - 6, 0),
            QtCore.QPoint(playhead_x + 6, 0),
            QtCore.QPoint(playhead_x, 10)
        ])
        painter.setBrush(QtGui.QBrush(QtGui.QColor("#ff1744")))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(playhead_head)

        # 8. CapCut-Style Magnetic Drop Indicator
        if getattr(self, '_is_drag_hovering', False) and getattr(self, '_drop_indicator_sec', None) is not None:
            drop_x = int(self._drop_indicator_sec * pixels_per_sec)
            drop_pen = QtGui.QPen(QtGui.QColor("#38bdf8"), 2, Qt.DashLine)
            painter.setPen(drop_pen)
            painter.drawLine(drop_x, 0, drop_x, height)
            
            m = int(self._drop_indicator_sec // 60)
            s = int(self._drop_indicator_sec % 60)
            ms = int((self._drop_indicator_sec % 1.0) * 100)
            badge_text = f"➕ Drop Here: {m:02d}:{s:02d}.{ms:02d}"
            painter.setFont(QtGui.QFont("Inter", 8, QtGui.QFont.Bold))
            painter.setBrush(QtGui.QColor("#0284c7"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(drop_x - 55, 2, 110, 18, 4, 4)
            painter.setPen(QtGui.QColor("#ffffff"))
            painter.drawText(drop_x - 50, 15, badge_text)

    # ==================== CAPCUT-STYLE DRAG & DROP ====================
    def _get_canvas_x_from_event(self, event) -> int:
        if hasattr(event, 'globalPosition'):
            gp = event.globalPosition().toPoint()
            return self.mapFromGlobal(gp).x()
        elif hasattr(event, 'globalPos'):
            return self.mapFromGlobal(event.globalPos()).x()
        return event.pos().x()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-mediabin-clip") or event.mimeData().hasText():
            event.acceptProposedAction()
            self._is_drag_hovering = True
            self.update()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-mediabin-clip") or event.mimeData().hasText():
            event.acceptProposedAction()
            x = self._get_canvas_x_from_event(event)
            pixels_per_sec = 40.0 * self.zoom_factor
            if pixels_per_sec > 0:
                raw_sec = max(0.0, x / pixels_per_sec)
                
                # Magnetic snapping targets: 0.0, playhead, clip boundaries
                snap_targets = [0.0, getattr(self, 'playhead_pos_sec', 0.0)]
                if getattr(self, 'video_clips', None):
                    cum = 0.0
                    for c in self.video_clips:
                        cum += c.get("duration", 0.0)
                        snap_targets.append(cum)

                snapped_sec = raw_sec
                for target in snap_targets:
                    if abs(x - (target * pixels_per_sec)) < 15:
                        snapped_sec = target
                        break

                self._drop_indicator_sec = snapped_sec
            self._is_drag_hovering = True
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._is_drag_hovering = False
        self._drop_indicator_sec = None
        self.update()

    def dropEvent(self, event):
        self._is_drag_hovering = False
        drop_sec = getattr(self, '_drop_indicator_sec', None)
        if drop_sec is None:
            x = self._get_canvas_x_from_event(event)
            pixels_per_sec = 40.0 * self.zoom_factor
            drop_sec = max(0.0, x / max(1.0, pixels_per_sec))
        self._drop_indicator_sec = None
        self.update()

        fpath = None
        if event.mimeData().hasFormat("application/x-mediabin-clip"):
            fpath = bytes(event.mimeData().data("application/x-mediabin-clip")).decode('utf-8')
        elif event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                fpath = urls[0].toLocalFile()
        elif event.mimeData().hasText():
            text = event.mimeData().text().strip()
            if os.path.exists(text):
                fpath = text

        if fpath and os.path.exists(fpath):
            event.acceptProposedAction()
            self.clip_dropped_on_timeline.emit(fpath, drop_sec)
        else:
            event.ignore()

    def mousePressEvent(self, event):
        x = event.pos().x()
        y = event.pos().y()
        pixels_per_sec = 40.0 * self.zoom_factor
        if pixels_per_sec <= 0:
            return
        sec_at_x = max(0.0, x / pixels_per_sec)
        height = max(172, self.height())
        layout = self.compute_track_layout(height)
        vid_top, vid_h = layout['vid']
        sub_top, sub_h = layout['sub']

        # 0. Check Right-Click on Tracks for Context Menu
        if event.button() == Qt.RightButton:
            if vid_top <= y <= vid_top + vid_h:
                self._show_video_context_menu(sec_at_x, event.globalPos())
                return
            elif layout.get('text') and layout['text'][0] <= y <= layout['text'][0] + layout['text'][1]:
                t_items = getattr(self, 'text_items', None) or [self.text_clip]
                for t_item in reversed(t_items):
                    t_st = t_item.get("start_sec", t_item.get("start", 0.0))
                    t_dur = max(0.5, t_item.get("duration_sec", t_item.get("duration", 5.0)))
                    t_x1 = int(t_st * pixels_per_sec)
                    t_w = max(24, int(t_dur * pixels_per_sec))
                    if t_x1 - 4 <= x <= t_x1 + t_w + 4:
                        self._show_text_context_menu(t_item.get("id", "text_1"), event.globalPos())
                        return
            elif layout.get('logo') and layout['logo'][0] <= y <= layout['logo'][0] + layout['logo'][1]:
                l_st = self.logo_clip.get("start", 0.0)
                l_dur = max(0.5, self.logo_clip.get("duration", 60.0))
                l_x1 = int(l_st * pixels_per_sec)
                l_w = max(24, int(l_dur * pixels_per_sec))
                if l_x1 - 4 <= x <= l_x1 + l_w + 4:
                    self._show_logo_context_menu(event.globalPos())
                    return
            elif layout.get('blur') and self.blur_items and layout['blur'][0] <= y <= layout['blur'][0] + layout['blur'][1]:
                for b_item in self.blur_items:
                    st = b_item.get("start_sec", 0.0)
                    et = b_item.get("end_sec", st + 5.0)
                    bx1 = int(st * pixels_per_sec)
                    bx2 = int(et * pixels_per_sec)
                    if bx1 - 4 <= x <= bx2 + 4:
                        self._show_blur_context_menu(b_item.get("id"), event.globalPos())
                        return
            elif y <= 22:
                self._show_ruler_context_menu(sec_at_x, event.globalPos())
                return

        # 1. Check Hand Tool Canvas Pan (Middle click / Right click / Space + Left / Alt + Left)
        if (event.button() in (Qt.MiddleButton, Qt.RightButton)) or (
            event.button() == Qt.LeftButton and (self._space_pressed or (event.modifiers() & Qt.AltModifier))
        ):
            self._is_panning = True
            self._pan_start_x = event.globalPos().x()
            sa = self._get_scroll_area()
            self._pan_start_scroll = sa.horizontalScrollBar().value() if sa else 0
            self.setCursor(Qt.ClosedHandCursor)
            return

        if event.button() == Qt.LeftButton:
            # Hit-test Track 0: Video Clips (Select clip on click & Prepare Drag Reorder)
            if vid_top <= y <= vid_top + vid_h:
                v_clips = getattr(self, 'video_clips', [])
                # Hit-test transition cut badges (within 14px of seam)
                if len(v_clips) >= 2:
                    for c_i in range(1, len(v_clips)):
                        c_x1 = int(v_clips[c_i].get("start", 0.0) * pixels_per_sec)
                        if abs(x - c_x1) <= 14:
                            self.transition_clicked.emit(c_i - 1)
                            self.update()
                            return

                found_clip = None
                for c_i, c in enumerate(v_clips):
                    c_st = c.get("start", 0.0)
                    c_dur = c.get("duration", 0.0)
                    if c_st <= sec_at_x <= (c_st + c_dur):
                        found_clip = c_i
                        break
                self.selected_clip_idx = found_clip
                if found_clip is not None:
                    self.video_clip_selected.emit(found_clip)
                    # Prepare CapCut Video Clip Drag Reordering
                    self._drag_target = ('video_clip', 'move', found_clip)
                    self._drag_start_x = x
                    self._drag_curr_x = x
                    self._drag_moved = False
                    self._drop_insert_idx = None
                    self.setCursor(Qt.ClosedHandCursor)
                else:
                    self._drag_target = ('playhead', None, None)
                    self._drag_start_x = x
                    self._drag_moved = False

                self.playhead_pos_sec = sec_at_x
                self.seek_requested.emit(sec_at_x)
                self.update()
                return

            # Hit-test In / Out Point brackets on ruler bar (y <= 22)
            if y <= 22:
                if self.in_point_sec is not None:
                    x_in = int(self.in_point_sec * pixels_per_sec)
                    if abs(x - x_in) <= 8:
                        self._drag_target = ('in_point', 'move', None)
                        self._drag_start_x = x
                        self._drag_orig_start = self.in_point_sec
                        self._drag_moved = False
                        self.setCursor(Qt.SizeHorCursor)
                        return
                if self.out_point_sec is not None:
                    x_out = int(self.out_point_sec * pixels_per_sec)
                    if abs(x - x_out) <= 8:
                        self._drag_target = ('out_point', 'move', None)
                        self._drag_start_x = x
                        self._drag_orig_start = self.out_point_sec
                        self._drag_moved = False
                        self.setCursor(Qt.SizeHorCursor)
                        return

            # Hit-test Track 1 (Text Overlay items) (if active)
            if layout.get('text'):
                t_top, t_h = layout['text']
                t_items = getattr(self, 'text_items', None)
                if not t_items:
                    t_items = [dict(self.text_clip, id="text_1")]
                for t_item in reversed(t_items):
                    t_id = t_item.get("id", "text_1")
                    t_st = t_item.get("start_sec", t_item.get("start", 0.0))
                    t_dur = max(0.5, t_item.get("duration_sec", t_item.get("duration", 5.0)))
                    t_x1 = int(t_st * pixels_per_sec)
                    t_w = max(24, int(t_dur * pixels_per_sec))
                    t_x2 = t_x1 + t_w
                    if t_top <= y <= t_top + t_h and (t_x1 - 6 <= x <= t_x2 + 6):
                        self.active_text_id = t_id
                        self.text_selected.emit(t_id)
                        if abs(x - t_x1) <= 6:
                            self._drag_target = ('text', 'left', t_id)
                        elif abs(x - t_x2) <= 6:
                            self._drag_target = ('text', 'right', t_id)
                        else:
                            self._drag_target = ('text', 'move', t_id)
                        self._drag_start_x = x
                        self._drag_orig_start = t_st
                        self._drag_orig_dur = t_dur
                        self._drag_moved = False
                        self.setCursor(Qt.ClosedHandCursor if self._drag_target[1] == 'move' else Qt.SizeHorCursor)
                        self.update()
                        return

            # Hit-test Track 2 (Logo) (if active)
            if layout.get('logo'):
                l_top, l_h = layout['logo']
                l_st = self.logo_clip["start"]
                l_dur = self.logo_clip["duration"]
                l_x1 = int(l_st * pixels_per_sec)
                l_w = max(24, int(l_dur * pixels_per_sec))
                l_x2 = l_x1 + l_w
                if l_top <= y <= l_top + l_h and (l_x1 - 6 <= x <= l_x2 + 6):
                    if abs(x - l_x1) <= 6:
                        self._drag_target = ('logo', 'left', None)
                    elif abs(x - l_x2) <= 6:
                        self._drag_target = ('logo', 'right', None)
                    else:
                        self._drag_target = ('logo', 'move', None)
                    self._drag_start_x = x
                    self._drag_orig_start = l_st
                    self._drag_orig_dur = l_dur
                    self._drag_moved = False
                    self.setCursor(Qt.ClosedHandCursor if self._drag_target[1] == 'move' else Qt.SizeHorCursor)
                    return

            # Hit-test Track 3 (Subtitle Segment: Trim Left, Trim Right, or Move)
            if sub_top <= y <= sub_top + sub_h and self.segments:
                for idx, seg in enumerate(self.segments):
                    st = seg.get("start", 0.0)
                    et = seg.get("end", 0.0)
                    sx1 = int(st * pixels_per_sec)
                    sx2 = int(et * pixels_per_sec)
                    if sx1 - 6 <= x <= sx2 + 6:
                        if abs(x - sx1) <= 6:
                            handle = 'left'
                        elif abs(x - sx2) <= 6:
                            handle = 'right'
                        else:
                            handle = 'move'
                        self._drag_target = ('sub', handle, idx)
                        self._drag_start_x = x
                        self._drag_orig_start = st
                        self._drag_orig_dur = max(0.2, et - st)
                        self._drag_moved = False
                        self.setCursor(Qt.ClosedHandCursor if handle == 'move' else Qt.SizeHorCursor)
                        return

            # Hit-test Track 4 (Multi-Blur Items) (if active)
            if layout.get('blur') and self.blur_items:
                b_top, b_h = layout['blur']
                if b_top <= y <= b_top + b_h:
                    for b_item in self.blur_items:
                        b_id = b_item.get("id")
                        st = b_item.get("start_sec", 0.0)
                        et = b_item.get("end_sec", st + 5.0)
                        bx1 = int(st * pixels_per_sec)
                        bx2 = int(et * pixels_per_sec)
                        if bx1 - 6 <= x <= bx2 + 6:
                            if abs(x - bx1) <= 6:
                                handle = 'left'
                            elif abs(x - bx2) <= 6:
                                handle = 'right'
                            else:
                                handle = 'move'
                            self.active_blur_id = b_id
                            self.blur_item_selected.emit(b_id)
                            self._drag_target = ('blur_item', handle, b_id)
                            self._drag_start_x = x
                            self._drag_orig_start = st
                            self._drag_orig_dur = max(0.5, et - st)
                            self._drag_moved = False
                            self.update()
                            self.setCursor(Qt.ClosedHandCursor if handle == 'move' else Qt.SizeHorCursor)
                            return

            # Default / Track 0 (Video): seek playhead
            self._drag_target = ('playhead', None, None)
            self._drag_moved = False
            self.playhead_pos_sec = sec_at_x
            self.update()
            self.seek_requested.emit(sec_at_x)

    def mouseMoveEvent(self, event):
        x = event.pos().x()
        y = event.pos().y()
        pixels_per_sec = 40.0 * self.zoom_factor
        if pixels_per_sec <= 0:
            return
        height = max(172, self.height())
        layout = self.compute_track_layout(height)
        vid_top, vid_h = layout['vid']
        sub_top, sub_h = layout['sub']

        # 1. Handle Smooth Hand Tool Panning
        if self._is_panning:
            dx = event.globalPos().x() - self._pan_start_x
            sa = self._get_scroll_area()
            if sa:
                sa.horizontalScrollBar().setValue(self._pan_start_scroll - dx)
            return

        # 2. Handle Item Dragging & Trimming
        if self._drag_target is not None:
            target, handle, seg_idx = self._drag_target
            dx = x - self._drag_start_x
            dt = dx / pixels_per_sec

            if abs(dx) > 3:
                self._drag_moved = True

            if target == 'playhead':
                sec = max(0.0, min(self.total_duration_sec, x / pixels_per_sec))
                self.playhead_pos_sec = sec
                self.update()
                self.seek_requested.emit(sec)
            elif target == 'video_clip' and seg_idx is not None:
                self._drag_curr_x = x
                v_clips = getattr(self, 'video_clips', [])
                if len(v_clips) > 1:
                    orig_idx = seg_idx
                    sec_at_x = max(0.0, x / pixels_per_sec)
                    hover_idx = None
                    for c_i, c in enumerate(v_clips):
                        c_st = c.get("start", 0.0)
                        c_dur = c.get("duration", 0.0)
                        if c_st <= sec_at_x <= (c_st + c_dur):
                            hover_idx = c_i
                            break
                    if hover_idx is None:
                        if sec_at_x < 0.0:
                            hover_idx = 0
                        elif sec_at_x >= self.total_duration_sec:
                            hover_idx = len(v_clips) - 1

                    if hover_idx is not None and hover_idx != orig_idx:
                        self._drop_insert_idx = hover_idx
                    else:
                        self._drop_insert_idx = None
                    self.setCursor(Qt.ClosedHandCursor)
                    self.update()
            elif target == 'in_point':
                limit_out = (self.out_point_sec - 0.1) if self.out_point_sec is not None else self.total_duration_sec
                new_sec = max(0.0, min(limit_out, x / pixels_per_sec))
                self.in_point_sec = round(new_sec, 2)
                self.update()
                self.in_out_changed.emit(self.in_point_sec, self.out_point_sec)
            elif target == 'out_point':
                limit_in = (self.in_point_sec + 0.1) if self.in_point_sec is not None else 0.1
                new_sec = max(limit_in, min(self.total_duration_sec, x / pixels_per_sec))
                self.out_point_sec = round(new_sec, 2)
                self.update()
                self.in_out_changed.emit(self.in_point_sec, self.out_point_sec)
            elif target == 'text':
                t_id = seg_idx  # seg_idx holds extra_data / text_id
                t_item = next((t for t in getattr(self, 'text_items', []) if t.get("id") == t_id), None)
                if handle == 'move':
                    new_st = max(0.0, self._drag_orig_start + dt)
                    self.text_clip["start"] = new_st
                    if t_item: t_item["start_sec"] = new_st
                elif handle == 'left':
                    new_st = max(0.0, min(self._drag_orig_start + self._drag_orig_dur - 0.5, self._drag_orig_start + dt))
                    new_dur = (self._drag_orig_start + self._drag_orig_dur) - new_st
                    self.text_clip["start"] = new_st
                    self.text_clip["duration"] = new_dur
                    if t_item:
                        t_item["start_sec"] = new_st
                        t_item["duration_sec"] = new_dur
                elif handle == 'right':
                    new_dur = max(0.5, self._drag_orig_dur + dt)
                    self.text_clip["duration"] = new_dur
                    if t_item:
                        t_item["duration_sec"] = new_dur
                self.update()
                self.text_clip_changed.emit(self.text_clip["start"], self.text_clip["duration"])
                if t_id:
                    self.text_item_timing_changed.emit(t_id, self.text_clip["start"], self.text_clip["duration"])
            elif target == 'logo':
                self.logo_clip["full_video"] = False
                if handle == 'move':
                    new_st = max(0.0, self._drag_orig_start + dt)
                    self.logo_clip["start"] = new_st
                elif handle == 'left':
                    new_st = max(0.0, min(self._drag_orig_start + self._drag_orig_dur - 0.5, self._drag_orig_start + dt))
                    new_dur = (self._drag_orig_start + self._drag_orig_dur) - new_st
                    self.logo_clip["start"] = new_st
                    self.logo_clip["duration"] = new_dur
                elif handle == 'right':
                    new_dur = max(0.5, self._drag_orig_dur + dt)
                    self.logo_clip["duration"] = new_dur
                self.update()
                self.logo_clip_changed.emit(self.logo_clip["start"], self.logo_clip["duration"])
            elif target == 'sub' and seg_idx is not None and 0 <= seg_idx < len(self.segments):
                seg = self.segments[seg_idx]
                prev_end = self.segments[seg_idx - 1].get("end", 0.0) if seg_idx > 0 else 0.0
                next_start = self.segments[seg_idx + 1].get("start", self.total_duration_sec) if seg_idx < len(self.segments) - 1 else self.total_duration_sec
                
                if handle == 'move':
                    new_st = max(prev_end, self._drag_orig_start + dt)
                    new_dur = self._drag_orig_dur
                    if new_st + new_dur > next_start:
                        new_st = max(prev_end, next_start - new_dur)
                    seg["start"] = max(0.0, round(new_st, 2))
                    seg["end"] = round(seg["start"] + new_dur, 2)
                elif handle == 'left':
                    new_st = max(prev_end, min(self._drag_orig_start + self._drag_orig_dur - 0.2, self._drag_orig_start + dt))
                    seg["start"] = max(0.0, round(new_st, 2))
                elif handle == 'right':
                    new_et = min(next_start, max(self._drag_orig_start + 0.2, (self._drag_orig_start + self._drag_orig_dur) + dt))
                    seg["end"] = round(new_et, 2)
                self.update()
                self.subtitle_segment_adjusted.emit(seg_idx, seg["start"], seg["end"])
            elif target == 'blur_item' and seg_idx is not None:
                b_id = seg_idx
                b_item = next((b for b in self.blur_items if b.get("id") == b_id), None)
                if b_item:
                    if handle == 'move':
                        new_st = max(0.0, self._drag_orig_start + dt)
                        new_dur = self._drag_orig_dur
                        b_item["start_sec"] = round(new_st, 2)
                        b_item["end_sec"] = round(new_st + new_dur, 2)
                    elif handle == 'left':
                        new_st = max(0.0, min(self._drag_orig_start + self._drag_orig_dur - 0.2, self._drag_orig_start + dt))
                        b_item["start_sec"] = round(new_st, 2)
                    elif handle == 'right':
                        new_et = max(self._drag_orig_start + 0.2, (self._drag_orig_start + self._drag_orig_dur) + dt)
                        b_item["end_sec"] = round(new_et, 2)
                    self.update()
                    self.blur_item_timing_changed.emit(b_id, b_item["start_sec"], b_item["end_sec"])
        else:
            # 3. Dynamic Hover cursor update
            if self._space_pressed:
                self.setCursor(Qt.OpenHandCursor)
                return

            if layout.get('text'):
                t_top, t_h = layout['text']
                t_st = self.text_clip["start"]
                t_dur = self.text_clip["duration"]
                t_x1 = int(t_st * pixels_per_sec)
                t_x2 = t_x1 + max(24, int(t_dur * pixels_per_sec))
                if t_top <= y <= t_top + t_h and (t_x1 - 6 <= x <= t_x2 + 6):
                    if abs(x - t_x1) <= 6 or abs(x - t_x2) <= 6:
                        self.setCursor(Qt.SizeHorCursor)
                    else:
                        self.setCursor(Qt.OpenHandCursor)
                    return
            
            if layout.get('logo'):
                l_top, l_h = layout['logo']
                l_st = self.logo_clip["start"]
                l_dur = self.logo_clip["duration"]
                l_x1 = int(l_st * pixels_per_sec)
                l_x2 = l_x1 + max(24, int(l_dur * pixels_per_sec))
                if l_top <= y <= l_top + l_h and (l_x1 - 6 <= x <= l_x2 + 6):
                    if abs(x - l_x1) <= 6 or abs(x - l_x2) <= 6:
                        self.setCursor(Qt.SizeHorCursor)
                    else:
                        self.setCursor(Qt.OpenHandCursor)
                    return

            if vid_top <= y <= vid_top + vid_h:
                v_clips = getattr(self, 'video_clips', [])
                if v_clips:
                    self.setCursor(Qt.OpenHandCursor)
                else:
                    self.setCursor(Qt.ArrowCursor)
                return

            if sub_top <= y <= sub_top + sub_h and self.segments:
                hovered_sub = False
                for seg in self.segments:
                    sx1 = int(seg.get("start", 0.0) * pixels_per_sec)
                    sx2 = int(seg.get("end", 0.0) * pixels_per_sec)
                    if sx1 - 6 <= x <= sx2 + 6:
                        hovered_sub = True
                        if abs(x - sx1) <= 6 or abs(x - sx2) <= 6:
                            self.setCursor(Qt.SizeHorCursor)
                        else:
                            self.setCursor(Qt.OpenHandCursor)
                        break
                if not hovered_sub:
                    self.setCursor(Qt.ArrowCursor)
                return

            if layout.get('blur') and self.blur_items:
                b_top, b_h = layout['blur']
                if b_top <= y <= b_top + b_h:
                    hovered_blur = False
                    for b_item in self.blur_items:
                        st = b_item.get("start_sec", 0.0)
                        et = b_item.get("end_sec", st + 5.0)
                        bx1 = int(st * pixels_per_sec)
                        bx2 = int(et * pixels_per_sec)
                        if bx1 - 6 <= x <= bx2 + 6:
                            hovered_blur = True
                            if abs(x - bx1) <= 6 or abs(x - bx2) <= 6:
                                self.setCursor(Qt.SizeHorCursor)
                            else:
                                self.setCursor(Qt.OpenHandCursor)
                            break
                    if not hovered_blur:
                        self.setCursor(Qt.ArrowCursor)
                    return
            elif y <= 22:
                near_marker = False
                if self.in_point_sec is not None:
                    x_in = int(self.in_point_sec * pixels_per_sec)
                    if abs(x - x_in) <= 8:
                        self.setCursor(Qt.SizeHorCursor)
                        near_marker = True
                if not near_marker and self.out_point_sec is not None:
                    x_out = int(self.out_point_sec * pixels_per_sec)
                    if abs(x - x_out) <= 8:
                        self.setCursor(Qt.SizeHorCursor)
                        near_marker = True
                if not near_marker:
                    self.setCursor(Qt.SplitHCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.OpenHandCursor if self._space_pressed else Qt.ArrowCursor)
            return

        if self._drag_target is not None:
            target, handle, seg_idx = self._drag_target
            if target == 'sub' and not self._drag_moved and seg_idx is not None:
                # Click without dragging -> seek directly to segment start
                st = self.segments[seg_idx].get("start", 0.0)
                self.playhead_pos_sec = st
                self.update()
                self.seek_requested.emit(st)
            elif target == 'video_clip' and seg_idx is not None:
                v_clips = getattr(self, 'video_clips', [])
                target_idx = getattr(self, '_drop_insert_idx', None)
                orig_idx = seg_idx
                if getattr(self, '_drag_moved', False) and target_idx is not None and len(v_clips) > 1 and target_idx != orig_idx:
                    moved_clip = v_clips.pop(orig_idx)
                    v_clips.insert(target_idx, moved_clip)

                    # Sequential start recalculation
                    curr_offset = 0.0
                    for c in v_clips:
                        c["start"] = curr_offset
                        curr_offset += c.get("duration", 0.0)

                    self.total_duration_sec = curr_offset
                    self.selected_clip_idx = target_idx
                    new_clip_start = v_clips[target_idx]["start"]
                    self.playhead_pos_sec = new_clip_start
                    self.update()
                    self.video_clips_reordered.emit(v_clips)
                    self.seek_requested.emit(new_clip_start)
                self._drop_insert_idx = None
                self._drag_curr_x = None
                self.update()

        self._drag_target = None
        self.setCursor(Qt.OpenHandCursor if self._space_pressed else Qt.ArrowCursor)

    def apply_zoom(self, new_zoom: float, anchor_canvas_x: float = None, anchor_time_sec: float = None):
        """CapCut-standard Anchor-preserving Zoom:
        Maintains the exact screen position of whatever point is being zoomed into
        (cursor position on wheel/pinch, or playhead/center on slider).
        """
        old_zoom = getattr(self, 'zoom_factor', 1.0)
        old_pps = 40.0 * old_zoom
        new_zoom = max(0.1, min(6.0, round(float(new_zoom), 3)))
        new_pps = 40.0 * new_zoom

        sa = self._get_scroll_area()
        sb = sa.horizontalScrollBar() if sa else None
        viewport_w = sa.viewport().width() if (sa and sa.viewport()) else 800
        curr_scroll = sb.value() if sb else 0

        # Determine anchor timestamp (sec) and its target viewport X position (pixels from viewport left)
        if anchor_canvas_x is not None:
            anchor_sec = anchor_canvas_x / max(1.0, old_pps)
            viewport_x = anchor_canvas_x - curr_scroll
        elif anchor_time_sec is not None:
            anchor_sec = anchor_time_sec
            old_px = anchor_sec * old_pps
            viewport_x = old_px - curr_scroll
            if viewport_x < 0 or viewport_x > viewport_w:
                viewport_x = viewport_w / 2.0
        else:
            p_sec = getattr(self, 'playhead_pos_sec', 0.0)
            old_p_px = p_sec * old_pps
            p_offset = old_p_px - curr_scroll
            if 0 <= p_offset <= viewport_w:
                anchor_sec = p_sec
                viewport_x = p_offset
            else:
                anchor_sec = (curr_scroll + viewport_w / 2.0) / max(1.0, old_pps)
                viewport_x = viewport_w / 2.0

        # Apply new zoom factor & update minimum canvas width
        self.zoom_factor = new_zoom
        calc_w = int(max(viewport_w, self.total_duration_sec * new_pps))
        self._cached_canvas_w = calc_w
        self.setMinimumWidth(calc_w)

        # Reposition scrollbar so anchor_sec lands at exactly viewport_x
        if sb:
            max_scroll = max(0, calc_w - viewport_w)
            if sb.maximum() < max_scroll:
                sb.setRange(0, max_scroll)
            new_anchor_px = anchor_sec * new_pps
            target_scroll = int(round(new_anchor_px - viewport_x))
            sb.setValue(max(0, min(max_scroll, target_scroll)))

        # Synchronize zoom slider and zoom label on parent TimelineEditor
        if sa and sa.parent():
            te = sa.parent()
            if hasattr(te, 'zoom_slider'):
                slider = te.zoom_slider
                slider.blockSignals(True)
                slider.setValue(int(round(new_zoom * 100)))
                slider.blockSignals(False)
            if hasattr(te, '_update_zoom_label'):
                te._update_zoom_label(new_zoom)

        self.update()

    def event(self, event):
        """Native macOS Trackpad Pinch-to-Zoom gesture support (CapCut 100%)."""
        if event.type() == QtCore.QEvent.NativeGesture:
            if hasattr(event, 'gestureType') and event.gestureType() == Qt.ZoomNativeGesture:
                val_attr = getattr(event, 'value', None)
                delta = val_attr() if callable(val_attr) else (val_attr or 0.0)
                factor = 1.0 + float(delta) * 1.8
                new_zoom = max(0.1, min(6.0, self.zoom_factor * factor))
                pos_attr = getattr(event, 'pos', None)
                pos = pos_attr() if callable(pos_attr) else pos_attr
                canvas_x = pos.x() if pos else None
                self.apply_zoom(new_zoom, anchor_canvas_x=canvas_x)
                event.accept()
                return True
        return super().event(event)

    def wheelEvent(self, event):
        """CapCut 100% Zoom & Pan Wheel Behavior:
        - Cmd/Ctrl + Vertical Wheel -> Anchor-based smooth zoom at mouse cursor
        - Trackpad Pinch / Horizontal Wheel -> Native horizontal pan
        - Normal Vertical Wheel -> Horizontal pan across timeline
        """
        # 1. Zoom with Cmd (macOS) or Ctrl (Windows/Linux)
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            dy = event.angleDelta().y()
            if dy == 0 and not event.pixelDelta().isNull():
                dy = event.pixelDelta().y()
            if dy != 0:
                factor = 1.15 if dy > 0 else (1.0 / 1.15)
                new_zoom = max(0.1, min(6.0, self.zoom_factor * factor))
                self.apply_zoom(new_zoom, anchor_canvas_x=event.pos().x())
            event.accept()
            return

        # 2. Horizontal Scroll Pan (Mouse wheel or Trackpad swipe)
        sa = self._get_scroll_area()
        if sa:
            sb = sa.horizontalScrollBar()
            step = 0
            if not event.pixelDelta().isNull():
                pd = event.pixelDelta()
                # On macOS trackpad: horizontal swipe gives pd.x(), vertical gives pd.y()
                step = -pd.x() if pd.x() != 0 else -pd.y()
            elif not event.angleDelta().isNull():
                ad = event.angleDelta()
                # Normal mouse wheel (120 per notch)
                step = -int(ad.x() * 0.75) if ad.x() != 0 else -int(ad.y() * 0.75)

            if step != 0:
                sb.setValue(sb.value() + step)
                event.accept()
                return

        super().wheelEvent(event)

    def keyPressEvent(self, event):
        # CapCut Zoom Shortcuts: Cmd + '+' / Cmd + '=' (Zoom In), Cmd + '-' (Zoom Out), Cmd + '0' or Shift + 'Z' (Fit)
        if (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
                self.apply_zoom(self.zoom_factor * 1.25)
                event.accept()
                return
            elif event.key() == Qt.Key_Minus:
                self.apply_zoom(self.zoom_factor / 1.25)
                event.accept()
                return
            elif event.key() == Qt.Key_0:
                sa = self._get_scroll_area()
                if sa and sa.parent() and hasattr(sa.parent(), '_fit_to_window'):
                    sa.parent()._fit_to_window()
                event.accept()
                return
        if event.key() == Qt.Key_Z and (event.modifiers() & Qt.ShiftModifier):
            sa = self._get_scroll_area()
            if sa and sa.parent() and hasattr(sa.parent(), '_fit_to_window'):
                sa.parent()._fit_to_window()
            event.accept()
            return

        if event.key() == Qt.Key_I:
            self.set_in_point()
            event.accept()
            return
        elif event.key() == Qt.Key_O:
            self.set_out_point()
            event.accept()
            return
        elif event.key() in (Qt.Key_B, Qt.Key_S) and (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.split_requested.emit(self.playhead_pos_sec)
            event.accept()
            return
        elif event.key() == Qt.Key_Escape:
            if self.in_point_sec is not None or self.out_point_sec is not None:
                self.clear_in_out()
                event.accept()
                return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if getattr(self, 'selected_clip_idx', None) is not None:
                self.video_clip_delete_requested.emit(self.selected_clip_idx)
                event.accept()
                return
            elif self.in_point_sec is not None and self.out_point_sec is not None:
                self.in_out_delete_requested.emit(self.in_point_sec, self.out_point_sec)
                event.accept()
                return
            elif self.active_blur_id:
                self.blur_item_delete_requested.emit(self.active_blur_id)
                event.accept()
                return
            elif getattr(self, 'active_text_id', None):
                self.text_item_delete_requested.emit(self.active_text_id)
                event.accept()
                return
            else:
                p_sec = getattr(self, 'playhead_pos_sec', 0.0)
                v_clips = getattr(self, 'video_clips', [])
                for c_i, c in enumerate(v_clips):
                    c_st = c.get("start", 0.0)
                    c_dur = c.get("duration", 0.0)
                    if c_st <= p_sec <= (c_st + c_dur):
                        self.video_clip_delete_requested.emit(c_i)
                        event.accept()
                        return
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space_pressed = True
            self.setCursor(Qt.OpenHandCursor)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space_pressed = False
            self.setCursor(Qt.ArrowCursor)
        else:
            super().keyReleaseEvent(event)




# ==================== DEDICATED PASTE SRT DIALOG ====================
class PasteSRTDialog(QDialog):
    """Dedicated Dialog for pasting, reviewing, and applying SRT subtitles into Desktop Studio."""
    subtitles_applied = Signal(str)

    def __init__(self, parent=None, initial_text=""):
        super().__init__(parent)
        self.setWindowTitle("📋 កន្លែង Paste SRT Subtitle (Paste SRT Zone)")
        self.resize(760, 560)
        self.setMinimumSize(600, 420)
        self._init_ui(initial_text)

    def _init_ui(self, initial_text):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 1. Header Banner
        header = QFrame(self)
        header.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1e1b4b, stop:1 #0f172a);
                border: 1px solid #6366f1;
                border-radius: 10px;
                padding: 8px;
            }
        """)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(8, 4, 8, 4)

        icon_lbl = QLabel("📋", self)
        icon_lbl.setStyleSheet("font-size: 26px;")
        h_lay.addWidget(icon_lbl)

        info_lay = QVBoxLayout()
        title_lbl = QLabel("កន្លែង Paste អត្ថបទ SRT ចូល Desktop Studio", self)
        title_lbl.setStyleSheet("color: #ffffff; font-weight: bold; font-size: 14px;")
        desc_lbl = QLabel("លោកអ្នកអាចចុច Cmd+V ឬចុច Mouse ស្ដាំ ➔ Paste អត្ថបទ SRT ពី Web Studio, ChatGPT ឬ File ចូលក្នុងប្រអប់ខាងក្រោម៖", self)
        desc_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        info_lay.addWidget(title_lbl)
        info_lay.addWidget(desc_lbl)
        h_lay.addLayout(info_lay)
        h_lay.addStretch()

        layout.addWidget(header)

        # 2. Quick Action Toolbar
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)

        self.paste_clip_btn = QPushButton("📋 Paste ពី Clipboard (Cmd+V)", self)
        self.paste_clip_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0284c7, stop:1 #0369a1);
                color: #ffffff; font-weight: bold; font-size: 11px; border-radius: 6px; padding: 6px 14px;
            }
            QPushButton:hover { background: #38bdf8; color: #000; }
        """)
        self.paste_clip_btn.clicked.connect(self._paste_from_clipboard)
        action_bar.addWidget(self.paste_clip_btn)

        self.fetch_web_btn = QPushButton("⚡ យក SRT ពី Web Studio", self)
        self.fetch_web_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #059669, stop:1 #047857);
                color: #ffffff; font-weight: bold; font-size: 11px; border-radius: 6px; padding: 6px 14px;
            }
            QPushButton:hover { background: #34d399; color: #000; }
        """)
        self.fetch_web_btn.clicked.connect(self._fetch_from_web_studio)
        action_bar.addWidget(self.fetch_web_btn)

        self.open_file_btn = QPushButton("📂 បើក File SRT...", self)
        self.open_file_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #cbd5e1; font-size: 11px; border: 1px solid #334155; border-radius: 6px; padding: 6px 12px;
            }
            QPushButton:hover { background-color: #334155; color: #fff; }
        """)
        self.open_file_btn.clicked.connect(self._open_srt_file)
        action_bar.addWidget(self.open_file_btn)

        self.clear_btn = QPushButton("🧹 Clear", self)
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #94a3b8; font-size: 11px; border: 1px solid #334155; border-radius: 6px; padding: 6px 10px;
            }
            QPushButton:hover { background-color: #475569; color: #fff; }
        """)
        self.clear_btn.clicked.connect(self._clear_text)
        action_bar.addWidget(self.clear_btn)

        action_bar.addStretch()

        self.status_lbl = QLabel("", self)
        self.status_lbl.setStyleSheet("color: #38bdf8; font-size: 11px; font-weight: bold;")
        action_bar.addWidget(self.status_lbl)

        layout.addLayout(action_bar)

        # 3. Big SRT Text Area
        self.text_edit = QTextEdit(self)
        self.text_edit.setPlaceholderText(
            "📋 ចុច Cmd+V ឬ Right-Click ➔ Paste អត្ថបទ SRT នៅទីនេះ...\n\n"
            "ទម្រង់គំរូ SRT:\n"
            "1\n"
            "00:00:01,000 --> 00:00:04,500\n"
            "[ប្រុស] សួស្តីអ្នកទាំងអស់គ្នា!\n\n"
            "2\n"
            "00:00:05,000 --> 00:00:08,200\n"
            "[ស្រី] ថ្ងៃនេះយើងនឹងសិក្សាអំពី AI..."
        )
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #030712;
                color: #e2e8f0;
                border: 2px solid #334155;
                border-radius: 8px;
                font-family: Menlo, Monaco, 'Courier New', monospace;
                font-size: 12px;
                padding: 10px;
                line-height: 1.4;
            }
        """)

        # Pre-create count_lbl before textChanged events can fire
        self.count_lbl = QLabel("0 segments", self)
        self.count_lbl.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: bold;")

        self.text_edit.textChanged.connect(self._update_segment_count)
        layout.addWidget(self.text_edit, stretch=1)

        # Pre-fill text if available
        if initial_text:
            self.text_edit.setPlainText(initial_text)
        else:
            from qt_compat import QApplication
            clip_txt = (QApplication.clipboard().text() or "").strip()
            if clip_txt and ("-->" in clip_txt or clip_txt.startswith("[") or clip_txt.startswith("{")):
                self.text_edit.setPlainText(clip_txt)
                self.status_lbl.setText("✅ បានទាញយកពី Clipboard ស្វ័យប្រវត្តិ!")

        # 4. Bottom Action Bar
        bot_bar = QHBoxLayout()
        bot_bar.addWidget(self.count_lbl)
        self._update_segment_count()

        bot_bar.addStretch()

        self.cancel_btn = QPushButton("បោះបង់ (Cancel)", self)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #94a3b8; font-weight: bold; border-radius: 6px; padding: 8px 16px;
            }
            QPushButton:hover { background-color: #334155; color: #fff; }
        """)
        self.cancel_btn.clicked.connect(self.reject)
        bot_bar.addWidget(self.cancel_btn)

        self.apply_btn = QPushButton("✅ នាំចូលទៅក្នុងតារាង (Import Subtitles)", self)
        self.apply_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #7c3aed, stop:1 #6d28d9);
                color: #ffffff; font-weight: bold; font-size: 12px; border-radius: 6px; padding: 8px 20px;
            }
            QPushButton:hover { background: #8b5cf6; }
        """)
        self.apply_btn.clicked.connect(self._apply_subtitles)
        bot_bar.addWidget(self.apply_btn)

        layout.addLayout(bot_bar)

    def _paste_from_clipboard(self):
        from qt_compat import QApplication
        txt = (QApplication.clipboard().text() or "").strip()
        if txt:
            self.text_edit.setPlainText(txt)
            self.status_lbl.setText("✅ បាន Paste ពី Clipboard!")
        else:
            self.status_lbl.setText("⚠️ មិនមានអត្ថបទក្នុង Clipboard ឡើយ")

    def _fetch_from_web_studio(self):
        from utils.file_utils import OUTPUT_DIR
        srt_file = os.path.join(str(OUTPUT_DIR), "latest_web_subtitles.srt")
        json_file = os.path.join(str(OUTPUT_DIR), "latest_web_subtitles.json")
        files_to_check = []
        if os.path.exists(json_file):
            files_to_check.append((os.path.getmtime(json_file), json_file, "JSON with Character Tags"))
        if os.path.exists(srt_file):
            files_to_check.append((os.path.getmtime(srt_file), srt_file, "SRT"))
        
        if files_to_check:
            files_to_check.sort(key=lambda x: x[0], reverse=True)
            chosen_file = files_to_check[0][1]
            label = files_to_check[0][2]
            try:
                with open(chosen_file, "r", encoding="utf-8") as f:
                    self.text_edit.setPlainText(f.read())
                self.status_lbl.setText(f"✅ បានទាញយកពី Web Studio ({label})!")
                return
            except Exception as e:
                pass
        self.status_lbl.setText("⚠️ រកមិនឃើញ Subtitle ថ្មីពី Web Studio ឡើយ")

    def _open_srt_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open SRT / Subtitle File", "", "Subtitle Files (*.srt *.vtt *.txt *.json)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    self.text_edit.setPlainText(f.read())
                self.status_lbl.setText(f"✅ បានបើក: {os.path.basename(file_path)}")
            except Exception as e:
                self.status_lbl.setText(f"⚠️ កំហុស: {e}")

    def _clear_text(self):
        self.text_edit.clear()
        self.status_lbl.setText("")

    def _update_segment_count(self):
        txt = self.text_edit.toPlainText().strip()
        if not txt:
            self.count_lbl.setText("0 segments")
            return
        c = txt.count("-->")
        if c > 0:
            self.count_lbl.setText(f"📊 រកឃើញ {c} subtitle segments")
        elif txt.startswith("[") or txt.startswith("{"):
            self.count_lbl.setText("📊 JSON format detected")
        else:
            lines = [l for l in txt.split("\n") if l.strip()]
            self.count_lbl.setText(f"📊 {len(lines)} lines")

    def _apply_subtitles(self):
        txt = self.text_edit.toPlainText().strip()
        if not txt:
            QMessageBox.warning(self, "Warning", "សូម Paste ឬបញ្ចូលអត្ថបទ SRT ជាមុនសិន។")
            return
        self.subtitles_applied.emit(txt)
        self.accept()


# ==================== SUBTITLE TABLE WITH VOICE ACTIONS & CLONE VOICE ====================
class SubtitleTableWidget(QWidget):
    """Enhanced Subtitle Table with Voice Actions, Speaker Detection, Voice Categories, and Voice Cloning"""
    seek_requested = Signal(float)
    generate_voices_requested = Signal()
    cancel_voices_requested = Signal()
    subtitle_style_changed = Signal(str)
    gemini_ai_requested = Signal()
    paste_srt_requested = Signal()
    export_srt_requested = Signal()
    export_khmer_srt_requested = Signal()
    export_original_srt_requested = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.segments = []
        self.clone_voice_samples = {}
        self._init_ui()

    def set_voice_generation_state(self, is_running: bool):
        """Toggle UI between Synthesizing and Idle state with instant Cancel support."""
        if is_running:
            self.generate_voices_btn.setEnabled(False)
            self.generate_voices_btn.setText("Synthesizing...")
            self.cancel_voices_btn.setVisible(True)
            self.cancel_voices_btn.setEnabled(True)
        else:
            self.generate_voices_btn.setEnabled(True)
            self.generate_voices_btn.setText("Generate Voices")
            self.cancel_voices_btn.setVisible(False)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Top Toolbar for Subtitle & Voice Actions (Clean 1-Row Streamlined Layout)
        toolbar = QFrame(self)
        toolbar.setObjectName("subtitleToolbar")
        toolbar.setStyleSheet("""
            QFrame#subtitleToolbar {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0a0e1c, stop:0.5 #0f152a, stop:1 #0a0e1c);
                border: 1px solid #1a233a;
                border-radius: 8px;
                padding: 4px 8px;
            }
        """)
        tb_vbox = QVBoxLayout(toolbar)
        tb_vbox.setContentsMargins(6, 5, 6, 5)
        tb_vbox.setSpacing(6)

        # Row 1: Action buttons (Auto Subtitle & Voice Generation)
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(6)

        # 1. AI Subtitle Generation
        self.gemini_ai_btn = QPushButton("✨ Auto Subtitle", self)
        self.gemini_ai_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1d4ed8, stop:0.5 #2563eb, stop:1 #06b6d4);
                color: #ffffff;
                font-weight: 800;
                font-size: 11px;
                border: 1px solid #38bdf8;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #0891b2);
            }
        """)
        self.gemini_ai_btn.setToolTip("បង្កើតអក្សររត់ និងបកប្រែជាភាសាខ្មែរតាម Gemini AI ចូលក្នុងតារាង (Auto Subtitle Generation)")
        self.gemini_ai_btn.clicked.connect(self.gemini_ai_requested.emit)
        row1.addWidget(self.gemini_ai_btn)

        # 2. Voice Generation
        self.generate_voices_btn = QPushButton("🎙 Generate Voices", self)
        self.generate_voices_btn.setProperty("class", "btn-primary")
        self.generate_voices_btn.setIcon(get_svg_icon("generate", "#ffffff", 14))
        self.generate_voices_btn.setToolTip("សំយោគសំឡេងខ្មែរគ្រប់ជួរទាំងអស់ (Generate All Voices) ដើម្បីចាក់ស្តាប់សាកល្បងជាមួយវីដេអូ")
        self.generate_voices_btn.clicked.connect(self.generate_voices_requested.emit)
        row1.addWidget(self.generate_voices_btn)

        # Cancel Voice Generation Button (Visible only when generating)
        self.cancel_voices_btn = QPushButton("Cancel", self)
        self.cancel_voices_btn.setProperty("class", "btn-red")
        self.cancel_voices_btn.setIcon(get_svg_icon("cancel", "#ffffff", 13))
        self.cancel_voices_btn.setToolTip("បញ្ឈប់ការសំយោគសំឡេងភ្លាមៗ (Stop Voice Generation)")
        self.cancel_voices_btn.setVisible(False)
        self.cancel_voices_btn.clicked.connect(self.cancel_voices_requested.emit)
        row1.addWidget(self.cancel_voices_btn)

        # 3. Export SRT (Placed right next to Generate Voices)
        self.export_srt_btn = QPushButton("📤 Export SRT", self)
        self.export_srt_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f172a, stop:0.5 #1e293b, stop:1 #0f172a);
                color: #38bdf8;
                font-weight: 700;
                font-size: 11px;
                border: 1px solid #0284c7;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background: #0284c7;
                color: #ffffff;
            }
        """)
        self.export_srt_btn.setIcon(get_svg_icon("export", "#38bdf8", 13))
        self.export_srt_btn.setToolTip("ទាញយក ឬរក្សាទុកអក្សររត់ជាឯកសារ SRT (Export Subtitles to SRT File)")
        self.export_srt_btn.clicked.connect(self._on_export_srt_clicked)
        row1.addWidget(self.export_srt_btn)

        row1.addStretch()
        tb_vbox.addLayout(row1)

        # Row 2: Voice Preset selection and batch apply
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)

        voice_lbl = QLabel("🗣 Voice:", self)
        voice_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
        row2.addWidget(voice_lbl)

        self.voice_preset_combo = QComboBox(self)
        self.voice_preset_combo.addItems(list(VOICE_PRESETS.keys()))
        self.voice_preset_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122;
                border: 1px solid #1e2942;
                border-radius: 4px;
                padding: 3px 8px;
                color: #e2e8f0;
                font-size: 11px;
                font-weight: 600;
            }
            QComboBox:hover { border-color: #38bdf8; }
        """)
        row2.addWidget(self.voice_preset_combo, stretch=1)

        self.apply_voice_btn = QPushButton("✓ Apply", self)
        self.apply_voice_btn.setProperty("class", "btn-primary")
        self.apply_voice_btn.setIcon(get_svg_icon("check", "#ffffff", 13))
        self.apply_voice_btn.setToolTip("អនុវត្ត Voice ខាងលើទៅលើគ្រប់បន្ទាត់ដែលបាន Select (ឬទាំងអស់)")
        self.apply_voice_btn.clicked.connect(self._apply_voice_to_selected)
        row2.addWidget(self.apply_voice_btn)
        tb_vbox.addLayout(row2)

        # Retain backward-compatible references as None (no widgets created)
        self.paste_srt_btn = None
        self.scene_preview_btn = None
        self.spk_profiles_btn = None
        self.quick_style_combo = None

        layout.addWidget(toolbar)

        # Table Widget (9 Columns: #, Start, End, Persona, Emotion, Style, Khmer Text, Voice, Action)
        self.table = QTableWidget(0, 9, self)
        self.table.setHorizontalHeaderLabels([
            "#", "START", "END", "PERSONA", "EMOTION", "STYLE", "KHMER TEXT", "VOICE", "▶"
        ])
        
        # Hide duplicate row number header and top-left box
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setShowGrid(False)
        self.table.setCornerButtonEnabled(False)

        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setStyleSheet("background-color: #0d1222; border: none;")
        if header.viewport():
            header.viewport().setStyleSheet("background-color: #0d1222; border: none;")

        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 32)   # #
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 62)   # Start (62px prevents '23.5...' cutoff)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 62)   # End (62px prevents '26.3...' cutoff)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 90)   # Persona (90px prevents 'Male Ad' cutoff)
        header.setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(4, 90)   # Emotion (90px prevents 'Neutra' cutoff)
        header.setSectionResizeMode(5, QHeaderView.Interactive)
        self.table.setColumnWidth(5, 84)   # Style (84px prevents 'Norm' cutoff)
        header.setSectionResizeMode(6, QHeaderView.Stretch)       # Khmer Text (Expands to fill all available width!)
        header.setSectionResizeMode(7, QHeaderView.Interactive)
        self.table.setColumnWidth(7, 105)  # Voice
        header.setSectionResizeMode(8, QHeaderView.Fixed)
        self.table.setColumnWidth(8, 38)   # Action (Single Play Button)

        # Center align headers for #, START, END, and ACTION
        for col_i in (0, 1, 2, 8):
            hi = self.table.horizontalHeaderItem(col_i)
            if hi:
                hi.setTextAlignment(Qt.AlignCenter)

        # Visually reorder so KHMER TEXT & VOICE appear directly after timing (#, START, END)
        header.moveSection(header.visualIndex(6), 3)  # Logical 6 (KHMER TEXT) -> Visual 3
        header.moveSection(header.visualIndex(7), 4)  # Logical 7 (VOICE) -> Visual 4
        
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #0b101e;
                border: 1px solid #1a243d;
                border-radius: 8px;
                gridline-color: transparent;
                color: #e2e8f0;
                font-size: 12px;
                alternate-background-color: #0f1527;
                selection-background-color: #192747;
                selection-color: #38bdf8;
                outline: none;
            }
            QTableWidget::item {
                padding: 4px 6px;
                border-bottom: 1px solid #141c30;
            }
            QTableWidget::item:selected {
                background-color: #1a2b4c;
                color: #38bdf8;
            }
            QHeaderView {
                background-color: #0d1222;
                border: none;
            }
            QHeaderView::section {
                background-color: #0d1222;
                color: #7dd3fc;
                padding: 7px 8px;
                border: none;
                border-bottom: 2px solid #1e293b;
                font-weight: 700;
                font-size: 10px;
                letter-spacing: 0.6px;
            }
            QTableCornerButton::section {
                background-color: #0d1222;
                border: none;
            }
        """)

        self.table.itemClicked.connect(self._on_table_item_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        layout.addWidget(self.table)

    def _on_export_srt_clicked(self):
        """Show popup menu to export either Khmer SRT or Original Source SRT."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QPoint
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #18191c;
                border: 1px solid #334155;
                color: #e2e8f0;
                padding: 5px;
                border-radius: 6px;
            }
            QMenu::item {
                padding: 7px 16px;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 600;
            }
            QMenu::item:selected {
                background-color: #0284c7;
                color: #ffffff;
            }
        """)
        act_khmer = menu.addAction("🇰🇭 Export Khmer Subtitle (.srt)")
        act_orig = menu.addAction("🌐 Export Original Source Subtitle (.srt)")

        chosen = menu.exec(self.export_srt_btn.mapToGlobal(QPoint(0, self.export_srt_btn.height() + 2)))
        if chosen == act_khmer:
            self.export_khmer_srt_requested.emit()
            self.export_srt_requested.emit()
        elif chosen == act_orig:
            self.export_original_srt_requested.emit()

    def _on_table_item_clicked(self, item):
        if not item:
            return
        row = item.row()
        if 0 <= row < len(self.segments):
            start_sec = float(self.segments[row].get("start", 0.0))
            self.seek_requested.emit(start_sec)

    def _update_voice_presets(self, category: str):
        """Update voice preset dropdown based on selected category"""
        self.voice_preset_combo.clear()
        if category == "All":
            self.voice_preset_combo.addItems(list(VOICE_PRESETS.keys()))
        else:
            voices = VOICE_CATEGORIES.get(category, [])
            self.voice_preset_combo.addItems(voices if voices else list(VOICE_PRESETS.keys()))

    def set_segments(self, segments: list):
        self.segments = [s.to_dict() if hasattr(s, 'to_dict') else s for s in (segments or [])]
        
        # High Performance UI Optimization: Disable updates and batch rows
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.clearContents()
            self.table.setRowCount(len(self.segments))

            for i, seg in enumerate(self.segments):
                self._insert_segment_row(i, seg)
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)


    def _insert_segment_row(self, row: int, seg: dict):
        if row >= self.table.rowCount():
            self.table.insertRow(row)
        
        # 0: Index #
        idx_item = QTableWidgetItem(str(row + 1))
        idx_item.setFlags(idx_item.flags() & ~Qt.ItemIsEditable)
        idx_item.setTextAlignment(Qt.AlignCenter)
        idx_item.setForeground(QtGui.QColor("#64748b"))
        font_idx = QtGui.QFont("Inter, -apple-system, sans-serif", 10, QtGui.QFont.Bold)
        idx_item.setFont(font_idx)
        self.table.setItem(row, 0, idx_item)
        
        # 1: Start
        start_val = seg.get('start', 0.0)
        item_start = QTableWidgetItem(f"{start_val:.2f}s")
        item_start.setFlags(item_start.flags() & ~Qt.ItemIsEditable)
        item_start.setTextAlignment(Qt.AlignCenter)
        item_start.setForeground(QtGui.QColor("#38bdf8"))
        font_time = QtGui.QFont("Menlo" if sys.platform == "darwin" else "Courier New", 10)
        item_start.setFont(font_time)
        self.table.setItem(row, 1, item_start)

        # 2: End
        end_val = seg.get('end', 0.0)
        item_end = QTableWidgetItem(f"{end_val:.2f}s")
        item_end.setFlags(item_end.flags() & ~Qt.ItemIsEditable)
        item_end.setTextAlignment(Qt.AlignCenter)
        item_end.setForeground(QtGui.QColor("#38bdf8"))
        item_end.setFont(font_time)
        self.table.setItem(row, 2, item_end)

        # 3: Persona / Speaker Persona Combo
        persona_combo = QComboBox(self)
        persona_combo.addItems(PERSONA_CHOICES)
        raw_persona = seg.get("persona") or seg.get("character") or ""
        khm_txt = seg.get("khmer_text") or ""
        spk_tag = seg.get("speaker_tag") or ""
        gender = (seg.get("gender") or "").lower()

        if raw_persona in PERSONA_CHOICES:
            cur_persona = raw_persona
        elif "[ក្មេង]" in khm_txt or "[ក្មេង]" in spk_tag or gender == "child":
            cur_persona = "Boy / Child"
        elif "[ចាស់ស្រី]" in khm_txt or "[ចាស់ស្រី]" in spk_tag or gender == "elder_female":
            cur_persona = "Elderly Female"
        elif "[ចាស់ប្រុស]" in khm_txt or "[ចាស់ប្រុស]" in spk_tag or "[ចាស់]" in khm_txt or "[ចាស់]" in spk_tag or gender in ("elder", "elder_male"):
            cur_persona = "Elderly Male"
        elif "[ស្រី]" in khm_txt or "[ស្រី]" in spk_tag or gender == "female":
            cur_persona = "Female Adult"
        else:
            cur_persona = normalize_persona(raw_persona)

        if cur_persona in PERSONA_CHOICES:
            persona_combo.setCurrentText(cur_persona)
        else:
            persona_combo.setCurrentIndex(0)
        persona_combo.setStyleSheet("""
            QComboBox {
                background-color: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 2px 5px;
                color: #38bdf8;
                font-size: 10.5px;
                font-weight: bold;
            }
            QComboBox:hover {
                border-color: #38bdf8;
                background-color: #14223d;
            }
            QComboBox::drop-down { border: none; width: 12px; }
            QComboBox QAbstractItemView {
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
                color: #f1f5f9;
                selection-background-color: #1e293b;
                selection-color: #38bdf8;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 3px 8px;
            }
        """)
        self.table.setCellWidget(row, 3, persona_combo)

        # 4: Emotion Combo (9 Emotions)
        emotion_combo = QComboBox(self)
        emotion_combo.addItems(EMOTION_CHOICES)
        cur_emotion = normalize_emotion(seg.get("emotion") or "Neutral")
        if cur_emotion in EMOTION_CHOICES:
            emotion_combo.setCurrentText(cur_emotion)
        else:
            emotion_combo.setCurrentIndex(0)
        emotion_combo.setStyleSheet("""
            QComboBox {
                background-color: #141624;
                border: 1px solid #28243d;
                border-radius: 6px;
                padding: 2px 5px;
                color: #facc15;
                font-size: 10.5px;
                font-weight: 600;
            }
            QComboBox:hover {
                border-color: #facc15;
                background-color: #211e38;
            }
            QComboBox::drop-down { border: none; width: 12px; }
            QComboBox QAbstractItemView {
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
                color: #f1f5f9;
                selection-background-color: #1e293b;
                selection-color: #facc15;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 3px 8px;
            }
        """)
        self.table.setCellWidget(row, 4, emotion_combo)

        # 5: Speaking Style Combo (8 Styles)
        style_combo = QComboBox(self)
        style_combo.addItems(STYLE_CHOICES)
        cur_style = seg.get("speaking_style") or seg.get("style") or "Normal"
        if cur_style in STYLE_CHOICES:
            style_combo.setCurrentText(cur_style)
        else:
            style_combo.setCurrentIndex(0)
        style_combo.setStyleSheet("""
            QComboBox {
                background-color: #151426;
                border: 1px solid #2d204a;
                border-radius: 6px;
                padding: 2px 5px;
                color: #c084fc;
                font-size: 10.5px;
                font-weight: 600;
            }
            QComboBox:hover {
                border-color: #c084fc;
                background-color: #221b3b;
            }
            QComboBox::drop-down { border: none; width: 12px; }
            QComboBox QAbstractItemView {
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
                color: #f1f5f9;
                selection-background-color: #1e293b;
                selection-color: #c084fc;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 3px 8px;
            }
        """)
        self.table.setCellWidget(row, 5, style_combo)

        # 6: Khmer Text (Editable)
        disp_text = seg.get("khmer_text", "")
        if not disp_text:
            disp_text = seg.get("original_text", seg.get("text", ""))
        text_item = QTableWidgetItem(disp_text)
        text_item.setForeground(QtGui.QColor("#f8fafc"))
        text_font = QtGui.QFont("Kantumruy Pro", 12)
        if not text_font.exactMatch():
            text_font = QtGui.QFont("Khmer OS Battambang", 12)
        text_item.setFont(text_font)
        text_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.table.setItem(row, 6, text_item)

        # 7: Voice Selector Combo
        v_combo = QComboBox(self)
        v_combo.addItems(list(VOICE_PRESETS.keys()))
        
        current_voice = seg.get("voice_id") or seg.get("voice")
        if not current_voice or current_voice not in VOICE_PRESETS:
            current_voice = PERSONA_DEFAULT_VOICE.get(cur_persona, "Khmer Male - Piseth")

        if current_voice in VOICE_PRESETS:
            v_combo.setCurrentText(current_voice)
        else:
            v_combo.setCurrentIndex(0)
        
        v_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122;
                border: 1px solid #1e2942;
                border-radius: 6px;
                padding: 3px 8px;
                color: #f1f5f9;
                font-size: 11px;
            }
            QComboBox:hover {
                border-color: #60a5fa;
                background-color: #121c36;
            }
            QComboBox::drop-down { border: none; width: 14px; }
            QComboBox QAbstractItemView {
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
                color: #f1f5f9;
                selection-background-color: #1e293b;
                selection-color: #38bdf8;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 3px 8px;
            }
        """)
        self.table.setCellWidget(row, 7, v_combo)

        # Auto-suggest default voice when persona changes
        persona_combo.currentTextChanged.connect(
            lambda new_p, vc=v_combo: vc.setCurrentText(PERSONA_DEFAULT_VOICE.get(new_p, "Khmer Male - Piseth"))
        )

        # 8: Action Button (▶ Single Line Preview Button)
        action_widget = QWidget(self)
        action_lay = QHBoxLayout(action_widget)
        action_lay.setContentsMargins(2, 2, 2, 2)
        action_lay.setSpacing(0)
        action_lay.setAlignment(Qt.AlignCenter)

        play_btn = QPushButton("▶", self)
        play_btn.setToolTip("▶ ចាក់ស្តាប់សម្លេងបន្ទាត់នេះ (Play Line Voice)")
        play_btn.setFixedSize(36, 26)
        play_btn.setCursor(Qt.PointingHandCursor)
        play_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #10b981, stop:1 #059669);
                color: #ffffff;
                border: 1px solid #34d399;
                border-radius: 6px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #34d399, stop:1 #10b981);
                border-color: #6ee7b7;
            }
            QPushButton:pressed {
                background-color: #047857;
            }
        """)
        play_btn.clicked.connect(lambda _, r=row: self._preview_tts(r))
        action_lay.addWidget(play_btn)

        self.table.setCellWidget(row, 8, action_widget)

    def update_segment_timing(self, seg_idx: int, start_sec: float, end_sec: float):
        """Update subtitle segment start and end times dynamically from timeline dragging."""
        if 0 <= seg_idx < len(self.segments):
            self.segments[seg_idx]["start"] = round(start_sec, 2)
            self.segments[seg_idx]["end"] = round(end_sec, 2)
            if seg_idx < self.table.rowCount():
                st_item = self.table.item(seg_idx, 1)
                if st_item:
                    st_item.setText(f"{start_sec:.2f}s")
                et_item = self.table.item(seg_idx, 2)
                if et_item:
                    et_item.setText(f"{end_sec:.2f}s")

    def get_updated_segments(self) -> list:
        updated = []
        for i in range(self.table.rowCount()):
            start_item = self.table.item(i, 1)
            end_item = self.table.item(i, 2)
            persona_widget = self.table.cellWidget(i, 3)
            emotion_widget = self.table.cellWidget(i, 4)
            style_widget = self.table.cellWidget(i, 5)
            text_item = self.table.item(i, 6)
            voice_widget = self.table.cellWidget(i, 7)
            
            try:
                st_str = start_item.text().replace("s", "") if start_item else f"{i * 3.0}"
                et_str = end_item.text().replace("s", "") if end_item else f"{(i + 1) * 3.0}"
                st = float(st_str)
                et = float(et_str)
            except Exception:
                st, et = i * 3.0, (i + 1) * 3.0

            curr_text = text_item.text() if text_item else ""
            persona = normalize_persona(persona_widget.currentText()) if persona_widget else "Male Adult"
            emotion = normalize_emotion(emotion_widget.currentText()) if emotion_widget else "Neutral"
            style = style_widget.currentText() if style_widget else "Normal"
            voice = voice_widget.currentText() if voice_widget else PERSONA_DEFAULT_VOICE.get(persona, "Khmer Male - Piseth")
            
            orig_seg = self.segments[i] if i < len(self.segments) else {}
            orig_text = orig_seg.get("original_text", orig_seg.get("text", curr_text))
            spk_id = orig_seg.get("speaker_id") or orig_seg.get("speaker") or f"speaker_{1 + (i % 2):02d}"

            seg_obj = Segment(
                id=str(orig_seg.get("id", i + 1)),
                start=st,
                end=et,
                speaker_id=spk_id,
                persona=persona,
                emotion=emotion,
                speaking_style=style,
                original_text=orig_text,
                translated_text=curr_text,
                voice_id=voice,
                tts_audio=orig_seg.get("tts_audio")
            )
            updated.append(seg_obj.to_dict())
        return updated

    # ==================== VOICE PROMPT DESIGNER DIALOG ====================
    def _open_voice_prompt_designer(self):
        """Open the AI Voice Studio Dialog (Prompt -> Text -> Generate -> Result)"""
        dialog = AIVoiceStudioDialog(self)
        dialog.exec()
        # Always refresh voice presets dropdowns
        self._update_voice_presets("All")

    # ==================== CLONE VOICE & ADD VOICE STUDIO ====================
    def _add_custom_voice_dialog(self, initial_audio=None, initial_text=None, initial_name=None):
        """Developer-Grade VoxCPM2 Voice Clone Studio Dialog"""
        from utils.file_utils import get_temp_path
        from services.voxcpm_service import (
            VOICE_PRESETS, VOICE_NAMES, VOICE_CATEGORIES, 
            add_custom_voice_preset, preprocess_reference_audio,
            extract_best_speech_segment, extract_segment_audio,
            extract_audio_from_video, export_mp3, VoxCPM2Runner
        )
        
        dialog = QDialog(self)
        dialog.setWindowTitle("🎙️ VoxCPM2 Neural Voice Clone & Voice Studio")
        dialog.setMinimumSize(680, 700)
        dialog.resize(720, 720)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #080c16;
                color: #f1f5f9;
                font-family: 'Segoe UI', 'Kantumruy Pro', 'Khmer OS Battambang', sans-serif;
            }
            QLabel {
                color: #e2e8f0;
                font-size: 12px;
            }
            QLineEdit, QTextEdit {
                background-color: #0c101d;
                border: 1px solid #1e2942;
                border-radius: 6px;
                padding: 7px 10px;
                color: #f1f5f9;
                font-size: 12px;
            }
            QLineEdit:focus, QTextEdit:focus {
                border: 1px solid #38bdf8;
            }
            QTabWidget::pane {
                border: 1px solid #1e2942;
                border-radius: 8px;
                background-color: #0a0e1a;
                top: -1px;
            }
            QTabBar::tab {
                background: #080c16;
                color: #94a3b8;
                border: 1px solid #1e2942;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 2px;
                font-weight: 600;
                font-size: 12px;
            }
            QTabBar::tab:selected {
                background: #0a0e1a;
                color: #38bdf8;
                border-color: #2563eb;
                border-bottom: 2px solid #38bdf8;
            }
            QGroupBox {
                background-color: #0c101d;
                border: 1px solid #1a233a;
                border-radius: 8px;
                padding: 12px;
                margin-top: 10px;
                font-size: 11px;
                font-weight: bold;
                color: #38bdf8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
            }
            QScrollArea {
                border: 1px solid #1e2942;
                border-radius: 8px;
                background-color: #080c16;
            }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header Title Banner
        banner = QFrame(dialog)
        banner.setStyleSheet("background-color: #0c101d; border: 1px solid #1a233a; border-radius: 8px; padding: 6px 12px;")
        ban_lay = QHBoxLayout(banner)
        ban_lay.setContentsMargins(4, 4, 4, 4)
        
        ban_title = QLabel("🎙️ VoxCPM2 Neural Voice Clone & Studio", banner)
        ban_title.setStyleSheet("color: #38bdf8; font-size: 15px; font-weight: 800;")
        ban_sub = QLabel("Zero-Shot Timbre Cloning • 8-Band Formant EQ • Multi-Speaker Studio", banner)
        ban_sub.setStyleSheet("color: #64748b; font-size: 11px;")
        
        ban_col = QVBoxLayout()
        ban_col.addWidget(ban_title)
        ban_col.addWidget(ban_sub)
        ban_lay.addLayout(ban_col)
        ban_lay.addStretch()
        
        layout.addWidget(banner)

        # Non-blocking audio playback helper
        _current_audio_proc = [None]
        def _play_audio_non_blocking(audio_path: str):
            if not audio_path or not os.path.exists(audio_path):
                return
            try:
                if _current_audio_proc[0] and _current_audio_proc[0].poll() is None:
                    try: _current_audio_proc[0].terminate()
                    except Exception: pass
                
                if sys.platform == "darwin":
                    _current_audio_proc[0] = subprocess.Popen(["afplay", audio_path])
                elif sys.platform.startswith("linux"):
                    _current_audio_proc[0] = subprocess.Popen(["aplay", audio_path])
                elif sys.platform == "win32":
                    os.startfile(audio_path)
            except Exception as e:
                logger.error(f"Playback error: {e}")

        def _process_and_extract_ref_audio(file_path, name="RefVoice"):
            if not file_path or not os.path.exists(file_path):
                return None
            
            clean_audio = get_temp_path(f"clone_clean_{name}.wav")
            audio_path = file_path
            
            ext = os.path.splitext(file_path)[1].lower()
            if ext in ['.mp4', '.mov', '.mkv', '.avi', '.webm']:
                from services.audio_extractor import AudioExtractor
                temp_extracted = get_temp_path(f"clone_extract_{name}.wav")
                extracted = AudioExtractor().extract_audio(file_path, temp_extracted)
                if extracted and os.path.exists(extracted):
                    audio_path = extracted

            from services.vad_service import VADService
            best_seg = VADService().extract_best_speech_segment(audio_path)
            if not best_seg:
                logger.error(f"❌ [VAD] No human speech segments detected in '{audio_path}'")
                return None

            start_sec, end_sec = best_seg
            seg_audio = get_temp_path(f"clone_segment_{name}.wav")
            if extract_segment_audio(audio_path, start_sec, end_sec, seg_audio):
                audio_path = seg_audio

            from services.voice_cleaner import VoiceCleaner
            cleaned = VoiceCleaner().clean_reference_audio(audio_path, clean_audio)
            if cleaned and os.path.exists(cleaned):
                return cleaned

            return audio_path

        # Main Tab Widget
        tabs = QTabWidget(dialog)

        # ==================== TAB 1: CLONE NEW VOICE ====================
        clone_tab = QWidget()
        c_lay = QVBoxLayout(clone_tab)
        c_lay.setContentsMargins(12, 12, 12, 12)
        c_lay.setSpacing(10)

        # Step 1: Voice Info Box
        step1_box = QGroupBox("1. Voice Profile & Identity", clone_tab)
        s1_lay = QHBoxLayout(step1_box)
        s1_lay.setSpacing(10)

        s1_lay.addWidget(QLabel("Voice Name:", step1_box))
        vn_input = QLineEdit(initial_name or "", step1_box)
        vn_input.setPlaceholderText("e.g. Khmer Male - Alex, Sokha Narrator...")
        s1_lay.addWidget(vn_input, stretch=2)

        s1_lay.addWidget(QLabel("Gender:", step1_box))
        gender_combo = QComboBox(step1_box)
        gender_combo.addItems(["male", "female"])
        s1_lay.addWidget(gender_combo)

        s1_lay.addWidget(QLabel("Category:", step1_box))
        cat_combo = QComboBox(step1_box)
        cat_combo.addItems(["Cloned Voices", "Adult Male", "Adult Female", "Child Boy", "Child Girl", "Elder", "Cartoon/Special"])
        s1_lay.addWidget(cat_combo)

        c_lay.addWidget(step1_box)

        # Step 2: Reference Audio Source
        step2_box = QGroupBox("2. Reference Audio / Video Source (5s - 30s Speech)", clone_tab)
        s2_lay = QVBoxLayout(step2_box)
        s2_lay.setSpacing(8)

        s2_row1 = QHBoxLayout()
        ref_input = QLineEdit(initial_audio or "", step2_box)
        ref_input.setPlaceholderText("Upload reference audio (*.wav, *.mp3, *.m4a) or video (*.mp4, *.mov)...")
        s2_row1.addWidget(ref_input, 1)

        upload_btn = QPushButton("📂 Browse...", step2_box)
        upload_btn.setProperty("class", "btn-gray")
        def _upload_file():
            f_path, _ = QFileDialog.getOpenFileName(
                dialog,
                "Select Reference Audio or Video",
                "",
                "Supported Files (*.wav *.mp3 *.m4a *.flac *.mp4 *.mov *.mkv *.avi);;All Files (*.*)"
            )
            if f_path:
                ref_input.setText(f_path)
        upload_btn.clicked.connect(_upload_file)
        s2_row1.addWidget(upload_btn)

        clean_voice_btn = QPushButton("🧹 Clean VAD", step2_box)
        clean_voice_btn.setProperty("class", "btn-green")
        def _clean_voice_action():
            f_path = ref_input.text().strip()
            if not f_path or not os.path.exists(f_path):
                QMessageBox.warning(dialog, "Warning", "Please select a reference audio or video file first.")
                return
            v_name = vn_input.text().strip() or "RefVoice"
            clean_audio = _process_and_extract_ref_audio(f_path, v_name)
            if clean_audio and os.path.exists(clean_audio):
                ref_status_lbl.setText(f"✓ Clean Speech Extracted: {os.path.basename(clean_audio)}")
                ref_status_lbl.setStyleSheet("color: #10b981; font-weight: bold;")
                _play_audio_non_blocking(clean_audio)
            else:
                QMessageBox.warning(dialog, "Error", "Failed to extract clean speech clip.")
        clean_voice_btn.clicked.connect(_clean_voice_action)
        s2_row1.addWidget(clean_voice_btn)

        play_ref_btn = QPushButton("▶ Audition", step2_box)
        play_ref_btn.setProperty("class", "btn-primary")
        def _play_ref_file():
            f_path = ref_input.text().strip()
            if not f_path or not os.path.exists(f_path):
                QMessageBox.warning(dialog, "Warning", "Please select a reference audio or video file first.")
                return
            clean_audio = _process_and_extract_ref_audio(f_path, vn_input.text().strip() or "RefVoice")
            if clean_audio and os.path.exists(clean_audio):
                _play_audio_non_blocking(clean_audio)
        play_ref_btn.clicked.connect(_play_ref_file)
        s2_row1.addWidget(play_ref_btn)

        s2_lay.addLayout(s2_row1)

        ref_status_lbl = QLabel("💡 Tip: Upload a clear speech clip without background music for best neural clone accuracy.", step2_box)
        ref_status_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        s2_lay.addWidget(ref_status_lbl)

        c_lay.addWidget(step2_box)

        # Step 3: Reference Transcript (Prompt Text)
        step3_box = QGroupBox("3. Reference Transcript (Original Spoken Text)", clone_tab)
        s3_lay = QHBoxLayout(step3_box)
        s3_lay.setSpacing(8)

        ref_txt_input = QLineEdit(step3_box)
        ref_txt_input.setPlaceholderText("អត្ថបទដើមរបស់ Reference Audio... (Optional: Leave blank for auto-transcribe)")
        s3_lay.addWidget(ref_txt_input, 1)

        lang_combo = QComboBox(step3_box)
        lang_combo.addItems(["Auto Detect", "English (en)", "Khmer (km)", "Chinese (zh)", "Japanese (ja)"])
        s3_lay.addWidget(lang_combo)

        transcribe_ref_btn = QPushButton("📝 Transcribe Ref", step3_box)
        transcribe_ref_btn.setProperty("class", "btn-purple")
        def _transcribe_ref_action():
            f_path = ref_input.text().strip()
            if not f_path or not os.path.exists(f_path):
                QMessageBox.warning(dialog, "Warning", "Please select a reference file first.")
                return
            v_name = vn_input.text().strip() or "RefVoice"
            clean_audio = _process_and_extract_ref_audio(f_path, v_name)
            if not clean_audio:
                QMessageBox.warning(dialog, "Warning", "Could not extract clean audio.")
                return
            lang_map = {"English (en)": "en", "Khmer (km)": "km", "Chinese (zh)": "zh", "Japanese (ja)": "ja", "Auto Detect": "auto"}
            sel_lang = lang_map.get(lang_combo.currentText(), "auto")
            from services.whisper_service import WhisperService
            try:
                txt = WhisperService().transcribe_reference(clean_audio, language=sel_lang)
                if txt:
                    ref_txt_input.setText(txt)
                    ref_status_lbl.setText(f"✓ Transcribed ({sel_lang.upper()}): {txt[:40]}...")
            except Exception as e:
                QMessageBox.warning(dialog, "Error", f"Transcription error: {e}")
        transcribe_ref_btn.clicked.connect(_transcribe_ref_action)
        s3_lay.addWidget(transcribe_ref_btn)

        c_lay.addWidget(step3_box)

        # Step 4: Live Test & Audition
        step4_box = QGroupBox("4. Test Neural Synthesis & Audition", clone_tab)
        s4_lay = QVBoxLayout(step4_box)
        s4_lay.setSpacing(8)

        s4_row = QHBoxLayout()
        test_input = QLineEdit(step4_box)
        test_input.setPlaceholderText("បញ្ចូលអត្ថបទខ្មែរដើម្បីធ្វើតេស្តសំឡេង...")
        test_input.setText("សួស្តីអ្នកទាំងអស់គ្នា! នេះជាសំឡេងដែលបាន Clone ចេញពី Video។")
        s4_row.addWidget(test_input, 1)

        eval_mode_combo = QComboBox(step4_box)
        eval_mode_combo.addItems(["Full Neural + F0 + EQ (Mode C)", "Neural + F0 Pitch (Mode B)", "Pure Neural (Mode A)"])
        s4_row.addWidget(eval_mode_combo)

        test_run_btn = QPushButton("▶ Test Clone Voice", step4_box)
        test_run_btn.setProperty("class", "btn-gold")
        def _test_clone_action():
            name = vn_input.text().strip() or "TestVoice"
            f_path = ref_input.text().strip()
            if not f_path or not os.path.exists(f_path):
                QMessageBox.warning(dialog, "Warning", "Please select a reference audio/video file first.")
                return
            clean_audio = _process_and_extract_ref_audio(f_path, name)
            if not clean_audio:
                QMessageBox.warning(dialog, "Warning", "No clean speech audio found in reference.")
                return
            
            manual_ref_txt = ref_txt_input.text().strip()
            mode_map = {
                "Full Neural + F0 + EQ (Mode C)": "full_c",
                "Neural + F0 Pitch (Mode B)": "f0_b",
                "Pure Neural (Mode A)": "pure_a"
            }
            mode_key = mode_map.get(eval_mode_combo.currentText(), "full_c")

            test_run_btn.setEnabled(False)
            test_run_btn.setText("⏳ Synthesizing...")
            QApplication.processEvents()

            try:
                runner = VoxCPM2Runner()
                test_text = test_input.text().strip() or "សួស្តីអ្នកទាំងអស់គ្នា!"
                out_wav = get_temp_path(f"clone_audition_{hash(name)}.wav")
                if runner.generate(test_text, clean_audio, manual_ref_txt, out_wav, mode=mode_key):
                    _play_audio_non_blocking(out_wav)
                else:
                    QMessageBox.warning(dialog, "Error", "Failed to synthesize cloned sample.")
            except Exception as e:
                QMessageBox.warning(dialog, "Error", f"Test synthesis error: {e}")
            finally:
                test_run_btn.setEnabled(True)
                test_run_btn.setText("▶ Test Clone Voice")

        test_run_btn.clicked.connect(_test_clone_action)
        s4_row.addWidget(test_run_btn)
        s4_lay.addLayout(s4_row)

        c_lay.addWidget(step4_box)

        # Primary CTA Save Button
        save_clone_btn = QPushButton("💾 Save Cloned Voice to Presets", clone_tab)
        save_clone_btn.setProperty("class", "btn-primary")
        save_clone_btn.setStyleSheet("font-weight: 800; font-size: 13px; padding: 10px 20px;")
        def _save_clone_voice():
            name = vn_input.text().strip()
            if not name:
                QMessageBox.warning(dialog, "Warning", "Please enter a Voice Name.")
                return
            f_path = ref_input.text().strip()
            if not f_path or not os.path.exists(f_path):
                QMessageBox.warning(dialog, "Warning", "Please select a reference audio or video file.")
                return
            clean_audio = _process_and_extract_ref_audio(f_path, name)
            if not clean_audio:
                QMessageBox.warning(dialog, "Warning", "No clean speech detected in reference.")
                return
            
            manual_ref_txt = ref_txt_input.text().strip()
            from services.voice_profile_service import VoiceProfileService
            VoiceProfileService().create_voice_profile(name, clean_audio, prompt_text=manual_ref_txt)

            voice_key = add_custom_voice_preset(
                name=name,
                prompt_audio_path=clean_audio,
                prompt_text=manual_ref_txt,
                category=cat_combo.currentText(),
                gender=gender_combo.currentText(),
                age="adult"
            )

            self._apply_cloned_voice_to_table(voice_key, target_char=name)
            _refresh_voice_list()
            QMessageBox.information(dialog, "Voice Cloned 🎉", f"✅ Voice '{voice_key}' has been added to Presets and is ready for Dubbing!")
            tabs.setCurrentIndex(1)

        save_clone_btn.clicked.connect(_save_clone_voice)
        c_lay.addWidget(save_clone_btn)

        tabs.addTab(clone_tab, "🎙️ Clone New Voice")

        # ==================== TAB 2: VOICE LIBRARY & MANAGEMENT ====================
        lib_tab = QWidget()
        lib_lay = QVBoxLayout(lib_tab)
        lib_lay.setContentsMargins(12, 12, 12, 12)
        lib_lay.setSpacing(10)

        lib_header = QLabel("📋 Active Voice Library & Preset Bank", lib_tab)
        lib_header.setStyleSheet("color: #38bdf8; font-size: 13px; font-weight: bold;")
        lib_lay.addWidget(lib_header)

        scroll = QScrollArea(lib_tab)
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_content.setStyleSheet("background-color: #080c16;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(6, 6, 6, 6)
        scroll_layout.setSpacing(6)
        scroll.setWidget(scroll_content)
        lib_lay.addWidget(scroll, 1)

        def _refresh_voice_list():
            while scroll_layout.count():
                child = scroll_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()

            idx = 1
            for name, preset in list(VOICE_PRESETS.items()):
                is_clone = preset.get("is_clone", False)
                type_tag = "[Custom Clone]" if is_clone else "[Standard Preset]"

                card = QFrame()
                card.setStyleSheet("""
                    QFrame {
                        background-color: #0c101d;
                        border: 1px solid #1a233a;
                        border-radius: 6px;
                    }
                    QFrame:hover {
                        border: 1px solid #38bdf8;
                        background-color: #0f1527;
                    }
                """)
                card_lay = QHBoxLayout(card)
                card_lay.setContentsMargins(10, 6, 10, 6)
                card_lay.setSpacing(8)

                lbl_text = f"{idx}. {name}"
                item_lbl = QLabel(lbl_text, card)
                item_lbl.setStyleSheet("color: #f1f5f9; font-size: 12px; font-weight: 700; background: transparent; border: none;")
                card_lay.addWidget(item_lbl)

                tag_lbl = QLabel(type_tag, card)
                tag_style = "color: #38bdf8; font-weight: bold;" if is_clone else "color: #64748b;"
                tag_lbl.setStyleSheet(f"{tag_style} font-size: 10px; background: transparent; border: none;")
                card_lay.addWidget(tag_lbl)
                card_lay.addStretch()

                # Audition Button
                play_btn = QPushButton("▶ Audition", card)
                play_btn.setProperty("class", "btn-gray")
                play_btn.setStyleSheet("font-size: 11px; padding: 4px 8px;")

                def _make_play_handler(vname, p_audio):
                    def _play():
                        if p_audio and os.path.exists(p_audio):
                            _play_audio_non_blocking(p_audio)
                        else:
                            from services.voxcpm_service import VoxCPMService
                            svc = VoxCPMService(voice_name=vname)
                            out = get_temp_path(f"preview_{abs(hash(vname))}.wav")
                            if svc.synthesize("សួស្តី! នេះគឺជាសំឡេងគំរូ។", out):
                                _play_audio_non_blocking(out)
                    return _play
                
                play_btn.clicked.connect(_make_play_handler(name, preset.get("prompt_audio_path")))
                card_lay.addWidget(play_btn)

                if is_clone:
                    # Rename Button
                    rename_btn = QPushButton("✏ Rename", card)
                    rename_btn.setProperty("class", "btn-gray")
                    rename_btn.setStyleSheet("font-size: 11px; padding: 4px 8px;")
                    
                    def _make_rename_handler(old_name):
                        def _rename():
                            from qt_compat import QInputDialog
                            new_name, ok = QInputDialog.getText(dialog, "Rename Voice", "Enter new voice name:", QLineEdit.Normal, old_name)
                            if ok and new_name.strip() and new_name.strip() != old_name:
                                new_key = new_name.strip()
                                VOICE_PRESETS[new_key] = VOICE_PRESETS.pop(old_name)
                                VOICE_PRESETS[new_key]["name"] = new_key
                                self._update_voice_presets("All")
                                _refresh_voice_list()
                        return _rename
                    
                    rename_btn.clicked.connect(_make_rename_handler(name))
                    card_lay.addWidget(rename_btn)

                    # Delete Button
                    del_btn = QPushButton("🗑 Delete", card)
                    del_btn.setProperty("class", "btn-red")
                    del_btn.setStyleSheet("font-size: 11px; padding: 4px 8px;")
                    
                    def _make_delete_handler(vname):
                        def _del():
                            if vname in VOICE_PRESETS:
                                del VOICE_PRESETS[vname]
                                if vname in VOICE_NAMES:
                                    VOICE_NAMES.remove(vname)
                                self._update_voice_presets("All")
                                _refresh_voice_list()
                        return _del
                    
                    del_btn.clicked.connect(_make_delete_handler(name))
                    card_lay.addWidget(del_btn)

                scroll_layout.addWidget(card)
                idx += 1

            scroll_layout.addStretch()

        _refresh_voice_list()
        tabs.addTab(lib_tab, "📚 Voice Library & Presets")

        layout.addWidget(tabs, 1)

        # Dialog Footer
        footer = QHBoxLayout()
        footer.addStretch()
        
        close_btn = QPushButton("Done", dialog)
        close_btn.setProperty("class", "btn-gray")
        close_btn.setStyleSheet("padding: 6px 18px; font-weight: bold;")
        close_btn.clicked.connect(dialog.accept)
        footer.addWidget(close_btn)
        
        layout.addLayout(footer)

        dialog.exec()

    def _apply_cloned_voice_to_table(self, voice_key: str, target_char: str = None):
        """Automatically update preset dropdowns and assign cloned voice to subtitle table rows"""
        self._update_voice_presets("All")
        
        # 1. Update toolbar combo
        idx = self.voice_preset_combo.findText(voice_key)
        if idx >= 0:
            self.voice_preset_combo.setCurrentIndex(idx)

        # 2. Update table rows
        selected_rows = list(set([item.row() for item in self.table.selectedItems()]))

        for r in range(self.table.rowCount()):
            v_combo = self.table.cellWidget(r, 7)
            if not v_combo:
                continue
            
            if v_combo.findText(voice_key) < 0:
                v_combo.addItem(voice_key)
            
            c_widget = self.table.cellWidget(r, 3)
            char_name = c_widget.currentText().strip() if c_widget else ""
            
            if (target_char and target_char.lower() in char_name.lower()) or (r in selected_rows) or (not selected_rows and not target_char):
                v_combo.setCurrentText(voice_key)
                if r < len(self.segments):
                    self.segments[r]["voice"] = voice_key

        logger.info(f"✅ Assigned Cloned Voice '{voice_key}' across matching subtitle rows!")

    def _clone_voice(self):
        """Clone voice from selected segment or open Add Voice Studio"""
        selected_rows = list(set([item.row() for item in self.table.selectedItems()]))
        init_audio, init_text, init_name = None, None, None
        
        main_win = self.window()
        vid_p = getattr(main_win, 'video_player', None)
        vid_path = getattr(vid_p, 'video_path', None) if vid_p else None

        if selected_rows:
            r = selected_rows[0]
            text_item = self.table.item(r, 4)
            char_item = self.table.item(r, 3)
            start_item = self.table.item(r, 1)
            end_item = self.table.item(r, 2)

            if text_item and text_item.text().strip():
                init_text = text_item.text().strip()
            if char_item and char_item.text().strip():
                init_name = f"Clone - {char_item.text().strip()}"
            else:
                init_name = f"Clone - Segment {r+1}"

            if vid_path and os.path.exists(vid_path) and start_item and end_item:
                try:
                    def _to_sec(ts):
                        parts = ts.split(':')
                        if len(parts) == 2:
                            return float(parts[0])*60 + float(parts[1])
                        elif len(parts) == 3:
                            return float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                        return float(ts)
                    st = _to_sec(start_item.text())
                    et = _to_sec(end_item.text())
                    if et > st:
                        from services.voxcpm_service import extract_audio_from_video, extract_segment_audio
                        temp_v_audio = get_temp_path(f"video_full_{r}.wav")
                        temp_s_audio = get_temp_path(f"video_seg_{r}.wav")
                        if extract_audio_from_video(vid_path, temp_v_audio):
                            if extract_segment_audio(temp_v_audio, st, et, temp_s_audio):
                                init_audio = temp_s_audio
                except Exception as e:
                    logger.error(f"Error extracting row segment from video: {e}")
        
        if not init_audio and vid_path and os.path.exists(vid_path):
            init_audio = vid_path

        self._add_custom_voice_dialog(initial_audio=init_audio, initial_text=init_text, initial_name=init_name)

    def _clone_single_voice(self, row: int):
        """Clone voice from a single segment row"""
        text_item = self.table.item(row, 6)
        char_widget = self.table.cellWidget(row, 3)
        start_item = self.table.item(row, 1)
        end_item = self.table.item(row, 2)

        text = text_item.text().strip() if text_item else ""
        char = char_widget.currentText().strip() if char_widget else f"Segment {row+1}"
        init_name = f"Clone - {char}"
        init_audio = None

        main_win = self.window()
        vid_p = getattr(main_win, 'video_player', None)
        vid_path = getattr(vid_p, 'video_path', None) if vid_p else None

        if vid_path and os.path.exists(vid_path) and start_item and end_item:
            try:
                def _to_sec(ts):
                    parts = ts.split(':')
                    if len(parts) == 2:
                        return float(parts[0])*60 + float(parts[1])
                    elif len(parts) == 3:
                        return float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                    return float(ts)
                st = _to_sec(start_item.text())
                et = _to_sec(end_item.text())
                if et > st:
                    from services.voxcpm_service import extract_audio_from_video, extract_segment_audio
                    temp_v_audio = get_temp_path(f"video_full_{row}.wav")
                    temp_s_audio = get_temp_path(f"video_seg_{row}.wav")
                    if extract_audio_from_video(vid_path, temp_v_audio):
                        if extract_segment_audio(temp_v_audio, st, et, temp_s_audio):
                            init_audio = temp_s_audio
            except Exception as e:
                logger.error(f"Error extracting row segment from video: {e}")

        if not init_audio and vid_path and os.path.exists(vid_path):
            init_audio = vid_path

        self._add_custom_voice_dialog(initial_audio=init_audio, initial_text=text, initial_name=init_name)

    # ==================== VOICE ACTIONS & SPEAKER DETECTION ====================
    def _detect_speakers(self):
        """Advanced Multi-Modal Speaker Detection across 5 Voice Categories (Child, Male, Female, Elder Male, Elder Female)"""
        from services.speaker_detector import SpeakerDetector
        from utils.file_utils import get_temp_path
        
        main_win = self.window()
        vid_p = getattr(main_win, 'video_player', None)
        vid_path = getattr(vid_p, 'video_path', None) if vid_p else getattr(main_win, 'video_path', None)
        audio_path = get_temp_path("original_audio.wav")
        if (not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000) and vid_path and os.path.exists(vid_path):
            try:
                from services.voxcpm_service import extract_audio_from_video
                extract_audio_from_video(vid_path, audio_path)
            except Exception:
                pass

        detector = SpeakerDetector(audio_wav_path=audio_path if os.path.exists(audio_path) else None)
        counts = {"Child": 0, "Elder_Male": 0, "Elder_Female": 0, "Male": 0, "Female": 0}
        
        for i in range(self.table.rowCount()):
            persona_widget = self.table.cellWidget(i, 3)
            text_item = self.table.item(i, 6)
            voice_widget = self.table.cellWidget(i, 7)
            
            orig_seg = self.segments[i] if i < len(self.segments) else {}
            st = orig_seg.get("start", i * 3.0)
            et = orig_seg.get("end", (i + 1) * 3.0)
            orig_text = orig_seg.get("original_text", "")
            curr_text = text_item.text() if text_item else ""
            
            res = detector.detect_speaker_for_segment(
                khmer_text=curr_text,
                original_text=orig_text,
                start_sec=st,
                end_sec=et
            )
            
            clean_text = res.get("clean_khmer_text", curr_text)
            target_voice = res.get("voice", "Khmer Male - Piseth")
            spk_type = res.get("speaker_type", "Male")
            
            if spk_type == "Child" or "Child" in target_voice:
                counts["Child"] += 1
                matched_persona = "Girl / Child" if ("girl" in target_voice.lower() or "sreyka" in target_voice.lower() or "ស្រី" in clean_text) else "Boy / Child"
            elif spk_type == "Elderly Female" or "Grandmother" in target_voice or "Elder - Female" in target_voice:
                counts["Elder_Female"] += 1
                matched_persona = "Elderly Female"
            elif spk_type == "Elderly Male" or "Grandfather" in target_voice or "Elder - Male" in target_voice or "Elder" in spk_type:
                counts["Elder_Male"] += 1
                matched_persona = "Elderly Male"
            elif spk_type == "Female" or "Female" in target_voice or "Sreymom" in target_voice:
                counts["Female"] += 1
                matched_persona = "Female Adult"
            else:
                counts["Male"] += 1
                matched_persona = "Male Adult"
            
            if persona_widget and hasattr(persona_widget, 'setCurrentText'):
                if matched_persona in PERSONA_CHOICES:
                    persona_widget.setCurrentText(matched_persona)
            if text_item:
                text_item.setText(clean_text)
                
            if voice_widget:
                all_items = [voice_widget.itemText(idx) for idx in range(voice_widget.count())]
                if target_voice not in all_items:
                    voice_widget.addItem(target_voice)
                voice_widget.setCurrentText(target_voice)
                
            if i < len(self.segments):
                self.segments[i]["persona"] = matched_persona
                self.segments[i]["character"] = matched_persona
                self.segments[i]["khmer_text"] = clean_text
                self.segments[i]["voice"] = target_voice
                self.segments[i]["voice_id"] = target_voice

        summary_msg = (
            f"🎉 ស្វែងរក និងកំណត់សំឡេងតួអង្គទាំង ៥ ប្រភេទជោគជ័យ!\n\n"
            f"👶 សំឡេងក្មេង (Child): {counts['Child']} ជួរ\n"
            f"👴 សំឡេងមនុស្សចាស់ប្រុស (Elderly Male): {counts['Elder_Male']} ជួរ\n"
            f"👵 សំឡេងមនុស្សចាស់ស្រី (Elderly Female): {counts['Elder_Female']} ជួរ\n"
            f"👩 សំឡេងស្រី (Female Adult): {counts['Female']} ជួរ\n"
            f"👨 សំឡេងប្រុស (Male Adult): {counts['Male']} ជួរ\n\n"
            f"សំឡេងខ្មែរត្រូវបានកំណត់ទៅតាមតួអង្គនីមួយៗរួចរាល់!"
        )
        QMessageBox.information(self, "Detect Speakers 🔍", summary_msg)

    def _open_paste_srt_dialog(self):
        """Open the dedicated PasteSRTDialog to let user paste and apply subtitles."""
        from qt_compat import QApplication
        text = (QApplication.clipboard().text() or "").strip()
        dialog = PasteSRTDialog(self, initial_text=text)

        def on_applied(srt_text):
            p = self.parent()
            while p and not hasattr(p, '_paste_subtitles_from_clipboard'):
                p = p.parent()
            if p and hasattr(p, '_paste_subtitles_from_clipboard'):
                p._paste_subtitles_from_clipboard(srt_text)

        dialog.subtitles_applied.connect(on_applied)
        dialog.exec_()

    def _assign_voices_to_characters(self):
        """Assign voices to characters based on character names and age/gender detection"""
        self._detect_speakers()

    def _auto_sync_voices(self):
        """Auto sync voices with character detection"""
        self._detect_speakers()

    def _apply_voice_to_selected(self):
        """Apply selected voice preset to currently selected rows or all rows"""
        preset = self.voice_preset_combo.currentText()
        if not preset:
            return

        selected_rows = set()
        for item in self.table.selectedItems():
            selected_rows.add(item.row())
        
        if not selected_rows:
            for i in range(self.table.rowCount()):
                selected_rows.add(i)
        
        for row in selected_rows:
            voice_widget = self.table.cellWidget(row, 7)
            if voice_widget:
                all_items = [voice_widget.itemText(idx) for idx in range(voice_widget.count())]
                if preset not in all_items:
                    voice_widget.addItem(preset)
                voice_widget.setCurrentText(preset)
            
            if row < len(self.segments):
                self.segments[row]["voice"] = preset
                self.segments[row]["voice_id"] = preset
                
        QMessageBox.information(
            self,
            "Apply Voice Preset 🎯",
            f"Voice '{preset}' applied to {len(selected_rows)} segment(s) successfully!"
        )

    def _play_segment(self, row: int):
        """Play segment audio"""
        text_item = self.table.item(row, 6)
        if text_item and text_item.text().strip():
            self._preview_tts(row)

    def _preview_tts(self, row: int):
        """1️⃣ Line Preview: Preview TTS synthesis for a single row immediately."""
        if row < 0 or row >= self.table.rowCount():
            return
        
        text_item = self.table.item(row, 6)
        if not text_item or not text_item.text().strip():
            return
        
        text = text_item.text().strip()
        
        start_item = self.table.item(row, 1)
        end_item = self.table.item(row, 2)
        try:
            st = float(start_item.text().replace('s', '')) if start_item else float(row * 3.0)
            et = float(end_item.text().replace('s', '')) if end_item else float(st + 3.0)
        except Exception:
            st, et = float(row * 3.0), float((row + 1) * 3.0)

        persona_w = self.table.cellWidget(row, 3)
        emotion_w = self.table.cellWidget(row, 4)
        style_w = self.table.cellWidget(row, 5)
        voice_w = self.table.cellWidget(row, 7)
        
        persona = normalize_persona(persona_w.currentText()) if persona_w else "Male Adult"
        emotion = normalize_emotion(emotion_w.currentText()) if emotion_w else "Neutral"
        speaking_style = style_w.currentText() if style_w else "Normal"
        voice_name = voice_w.currentText() if voice_w else PERSONA_DEFAULT_VOICE.get(persona, "Khmer Female - Sreymom")

        seg = {
            "khmer_text": text,
            "text": text,
            "voice": voice_name,
            "voice_id": voice_name,
            "persona": persona,
            "emotion": emotion,
            "speaking_style": speaking_style,
            "start": st,
            "end": et
        }

        from core.tts import TextToSpeech
        tts = TextToSpeech()
        out_wav = tts.synthesize_single_line(seg, row)
        if out_wav and os.path.exists(out_wav):
            try:
                # Seek video preview to the line's start time so frame matches voice
                if hasattr(self, 'seek_requested'):
                    self.seek_requested.emit(st)

                # Stop any previous line preview to avoid overlapping audio
                if hasattr(self, '_preview_proc') and self._preview_proc and self._preview_proc.poll() is None:
                    try:
                        self._preview_proc.terminate()
                    except Exception:
                        pass

                logger.info(f"🔊 [LinePreview] Playing row {row+1} [{st:.1f}s -> {et:.1f}s] ({voice_name}): '{text[:35]}'")
                if sys.platform == "darwin":
                    self._preview_proc = subprocess.Popen(["afplay", "-v", "2", out_wav])
                else:
                    self._preview_proc = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", out_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logger.error(f"Playback error: {e}")

    def _preview_scene(self):
        """2️⃣ Scene Preview: Preview dialogue for selected lines or current scene range (15-30 seconds)."""
        selected_rows = sorted(list(set(item.row() for item in self.table.selectedItems())))
        if not selected_rows:
            curr = self.table.currentRow()
            if curr < 0:
                curr = 0
            start_time = None
            selected_rows = []
            for r in range(curr, min(self.table.rowCount(), curr + 10)):
                s_item = self.table.item(r, 1)
                e_item = self.table.item(r, 2)
                if not s_item or not e_item:
                    continue
                try:
                    s_t = float(s_item.text().replace('s', ''))
                    e_t = float(e_item.text().replace('s', ''))
                    if start_time is None:
                        start_time = s_t
                    if e_t - start_time > 30.0 and len(selected_rows) > 0:
                        break
                    selected_rows.append(r)
                except ValueError:
                    pass
        
        if not selected_rows:
            QMessageBox.information(self, "Scene Preview", "សូមជ្រើសរើសជួរ (Rows) សម្រាប់ Preview Scene!")
            return

        from core.tts import TextToSpeech
        from utils.file_utils import get_temp_path
        tts = TextToSpeech()
        
        scene_segs = []
        for r in selected_rows:
            text_item = self.table.item(r, 6)
            if not text_item or not text_item.text().strip():
                continue
            text = text_item.text().strip()
            s_item = self.table.item(r, 1)
            e_item = self.table.item(r, 2)
            try:
                st = float(s_item.text().replace('s', '')) if s_item else float(r * 3.0)
                et = float(e_item.text().replace('s', '')) if e_item else float(st + 3.0)
            except Exception:
                st, et = float(r * 3.0), float((r + 1) * 3.0)

            p_w = self.table.cellWidget(r, 3)
            em_w = self.table.cellWidget(r, 4)
            st_w = self.table.cellWidget(r, 5)
            v_w = self.table.cellWidget(r, 7)

            persona = normalize_persona(p_w.currentText()) if p_w else "Male Adult"
            seg = {
                "khmer_text": text,
                "text": text,
                "voice": v_w.currentText() if v_w else PERSONA_DEFAULT_VOICE.get(persona, "Khmer Female - Sreymom"),
                "persona": persona,
                "emotion": normalize_emotion(em_w.currentText()) if em_w else "Neutral",
                "speaking_style": st_w.currentText() if st_w else "Normal",
                "start": st,
                "end": et
            }
            scene_segs.append((r, seg))

        if not scene_segs:
            return

        base_start = scene_segs[0][1]["start"]
        total_duration = max(0.5, scene_segs[-1][1]["end"] - base_start)
        scene_wav = get_temp_path(f"scene_preview_{selected_rows[0]}_{selected_rows[-1]}.wav")

        # Create blank master canvas for scene
        cmd_blank = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=stereo:d={total_duration + 1.0}",
            "-ar", "44100", "-ac", "2",
            scene_wav
        ]
        subprocess.run(cmd_blank, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        filter_inputs = ["-i", scene_wav]
        amix_filters = ["[0:a]"]
        input_count = 1

        for idx, (r, seg) in enumerate(scene_segs):
            wav = tts.generate_segment_audio(seg, r)
            if wav and os.path.exists(wav):
                offset_ms = int(max(0.0, seg["start"] - base_start) * 1000)
                filter_inputs.extend(["-i", wav])
                amix_filters.append(f"[{input_count}:a]adelay={offset_ms}|{offset_ms}[a{input_count}];")
                input_count += 1

        if input_count > 1:
            complex_filter = "".join(amix_filters[1:]) + "".join(f"[a{i}]" for i in range(1, input_count)) + f"[0:a]amix=inputs={input_count}:duration=first:dropout_transition=0[outa]"
            final_scene_wav = get_temp_path(f"scene_final_{selected_rows[0]}.wav")
            stitch_cmd = ["ffmpeg", "-y"] + filter_inputs + ["-filter_complex", complex_filter, "-map", "[outa]", "-ar", "44100", "-ac", "2", final_scene_wav]
            res = subprocess.run(stitch_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and os.path.exists(final_scene_wav):
                scene_wav = final_scene_wav

        try:
            if hasattr(self, '_preview_proc') and self._preview_proc and self._preview_proc.poll() is None:
                try:
                    self._preview_proc.terminate()
                except Exception:
                    pass

            if sys.platform == "darwin":
                self._preview_proc = subprocess.Popen(["afplay", "-v", "1", scene_wav])
            else:
                self._preview_proc = subprocess.Popen(["xdg-open" if sys.platform != "win32" else "start", scene_wav], shell=True)
        except Exception as e:
            logger.error(f"Scene playback error: {e}")

    def _show_speaker_profiles_dialog(self):
        """👥 Dialog to view and configure global Speaker Profiles across segments."""
        segs = self.get_updated_segments()
        if not segs:
            QMessageBox.information(self, "Speaker Profiles", "មិនមាន Subtitle ក្នុងតារាងទេ (No subtitles loaded).")
            return
        
        speaker_stats = {}
        for seg in segs:
            spk_id = seg.get("speaker_id") or "speaker_01"
            if spk_id not in speaker_stats:
                speaker_stats[spk_id] = {
                    "count": 0,
                    "persona": normalize_persona(seg.get("persona", "Male Adult")),
                    "emotion": normalize_emotion(seg.get("emotion", "Neutral")),
                    "voice": seg.get("voice_id", "Khmer Male - Piseth")
                }
            speaker_stats[spk_id]["count"] += 1

        dlg = QDialog(self)
        dlg.setWindowTitle("Global Speaker Profiles")
        dlg.resize(680, 420)
        dlg.setStyleSheet("""
            QDialog { background-color: #0b0f19; color: #f1f5f9; }
            QLabel { color: #cbd5e1; font-size: 11px; }
            QComboBox { background-color: #131b2e; border: 1px solid #1e2942; border-radius: 4px; padding: 4px; color: #e2e8f0; }
            QPushButton { background-color: #6366f1; border-radius: 6px; padding: 6px 14px; font-weight: bold; color: white; }
            QPushButton:hover { background-color: #4f46e5; }
        """)

        d_layout = QVBoxLayout(dlg)
        title = QLabel("<h3>👥 Speaker Persona & Voice Assignments</h3><p style='color:#94a3b8;'>Assign default Persona, Emotion, and Voice for each detected speaker across the project:</p>", dlg)
        d_layout.addWidget(title)

        spk_table = QTableWidget(len(speaker_stats), 5, dlg)
        spk_table.setHorizontalHeaderLabels(["Speaker ID", "Lines", "Persona", "Default Emotion", "Assigned Voice"])
        spk_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        spk_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        spk_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        spk_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        spk_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

        row_map = {}
        for r, (spk_id, stat) in enumerate(sorted(speaker_stats.items())):
            row_map[r] = spk_id
            spk_item = QTableWidgetItem(spk_id)
            spk_item.setFlags(spk_item.flags() & ~Qt.ItemIsEditable)
            spk_table.setItem(r, 0, spk_item)

            cnt_item = QTableWidgetItem(f"{stat['count']} lines")
            cnt_item.setFlags(cnt_item.flags() & ~Qt.ItemIsEditable)
            spk_table.setItem(r, 1, cnt_item)

            p_combo = QComboBox(dlg)
            p_combo.addItems(PERSONA_CHOICES)
            if stat["persona"] in PERSONA_CHOICES:
                p_combo.setCurrentText(stat["persona"])
            spk_table.setCellWidget(r, 2, p_combo)

            e_combo = QComboBox(dlg)
            e_combo.addItems(EMOTION_CHOICES)
            if stat["emotion"] in EMOTION_CHOICES:
                e_combo.setCurrentText(stat["emotion"])
            spk_table.setCellWidget(r, 3, e_combo)

            v_combo = QComboBox(dlg)
            v_combo.addItems(list(VOICE_PRESETS.keys()))
            if stat["voice"] in VOICE_PRESETS:
                v_combo.setCurrentText(stat["voice"])
            spk_table.setCellWidget(r, 4, v_combo)

            def _make_change_handler(vc=v_combo):
                return lambda new_p: vc.setCurrentText(PERSONA_DEFAULT_VOICE.get(new_p, vc.currentText()))
            p_combo.currentTextChanged.connect(_make_change_handler(v_combo))

        d_layout.addWidget(spk_table)

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        cancel_btn = QPushButton("បោះបង់ (Cancel)", dlg)
        cancel_btn.setStyleSheet("background-color: #1e2942;")
        cancel_btn.clicked.connect(dlg.reject)
        btn_box.addWidget(cancel_btn)

        apply_btn = QPushButton("✓ អនុវត្តលើបន្ទាត់ទាំងអស់ (Apply to All Lines)", dlg)
        def _apply_all():
            for r, spk_id in row_map.items():
                p_c = spk_table.cellWidget(r, 2)
                e_c = spk_table.cellWidget(r, 3)
                v_c = spk_table.cellWidget(r, 4)
                if not (p_c and e_c and v_c):
                    continue
                new_p = p_c.currentText()
                new_e = e_c.currentText()
                new_v = v_c.currentText()

                for row_idx in range(self.table.rowCount()):
                    seg_spk = segs[row_idx].get("speaker_id") if row_idx < len(segs) else ""
                    if seg_spk == spk_id:
                        row_p = self.table.cellWidget(row_idx, 3)
                        row_e = self.table.cellWidget(row_idx, 4)
                        row_v = self.table.cellWidget(row_idx, 7)
                        if row_p: row_p.setCurrentText(new_p)
                        if row_e: row_e.setCurrentText(new_e)
                        if row_v: row_v.setCurrentText(new_v)
            dlg.accept()
            QMessageBox.information(self, "Speaker Profiles", "✅ បានអនុវត្ត Speaker Profiles ទៅលើបន្ទាត់ទាំងអស់រួចរាល់!")
        apply_btn.clicked.connect(_apply_all)
        btn_box.addWidget(apply_btn)

        d_layout.addLayout(btn_box)
        dlg.exec()

    def _show_table_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= self.table.rowCount():
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0c111e;
                border: 1px solid #1e2942;
                border-radius: 6px;
                padding: 4px;
                color: #f1f5f9;
                font-size: 11px;
            }
            QMenu::item {
                padding: 6px 16px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #1e2942;
                color: #38bdf8;
            }
        """)

        play_act = menu.addAction("▶ ស្តាប់សំឡេងកថាខណ្ឌនេះ (Preview Voice)")
        resynth_act = menu.addAction("🔊 បង្កើតសំឡេងឡើងវិញ (Re-synthesize Voice)")
        menu.addSeparator()
        condense_act = menu.addAction("✂️ AI Condense Shorter (បង្រួមពាក្យខ្មែរឱ្យខ្លី)")
        split_act = menu.addAction("✂️ បំបែកកថាខណ្ឌជាពីរ (Split Segment)")
        merge_act = menu.addAction("🔗 ច្របាច់ជាមួយជួរបន្ទាប់ (Merge with Next)")
        menu.addSeparator()
        del_act = menu.addAction("🗑 លុបកថាខណ្ឌនេះ (Delete Segment)")

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if not action:
            return

        if action == play_act:
            self._preview_tts(row)
        elif action == resynth_act:
            self._resynthesize_row(row)
        elif action == condense_act:
            self._condense_row_text(row)
        elif action == split_act:
            self._split_segment(row)
        elif action == merge_act:
            self._merge_segment_with_next(row)
        elif action == del_act:
            self._delete_segment(row)

    def _resynthesize_row(self, row: int):
        """Re-synthesize TTS audio for a single row and preview it."""
        updated = self.get_updated_segments()
        if 0 <= row < len(updated):
            seg = updated[row]
            from core.tts import TextToSpeech
            tts = TextToSpeech(voice_name=seg.get("voice", "Khmer Female - Sreymom"))
            out_path = tts.generate_segment_audio(seg, row)
            if out_path and os.path.exists(out_path):
                try:
                    if sys.platform == "darwin":
                        subprocess.Popen(["afplay", out_path])
                    else:
                        subprocess.Popen(["xdg-open" if sys.platform != "win32" else "start", out_path], shell=True)
                except Exception as e:
                    logger.debug(f"Playback error: {e}")

    def _condense_row_text(self, row: int):
        """Use AI to shorten/condense Khmer text to fit segment duration exactly."""
        updated = self.get_updated_segments()
        if 0 <= row < len(updated):
            seg = updated[row]
            khmer_text = seg.get("khmer_text", "")
            st = seg.get("start", 0.0)
            et = seg.get("end", 3.0)
            dur = max(0.5, et - st)
            if not khmer_text:
                return

            from services.translation_service import TranslationService
            svc = TranslationService()
            shorter = svc.condense_khmer_text(khmer_text, target_duration=dur)
            if shorter and shorter != khmer_text:
                seg["khmer_text"] = shorter
                item = self.table.item(row, 6)
                if item:
                    item.setText(shorter)
                self.segments = updated

    def _split_segment(self, row: int):
        """Split segment at midpoint into two segments."""
        updated = self.get_updated_segments()
        if not (0 <= row < len(updated)):
            return

        seg = updated[row]
        st = seg.get("start", 0.0)
        et = seg.get("end", 3.0)
        dur = et - st
        if dur < 0.6:
            QMessageBox.warning(self, "Split", "កថាខណ្ឌនេះខ្លីពេកមិនអាចបំបែកបានទេ (< 0.6s)!")
            return

        mid = round(st + dur / 2.0, 2)
        text = seg.get("khmer_text", "")
        orig_text = seg.get("original_text", "")
        
        words = text.split(" ")
        if len(words) > 1:
            mid_idx = len(words) // 2
            text1 = " ".join(words[:mid_idx])
            text2 = " ".join(words[mid_idx:])
        else:
            half = max(1, len(text) // 2)
            text1 = text[:half]
            text2 = text[half:]

        orig_words = orig_text.split(" ")
        if len(orig_words) > 1:
            m_idx = len(orig_words) // 2
            orig1 = " ".join(orig_words[:m_idx])
            orig2 = " ".join(orig_words[m_idx:])
        else:
            orig1 = orig_text
            orig2 = orig_text

        seg1 = dict(seg)
        seg1["start"] = st
        seg1["end"] = mid
        seg1["khmer_text"] = text1
        seg1["original_text"] = orig1

        seg2 = dict(seg)
        seg2["start"] = mid
        seg2["end"] = et
        seg2["khmer_text"] = text2
        seg2["original_text"] = orig2

        updated.pop(row)
        updated.insert(row, seg2)
        updated.insert(row, seg1)

        self.set_segments(updated)

    def _merge_segment_with_next(self, row: int):
        """Merge segment with the next segment."""
        updated = self.get_updated_segments()
        if not (0 <= row < len(updated) - 1):
            QMessageBox.information(self, "Merge", "មិនមានជួរបន្ទាប់សម្រាប់បញ្ចូលគ្នាទេ!")
            return

        seg1 = updated[row]
        seg2 = updated[row + 1]

        merged = dict(seg1)
        merged["end"] = seg2.get("end", seg1.get("end", 0.0))
        merged["khmer_text"] = f"{seg1.get('khmer_text', '')} {seg2.get('khmer_text', '')}".strip()
        merged["original_text"] = f"{seg1.get('original_text', '')} {seg2.get('original_text', '')}".strip()

        updated.pop(row + 1)
        updated[row] = merged

        self.set_segments(updated)

    def _delete_segment(self, row: int):
        """Delete segment from table."""
        updated = self.get_updated_segments()
        if not (0 <= row < len(updated)):
            return

        ret = QMessageBox.question(
            self, "Delete Segment",
            f"តើអ្នកពិតជាចង់លុបកថាខណ្ឌ #{row + 1} នេះមែនទេ?",
            QMessageBox.Yes | QMessageBox.No
        )
        if ret == QMessageBox.Yes:
            updated.pop(row)
            self.set_segments(updated)


# ==================== VIDEO PREVIEW WIDGET ====================
class VideoPreviewWidget(QWidget):
    file_dropped = Signal(str)
    files_dropped = Signal(list)
    playhead_moved = Signal(float)
    text_moved = Signal(int, int)
    logo_moved = Signal(int, int, int, int)
    burn_subtitle_moved = Signal(int, int)
    blur_items_changed = Signal(list, str)  # (blur_items, active_blur_id)
    blur_selected = Signal(str)
    text_items_changed = Signal(list, str)  # (text_items, active_text_id)
    mark_in_requested = Signal(float)
    mark_out_requested = Signal(float)
    cut_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_path = None
        self.cap = None
        self.fps = 30.0
        self.total_frames = 0
        self.current_frame = 0
        self._is_playing = False
        self._duration = 0
        self._timeline_segments = []
        self._cached_clean_frame = None

        # Virtual Multi-Clip Sequence State (CapCut NLE Architecture)
        self.video_clips = []
        self._active_clip_idx = None
        self._active_clip_path = None
        self._current_global_sec = 0.0
        self._loading_preview_audio = False
        
        # Interaction state
        self._active_target = None
        self._active_handle = None
        self._active_item_id = None
        self._drag_start_pos = None
        self._drag_start_rect = None
        self._drag_start_text_pos = None
        self._drag_start_sub_ratio = None
        self._drag_start_item = None
        
        # Multi-Blur settings
        self.blur_enabled = False
        self.blur_items = []
        self.active_blur_id = None
        self._next_blur_id = 1
        self.blur_intensity = 35
        self.blur_rect = None
        self.blur_auto_speech = False
        
        # Text Overlay settings
        self.text_overlay_enabled = True
        self.text_overlay = "ai 24 movei"
        self.text_font_name = "Kantumruy Pro"
        self.raw_text_size = 10
        self.text_size = max(0.2, self.raw_text_size / 20.0)
        self.text_color_bgr = (255, 255, 255)
        self.text_color_rgb = (255, 255, 255)
        self.text_bg_color_rgb = None
        self.text_outline_color_rgb = (0, 0, 0)
        self.text_outline_width = 2
        self.text_shadow_color_rgb = (0, 0, 0)
        self.text_shadow_offset = 3
        self.text_position = (50, 80)
        self.text_rect = None
        self.text_anim_type = "pop"  # "none", "fade", "slide_up", "slide_left", "pop"
        self.text_anim_speed = 0.5   # seconds
        self.text_anim_mode = "always"  # "always", "first_5s", "first_10s", "custom"
        self.text_start_sec = 0.0
        self.text_duration_sec = 0.0
        self.text_anim_repeat_sec = 2.0
        self.text_anim_preview_timer = None
        self.text_anim_preview_sec = None
        
        # Multi-Text Overlay Items
        self.active_text_id = "text_1"
        self._next_text_id = 2
        self.text_items = [{
            "id": "text_1",
            "name": "Text 1",
            "text": "ai 24 movei",
            "enabled": True,
            "font": "Kantumruy Pro",
            "size": 10,
            "size_pt": 10,
            "color_rgb": (255, 255, 255),
            "bg_color": None,
            "bg_color_rgb": None,
            "outline_color": "#000000",
            "outline_color_rgb": (0, 0, 0),
            "outline_width": 2,
            "shadow_color": "#000000",
            "shadow_color_rgb": (0, 0, 0),
            "shadow_offset": 3,
            "pos": (50, 80),
            "anim_type": "pop",
            "anim_speed": 0.5,
            "repeat_sec": 2.0,
            "mode": "always",
            "start_sec": 0.0,
            "duration_sec": 5.0,
            "is_watermark": False,
            "opacity": 1.0,
            "pos_zone": "mid",
            "full_video": True,
            "speed_str": "medium"
        }]
        
        # Logo Overlay settings
        self.logo_enabled = False
        self.logo_path = None
        self.logo_image = None
        self.logo_x = 233
        self.logo_y = 6
        self.logo_width = 100
        self.logo_height = 100
        self.logo_rect = QtCore.QRect(233, 6, 100, 100)
        self.logo_alpha = 0.8
        self.logo_remove_green = False
        self.logo_start_sec = 0.0
        self.logo_duration_sec = 0.0
        self.logo_full_video = True
        
        # Burn Subtitle settings
        self.burn_subtitle_enabled = True
        self.burn_subtitle_font_name = "Noto Sans Khmer"
        self.burn_subtitle_font_size = 13
        self.burn_subtitle_color_rgb = (226, 232, 240)
        self.burn_subtitle_bg_opacity = 0.05
        self.burn_sub_y_ratio = 0.85
        self.burn_sub_x_ratio = 0.50
        self.burn_sub_rect = None
        self.burn_sub_anim_type = "blur_dissolve"
        self.burn_subtitle_template = {
            "name": "🌫️ Blur Dissolve", "name_short": "Blur Focus", "category": "Cinematic",
            "font": "Noto Sans Khmer", "size": 13, "color": "#E2E8F0", "stroke_color": "#0F172A",
            "stroke_width": 2, "opacity": 5, "anim": "blur_dissolve", "badge_bg": "#0c4a6e", "badge_fg": "#7dd3fc"
        }
        self._sub_render_cache = {}
        
        self.play_timer = QtCore.QTimer(self)
        self.play_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.play_timer.timeout.connect(self._render_next_frame)

        # Seek throttler & debouncer (NLE butter-smooth scrubbing)
        self._seek_debounce_timer = QtCore.QTimer(self)
        self._seek_debounce_timer.setSingleShot(True)
        self._seek_debounce_timer.timeout.connect(self._execute_debounced_seek)
        self._pending_seek_frame = None
        self._pending_seek_sec = None
        
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self._pending_audio_pos_ms = None
        self._pending_play = False
        self._audition_proc = None
        try:
            from PySide6.QtMultimedia import QMediaDevices
            def_dev = QMediaDevices.defaultAudioOutput()
            if def_dev and not def_dev.isNull():
                self.audio_output.setDevice(def_dev)
        except Exception:
            pass
        if hasattr(self.player, 'setAudioOutput'):
            self.audio_output.setVolume(1.0)
            if hasattr(self.audio_output, 'setMuted'):
                self.audio_output.setMuted(False)
            self.player.setAudioOutput(self.audio_output)
        if hasattr(self.player, 'mediaStatusChanged'):
            self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        if hasattr(self.player, 'errorOccurred'):
            self.player.errorOccurred.connect(self._on_player_error)

        self._init_ui()

    def _on_player_error(self, error, error_string=""):
        logger.warning(f"⚠️ [VideoPreview] Audio player error: {error} - {error_string}")

    def cleanup(self):
        """Cleanly releases video capture, audio player, timers, and background threads."""
        self._stop_video()
        if hasattr(self, '_seek_debounce_timer'):
            self._seek_debounce_timer.stop()
        if hasattr(self, 'play_timer'):
            self.play_timer.stop()
        if hasattr(self, 'player') and self.player:
            try:
                self.player.stop()
                self.player.setSource(QtCore.QUrl())
            except Exception:
                pass
        if hasattr(self, 'cap') and self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Video Preview Screen Frame
        self.screen_frame = QFrame(self)
        self.screen_frame.setObjectName("videoPreviewFrame")
        self.screen_frame.setMinimumSize(280, 260)
        self.screen_frame.setMouseTracking(True)
        self.screen_frame.setAcceptDrops(True)
        
        screen_lay = QVBoxLayout(self.screen_frame)
        screen_lay.setContentsMargins(0, 0, 0, 0)
        screen_lay.setAlignment(Qt.AlignCenter)

        self.placeholder_lbl = QLabel("🎥\nNo Video\nLoaded", self.screen_frame)
        self.placeholder_lbl.setObjectName("videoPlaceholderText")
        self.placeholder_lbl.setAlignment(Qt.AlignCenter)
        self.placeholder_lbl.setScaledContents(True)
        self.placeholder_lbl.setMouseTracking(True)
        self.placeholder_lbl.setAcceptDrops(True)
        screen_lay.addWidget(self.placeholder_lbl)

        self.screen_frame.mousePressEvent = lambda ev: self._on_mouse_press(ev, self.screen_frame)
        self.screen_frame.mouseMoveEvent = lambda ev: self._on_mouse_move(ev, self.screen_frame)
        self.screen_frame.mouseReleaseEvent = lambda ev: self._on_mouse_release(ev, self.screen_frame)
        self.screen_frame.dragEnterEvent = self._on_screen_drag_enter
        self.screen_frame.dragMoveEvent = self._on_screen_drag_move
        self.screen_frame.dropEvent = lambda ev: self._on_screen_drop(ev, self.screen_frame)
        self.screen_frame.contextMenuEvent = self._on_context_menu

        self.placeholder_lbl.mousePressEvent = lambda ev: self._on_mouse_press(ev, self.placeholder_lbl)
        self.placeholder_lbl.mouseMoveEvent = lambda ev: self._on_mouse_move(ev, self.placeholder_lbl)
        self.placeholder_lbl.mouseReleaseEvent = lambda ev: self._on_mouse_release(ev, self.placeholder_lbl)
        self.placeholder_lbl.dragEnterEvent = self._on_screen_drag_enter
        self.placeholder_lbl.dragMoveEvent = self._on_screen_drag_move
        self.placeholder_lbl.dropEvent = lambda ev: self._on_screen_drop(ev, self.placeholder_lbl)
        self.placeholder_lbl.contextMenuEvent = self._on_context_menu

        layout.addWidget(self.screen_frame, stretch=1)

        # Slider Position Bar
        self.seek_slider = QSlider(Qt.Horizontal, self)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        layout.addWidget(self.seek_slider)

        # Playback Controls Bar (CapCut-grade responsive layout)
        ctrl_lay = QHBoxLayout()
        ctrl_lay.setContentsMargins(4, 2, 4, 2)
        ctrl_lay.setSpacing(4)

        self.play_btn = QPushButton("▶", self)
        self.play_btn.setFixedSize(32, 28)
        self.play_btn.setProperty("class", "btn-green")
        self.play_btn.setToolTip("Play / Pause (Space)")
        self.play_btn.clicked.connect(self._toggle_play)
        
        self.stop_btn = QPushButton("⏹", self)
        self.stop_btn.setFixedSize(32, 28)
        self.stop_btn.setProperty("class", "btn-red")
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.clicked.connect(self._stop_video)

        self.time_lbl = QLabel("00:00 / 00:00", self)
        self.time_lbl.setStyleSheet("font-family: Menlo, monospace; font-size: 11px; font-weight: bold; color: #94a3b8;")

        ctrl_lay.addWidget(self.play_btn)
        ctrl_lay.addWidget(self.stop_btn)
        ctrl_lay.addWidget(self.time_lbl)

        # Audio Track Switcher (Original Audio, Dubbed+BGM, Khmer Voice Only)
        self.audio_track_combo = QComboBox(self)
        self.audio_track_combo.addItems([
            "🔊 Original",
            "🇰🇭 Dub+BGM",
            "🗣 Khmer Only"
        ])
        self.audio_track_combo.setStyleSheet("""
            QComboBox {
                font-size: 10.5px;
                padding: 2px 4px;
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 4px;
                color: #e2e8f0;
                max-width: 110px;
            }
        """)
        self.audio_track_combo.setToolTip("Audio Track: Original / Dubbed+BGM / Voice Only")
        self.audio_track_combo.currentIndexChanged.connect(self._on_audio_track_changed)
        ctrl_lay.addWidget(self.audio_track_combo)

        # BGM / Original Audio Volume Quick Slider (controls background loudness in Dub+BGM mode)
        self.bgm_vol_icon = QLabel("🎶", self)
        self.bgm_vol_icon.setStyleSheet("font-size: 11px;")
        self.bgm_vol_icon.setToolTip("កម្រិតសំឡេងដើម / BGM Volume (0% - 100%)")
        ctrl_lay.addWidget(self.bgm_vol_icon)

        self.bgm_vol_slider = QSlider(Qt.Horizontal, self)
        self.bgm_vol_slider.setRange(0, 100)
        self.bgm_vol_slider.setValue(45)
        self.bgm_vol_slider.setFixedWidth(50)
        self.bgm_vol_slider.setToolTip("កម្រិតសំឡេងដើម / BGM Volume: 45%")
        self.bgm_vol_slider.valueChanged.connect(self._on_bgm_slider_changed)
        ctrl_lay.addWidget(self.bgm_vol_slider)

        # Instant Audition Button (Loud & direct playback without delays)
        self.audition_btn = QPushButton("🗣 Audition", self)
        self.audition_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:1 #ec4899);
                color: #ffffff;
                font-weight: bold;
                font-size: 10px;
                padding: 3px 6px;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #8b5cf6, stop:1 #f472b6);
            }
        """)
        self.audition_btn.setToolTip("ភ្លាមៗ: ចាក់ស្តាប់សំឡេងខ្មែរដែលបានសំយោគ (Instant loud audition)")
        self.audition_btn.clicked.connect(self.audition_voice)
        ctrl_lay.addWidget(self.audition_btn)

        # Volume Slider
        vol_ico = QLabel("🔊", self)
        vol_ico.setStyleSheet("font-size: 11px;")
        ctrl_lay.addWidget(vol_ico)
        self.vol_slider = QSlider(Qt.Horizontal, self)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(50)
        self.vol_slider.setToolTip("Preview Audio Volume")
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        ctrl_lay.addWidget(self.vol_slider)

        # Audio Output Device Selector (Quick switch between AirPods, Speakers, etc.)
        self.audio_device_combo = QComboBox(self)
        self.audio_device_combo.setObjectName("audioDeviceCombo")
        self.audio_device_combo.setStyleSheet("""
            QComboBox {
                font-size: 10px;
                padding: 1px 3px;
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 4px;
                color: #e2e8f0;
                max-width: 120px;
            }
        """)
        self.audio_device_combo.setToolTip("Audio Output Device (Speakers / Headphones)")
        self._populate_audio_devices()
        self.audio_device_combo.currentIndexChanged.connect(self._on_audio_device_changed)
        ctrl_lay.addWidget(self.audio_device_combo)

        # Quick In / Out / Cut Buttons
        self.mark_in_btn = QPushButton("[ In", self)
        self.mark_in_btn.setFixedSize(36, 24)
        self.mark_in_btn.setStyleSheet("""
            QPushButton {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 4px;
                padding: 1px 3px;
                font-weight: bold;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #0284c7;
                color: #ffffff;
            }
        """)
        self.mark_in_btn.setToolTip("Mark In [ (Shortcut: I)")
        self.mark_in_btn.clicked.connect(self._on_mark_in_clicked)
        ctrl_lay.addWidget(self.mark_in_btn)

        self.mark_out_btn = QPushButton("Out ]", self)
        self.mark_out_btn.setFixedSize(38, 24)
        self.mark_out_btn.setStyleSheet("""
            QPushButton {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 4px;
                padding: 1px 3px;
                font-weight: bold;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #0284c7;
                color: #ffffff;
            }
        """)
        self.mark_out_btn.setToolTip("Mark Out ] (Shortcut: O)")
        self.mark_out_btn.clicked.connect(self._on_mark_out_clicked)
        ctrl_lay.addWidget(self.mark_out_btn)

        self.cut_btn = QPushButton("✂️", self)
        self.cut_btn.setFixedSize(28, 24)
        self.cut_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #0284c7);
                color: #ffffff;
                border: 1px solid #34d399;
                border-radius: 4px;
                padding: 1px 2px;
                font-weight: bold;
                font-size: 10px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #0ea5e9);
            }
        """)
        self.cut_btn.setToolTip("បើកផ្ទាំងកាត់តវីដេអូ (Video Cutter Studio)")
        self.cut_btn.clicked.connect(self._on_cut_clicked)
        ctrl_lay.addWidget(self.cut_btn)

        ctrl_lay.addStretch()
        layout.addLayout(ctrl_lay)

    def _on_mark_in_clicked(self):
        cur_sec = self.current_frame / max(1.0, self.fps)
        self.mark_in_requested.emit(cur_sec)

    def _on_mark_out_clicked(self):
        cur_sec = self.current_frame / max(1.0, self.fps)
        self.mark_out_requested.emit(cur_sec)

    def _on_cut_clicked(self):
        cur_sec = self.current_frame / max(1.0, self.fps)
        self.cut_requested.emit(cur_sec)

    def _on_volume_changed(self, val: int):
        if hasattr(self, 'audio_output') and hasattr(self.audio_output, 'setVolume'):
            self.audio_output.setVolume(val / 100.0)

    def _on_bgm_slider_changed(self, val: int):
        if hasattr(self, 'bgm_vol_slider'):
            self.bgm_vol_slider.setToolTip(f"កម្រិតសំឡេងដើម / BGM Volume: {val}%")
        main_win = self.window()
        if hasattr(main_win, 'bgm_vol_spin'):
            main_win.bgm_vol_spin.blockSignals(True)
            main_win.bgm_vol_spin.setValue(val)
            main_win.bgm_vol_spin.blockSignals(False)
        if hasattr(self, 'audio_track_combo') and self.audio_track_combo.currentIndex() == 1:
            self.reload_mixed_audio()

    def _populate_audio_devices(self):
        """Populate all available audio output devices (AirPods, MacBook Speakers, etc.)."""
        try:
            from PySide6.QtMultimedia import QMediaDevices
            if not hasattr(self, 'audio_device_combo'):
                return
            self.audio_device_combo.blockSignals(True)
            self.audio_device_combo.clear()
            devs = QMediaDevices.audioOutputs()
            def_dev = QMediaDevices.defaultAudioOutput()
            selected_idx = 0
            for i, dev in enumerate(devs):
                name = dev.description()
                icon = "🎧 " if any(k in name.lower() for k in ["airpods", "headphone", "buds", "bluetooth"]) else "🔊 "
                label = f"{icon}{name}"
                if len(label) > 18:
                    label = label[:16] + ".."
                self.audio_device_combo.addItem(label, userData=dev)
                if def_dev and dev.id() == def_dev.id():
                    selected_idx = i
            if self.audio_device_combo.count() > 0:
                self.audio_device_combo.setCurrentIndex(selected_idx)
            self.audio_device_combo.blockSignals(False)
        except Exception as e:
            logger.debug(f"Audio device enumeration notice: {e}")

    def _on_audio_device_changed(self, index: int):
        """Switch audio output device dynamically."""
        try:
            dev = self.audio_device_combo.currentData()
            if dev and hasattr(self, 'audio_output'):
                self.audio_output.setDevice(dev)
                logger.info(f"🔊 [Audio] Output switched to: {dev.description()}")
        except Exception as e:
            logger.warning(f"Audio device switch failed: {e}")

    def _on_media_status_changed(self, status):
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            if status in [QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia]:
                if hasattr(self, '_pending_audio_pos_ms') and self._pending_audio_pos_ms is not None:
                    pos = self._pending_audio_pos_ms
                    self._pending_audio_pos_ms = None
                    self.player.setPosition(pos)
                elif self._is_playing:
                    cur_ms = int(getattr(self, '_current_global_sec', 0.0) * 1000)
                    self.player.setPosition(cur_ms)
                if getattr(self, '_pending_play', False) or (self._is_playing and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState):
                    self._pending_play = False
                    if hasattr(self, 'audio_output'):
                        if hasattr(self.audio_output, 'setMuted'):
                            self.audio_output.setMuted(False)
                        vol = self.vol_slider.value() / 100.0 if hasattr(self, 'vol_slider') else 1.0
                        self.audio_output.setVolume(vol)
                    self.player.play()
                    logger.info("🔊 [VideoPreview] Audio playback resumed from LoadedMedia status.")
        except Exception as e:
            logger.debug(f"Media status changed handler: {e}")

    def _on_audio_track_changed(self, index=0):
        was_playing = self._is_playing
        cur_sec = getattr(self, '_current_global_sec', 0.0)
        pos_ms = int(cur_sec * 1000)
        self._pending_audio_pos_ms = pos_ms
        self._load_audio_for_player()
        try:
            self.player.setPosition(pos_ms)
            if was_playing:
                if hasattr(self, 'audio_output') and hasattr(self.audio_output, 'setMuted'):
                    self.audio_output.setMuted(False)
                self.player.play()
        except Exception:
            pass

    def _extract_preview_audio_for_video(self, video_path: str) -> str:
        """Extract dedicated, verified preview audio track for the loaded video."""
        import hashlib
        try:
            stat = os.stat(video_path)
            cache_key = f"{os.path.abspath(video_path)}_{int(stat.st_mtime)}_{stat.st_size}"
        except Exception:
            cache_key = os.path.abspath(video_path)
        vid_id = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        from utils.file_utils import get_temp_path
        out_wav = get_temp_path(f"preview_track_{vid_id}.wav")

        needs_extract = True
        if os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000:
            try:
                if hasattr(stat, 'st_mtime') and os.path.getmtime(out_wav) >= stat.st_mtime:
                    needs_extract = False
            except Exception:
                needs_extract = True

        if needs_extract:
            logger.info(f"🎙 [VideoPreview] Fast-extracting preview audio from {os.path.basename(video_path)}...")
            cmd = [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-c:a", "pcm_s16le",
                "-ar", "44100", "-ac", "2",
                out_wav
            ]
            subprocess.run(cmd, capture_output=True, check=False)
        return out_wav

    def reload_mixed_audio(self):
        """Force re-generate and reload the mixed preview audio track."""
        if not self.video_path:
            return
        was_playing = self._is_playing
        pos_ms = int((self.current_frame / max(1.0, self.fps)) * 1000)
        self._pending_audio_pos_ms = pos_ms
        self._load_audio_for_player()
        if hasattr(self, 'player'):
            try:
                self.player.setPosition(pos_ms)
                if was_playing:
                    if hasattr(self, 'audio_output') and hasattr(self.audio_output, 'setMuted'):
                        self.audio_output.setMuted(False)
                    self.player.play()
            except Exception:
                pass

    def audition_voice(self):
        """Instant high-volume audition of the generated Khmer voices."""
        main_win = self.window()
        last_master = getattr(main_win, 'last_master_wav', None)
        from utils.file_utils import get_temp_path
        master_wav = last_master if (last_master and os.path.exists(last_master)) else get_temp_path("master_khmer_voice.wav")
        if not os.path.exists(master_wav) or os.path.getsize(master_wav) < 1000:
            logger.warning("No master Khmer voice available to audition.")
            return

        pos_sec = max(0.0, self.current_frame / max(1.0, self.fps))
        logger.info(f"🗣 [Audition] Playing Khmer master voice starting from {pos_sec:.1f}s...")

        # If currently playing via Qt player, pause it so audio doesn't double
        if self._is_playing:
            self._toggle_play()

        # Stop any previous audition process
        if hasattr(self, '_audition_proc') and self._audition_proc and self._audition_proc.poll() is None:
            try:
                self._audition_proc.terminate()
            except Exception:
                pass

        if sys.platform == "darwin":
            try:
                self._audition_proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-ss", str(pos_sec), master_wav],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception:
                self._audition_proc = subprocess.Popen(["afplay", "-v", "2", master_wav])
        else:
            self._audition_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-ss", str(pos_sec), master_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

    def resolve_clip_at_time(self, global_sec: float):
        """
        Single Source of Truth for timeline clip resolution (CapCut NLE Architecture).
        Maps global timeline time (seconds) to:
            (clip_index: int, clip: dict, local_sec: float)
        Accounts for:
            - source_in / source_out
            - speed multiplier (local_source_time = source_in + (global_time - clip.start) / speed)
            - epsilon boundary tolerance (eps = 0.001) for seamless boundary transitions
        """
        if not self.video_clips:
            return None, None, 0.0

        n = len(self.video_clips)
        if global_sec <= 0.0:
            first_clip = self.video_clips[0]
            s_in = max(0.0, float(first_clip.get("source_in", 0.0)))
            return 0, first_clip, s_in

        for idx, clip in enumerate(self.video_clips):
            st = float(clip.get("start", 0.0))
            dur = max(0.01, float(clip.get("duration", 0.0)))
            s_in = max(0.0, float(clip.get("source_in", 0.0)))
            speed = max(0.1, float(clip.get("speed", 1.0)))
            c_end = st + dur

            if idx == n - 1:
                # Last clip covers its start up to total duration
                if global_sec >= st:
                    raw_local = max(0.0, min(dur, global_sec - st))
                    local_sec = s_in + (raw_local * speed)
                    return idx, clip, local_sec
            else:
                # Strict half-open interval [start, end)
                if st <= global_sec < c_end:
                    raw_local = max(0.0, min(dur, global_sec - st))
                    local_sec = s_in + (raw_local * speed)
                    return idx, clip, local_sec

        last_idx = n - 1
        last_clip = self.video_clips[last_idx]
        s_in = max(0.0, float(last_clip.get("source_in", 0.0)))
        speed = max(0.1, float(last_clip.get("speed", 1.0)))
        dur = max(0.01, float(last_clip.get("duration", 0.0)))
        raw_local = max(0.0, min(dur, global_sec - float(last_clip.get("start", 0.0))))
        local_sec = s_in + (raw_local / speed)
        return last_idx, last_clip, local_sec

    def switch_to_clip(self, clip_idx: int) -> bool:
        """
        Switches the OpenCV decoder to the target clip if not already active.
        Maintains local decoder state without disturbing global timeline clock.
        """
        if not self.video_clips or clip_idx is None or not (0 <= clip_idx < len(self.video_clips)):
            return False

        target_clip = self.video_clips[clip_idx]
        p = os.path.abspath(target_clip.get("path", ""))
        if not p or not os.path.exists(p):
            logger.error(f"❌ [VideoPreview] Clip {clip_idx} path invalid: {p}")
            return False

        if getattr(self, '_active_clip_idx', None) == clip_idx and self.cap and self.cap.isOpened():
            return True

        old_idx = getattr(self, '_active_clip_idx', None)
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

        self.cap = cv2.VideoCapture(p)
        if not self.cap.isOpened():
            logger.error(f"❌ [VideoPreview] OpenCV cannot open clip {clip_idx}: {p}")
            return False

        self._active_clip_idx = clip_idx
        self._active_clip_path = p
        self.video_path = p

        c_fps = self.cap.get(cv2.CAP_PROP_FPS)
        if c_fps and c_fps > 5.0:
            self.fps = c_fps
        self.current_frame = -1  # Reset local frame counter for new decoder
        cur_g = getattr(self, '_current_global_sec', 0.0)
        logger.info(f"🎬 [CLIP_SWITCH] old={old_idx} new={clip_idx} global={cur_g:.3f}s path={os.path.basename(p)}")
        return True

    def _ensure_timeline_preview_audio(self, force_rebuild: bool = False) -> str:
        """
        Builds or returns a single continuous timeline preview audio WAV matching
        the entire virtual timeline duration 1:1.
        """
        if not self.video_clips:
            if self.video_path and os.path.exists(self.video_path):
                return self._extract_preview_audio_for_video(self.video_path)
            return None

        from utils.file_utils import get_temp_path
        import hashlib

        if len(self.video_clips) == 1:
            return self._extract_preview_audio_for_video(self.video_clips[0]["path"])

        cache_key = "|".join(
            f"{os.path.abspath(c.get('path',''))}:{c.get('start',0.0):.2f}:{c.get('duration',0.0):.2f}"
            for c in self.video_clips
        )
        hash_id = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        out_wav = get_temp_path(f"timeline_preview_audio_{hash_id}.wav")

        if not force_rebuild and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000:
            return out_wav

        logger.info(f"🎙 [VideoPreview] Building timeline composite audio for {len(self.video_clips)} clips...")
        inputs = []
        filter_parts = []
        for i, c in enumerate(self.video_clips):
            inputs.extend(["-i", c.get("path", "")])
            src_in = max(0.0, c.get("source_in", 0.0))
            dur = max(0.01, c.get("duration", 0.0))
            filter_parts.append(f"[{i}:a]atrim=start={src_in}:duration={dur},asetpts=PTS-STARTPTS[a{i}]")

        n = len(self.video_clips)
        concat_str = "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[aout]"
        filter_str = ";".join(filter_parts) + ";" + concat_str

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "[aout]",
            "-c:a", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            out_wav
        ]
        res = subprocess.run(cmd, capture_output=True, check=False)
        if res.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000:
            logger.info(f"✅ [VideoPreview] Timeline composite audio built: {os.path.basename(out_wav)}")
            return out_wav
        else:
            # Fallback using concat demuxer of per-clip extracted wavs
            clip_wavs = [self._extract_preview_audio_for_video(c["path"]) for c in self.video_clips]
            valid_wavs = [w for w in clip_wavs if w and os.path.exists(w)]
            if valid_wavs:
                list_txt = get_temp_path(f"timeline_audio_list_{hash_id}.txt")
                with open(list_txt, "w") as f:
                    for w in valid_wavs:
                        f.write(f"file '{os.path.abspath(w)}'\n")
                cmd_fallback = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", list_txt,
                    "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
                    out_wav
                ]
                subprocess.run(cmd_fallback, capture_output=True, check=False)
                if os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000:
                    return out_wav

        return self._extract_preview_audio_for_video(self.video_clips[0]["path"])

    def _load_audio_for_player(self):
        if not self.video_clips and not self.video_path:
            return

        if getattr(self, '_loading_preview_audio', False):
            return
        self._loading_preview_audio = True

        try:
            from utils.file_utils import get_temp_path
            combo_idx = self.audio_track_combo.currentIndex() if hasattr(self, 'audio_track_combo') else 1

            main_win = self.window()
            last_master = getattr(main_win, 'last_master_wav', None)
            default_master = get_temp_path("master_khmer_voice.wav")
            if last_master and os.path.exists(last_master) and os.path.getsize(last_master) > 1000:
                master_khmer_wav = last_master
            else:
                master_khmer_wav = default_master

            target_audio_file = None
            # Track 2: 🗣 Khmer Voice Only
            if combo_idx == 2 and os.path.exists(master_khmer_wav) and os.path.getsize(master_khmer_wav) > 1000:
                target_audio_file = master_khmer_wav
            # Track 1: 🇰🇭 Khmer Dubbed + BGM
            elif combo_idx == 1 and os.path.exists(master_khmer_wav) and os.path.getsize(master_khmer_wav) > 1000:
                if hasattr(self, 'bgm_vol_slider'):
                    bgm_vol = self.bgm_vol_slider.value() / 100.0
                else:
                    bgm_spin = getattr(main_win, 'bgm_vol_spin', None)
                    bgm_vol = (bgm_spin.value() / 100.0) if bgm_spin else 0.45

                orig_audio = self._ensure_timeline_preview_audio()
                segments = []
                if hasattr(main_win, 'subtitle_table') and hasattr(main_win.subtitle_table, 'get_updated_segments'):
                    segments = main_win.subtitle_table.get_updated_segments()

                if orig_audio and os.path.exists(orig_audio) and bgm_vol > 0.0:
                    from services.audio_separator import create_clean_background_track
                    cleaned_bg = get_temp_path("preview_bg_cleaned.wav")
                    try:
                        create_clean_background_track(
                            orig_audio_path=orig_audio,
                            segments=segments,
                            output_bgm_path=cleaned_bg,
                            bgm_volume=bgm_vol,
                            duck_speech_db=-70.0,
                            pre_pad_sec=0.08,
                            post_pad_sec=0.12
                        )
                        mixed_wav = get_temp_path("preview_khmer_mixed.wav")
                        cmd_mix = [
                            "ffmpeg", "-y",
                            "-i", master_khmer_wav,
                            "-i", cleaned_bg,
                            "-filter_complex", "[0:a]volume=1.3[v0];[1:a]volume=1.0[v1];[v0][v1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.99[aout]",
                            "-map", "[aout]",
                            "-ar", "44100",
                            "-ac", "2",
                            mixed_wav
                        ]
                        res = subprocess.run(cmd_mix, capture_output=True, check=False)
                        if res.returncode == 0 and os.path.exists(mixed_wav) and os.path.getsize(mixed_wav) > 1000:
                            target_audio_file = mixed_wav
                        else:
                            target_audio_file = master_khmer_wav
                    except Exception as e_mix:
                        logger.error(f"Error creating mixed preview: {e_mix}")
                        target_audio_file = master_khmer_wav
                else:
                    target_audio_file = master_khmer_wav
            else:
                # Track 0: 🔊 Original Audio (Represents the entire timeline 1:1)
                target_audio_file = self._ensure_timeline_preview_audio()

            if target_audio_file and os.path.exists(target_audio_file) and os.path.getsize(target_audio_file) > 1000:
                if target_audio_file.lower().endswith(".wav"):
                    try:
                        import wave
                        with wave.open(target_audio_file, 'rb') as wf:
                            fr = wf.getframerate()
                            ch = wf.getnchannels()
                        if fr != 44100 or ch != 2:
                            from pydub import AudioSegment
                            norm_audio = AudioSegment.from_file(target_audio_file).set_frame_rate(44100).set_channels(2)
                            norm_audio.export(target_audio_file, format="wav")
                    except Exception as e_norm:
                        logger.debug(f"Audio norm check: {e_norm}")

                url = QUrl.fromLocalFile(os.path.abspath(target_audio_file))
                if hasattr(self, 'player') and hasattr(self.player, 'source') and self.player.source() == url:
                    return
                logger.info(f"🔊 [VideoPreview] Loaded preview audio track: {os.path.basename(target_audio_file)} ({os.path.getsize(target_audio_file)/1024:.1f} KB)")
                if hasattr(self, 'audio_output'):
                    if hasattr(self.audio_output, 'setMuted'):
                        self.audio_output.setMuted(False)
                    if hasattr(self.audio_output, 'setVolume'):
                        self.audio_output.setVolume(self.vol_slider.value() / 100.0 if hasattr(self, 'vol_slider') else 1.0)
                if hasattr(self, 'player') and hasattr(self.player, 'setSource'):
                    self.player.setSource(url)
                elif hasattr(self, 'player') and hasattr(self.player, 'setMedia'):
                    content = create_media_content(url)
                    self.player.setMedia(content)
        finally:
            self._loading_preview_audio = False

    def set_video_path(self, video_path: str, preserve_playback: bool = False) -> bool:
        """Set loaded video path, initialize OpenCV VideoCapture & render frame 0 instantly."""
        if not video_path or not os.path.exists(video_path):
            logger.error(f"❌ Video path is invalid or does not exist: {video_path}")
            return False

        abs_path = os.path.abspath(video_path)
        self.video_path = abs_path
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

        self.cap = cv2.VideoCapture(video_path)
        if not self.cap.isOpened():
            logger.error(f"❌ OpenCV cannot open video file: {video_path}")
            return False

        self._active_clip_idx = 0
        self._active_clip_path = abs_path
        c_fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = c_fps if (c_fps and c_fps > 5.0) else 30.0
        frame_cnt = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur_sec = (frame_cnt / max(1.0, self.fps)) if self.fps > 0 else 0.0

        if not preserve_playback:
            self.current_frame = 0
            self._current_global_sec = 0.0
            self.video_clips = [{
                "name": os.path.basename(video_path),
                "start": 0.0,
                "duration": dur_sec,
                "path": abs_path
            }]
            self.total_frames = frame_cnt
            self._display_frame_at(0)
            self._update_time_label(0)
            self.play_btn.setText("▶")
            self._is_playing = False
        else:
            if getattr(self, 'video_clips', None):
                tot = sum(c.get("duration", 0.0) for c in self.video_clips)
                self.total_frames = int(tot * self.fps)

        self._load_audio_for_player()
        return True

    def set_video_clips(self, clips: list):
        """Set multi-clip virtual sequence composition for seamless CapCut-style playback."""
        self.video_clips = list(clips or [])
        if self.video_clips:
            tot = sum(c.get("duration", 0.0) for c in self.video_clips)
            cur_global = getattr(self, '_current_global_sec', 0.0)
            clip_idx, clip, local_sec = self.resolve_clip_at_time(cur_global)
            if clip_idx is not None:
                self.switch_to_clip(clip_idx)

            if getattr(self, 'fps', 0.0) > 0 and tot > 0:
                self.total_frames = int(tot * self.fps)
            if hasattr(self, 'time_slider'):
                self.time_slider.setMaximum(self.total_frames)

            # Build/ensure timeline preview audio matching all clips
            self._load_audio_for_player()

            # Render current frame
            if clip_idx is not None and self.cap and self.cap.isOpened():
                local_frame = int(local_sec * max(1.0, self.fps))
                self._display_frame_at(local_frame)
                global_frame = int(cur_global * max(1.0, self.fps))
                self._update_time_label(global_frame)
        else:
            self.total_frames = 0
            self.current_frame = 0
            self._current_global_sec = 0.0
            self._active_clip_idx = None
            self._active_clip_path = None
            self.video_path = None
            if hasattr(self, 'player'):
                try:
                    self.player.stop()
                except Exception:
                    pass
            if hasattr(self, 'play_btn'):
                self.play_btn.setIcon(get_svg_icon("play", "#ffffff", 14))
                self.play_btn.setText("▶")
            self._is_playing = False
            if hasattr(self, 'cap') and self.cap:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
            if hasattr(self, 'placeholder_lbl'):
                self.placeholder_lbl.setPixmap(QtGui.QPixmap())
                self.placeholder_lbl.setText("🎬 អូសទម្លាក់វីដេអូ ឬចុច '➕ Timeline' ដើម្បីកាត់ត")
            self._update_time_label(0)

    def set_timeline_segments(self, segments: list):
        """Set timeline segments for waveform and burn subtitle display with deterministic timing resolution"""
        if segments:
            try:
                from core.subtitle_timing import resolve_subtitle_timings
                tot_dur = (self.total_frames / max(1.0, self.fps)) if getattr(self, 'fps', 0) > 0 else 999999.0
                self._timeline_segments = resolve_subtitle_timings(segments, total_duration_sec=tot_dur)
            except Exception:
                self._timeline_segments = list(segments)
        else:
            self._timeline_segments = []
        if hasattr(self, '_sub_render_cache'):
            self._sub_render_cache.clear()
        self._update_display()

    def _get_active_blur_item(self):
        if not self.blur_items:
            return None
        for b in self.blur_items:
            if b.get("id") == self.active_blur_id:
                return b
        return self.blur_items[0]

    def _get_canvas_size(self):
        """Returns the actual on-screen video canvas dimensions (placeholder_lbl) where frames are displayed."""
        if hasattr(self, 'placeholder_lbl') and self.placeholder_lbl.width() > 30 and self.placeholder_lbl.height() > 30:
            return (self.placeholder_lbl.width(), self.placeholder_lbl.height())
        if hasattr(self, 'screen_frame') and self.screen_frame.width() > 30 and self.screen_frame.height() > 30:
            return (self.screen_frame.width(), self.screen_frame.height())
        return (640, 360)

    def _map_mouse_to_canvas(self, pos, source_widget=None):
        """Maps any mouse position into placeholder_lbl (canvas) coordinates."""
        if hasattr(self, 'placeholder_lbl') and source_widget is not None and source_widget != self.placeholder_lbl:
            try:
                global_pos = source_widget.mapToGlobal(pos)
                local_pos = self.placeholder_lbl.mapFromGlobal(global_pos)
                cw, ch = self._get_canvas_size()
                return QtCore.QPoint(max(0, min(cw, local_pos.x())), max(0, min(ch, local_pos.y())))
            except Exception:
                cw, ch = self._get_canvas_size()
                return QtCore.QPoint(max(0, min(cw, pos.x())), max(0, min(ch, pos.y())))
        return pos

    def _test_item_handle(self, item, pos, test_handles_only=False):
        x = item.get("x", 0)
        y = item.get("y", 0)
        w = max(20, item.get("width", 100))
        h = max(20, item.get("height", 80))
        rot = item.get("rotation", 0.0)
        cx = x + w / 2.0
        cy = y + h / 2.0
        
        dx = pos.x() - cx
        dy = pos.y() - cy
        rad = math.radians(rot)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        lx = dx * cos_r + dy * sin_r
        ly = -dx * sin_r + dy * cos_r
        
        # 1. Delete handle [ ✕ ]: 14px outside top-right corner
        if math.hypot(lx - (w / 2.0 + 14.0), ly - (-h / 2.0 - 14.0)) <= 15:
            return "delete"

        # 2. Rotation handle: 25px above top-center in local space
        if math.hypot(lx, ly - (-h / 2.0 - 25.0)) <= 13:
            return "rotate"
            
        # 3. 4 Corner resize handles
        if math.hypot(lx - (-w / 2.0), ly - (-h / 2.0)) <= 11: return "top_left"
        if math.hypot(lx - (w / 2.0), ly - (-h / 2.0)) <= 11: return "top_right"
        if math.hypot(lx - (w / 2.0), ly - (h / 2.0)) <= 11: return "bottom_right"
        if math.hypot(lx - (-w / 2.0), ly - (h / 2.0)) <= 11: return "bottom_left"
        
        if test_handles_only:
            return None
            
        # 4. Body hit test
        if (-w / 2.0 <= lx <= w / 2.0) and (-h / 2.0 <= ly <= h / 2.0):
            return "move"
            
        return None

    def _on_screen_drag_enter(self, event):
        md = event.mimeData()
        if md.hasFormat("application/x-mask-preset") or (md.hasText() and md.text().startswith("preset:")):
            event.acceptProposedAction()
        elif md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _on_screen_drag_move(self, event):
        md = event.mimeData()
        if md.hasFormat("application/x-mask-preset") or (md.hasText() and md.text().startswith("preset:")):
            event.acceptProposedAction()
        elif md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _on_screen_drop(self, event, source_widget=None):
        md = event.mimeData()
        preset_type = None
        if md.hasFormat("application/x-mask-preset"):
            try:
                preset_type = bytes(md.data("application/x-mask-preset")).decode("utf-8")
            except Exception:
                pass
        if not preset_type and md.hasText() and md.text().startswith("preset:"):
            preset_type = md.text().split(":", 1)[1]

        if preset_type:
            pos = event.pos() if hasattr(event, 'pos') else event.position().toPoint()
            if source_widget:
                pos = self._map_mouse_to_canvas(pos, source_widget)
            self.add_mask_preset(preset_type, drop_x=pos.x(), drop_y=pos.y())
            event.acceptProposedAction()
            return

        if md.hasUrls():
            valid_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v')
            valid_paths = [u.toLocalFile() for u in md.urls() if u.toLocalFile().lower().endswith(valid_exts)]
            if len(valid_paths) == 1:
                self.file_dropped.emit(valid_paths[0])
                event.acceptProposedAction()
                return
            elif len(valid_paths) > 1:
                self.files_dropped.emit(valid_paths)
                event.acceptProposedAction()
                return

        event.ignore()

    def _get_blur_handle_at(self, pos):
        if not self.blur_items:
            return (None, None)
        active_item = self._get_active_blur_item()
        if active_item:
            handle = self._test_item_handle(active_item, pos)
            if handle:
                return (active_item["id"], handle)
        for b_item in reversed(self.blur_items):
            if b_item.get("id") == self.active_blur_id:
                continue
            handle = self._test_item_handle(b_item, pos, test_handles_only=False)
            if handle:
                return (b_item["id"], handle)
        return (None, None)

    def _get_logo_handle_at(self, pos, test_handles_only=False):
        r = QtCore.QRect(self.logo_x, self.logo_y, self.logo_width, self.logo_height)
        m = 10
        if QtCore.QRect(r.left() - m, r.top() - m, m * 2, m * 2).contains(pos): return "top_left"
        if QtCore.QRect(r.right() - m, r.top() - m, m * 2, m * 2).contains(pos): return "top_right"
        if QtCore.QRect(r.left() - m, r.bottom() - m, m * 2, m * 2).contains(pos): return "bottom_left"
        if QtCore.QRect(r.right() - m, r.bottom() - m, m * 2, m * 2).contains(pos): return "bottom_right"
        if not test_handles_only and r.contains(pos): return "move"
        return None

    def _get_hit_target_at(self, pos):
        # 1. Handles of active blur mask (delete ❌, rotate, resize)
        if self.blur_enabled and self.blur_items and self.active_blur_id:
            active_blur = self._get_active_blur_item()
            if active_blur:
                h = self._test_item_handle(active_blur, pos, test_handles_only=True)
                if h:
                    return ("blur", h, active_blur["id"])

        # 2. Handles of active logo
        if self.logo_enabled:
            h = self._get_logo_handle_at(pos, test_handles_only=True)
            if h:
                return ("logo", h, None)

        # 3. Active blur mask BODY (immediately select and drag active mask without conflict)
        if self.blur_enabled and self.blur_items and self.active_blur_id:
            active_blur = self._get_active_blur_item()
            if active_blur:
                h = self._test_item_handle(active_blur, pos, test_handles_only=False)
                if h == "move":
                    return ("blur", "move", active_blur["id"])

        # 4. Other blur masks (from topmost to bottom)
        if self.blur_enabled and self.blur_items:
            for b_item in reversed(self.blur_items):
                if b_item.get("id") == self.active_blur_id:
                    continue
                h = self._test_item_handle(b_item, pos, test_handles_only=False)
                if h:
                    return ("blur", h, b_item["id"])

        # 5. Text Overlays (visual layer)
        if self.text_overlay_enabled:
            if self.active_text_id and self.text_items:
                cur = next((t for t in self.text_items if t.get("id") == self.active_text_id), None)
                if cur and cur.get("_screen_rect") and cur["_screen_rect"].adjusted(-5, -5, 5, 5).contains(pos):
                    return ("text", "move", cur["id"])
            if self.text_items:
                for t in reversed(self.text_items):
                    if t.get("enabled", True) and t.get("_screen_rect") and t["_screen_rect"].adjusted(-5, -5, 5, 5).contains(pos):
                        return ("text", "move", t["id"])
            elif self.text_rect and self.text_rect.adjusted(-5, -5, 5, 5).contains(pos):
                return ("text", "move", None)

        # 6. Logo Body
        if self.logo_enabled:
            h = self._get_logo_handle_at(pos, test_handles_only=False)
            if h:
                return ("logo", h, None)

        # 7. Burn Subtitle (Lowest priority: only if not hitting any mask, text, or logo!)
        if self.burn_subtitle_enabled and getattr(self, 'burn_sub_rect', None):
            if self.burn_sub_rect.adjusted(-4, -4, 4, 4).contains(pos):
                return ("burn_subtitle", "move", None)

        return (None, None, None)

    def _on_mouse_press(self, event, source_widget=None):
        if event.button() == Qt.RightButton:
            return

        pos = event.pos() if hasattr(event, 'pos') else event.position().toPoint()
        if source_widget:
            pos = self._map_mouse_to_canvas(pos, source_widget)

        target, handle, item_id = self._get_hit_target_at(pos)
        if target:
            # Auto-pause playback if active so user can drag comfortably
            if self._is_playing:
                self.play_timer.stop()
                if hasattr(self, 'player') and self.player:
                    try:
                        self.player.pause()
                    except Exception:
                        pass
                self._is_playing = False
                self._pending_play = False
                if hasattr(self, 'play_btn'):
                    self.play_btn.setText("▶")

            if target == "blur" and handle == "delete":
                self.delete_blur(item_id)
                self.setCursor(Qt.ArrowCursor)
                return

            self._active_target = target
            self._active_handle = handle
            self._active_item_id = item_id
            self._drag_start_pos = pos
            if target == "blur":
                b_item = next((b for b in self.blur_items if b.get("id") == item_id), None)
                if b_item:
                    self._drag_start_item = dict(b_item)
                    if self.active_blur_id != item_id:
                        self.active_blur_id = item_id
                        self.blur_selected.emit(item_id)
                        self.blur_items_changed.emit(self.blur_items, self.active_blur_id)
                        self._update_display()
            elif target == "logo":
                self._drag_start_rect = QtCore.QRect(self.logo_x, self.logo_y, self.logo_width, self.logo_height)
            elif target == "text":
                if item_id and item_id != self.active_text_id:
                    self.set_active_text(item_id)
                cur_item = self._get_active_text_item()
                if cur_item:
                    self._drag_start_text_pos = (cur_item["pos"][0], cur_item["pos"][1])
                else:
                    self._drag_start_text_pos = (self.text_position[0], self.text_position[1])
            elif target == "burn_subtitle":
                self._drag_start_sub_ratio = (self.burn_sub_x_ratio, self.burn_sub_y_ratio)

    def _on_mouse_move(self, event, source_widget=None):
        pos = event.pos() if hasattr(event, 'pos') else event.position().toPoint()
        if source_widget:
            pos = self._map_mouse_to_canvas(pos, source_widget)
        
        if not self._active_target:
            target, handle, item_id = self._get_hit_target_at(pos)
            if target == "blur":
                if handle == "delete": self.setCursor(Qt.PointingHandCursor)
                elif handle == "rotate": self.setCursor(Qt.CrossCursor)
                elif handle in ("top_left", "bottom_right"): self.setCursor(Qt.SizeFDiagCursor)
                elif handle in ("top_right", "bottom_left"): self.setCursor(Qt.SizeBDiagCursor)
                elif handle == "move": self.setCursor(Qt.SizeAllCursor)
            elif target == "logo":
                if handle in ("top_left", "bottom_right"): self.setCursor(Qt.SizeFDiagCursor)
                elif handle in ("top_right", "bottom_left"): self.setCursor(Qt.SizeBDiagCursor)
                elif handle == "move": self.setCursor(Qt.SizeAllCursor)
            elif target in ("text", "burn_subtitle"):
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        dx = pos.x() - self._drag_start_pos.x()
        dy = pos.y() - self._drag_start_pos.y()

        if self._active_target == "blur" and self._drag_start_item:
            b_item = next((b for b in self.blur_items if b.get("id") == self._active_item_id), None)
            if b_item:
                orig = self._drag_start_item
                orig_cx = orig["x"] + orig["width"] / 2.0
                orig_cy = orig["y"] + orig["height"] / 2.0
                rot = orig.get("rotation", 0.0)
                rad = math.radians(rot)
                cos_r = math.cos(rad)
                sin_r = math.sin(rad)
                
                if self._active_handle == "rotate":
                    deg = math.degrees(math.atan2(pos.y() - orig_cy, pos.x() - orig_cx)) + 90.0
                    while deg > 180.0: deg -= 360.0
                    while deg < -180.0: deg += 360.0
                    b_item["rotation"] = round(deg, 1)
                    self._update_display()
                elif self._active_handle == "move":
                    b_item["x"] = max(0, orig["x"] + dx)
                    b_item["y"] = max(0, orig["y"] + dy)
                    self.blur_rect = QtCore.QRect(b_item["x"], b_item["y"], b_item["width"], b_item["height"])
                    self._update_display()
                elif self._active_handle in ("top_left", "top_right", "bottom_left", "bottom_right"):
                    dlx = dx * cos_r + dy * sin_r
                    dly = -dx * sin_r + dy * cos_r
                    orig_w = orig["width"]
                    orig_h = orig["height"]
                    min_w, min_h = 20, 20
                    
                    if self._active_handle == "bottom_right":
                        new_w = max(min_w, orig_w + dlx)
                        new_h = max(min_h, orig_h + dly)
                        shift_lx = (new_w - orig_w) / 2.0
                        shift_ly = (new_h - orig_h) / 2.0
                    elif self._active_handle == "bottom_left":
                        new_w = max(min_w, orig_w - dlx)
                        new_h = max(min_h, orig_h + dly)
                        shift_lx = -(new_w - orig_w) / 2.0
                        shift_ly = (new_h - orig_h) / 2.0
                    elif self._active_handle == "top_right":
                        new_w = max(min_w, orig_w + dlx)
                        new_h = max(min_h, orig_h - dly)
                        shift_lx = (new_w - orig_w) / 2.0
                        shift_ly = -(new_h - orig_h) / 2.0
                    elif self._active_handle == "top_left":
                        new_w = max(min_w, orig_w - dlx)
                        new_h = max(min_h, orig_h - dly)
                        shift_lx = -(new_w - orig_w) / 2.0
                        shift_ly = -(new_h - orig_h) / 2.0
                        
                    new_cx = orig_cx + (shift_lx * cos_r - shift_ly * sin_r)
                    new_cy = orig_cy + (shift_lx * sin_r + shift_ly * cos_r)
                    
                    b_item["x"] = max(0, int(new_cx - new_w / 2.0))
                    b_item["y"] = max(0, int(new_cy - new_h / 2.0))
                    b_item["width"] = int(new_w)
                    b_item["height"] = int(new_h)
                    self.blur_rect = QtCore.QRect(b_item["x"], b_item["y"], b_item["width"], b_item["height"])
                    self._update_display()

        elif self._active_target == "logo":
            r = QtCore.QRect(self._drag_start_rect)
            if self._active_handle == "move": r.translate(dx, dy)
            elif self._active_handle == "top_left":
                r.setLeft(min(r.right() - 20, r.left() + dx))
                r.setTop(min(r.bottom() - 20, r.top() + dy))
            elif self._active_handle == "top_right":
                r.setRight(max(r.left() + 20, r.right() + dx))
                r.setTop(min(r.bottom() - 20, r.top() + dy))
            elif self._active_handle == "bottom_left":
                r.setLeft(min(r.right() - 20, r.left() + dx))
                r.setBottom(max(r.top() + 20, r.bottom() + dy))
            elif self._active_handle == "bottom_right":
                r.setRight(max(r.left() + 20, r.right() + dx))
                r.setBottom(max(r.top() + 20, r.bottom() + dy))
            
            self.logo_x = max(0, r.x())
            self.logo_y = max(0, r.y())
            self.logo_width = max(20, r.width())
            self.logo_height = max(20, r.height())
            self._update_display()

        elif self._active_target == "text":
            new_x = max(0, self._drag_start_text_pos[0] + dx)
            new_y = max(0, self._drag_start_text_pos[1] + dy)
            self.text_position = (new_x, new_y)
            cur = self._get_active_text_item()
            if cur:
                cur["pos"] = (new_x, new_y)
                cur["x"] = new_x
                cur["y"] = new_y
            self.text_moved.emit(new_x, new_y)
            self._update_display()

        elif self._active_target == "burn_subtitle" and self._drag_start_sub_ratio:
            canvas_w, canvas_h = self._get_canvas_size()
            dx_ratio = dx / float(canvas_w)
            dy_ratio = dy / float(canvas_h)
            new_x = max(0.05, min(0.95, self._drag_start_sub_ratio[0] + dx_ratio))
            new_y = max(0.05, min(0.95, self._drag_start_sub_ratio[1] + dy_ratio))
            self.burn_sub_x_ratio = new_x
            self.burn_sub_y_ratio = new_y
            self._update_display()

    def _on_mouse_release(self, event, source_widget=None):
        if self._active_target == "blur":
            self.blur_items_changed.emit(self.blur_items, self.active_blur_id)
        elif self._active_target == "text":
            cur = self._get_active_text_item()
            if cur:
                self.text_moved.emit(cur["pos"][0], cur["pos"][1])
            else:
                self.text_moved.emit(self.text_position[0], self.text_position[1])
            self.text_items_changed.emit(self.text_items, self.active_text_id)
        elif self._active_target == "logo":
            self.logo_moved.emit(self.logo_x, self.logo_y, self.logo_width, self.logo_height)
        elif self._active_target == "burn_subtitle":
            self.burn_subtitle_moved.emit(int(self.burn_sub_y_ratio * 100), int(self.burn_sub_x_ratio * 100))

        self._active_target = None
        self._active_handle = None
        self._active_item_id = None
        self._drag_start_pos = None
        self._drag_start_rect = None
        self._drag_start_item = None
        self._drag_start_sub_ratio = None
        self.setCursor(Qt.ArrowCursor)

    def _on_context_menu(self, event):
        pos = event.pos()
        target, handle, item_id = self._get_hit_target_at(pos)

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0f172a;
                color: #f8fafc;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 18px;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 500;
            }
            QMenu::item:selected {
                background-color: #2563eb;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #334155;
                margin: 4px 6px;
            }
        """)

        if target == "blur" and item_id:
            b_item = next((b for b in self.blur_items if b.get("id") == item_id), None)
            name = b_item.get("name", "Mask") if b_item else "Mask"
            
            act_del = menu.addAction(f"🗑️ លុប {name} (Delete)")
            act_del.triggered.connect(lambda: self.delete_blur(item_id))
            
            act_dup = menu.addAction(f"📋 ចម្លង {name} (Duplicate)")
            act_dup.triggered.connect(lambda: self.duplicate_blur(item_id))
            
            menu.addSeparator()
            act_reset_rot = menu.addAction("↺ កំណត់មុំដើម (0° Rotation)")
            act_reset_rot.triggered.connect(lambda: self.set_blur_rotation(0.0, item_id))
        else:
            act_p_logo = menu.addAction("📌 បន្ថែម Mask: បិត Logo")
            act_p_logo.triggered.connect(lambda: self.add_mask_preset("logo", drop_x=pos.x(), drop_y=pos.y()))
            
            act_p_sub = menu.addAction("🎙️ បន្ថែម Mask: បិត Subtitle")
            act_p_sub.triggered.connect(lambda: self.add_mask_preset("subtitle", drop_x=pos.x(), drop_y=pos.y()))
            
            act_p_title = menu.addAction("⏱️ បន្ថែម Mask: បិត Title")
            act_p_title.triggered.connect(lambda: self.add_mask_preset("title", drop_x=pos.x(), drop_y=pos.y()))

        menu.exec(event.globalPos() if hasattr(event, 'globalPos') else QtGui.QCursor.pos())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self.active_blur_id:
                self.delete_blur(self.active_blur_id)
                event.accept()
                return
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier) and event.key() == Qt.Key_D:
            if self.active_blur_id:
                self.duplicate_blur(self.active_blur_id)
                event.accept()
                return
        super().keyPressEvent(event)

    # ==================== MULTI-BLUR FUNCTIONS ====================
    def set_blur_enabled(self, enabled: bool):
        self.blur_enabled = enabled
        if enabled and not self.blur_items:
            self.add_blur()
        self._update_display()

    def add_blur(self, start_sec=None, end_sec=None, full_video=True):
        self.blur_enabled = True
        canvas_w, canvas_h = self._get_canvas_size()
        w = max(120, int(canvas_w * 0.35))
        h = max(80, int(canvas_h * 0.25))
        cx = max(10, (canvas_w - w) // 2)
        cy = max(10, (canvas_h - h) // 2)
        
        cur = self.current_frame / max(1.0, self.fps)
        st = cur if start_sec is None else start_sec
        tot = self.total_frames / max(1.0, self.fps)
        et = min(tot if tot > 0 else 60.0, st + 10.0) if end_sec is None else end_sec
        if et <= st:
            et = st + 5.0
            
        b_id = f"blur_{self._next_blur_id}"
        b_name = f"Blur {self._next_blur_id}"
        self._next_blur_id += 1
        
        new_blur = {
            "id": b_id,
            "name": b_name,
            "x": cx,
            "y": cy,
            "width": w,
            "height": h,
            "rotation": 0.0,
            "intensity": getattr(self, 'blur_intensity', 35),
            "type": "blur",
            "mode": "full" if full_video else "range",
            "start_sec": 0.0 if full_video else round(st, 2),
            "end_sec": 999999.0 if full_video else round(et, 2),
            "full_video": full_video,
            "auto_speech": False,
            "solid_color": "#000000"
        }
        self.blur_items.append(new_blur)
        self.active_blur_id = b_id
        self.blur_rect = QtCore.QRect(cx, cy, w, h)
        self._update_display()
        self.blur_items_changed.emit(self.blur_items, self.active_blur_id)
        return b_id

    def add_mask_preset(self, preset_type: str = "logo", drop_x=None, drop_y=None):
        fw, fh = self._get_canvas_size()
        b_id = f"blur_{self._next_blur_id}"
        self._next_blur_id += 1
        offset = (len(self.blur_items) % 5) * 12

        if preset_type == "logo":
            w = max(80, int(fw * 0.22))
            h = max(40, int(fh * 0.12))
            if drop_x is not None and drop_y is not None:
                x = max(0, min(fw - w, drop_x - w // 2))
                y = max(0, min(fh - h, drop_y - h // 2))
            else:
                x = max(10, fw - w - 20 - offset)
                y = min(fh - h - 10, 20 + offset)
            name = f"Logo Cover {self._next_blur_id - 1}"
            m_type = "inpaint"
            mode = "full"
            full_video = True
            auto_speech = False
            st, et = 0.0, 999999.0
        elif preset_type == "subtitle":
            w = max(180, int(fw * 0.70))
            h = max(50, int(fh * 0.14))
            if drop_x is not None and drop_y is not None:
                x = max(0, min(fw - w, drop_x - w // 2))
                y = max(0, min(fh - h, drop_y - h // 2))
            else:
                x = max(10, (fw - w) // 2)
                y = max(10, fh - h - 25 - offset)
            name = f"Sub Mask {self._next_blur_id - 1}"
            m_type = "blur"
            mode = "speech"
            full_video = False
            auto_speech = True
            st, et = 0.0, 999999.0
        else:  # "title"
            w = max(180, int(fw * 0.60))
            h = max(50, int(fh * 0.15))
            if drop_x is not None and drop_y is not None:
                x = max(0, min(fw - w, drop_x - w // 2))
                y = max(0, min(fh - h, drop_y - h // 2))
            else:
                x = max(10, (fw - w) // 2)
                y = max(10, int(fh * 0.10) + offset)
            name = f"Title Mask {self._next_blur_id - 1}"
            m_type = "blur"
            mode = "range"
            full_video = False
            auto_speech = False
            st, et = 0.0, 15.0

        new_mask = {
            "id": b_id,
            "name": name,
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "rotation": 0.0,
            "intensity": 35,
            "type": m_type,
            "mode": mode,
            "full_video": full_video,
            "auto_speech": auto_speech,
            "start_sec": st,
            "end_sec": et,
            "solid_color": "#000000"
        }
        self.blur_items.append(new_mask)
        self.active_blur_id = b_id
        self.blur_rect = QtCore.QRect(x, y, w, h)
        self.blur_enabled = True
        self._update_display()
        self.blur_items_changed.emit(self.blur_items, self.active_blur_id)
        return b_id

    def duplicate_blur(self, blur_id=None):
        target_id = blur_id or self.active_blur_id
        orig = next((b for b in self.blur_items if b.get("id") == target_id), None)
        if not orig:
            return None
            
        b_id = f"blur_{self._next_blur_id}"
        self._next_blur_id += 1
        new_blur = dict(orig)
        new_blur["id"] = b_id
        new_blur["name"] = f"{orig.get('name', 'Blur')} Copy"
        canvas_w, canvas_h = self._get_canvas_size()
        new_blur["x"] = min(canvas_w - orig["width"], orig["x"] + 15)
        new_blur["y"] = min(canvas_h - orig["height"], orig["y"] + 15)
        new_blur["full_video"] = orig.get("full_video", True)
        
        self.blur_items.append(new_blur)
        self.active_blur_id = b_id
        self.blur_rect = QtCore.QRect(new_blur["x"], new_blur["y"], new_blur["width"], new_blur["height"])
        self._update_display()
        self.blur_items_changed.emit(self.blur_items, self.active_blur_id)
        return b_id

    def set_blur_mode(self, mode: str, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["mode"] = mode
            if mode == "full":
                b["full_video"] = True
                b["auto_speech"] = False
                b["start_sec"] = 0.0
                b["end_sec"] = 999999.0
            elif mode == "speech":
                b["full_video"] = False
                b["auto_speech"] = True
                b["start_sec"] = 0.0
                b["end_sec"] = 999999.0
            elif mode == "range":
                b["full_video"] = False
                b["auto_speech"] = False
            self._update_display()
            self.blur_items_changed.emit(self.blur_items, self.active_blur_id)

    def set_blur_color(self, hex_color: str, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["solid_color"] = hex_color
            self._update_display()

    def set_blur_full_video(self, full_video: bool, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["full_video"] = full_video
            b["mode"] = "full" if full_video else "range"
            if full_video:
                b["start_sec"] = 0.0
                b["end_sec"] = 999999.0
        else:
            for b_it in self.blur_items:
                b_it["full_video"] = full_video
                b_it["mode"] = "full" if full_video else "range"
                if full_video:
                    b_it["start_sec"] = 0.0
                    b_it["end_sec"] = 999999.0
        self._update_display()
        self.blur_items_changed.emit(self.blur_items, self.active_blur_id)

    def delete_blur(self, blur_id=None):
        target_id = blur_id or self.active_blur_id
        self.blur_items = [b for b in self.blur_items if b.get("id") != target_id]
        if self.active_blur_id == target_id:
            self.active_blur_id = self.blur_items[0]["id"] if self.blur_items else None
            if self.active_blur_id:
                cur = self.blur_items[0]
                self.blur_rect = QtCore.QRect(cur["x"], cur["y"], cur["width"], cur["height"])
            else:
                self.blur_rect = None
        self._update_display()
        self.blur_items_changed.emit(self.blur_items, self.active_blur_id)

    def set_active_blur(self, blur_id: str):
        if self.active_blur_id != blur_id:
            self.active_blur_id = blur_id
            b = next((x for x in self.blur_items if x.get("id") == blur_id), None)
            if b:
                self.blur_rect = QtCore.QRect(b["x"], b["y"], b["width"], b["height"])
            self._update_display()
            self.blur_items_changed.emit(self.blur_items, self.active_blur_id)

    def set_blur_intensity(self, intensity: int, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["intensity"] = max(1, intensity)
        self.blur_intensity = max(1, intensity)
        self._update_display()

    def set_blur_rotation(self, deg: float, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["rotation"] = round(deg, 1)
            self._update_display()

    def set_blur_type(self, b_type: str, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["type"] = b_type
            self._update_display()

    def set_blur_auto_speech(self, enabled: bool, blur_id: str = None):
        target_id = blur_id or self.active_blur_id
        b = next((x for x in self.blur_items if x.get("id") == target_id), None)
        if b:
            b["auto_speech"] = enabled
            b["mode"] = "speech" if enabled else ("full" if b.get("full_video", True) else "range")
        else:
            self.blur_auto_speech = enabled
            for b_it in self.blur_items:
                b_it["auto_speech"] = enabled
                b_it["mode"] = "speech" if enabled else ("full" if b_it.get("full_video", True) else "range")
        self._update_display()

    def set_blur_item_timing(self, blur_id: str, start_sec: float, end_sec: float):
        b = next((x for x in self.blur_items if x.get("id") == blur_id), None)
        if b:
            b["start_sec"] = round(start_sec, 2)
            b["end_sec"] = round(end_sec, 2)
            self._update_display()

    def reset_blur_position(self):
        canvas_w, canvas_h = self._get_canvas_size()
        w = max(120, int(canvas_w * 0.35))
        h = max(80, int(canvas_h * 0.25))
        cx = max(10, (canvas_w - w) // 2)
        cy = max(10, (canvas_h - h) // 2)
        b = self._get_active_blur_item()
        if b:
            b["x"] = cx
            b["y"] = cy
            b["width"] = w
            b["height"] = h
            b["rotation"] = 0.0
            self.blur_rect = QtCore.QRect(cx, cy, w, h)
            self._update_display()
            self.blur_items_changed.emit(self.blur_items, self.active_blur_id)

    def _apply_blur_to_frame(self, frame, cur_sec: float = 0.0):
        if not self.blur_enabled:
            return frame
        
        h, w, ch = frame.shape
        canvas_w, canvas_h = self._get_canvas_size()
        scale_x = w / float(canvas_w)
        scale_y = h / float(canvas_h)
        
        items = self.blur_items
        if not items and self.blur_rect:
            items = [{
                "id": "legacy_blur",
                "x": self.blur_rect.x(), "y": self.blur_rect.y(),
                "width": self.blur_rect.width(), "height": self.blur_rect.height(),
                "rotation": 0.0, "intensity": self.blur_intensity,
                "type": "blur", "start_sec": 0.0, "end_sec": 99999.0,
                "mode": "full"
            }]
            
        is_speech_time = False
        if self._timeline_segments:
            is_speech_time = any(
                (seg.get("start", 0.0) - 0.15) <= cur_sec <= (seg.get("end", 0.0) + 0.15)
                for seg in self._timeline_segments
            )

        for b_item in items:
            b_id = b_item.get("id")
            is_active_target = (getattr(self, '_active_target', None) == "blur" and getattr(self, '_active_item_id', None) == b_id)
            
            if not check_mask_active(b_item, cur_sec, is_speech_time, is_playing=self._is_playing, is_active_target=is_active_target):
                continue

            try:
                frame = apply_mask_item_to_frame(frame, b_item, scale_x, scale_y)
            except Exception as e_m:
                logger.error(f"Error applying mask in preview: {e_m}")

        return frame

    def _draw_blur_rectangle(self, frame, cur_sec: float = 0.0):
        if not self.blur_enabled:
            return frame
            
        h, w, ch = frame.shape
        canvas_w, canvas_h = self._get_canvas_size()
        scale_x = w / float(canvas_w)
        scale_y = h / float(canvas_h)
        
        items = self.blur_items
        if not items and self.blur_rect:
            items = [{
                "id": "legacy_blur", "name": "Blur 1",
                "x": self.blur_rect.x(), "y": self.blur_rect.y(),
                "width": self.blur_rect.width(), "height": self.blur_rect.height(),
                "rotation": 0.0, "intensity": self.blur_intensity,
                "start_sec": 0.0, "end_sec": 99999.0
            }]
            
        for b_item in items:
            b_id = b_item.get("id")
            is_active = (b_id == self.active_blur_id)
            
            if self._is_playing and getattr(self, '_active_target', None) != "blur":
                continue
                
            bx = b_item.get("x", 0)
            by = b_item.get("y", 0)
            bw = b_item.get("width", 100)
            bh = b_item.get("height", 80)
            rot = b_item.get("rotation", 0.0)
            
            fcx = (bx + bw / 2.0) * scale_x
            fcy = (by + bh / 2.0) * scale_y
            fw = bw * scale_x
            fh = bh * scale_y
            
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
                pts.append([int(px), int(py)])
            pts = np.array(pts, dtype=np.int32)
            
            if is_active:
                cv2.polylines(frame, [pts], isClosed=True, color=(0, 240, 255), thickness=max(1, int(2 * scale_x)))
                
                hs = max(3, int(5 * scale_x))
                for pt in pts:
                    cv2.rectangle(frame, (pt[0] - hs, pt[1] - hs), (pt[0] + hs, pt[1] + hs), (255, 255, 255), -1)
                    cv2.rectangle(frame, (pt[0] - hs, pt[1] - hs), (pt[0] + hs, pt[1] + hs), (0, 230, 118), 1)
                    
                top_mid_x = int(fcx - (fh / 2.0) * (-sin_r))
                top_mid_y = int(fcy + (-fh / 2.0) * cos_r)
                rot_handle_x = int(fcx + (0 * cos_r - (-fh / 2.0 - 25.0 * scale_y) * sin_r))
                rot_handle_y = int(fcy + (0 * sin_r + (-fh / 2.0 - 25.0 * scale_y) * cos_r))
                
                cv2.line(frame, (top_mid_x, top_mid_y), (rot_handle_x, rot_handle_y), (0, 240, 255), max(1, int(1.5 * scale_x)))
                cv2.circle(frame, (rot_handle_x, rot_handle_y), max(4, int(7 * scale_x)), (255, 255, 255), -1)
                cv2.circle(frame, (rot_handle_x, rot_handle_y), max(4, int(7 * scale_x)), (0, 200, 255), max(1, int(2 * scale_x)))
                
                # Top-Right Delete Button [ ✕ ]
                del_btn_lx = fw / 2.0 + 14.0 * scale_x
                del_btn_ly = -fh / 2.0 - 14.0 * scale_y
                del_x = int(fcx + (del_btn_lx * cos_r - del_btn_ly * sin_r))
                del_y = int(fcy + (del_btn_lx * sin_r + del_btn_ly * cos_r))
                del_radius = max(8, int(12 * scale_x))

                cv2.circle(frame, (del_x, del_y), del_radius + 1, (0, 0, 0), -1)
                cv2.circle(frame, (del_x, del_y), del_radius, (40, 40, 230), -1)  # Vivid Red
                cv2.circle(frame, (del_x, del_y), del_radius, (255, 255, 255), max(1, int(1.5 * scale_x)))
                cr = max(3, int(del_radius * 0.45))
                cv2.line(frame, (del_x - cr, del_y - cr), (del_x + cr, del_y + cr), (255, 255, 255), max(1, int(2 * scale_x)), cv2.LINE_AA)
                cv2.line(frame, (del_x - cr, del_y + cr), (del_x + cr, del_y - cr), (255, 255, 255), max(1, int(2 * scale_x)), cv2.LINE_AA)

                label = f"{b_item.get('name', 'Blur')} ({int(rot)}°)"
                cv2.putText(frame, label, (pts[0][0] + 5, max(15, pts[0][1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.45 * scale_x, (0, 240, 255), max(1, int(1.2 * scale_x)), cv2.LINE_AA)
            else:
                cv2.polylines(frame, [pts], isClosed=True, color=(160, 160, 160), thickness=max(1, int(1 * scale_x)))
        return frame

    # ==================== TEXT OVERLAY FUNCTIONS ====================
    def _get_active_text_item(self):
        if not self.text_items:
            return None
        for t in self.text_items:
            if t.get("id") == self.active_text_id:
                return t
        return self.text_items[0]

    def _sync_active_text_to_scalars(self, item: dict):
        if not item:
            return
        self.text_overlay = item.get("text", "")
        self.text_overlay_enabled = item.get("enabled", True)
        self.text_font_name = item.get("font", "Kantumruy Pro")
        self.raw_text_size = item.get("size", 12)
        self.text_size = max(0.2, self.raw_text_size / 20.0)
        self.text_color_rgb = item.get("color_rgb", (255, 255, 255))
        self.text_color_bgr = (self.text_color_rgb[2], self.text_color_rgb[1], self.text_color_rgb[0])
        self.text_bg_color_rgb = item.get("bg_color_rgb", (0, 0, 0))
        self.text_outline_color_rgb = item.get("outline_color_rgb", (0, 0, 0))
        self.text_outline_width = item.get("outline_width", 2)
        self.text_shadow_color_rgb = item.get("shadow_color_rgb", (0, 0, 0))
        self.text_shadow_offset = item.get("shadow_offset", 3)
        self.text_position = item.get("pos", (50, 80))
        self.text_anim_type = item.get("anim_type", "pop")
        self.text_anim_speed = item.get("anim_speed", 0.5)
        self.text_anim_repeat_sec = item.get("repeat_sec", 2.0)
        self.text_anim_mode = item.get("mode", "always")
        self.text_start_sec = item.get("start_sec", 0.0)
        self.text_duration_sec = item.get("duration_sec", 0.0)
        self.text_is_watermark = item.get("is_watermark", False)
        self.text_opacity = item.get("opacity", 1.0)
        self.text_pos_zone = item.get("pos_zone", "mid")
        self.text_full_video = item.get("full_video", True)
        self.text_speed_str = item.get("speed_str", "medium")

    def add_text(self, text: str = "New Text"):
        self.text_overlay_enabled = True
        t_id = f"text_{self._next_text_id}"
        t_name = f"Text {self._next_text_id}"
        self._next_text_id += 1
        
        cur = self.current_frame / max(1.0, self.fps)
        cw, ch = self._get_canvas_size()
        pos_y = min(max(20, ch - 40), 60 + len(self.text_items) * 35)
        new_text = {
            "id": t_id,
            "name": t_name,
            "text": text,
            "enabled": True,
            "font": getattr(self, 'text_font_name', 'Kantumruy Pro'),
            "size": getattr(self, 'raw_text_size', 12),
            "color_rgb": getattr(self, 'text_color_rgb', (255, 255, 255)),
            "bg_color": None,
            "bg_color_rgb": getattr(self, 'text_bg_color_rgb', None),
            "outline_color_rgb": getattr(self, 'text_outline_color_rgb', (0, 0, 0)),
            "outline_width": getattr(self, 'text_outline_width', 2),
            "shadow_color_rgb": getattr(self, 'text_shadow_color_rgb', (0, 0, 0)),
            "shadow_offset": getattr(self, 'text_shadow_offset', 3),
            "pos": (50, pos_y),
            "anim_type": getattr(self, 'text_anim_type', 'pop'),
            "anim_speed": getattr(self, 'text_anim_speed', 0.5),
            "repeat_sec": getattr(self, 'text_anim_repeat_sec', 2.0),
            "mode": getattr(self, 'text_anim_mode', 'always'),
            "start_sec": round(cur, 2),
            "duration_sec": 5.0,
            "is_watermark": getattr(self, 'text_is_watermark', False),
            "opacity": getattr(self, 'text_opacity', 1.0),
            "pos_zone": getattr(self, 'text_pos_zone', 'mid'),
            "full_video": getattr(self, 'text_full_video', True),
            "speed_str": getattr(self, 'text_speed_str', 'medium')
        }
        self.text_items.append(new_text)
        self.set_active_text(t_id)
        self.text_items_changed.emit(self.text_items, self.active_text_id)
        return t_id

    def duplicate_text(self, text_id: str = None):
        target_id = text_id or self.active_text_id
        orig = next((t for t in self.text_items if t.get("id") == target_id), None)
        if not orig:
            return None
        t_id = f"text_{self._next_text_id}"
        self._next_text_id += 1
        new_t = dict(orig)
        new_t["id"] = t_id
        new_t["name"] = f"{orig.get('name', 'Text')} Copy"
        new_t["pos"] = (orig["pos"][0] + 15, orig["pos"][1] + 15)
        self.text_items.append(new_t)
        self.set_active_text(t_id)
        self.text_items_changed.emit(self.text_items, self.active_text_id)
        return t_id

    def delete_text(self, text_id: str = None):
        target_id = text_id or self.active_text_id
        self.text_items = [t for t in self.text_items if t.get("id") != target_id]
        if self.active_text_id == target_id:
            self.active_text_id = self.text_items[0]["id"] if self.text_items else None
            if self.active_text_id:
                self._sync_active_text_to_scalars(self.text_items[0])
            else:
                self.text_overlay = ""
                self.text_overlay_enabled = False
        self._update_display()
        self.text_items_changed.emit(self.text_items, self.active_text_id)

    def set_active_text(self, text_id: str):
        self.active_text_id = text_id
        cur = next((t for t in self.text_items if t.get("id") == text_id), None)
        if cur:
            self._sync_active_text_to_scalars(cur)
        self._update_display()
        self.text_items_changed.emit(self.text_items, self.active_text_id)

    def set_text_overlay_enabled(self, enabled: bool):
        self.text_overlay_enabled = enabled
        cur = self._get_active_text_item()
        if cur:
            cur["enabled"] = enabled
        self._update_display()

    def set_text_overlay_text(self, text: str):
        self.text_overlay = text
        cur = self._get_active_text_item()
        if cur:
            cur["text"] = text
        if self.text_overlay_enabled:
            self._update_display()

    def set_text_overlay_font(self, font_name: str):
        self.text_font_name = font_name
        cur = self._get_active_text_item()
        if cur:
            cur["font"] = font_name
        if self.text_overlay_enabled:
            self._update_display()

    def set_text_overlay_color(self, color_hex: str):
        hex_color = color_hex.lstrip('#')
        if len(hex_color) == 6:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            self.text_color_bgr = (b, g, r)
            self.text_color_rgb = (r, g, b)
            cur = self._get_active_text_item()
            if cur:
                cur["color_rgb"] = (r, g, b)
            if self.text_overlay_enabled:
                self._update_display()

    def set_text_overlay_bg_color(self, color_hex: str):
        if not color_hex or color_hex == "transparent" or color_hex.lower() == "none":
            self.text_bg_color_rgb = None
            cur = self._get_active_text_item()
            if cur:
                cur["bg_color"] = None
                cur["bg_color_rgb"] = None
            if self.text_overlay_enabled:
                self._update_display()
            return
        hex_color = color_hex.lstrip('#')
        if len(hex_color) == 6:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            self.text_bg_color_rgb = (r, g, b)
            cur = self._get_active_text_item()
            if cur:
                cur["bg_color"] = color_hex
                cur["bg_color_rgb"] = (r, g, b)
            if self.text_overlay_enabled:
                self._update_display()

    def set_text_outline(self, color_hex: str, width: int):
        hex_color = color_hex.lstrip('#')
        if len(hex_color) == 6:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            self.text_outline_color_rgb = (r, g, b)
        self.text_outline_width = max(0, width)
        cur = self._get_active_text_item()
        if cur:
            cur["outline_color"] = color_hex
            cur["outline_color_rgb"] = self.text_outline_color_rgb
            cur["outline_width"] = self.text_outline_width
        if self.text_overlay_enabled:
            self._update_display()

    def set_text_shadow(self, color_hex: str, offset: int):
        hex_color = color_hex.lstrip('#')
        if len(hex_color) == 6:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            self.text_shadow_color_rgb = (r, g, b)
        self.text_shadow_offset = max(0, offset)
        cur = self._get_active_text_item()
        if cur:
            cur["shadow_color"] = color_hex
            cur["shadow_color_rgb"] = self.text_shadow_color_rgb
            cur["shadow_offset"] = self.text_shadow_offset
        if self.text_overlay_enabled:
            self._update_display()

    def set_text_overlay_size(self, size: int):
        self.raw_text_size = size
        self.text_size = max(0.5, size / 20.0)
        cur = self._get_active_text_item()
        if cur:
            cur["size"] = size
            cur["size_pt"] = size
        for it in getattr(self, 'text_items', []):
            if it.get("id") == getattr(self, 'active_text_id', None):
                it["size"] = size
                it["size_pt"] = size
        if self.text_overlay_enabled:
            self._update_display()

    def set_text_overlay_position(self, x: int, y: int):
        self.text_position = (x, y)
        cur = self._get_active_text_item()
        if cur:
            cur["pos"] = (x, y)
            cur["x"] = x
            cur["y"] = y
        if self.text_overlay_enabled:
            self._update_display()

    def set_text_position_preset(self, preset: str):
        label_w, label_h = self._get_canvas_size()
        p_low = str(preset).lower()

        cur = self._get_active_text_item()
        item_text = cur.get("text", "") if cur else getattr(self, '_current_text', 'Sample Text')
        font_family = resolve_qt_font_name(cur.get("font", "Kantumruy Pro") if cur else "Kantumruy Pro")
        font_size = cur.get("size", 12) if cur else getattr(self, 'raw_text_size', 12)

        try:
            fm = QtGui.QFontMetrics(QtGui.QFont(font_family, font_size, QtGui.QFont.Bold))
            text_w = fm.horizontalAdvance(item_text) if item_text else int(font_size * 4)
            text_h = fm.height()
        except Exception:
            text_w = int(font_size * 4)
            text_h = int(font_size * 1.2)

        center_x = max(10, int((label_w - text_w) / 2.0))
        left_x = max(15, int(label_w * 0.05))
        right_x = max(10, int(label_w * 0.95 - text_w))

        top_y = max(int(text_h + 10), int(label_h * 0.12))
        mid_y = int((label_h + text_h * 0.4) / 2.0)
        bot_y = min(int(label_h - 15), int(label_h * 0.88))

        if p_low in ["top", "top_center"]:
            x, y = center_x, top_y
        elif p_low in ["mid", "middle", "center"]:
            x, y = center_x, mid_y
        elif p_low in ["bot", "bottom", "bottom_center"]:
            x, y = center_x, bot_y
        elif p_low == "top_left":
            x, y = left_x, top_y
        elif p_low == "top_right":
            x, y = right_x, top_y
        elif p_low == "bottom_left":
            x, y = left_x, bot_y
        elif p_low == "bottom_right":
            x, y = right_x, bot_y
        elif p_low in ["left", "left_center"]:
            x, y = left_x, mid_y
        elif p_low in ["right", "right_center"]:
            x, y = right_x, mid_y
        elif p_low in ["full", "fullscreen", "full screen"]:
            x, y = center_x, mid_y
            p_low = "full"
        elif p_low in ["up_down", "vertical", "run_up_down"]:
            x, y = center_x, mid_y
            p_low = "up_down"
        else:
            return

        self.set_text_overlay_position(x, y)
        self.text_pos_zone = p_low
        if cur:
            cur["pos_zone"] = p_low
            if p_low in ["full", "up_down"] and cur.get("anim_type") != "dynamic":
                cur["anim_type"] = "dynamic"
                self.text_anim_type = "dynamic"
        self.text_moved.emit(x, y)

    def set_text_watermark_options(self, is_watermark: bool, opacity: float, zone: str, speed_str: str, full_video: bool):
        cur = self._get_active_text_item()
        if cur:
            cur["is_watermark"] = is_watermark
            cur["opacity"] = opacity
            cur["pos_zone"] = zone
            cur["speed_str"] = speed_str
            cur["full_video"] = full_video
        self.text_is_watermark = is_watermark
        self.text_opacity = opacity
        self.text_pos_zone = zone
        self.text_speed_str = speed_str
        self.text_full_video = full_video
        if self.text_overlay_enabled:
            self._update_display()

    @staticmethod
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

    def set_text_overlay_animation(self, anim_type: str, speed: float = 0.5, mode: str = "always", start_sec: float = 0.0, duration_sec: float = 0.0, repeat_sec: float = 2.0):
        self.text_anim_type = anim_type
        self.text_anim_speed = max(0.1, speed)
        self.text_anim_mode = mode
        self.text_start_sec = max(0.0, start_sec)
        self.text_duration_sec = max(0.0, duration_sec)
        self.text_anim_repeat_sec = max(0.0, repeat_sec)
        cur = self._get_active_text_item()
        if cur:
            cur["anim_type"] = anim_type
            cur["anim_speed"] = self.text_anim_speed
            cur["mode"] = mode
            cur["start_sec"] = self.text_start_sec
            cur["duration_sec"] = self.text_duration_sec
            cur["repeat_sec"] = self.text_anim_repeat_sec
        if self.text_overlay_enabled:
            self._update_display()

    def preview_text_animation(self):
        """Play a smooth test preview of the text overlay animation."""
        if not self.text_overlay_enabled:
            self.text_overlay_enabled = True
        
        if getattr(self, 'text_anim_preview_timer', None) and self.text_anim_preview_timer.isActive():
            self.text_anim_preview_timer.stop()
            
        import time
        self._anim_test_start = time.time()
        repeat_sec = getattr(self, 'text_anim_repeat_sec', 2.0)
        anim_type = getattr(self, 'text_anim_type', 'none')
        if anim_type in ["dynamic", "dynamic_watermark", "watermark"]:
            self._anim_test_dur = 6.0
        elif repeat_sec > 0.0:
            self._anim_test_dur = min(4.5, max(self.text_anim_speed + 0.5, repeat_sec + self.text_anim_speed + 0.3))
        else:
            self._anim_test_dur = max(0.8, self.text_anim_speed * 1.5)
        
        self.text_anim_preview_timer = QtCore.QTimer(self)
        def _tick():
            now = time.time() - self._anim_test_start
            if now > self._anim_test_dur:
                self.text_anim_preview_timer.stop()
                self.text_anim_preview_sec = None
                self._update_display()
            else:
                self.text_anim_preview_sec = now
                self._update_display()
        self.text_anim_preview_timer.timeout.connect(_tick)
        self.text_anim_preview_timer.start(25)

    def _apply_text_overlay(self, frame, cur_sec: float = 0.0):
        """Apply text overlay items to frame using Qt QPainter with Outline, Shadow, and Bounce animations"""
        if not self.text_overlay_enabled:
            return frame
            
        if getattr(self, 'text_anim_preview_sec', None) is not None:
            cur_sec = self.text_anim_preview_sec

        items_to_render = self.text_items if self.text_items else [{
            "id": "single",
            "name": "Text",
            "text": self.text_overlay,
            "enabled": self.text_overlay_enabled,
            "font": self.text_font_name,
            "size": self.raw_text_size,
            "color_rgb": self.text_color_rgb,
            "bg_color_rgb": self.text_bg_color_rgb,
            "outline_color_rgb": getattr(self, 'text_outline_color_rgb', (0, 0, 0)),
            "outline_width": getattr(self, 'text_outline_width', 2),
            "shadow_color_rgb": getattr(self, 'text_shadow_color_rgb', (0, 0, 0)),
            "shadow_offset": getattr(self, 'text_shadow_offset', 3),
            "pos": self.text_position,
            "anim_type": self.text_anim_type,
            "anim_speed": self.text_anim_speed,
            "repeat_sec": self.text_anim_repeat_sec,
            "mode": self.text_anim_mode,
            "start_sec": self.text_start_sec,
            "duration_sec": self.text_duration_sec
        }]

        try:
            ensure_qt_fonts()
            h, w, ch = frame.shape
            label_w, label_h = self._get_canvas_size()
            scale_x = w / float(label_w)
            scale_y = h / float(label_h)

            for t_item in items_to_render:
                if not t_item.get("enabled", True):
                    continue
                item_text = t_item.get("text", "")
                if not item_text:
                    continue

                t_id = t_item.get("id")
                is_active = (t_id == self.active_text_id)
                is_dragging = (getattr(self, '_active_target', None) == "text" and is_active)

                anim_type = t_item.get("anim_type", "none")
                anim_speed = max(0.1, t_item.get("anim_speed", 0.5))
                anim_mode = t_item.get("mode", "always")
                repeat_sec = max(0.0, t_item.get("repeat_sec", 2.0))
                start_offset = t_item.get("start_sec", 0.0)
                custom_dur = t_item.get("duration_sec", 0.0)

                full_vid = t_item.get("full_video", True)
                if not full_vid and anim_mode != "always":
                    max_duration = 0.0
                    if anim_mode == "first_5s":
                        max_duration = 5.0
                    elif anim_mode == "first_10s":
                        max_duration = 10.0
                    elif anim_mode == "custom" or custom_dur > 0.0:
                        max_duration = start_offset + custom_dur
                elif not full_vid and custom_dur > 0.0:
                    max_duration = start_offset + custom_dur
                else:
                    max_duration = 0.0 # Full video!

                is_re_trigger = False
                if is_dragging or anim_type in ["none", "dynamic", "dynamic_watermark", "watermark"]:
                    p = 1.0
                    outro_factor = 1.0
                else:
                    if max_duration > 0.0:
                        if cur_sec < start_offset or cur_sec > max_duration:
                            t_item["_screen_rect"] = None
                            continue
                        elif cur_sec > max_duration - anim_speed:
                            outro_factor = max(0.0, min(1.0, (max_duration - cur_sec) / anim_speed))
                        else:
                            outro_factor = 1.0
                        rel_sec = max(0.0, cur_sec - start_offset)
                    else:
                        outro_factor = 1.0
                        rel_sec = max(0.0, cur_sec - start_offset)

                    if repeat_sec > 0.0:
                        cycle_sec = rel_sec % repeat_sec
                        is_re_trigger = (rel_sec >= repeat_sec)
                        p = max(0.0, min(1.0, cycle_sec / anim_speed))
                    else:
                        p = max(0.0, min(1.0, rel_sec / anim_speed))

                scale = 1.0
                dx = 0
                dy = 0
                alpha_mult = 1.0

                if anim_type == "fade":
                    alpha_mult = ((0.5 + 0.5 * p) if is_re_trigger else p) * outro_factor
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

                opacity_factor = float(t_item.get("opacity", 1.0))
                if t_item.get("is_watermark") and "opacity" not in t_item:
                    opacity_factor = 0.6
                alpha_mult = alpha_mult * opacity_factor

                if alpha_mult <= 0.001 and not is_dragging:
                    continue

                font_size_pt = max(4, int(t_item.get("size", 12) * scale_y))
                font_family = resolve_qt_font_name(t_item.get("font", "Kantumruy Pro"))
                font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
                fm = QtGui.QFontMetrics(font)
                text_w = fm.horizontalAdvance(item_text)
                text_h = fm.height()

                if anim_type in ["dynamic", "dynamic_watermark", "watermark"] and not is_dragging:
                    zone = t_item.get("pos_zone", getattr(self, 'text_pos_zone', 'mid'))
                    sp = t_item.get("speed_str", getattr(self, 'text_speed_str', 'medium'))
                    x, y = self._get_dynamic_text_pos(cur_sec, zone, sp, w, h, text_w, text_h, scale_y)
                else:
                    item_pos = t_item.get("pos", (50, 80))
                    x = int(item_pos[0] * scale_x)
                    y = int(item_pos[1] * scale_y)
                    x = max(10, min(w - 30, x))
                    y = max(20, min(h - 20, y))

                qimg = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32_Premultiplied)
                qimg.fill(Qt.transparent)

                painter = QtGui.QPainter(qimg)
                painter.setRenderHint(QtGui.QPainter.Antialiasing)
                painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
                painter.setFont(font)

                pad_x, pad_y = 14, 10
                bg_x = max(0, x - pad_x)
                bg_y = max(0, y - fm.ascent() - pad_y)
                bg_w = min(w - bg_x, text_w + pad_x * 2)
                bg_h = min(h - bg_y, text_h + pad_y * 2)

                screen_bg_x = int(bg_x / scale_x)
                screen_bg_y = int(bg_y / scale_y)
                screen_bg_w = int(bg_w / scale_x)
                screen_bg_h = int(bg_h / scale_y)
                item_screen_rect = QtCore.QRect(screen_bg_x, screen_bg_y, screen_bg_w, screen_bg_h)
                t_item["_screen_rect"] = item_screen_rect
                if is_active:
                    self.text_rect = item_screen_rect

                box_cx = bg_x + bg_w / 2.0
                box_cy = bg_y + bg_h / 2.0
                painter.save()
                painter.translate(box_cx + dx * scale_x, box_cy + dy * scale_y)
                if scale != 1.0:
                    painter.scale(scale, scale)
                painter.translate(-box_cx, -box_cy)

                # Background box
                bg_c = t_item.get("bg_color_rgb", None)
                if bg_c is not None and bg_c != "transparent":
                    bg_alpha = int(180 * alpha_mult)
                    painter.setBrush(QtGui.QBrush(QtGui.QColor(bg_c[0], bg_c[1], bg_c[2], bg_alpha)))
                    painter.setPen(Qt.NoPen)
                    painter.drawRoundedRect(QtCore.QRect(bg_x, bg_y, bg_w, bg_h), 10, 10)

                path = QtGui.QPainterPath()
                path.addText(x, y, font, item_text)

                # 1. Drop Shadow
                shadow_off = t_item.get("shadow_offset", 3)
                if shadow_off > 0:
                    shadow_c = t_item.get("shadow_color_rgb", (0, 0, 0))
                    shadow_alpha = int(160 * alpha_mult)
                    shadow_path = path.translated(shadow_off * scale_x, shadow_off * scale_y)
                    painter.setBrush(QtGui.QBrush(QtGui.QColor(shadow_c[0], shadow_c[1], shadow_c[2], shadow_alpha)))
                    painter.setPen(Qt.NoPen)
                    painter.drawPath(shadow_path)

                # 2. Outline Stroke
                outline_w = t_item.get("outline_width", 2)
                if outline_w > 0:
                    stroke_w = max(1, int(outline_w * scale_x))
                    stroke_alpha = int(255 * alpha_mult)
                    outline_c = t_item.get("outline_color_rgb", (0, 0, 0))
                    painter.setPen(QtGui.QPen(QtGui.QColor(outline_c[0], outline_c[1], outline_c[2], stroke_alpha), stroke_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPath(path)

                # 3. Text Fill
                rgb_color = t_item.get("color_rgb", (255, 255, 255))
                text_alpha = int(255 * alpha_mult)
                painter.setBrush(QtGui.QBrush(QtGui.QColor(rgb_color[0], rgb_color[1], rgb_color[2], text_alpha)))
                painter.setPen(Qt.NoPen)
                painter.drawPath(path)

                painter.restore()
                painter.end()

                qimg_rgb = qimg.convertToFormat(QtGui.QImage.Format_RGB888)
                ptr = qimg_rgb.bits()
                if hasattr(ptr, 'setsize'): ptr.setsize(h * w * 3)
                arr = np.frombuffer(ptr, np.uint8).reshape((h, w, 3))
                text_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

                mask = (arr > 0).any(axis=2)
                frame[mask] = text_bgr[mask]

                # Draw selection border and corner handles on active text when not playing
                if is_active and not self._is_playing:
                    cv2.rectangle(frame, (bg_x, bg_y), (bg_x + bg_w, bg_y + bg_h), (255, 200, 0), max(1, int(1.5 * scale_x)))
                    hs = max(3, int(4 * scale_x))
                    corners = [(bg_x, bg_y), (bg_x + bg_w, bg_y), (bg_x + bg_w, bg_y + bg_h), (bg_x, bg_y + bg_h)]
                    for cx, cy in corners:
                        cv2.rectangle(frame, (cx - hs, cy - hs), (cx + hs, cy + hs), (255, 255, 255), -1)
                        cv2.rectangle(frame, (cx - hs, cy - hs), (cx + hs, cy + hs), (0, 200, 255), 1)

            return frame
        except Exception as e:
            print(f"Error applying Qt text overlay: {e}")
            return frame

    # ==================== LOGO OVERLAY FUNCTIONS ====================
    def set_logo_enabled(self, enabled: bool):
        self.logo_enabled = enabled
        if enabled and (self.logo_image is None) and self.logo_path:
            self._load_logo()
        self._update_display()

    def set_logo_path(self, path: str):
        self.logo_path = path.strip(' "\'') if path else ""
        self._load_logo()
        self._update_display()

    def set_logo_position(self, x: int, y: int):
        self.logo_x = x
        self.logo_y = y
        self._update_display()

    def set_logo_size(self, width: int, height: int):
        self.logo_width = max(10, width)
        self.logo_height = max(10, height)
        self._update_display()

    def set_logo_remove_green(self, remove: bool):
        self.logo_remove_green = remove
        if self.logo_path:
            self._load_logo()
            self._update_display()

    def _load_logo(self):
        if not self.logo_path:
            self.logo_image = None
            return
            
        target_path = self.logo_path.strip(' "\'')
        base_name = os.path.basename(target_path)
        from utils.file_utils import get_temp_path
        candidates = [
            target_path,
            get_temp_path(f"safe_logo_{base_name}"),
            get_temp_path(base_name),
            os.path.join(os.getcwd(), base_name)
        ]
        
        img = None
        loaded_path = None
        for p in candidates:
            if not p or not os.path.exists(p):
                continue
            try:
                with open(p, 'rb') as f:
                    bytes_data = np.frombuffer(f.read(), np.uint8)
                    img = cv2.imdecode(bytes_data, cv2.IMREAD_UNCHANGED)
                if img is not None:
                    loaded_path = p
                    break
            except Exception as e:
                logger.warning(f"Could not read logo from {p}: {e}")
                continue
                
        if img is None:
            self.logo_image = None
            logger.error(f"❌ Failed to load logo image from all candidates for {self.logo_path}")
            return
            
        self.logo_path = loaded_path
        if self.logo_remove_green:
            hsv = cv2.cvtColor(img if img.shape[2] == 3 else img[:, :, :3], cv2.COLOR_BGR2HSV)
            lower_green = np.array([35, 40, 40])
            upper_green = np.array([85, 255, 255])
            mask = cv2.inRange(hsv, lower_green, upper_green)
            if img.shape[2] == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            img[mask > 0, 3] = 0
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        self.logo_image = img

    def set_logo_timing(self, start_sec: float, duration_sec: float):
        self.logo_start_sec = max(0.0, start_sec)
        self.logo_duration_sec = max(0.0, duration_sec)
        self._update_display()

    def set_logo_full_video(self, full_video: bool):
        self.logo_full_video = full_video
        self._update_display()

    def _apply_logo_overlay(self, frame, cur_sec: float = 0.0):
        if not self.logo_enabled:
            return frame
        if self.logo_image is None and self.logo_path:
            self._load_logo()
        if self.logo_image is None:
            return frame
        
        # Check timeline duration if specified
        is_full = getattr(self, 'logo_full_video', True)
        l_dur = getattr(self, 'logo_duration_sec', 0.0)
        l_st = getattr(self, 'logo_start_sec', 0.0)
        is_dragging = (getattr(self, '_active_target', None) == "logo")
        if not is_full and l_dur > 0.0 and not is_dragging:
            if cur_sec < l_st or cur_sec > (l_st + l_dur):
                return frame
        
        h, w, ch = frame.shape
        label_w, label_h = self._get_canvas_size()
        scale_x = w / float(label_w)
        scale_y = h / float(label_h)
        
        x = int(self.logo_x * scale_x)
        y = int(self.logo_y * scale_y)
        target_w = max(10, int(self.logo_width * scale_x))
        target_h = max(10, int(self.logo_height * scale_y))
        
        # Visible bounding box within frame
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(w, x + target_w)
        y2 = min(h, y + target_h)
        
        if x1 >= x2 or y1 >= y2:
            return frame
            
        orig_h, orig_w = self.logo_image.shape[:2]
        if target_w < orig_w and target_h < orig_h:
            logo_resized = cv2.resize(self.logo_image, (target_w, target_h), interpolation=cv2.INTER_AREA)
        else:
            logo_resized = cv2.resize(self.logo_image, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
            
        crop_x1 = x1 - x
        crop_y1 = y1 - y
        crop_x2 = crop_x1 + (x2 - x1)
        crop_y2 = crop_y1 + (y2 - y1)
        sub_logo = logo_resized[crop_y1:crop_y2, crop_x1:crop_x2]
        
        if sub_logo.shape[2] == 4:
            alpha = (sub_logo[:, :, 3:4].astype(np.float32) / 255.0)
            logo_rgb = sub_logo[:, :, :3].astype(np.float32)
            roi_f = frame[y1:y2, x1:x2].astype(np.float32)
            frame[y1:y2, x1:x2] = np.clip(alpha * logo_rgb + (1.0 - alpha) * roi_f + 0.5, 0, 255).astype(np.uint8)
        else:
            frame[y1:y2, x1:x2] = sub_logo[:, :, :3]
            
        # Draw bounding outline and resize handles for interactive editing
        cv2.rectangle(frame, (x, y), (x + target_w, y + target_h), (0, 230, 118), max(1, int(2 * scale_x)))
        hs = max(3, int(5 * scale_x))
        for pt in [(x, y), (x + target_w, y), (x, y + target_h), (x + target_w, y + target_h)]:
            cv2.rectangle(frame, (pt[0]-hs, pt[1]-hs), (pt[0]+hs, pt[1]+hs), (255, 255, 255), -1)
            cv2.rectangle(frame, (pt[0]-hs, pt[1]-hs), (pt[0]+hs, pt[1]+hs), (0, 230, 118), 1)
            
        return frame

    # ==================== BURN SUBTITLE FUNCTIONS ====================
    def set_burn_subtitle_enabled(self, enabled: bool):
        self.burn_subtitle_enabled = enabled
        self._update_display()

    def set_burn_subtitle_config(self, enabled: bool, font_name: str, font_size: int, color_hex: str, bg_opacity: float, anim_type: str = "pop_bounce", anim_dur: float = 0.35, template: dict = None):
        self.burn_subtitle_enabled = enabled
        self.burn_subtitle_font_name = font_name
        self.burn_subtitle_font_size = font_size
        self.burn_subtitle_bg_opacity = bg_opacity
        self.burn_sub_anim_type = anim_type
        self.burn_sub_anim_dur = anim_dur
        if template is not None:
            self.burn_subtitle_template = dict(template)
        hex_c = color_hex.lstrip('#')
        if len(hex_c) == 6:
            r = int(hex_c[0:2], 16)
            g = int(hex_c[2:4], 16)
            b = int(hex_c[4:6], 16)
            self.burn_subtitle_color_rgb = (r, g, b)
        if hasattr(self, '_sub_render_cache'):
            self._sub_render_cache.clear()
        self._update_display()

    def test_burn_subtitle_animation(self):
        """Triggers a 60 FPS live preview playback of the subtitle animation."""
        self._test_anim_active = True
        self._test_anim_start_time = time.time()
        if hasattr(self, '_test_anim_timer') and self._test_anim_timer.isActive():
            self._test_anim_timer.stop()
        self._test_anim_timer = QtCore.QTimer(self)
        self._test_anim_timer.setInterval(16)
        
        def _on_tick():
            if not getattr(self, '_test_anim_active', False):
                if hasattr(self, '_test_anim_timer'):
                    self._test_anim_timer.stop()
                return
            t_elapsed = time.time() - getattr(self, '_test_anim_start_time', 0.0)
            anim_dur = getattr(self, 'burn_sub_anim_dur', 0.35)
            if t_elapsed >= anim_dur + 0.15:
                self._test_anim_active = False
                if hasattr(self, '_test_anim_timer'):
                    self._test_anim_timer.stop()
            self._update_display()
        
        self._test_anim_timer.timeout.connect(_on_tick)
        self._test_anim_timer.start()

    def set_burn_subtitle_position(self, y_percent: int, x_percent: int = 50):
        self.burn_sub_y_ratio = max(0.05, min(0.95, y_percent / 100.0))
        self.burn_sub_x_ratio = max(0.05, min(0.95, x_percent / 100.0))
        if hasattr(self, '_sub_render_cache'):
            self._sub_render_cache.clear()
        self._update_display()

    def reset_burn_subtitle_position(self):
        self.burn_sub_y_ratio = 0.85
        self.burn_sub_x_ratio = 0.50
        self.burn_subtitle_moved.emit(85, 50)
        if hasattr(self, '_sub_render_cache'):
            self._sub_render_cache.clear()
        self._update_display()

    def _apply_burn_subtitle(self, frame, cur_sec: float):
        """Render active Khmer subtitle using Qt QPainter with CapCut styling and ultra-smooth caching."""
        if not self.burn_subtitle_enabled or not self._timeline_segments:
            return frame
        
        ensure_qt_fonts()
        
        active_seg = None
        for seg in self._timeline_segments:
            st = seg.get("start", 0.0)
            et = seg.get("end", 0.0)
            if st <= cur_sec <= et:
                active_seg = seg
                break
        
        is_preview_placeholder = False
        if not active_seg:
            if not self._is_playing and self._timeline_segments:
                sample_seg = self._timeline_segments[0]
                raw_sub = sample_seg.get("khmer_text") or sample_seg.get("original_text") or sample_seg.get("text", "")
                if not raw_sub:
                    raw_sub = "💬 [ចំណងជើងរង - អូសទាញទីតាំងនៅទីនេះ]"
                is_preview_placeholder = True
            else:
                self.burn_sub_rect = None
                return frame
        else:
            raw_sub = active_seg.get("khmer_text") or active_seg.get("original_text") or active_seg.get("text", "")

        # Clean any leading speaker tags
        from services.khmer_frontend import strip_speaker_tags
        sub_text = strip_speaker_tags(raw_sub)
        if not sub_text:
            return frame
        
        try:
            h, w, ch = frame.shape
            label_w, label_h = self._get_canvas_size()
            scale_x_disp = w / float(label_w)
            scale_y_disp = h / float(label_h)
            
            # Responsive display scaling: font size matches spinbox value on screen
            base_font_size = getattr(self, 'burn_subtitle_font_size', 22)
            font_size_pt = max(8, min(150, int(round(base_font_size * scale_y_disp))))
            font_family = resolve_qt_font_name(getattr(self, 'burn_subtitle_font_name', 'Kantumruy Pro'))
            rgb_color = getattr(self, 'burn_subtitle_color_rgb', (255, 255, 255))
            bg_opacity = getattr(self, 'burn_subtitle_bg_opacity', 0.0)
            x_ratio = getattr(self, 'burn_sub_x_ratio', 0.50)
            y_ratio = getattr(self, 'burn_sub_y_ratio', 0.85)

            tpl = getattr(self, 'burn_subtitle_template', {}) or {}
            stroke_color_hex = tpl.get("stroke_color", "#000000")
            raw_stroke = tpl.get("stroke_width", max(2, base_font_size // 8))
            stroke_w = max(1, int(round(raw_stroke * scale_y_disp)))
            has_3d = bool(tpl.get("has_3d", False))
            shadow_color_hex = tpl.get("shadow_color", "#000000")
            glow_color_hex = tpl.get("glow_color", None)

            cache_key = (
                sub_text, font_family, font_size_pt, rgb_color,
                stroke_color_hex, stroke_w, has_3d, shadow_color_hex, glow_color_hex,
                bg_opacity, w, h, x_ratio, y_ratio
            )

            if not hasattr(self, '_sub_render_cache'):
                self._sub_render_cache = {}

            cached = self._sub_render_cache.get(cache_key)
            if cached is not None:
                text_bgr, alpha, sub_box_w, sub_box_h, bg_x1, bg_y1, bg_x2, bg_y2, screen_sub_rect = cached
                self.burn_sub_rect = screen_sub_rect
            else:
                font = QtGui.QFont(font_family, font_size_pt, QtGui.QFont.Bold)
                fm = QtGui.QFontMetrics(font)
                
                max_line_w = max(100, int(w * 0.85))
                from services.khmer_frontend import wrap_khmer_subtitle_lines
                lines = wrap_khmer_subtitle_lines(sub_text, fm, max_line_w)
                if not lines:
                    lines = [sub_text]
                
                line_height = fm.height() + 6
                total_h = len(lines) * line_height
                line_widths = [fm.horizontalAdvance(l) for l in lines]
                max_w = max(line_widths) if line_widths else 100
                
                center_x = int(w * x_ratio)
                center_y = int(h * y_ratio)
                
                # Expand box margin to accommodate glow and 3D extrusion shadows
                margin = max(16, stroke_w + 14)
                sub_box_w = max_w + margin * 2
                sub_box_h = total_h + margin * 2
                
                bg_x1 = max(10, min(w - sub_box_w - 10, center_x - sub_box_w // 2))
                bg_x2 = bg_x1 + sub_box_w
                bg_y1 = max(10, min(h - sub_box_h - 10, center_y - sub_box_h // 2))
                bg_y2 = bg_y1 + sub_box_h
                
                screen_sub_x = int(bg_x1 / scale_x_disp)
                screen_sub_y = int(bg_y1 / scale_y_disp)
                screen_sub_w = int((bg_x2 - bg_x1) / scale_x_disp)
                screen_sub_h = int((bg_y2 - bg_y1) / scale_y_disp)
                screen_sub_rect = QtCore.QRect(screen_sub_x, screen_sub_y, screen_sub_w, screen_sub_h)
                self.burn_sub_rect = screen_sub_rect
                
                qimg = QtGui.QImage(sub_box_w, sub_box_h, QtGui.QImage.Format_ARGB32_Premultiplied)
                qimg.fill(Qt.transparent)
                
                painter = QtGui.QPainter(qimg)
                painter.setRenderHint(QtGui.QPainter.Antialiasing)
                painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
                painter.setFont(font)
                
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

                # Layer 1: Neon Glow (if template specifies glow_color)
                if glow_color_hex:
                    gc = QtGui.QColor(glow_color_hex)
                    for gw_add, ga in [(12, 30), (8, 65), (4, 120)]:
                        g_pen = QtGui.QPen(QtGui.QColor(gc.red(), gc.green(), gc.blue(), ga), stroke_w + gw_add, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                        painter.setPen(g_pen)
                        painter.setBrush(Qt.NoBrush)
                        for p in paths:
                            painter.drawPath(p)

                # Layer 2: 3D Extrusion Shadow (if template specifies 3D)
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
                fill_color = QtGui.QColor(rgb_color[0], rgb_color[1], rgb_color[2])
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

                # Keep cache bounded to 50 items
                if len(self._sub_render_cache) > 50:
                    self._sub_render_cache.clear()
                self._sub_render_cache[cache_key] = (
                    text_bgr, alpha, sub_box_w, sub_box_h, bg_x1, bg_y1, bg_x2, bg_y2, screen_sub_rect
                )

            active_st = float(active_seg.get("start", 0.0)) if (active_seg and not is_preview_placeholder) else 0.0
            active_dur = max(0.1, float(active_seg.get("end", 0.0)) - active_st) if (active_seg and not is_preview_placeholder) else 1.0
            anim_type = getattr(self, 'burn_sub_anim_type', 'pop_bounce')
            anim_dur = getattr(self, 'burn_sub_anim_dur', 0.35)

            if getattr(self, '_test_anim_active', False):
                t_elapsed = time.time() - getattr(self, '_test_anim_start_time', 0.0)
                cur_calc_sec = active_st + t_elapsed
            else:
                cur_calc_sec = cur_sec

            frame = apply_subtitle_animation_effect(
                frame=frame,
                text_bgr=text_bgr,
                alpha=alpha,
                sx=bg_x1,
                sy=bg_y1,
                sw=sub_box_w,
                sh=sub_box_h,
                anim_type=anim_type,
                anim_dur=anim_dur,
                cur_sec=cur_calc_sec,
                seg_start=active_st,
                seg_dur=active_dur
            )
            
            # Visual feedback when user is dragging or hovering subtitle
            if getattr(self, '_active_target', None) == "burn_subtitle":
                cv2.rectangle(frame, (bg_x1 - 2, bg_y1 - 2), (bg_x2 + 2, bg_y2 + 2), (248, 189, 56), 2, cv2.LINE_AA)
            
            return frame
        except Exception as e:
            logger.warning(f"Error applying burn subtitle: {e}")
            return frame

    def get_effects_config(self) -> dict:
        cw, ch = self._get_canvas_size()
        preview_w = max(1, cw)
        preview_h = max(1, ch)
        
        has_text = bool(self.text_overlay and self.text_overlay.strip()) or any(bool(t.get("text", "").strip()) for t in self.text_items)
        
        # Robust Logo Path Resolution
        resolved_logo = self.logo_path
        if resolved_logo and not os.path.exists(str(resolved_logo)):
            from utils.file_utils import get_temp_path
            base_n = os.path.basename(str(resolved_logo))
            safe_c = get_temp_path(f"safe_logo_{base_n}")
            if os.path.exists(safe_c):
                resolved_logo = safe_c
            elif os.path.exists(base_n):
                resolved_logo = os.path.abspath(base_n)
        has_logo = bool(resolved_logo and os.path.exists(str(resolved_logo)))
        
        # Robust Blur List
        export_blurs = list(self.blur_items) if self.blur_items else []
        if self.blur_enabled and not export_blurs and not self.blur_rect:
            export_blurs = [{
                "id": "blur_1",
                "name": "Blur 1",
                "x": max(10, int(preview_w * 0.1)),
                "y": max(10, int(preview_h * 0.1)),
                "width": max(60, int(preview_w * 0.4)),
                "height": max(40, int(preview_h * 0.2)),
                "rotation": 0.0,
                "intensity": getattr(self, 'blur_intensity', 35),
                "type": "gaussian",
                "auto_speech": getattr(self, 'blur_auto_speech', False),
                "full_video": True,
                "start_sec": 0.0,
                "end_sec": 999999.0
            }]
        else:
            for b in export_blurs:
                if b.get("full_video", True):
                    b["full_video"] = True
                    b["start_sec"] = 0.0
                    b["end_sec"] = 999999.0
        
        return {
            "preview_size": (preview_w, preview_h),
            "blur": {
                "enabled": self.blur_enabled and (bool(export_blurs) or bool(self.blur_rect)),
                "intensity": self.blur_intensity,
                "rect": self.blur_rect,
                "blurs": export_blurs,
                "active_blur_id": self.active_blur_id,
                "auto_speech": getattr(self, 'blur_auto_speech', False),
                "full_video": True
            },
            "text_overlay": {
                "enabled": self.text_overlay_enabled or has_text or any(t.get("enabled", False) for t in self.text_items),
                "text": self.text_overlay,
                "font_name": getattr(self, 'text_font_name', 'Kantumruy Pro'),
                "color_rgb": getattr(self, 'text_color_rgb', (255, 255, 255)),
                "bg_color_rgb": getattr(self, 'text_bg_color_rgb', (0, 0, 0)),
                "outline_color_rgb": getattr(self, 'text_outline_color_rgb', (0, 0, 0)),
                "outline_width": getattr(self, 'text_outline_width', 2),
                "shadow_color_rgb": getattr(self, 'text_shadow_color_rgb', (0, 0, 0)),
                "shadow_offset": getattr(self, 'text_shadow_offset', 3),
                "size_pt": getattr(self, 'raw_text_size', 12),
                "position": self.text_position,
                "animation": {
                    "type": getattr(self, 'text_anim_type', 'none'),
                    "speed": getattr(self, 'text_anim_speed', 0.5),
                    "mode": getattr(self, 'text_anim_mode', 'always'),
                    "start_sec": getattr(self, 'text_start_sec', 0.0),
                    "duration_sec": getattr(self, 'text_duration_sec', 0.0),
                    "repeat_sec": getattr(self, 'text_anim_repeat_sec', 2.0),
                },
                "items": self.text_items,
                "active_id": self.active_text_id
            },
            "logo": {
                "enabled": self.logo_enabled and has_logo,
                "path": resolved_logo,
                "x": self.logo_x,
                "y": self.logo_y,
                "width": self.logo_width,
                "height": self.logo_height,
                "remove_green": self.logo_remove_green,
                "full_video": getattr(self, 'logo_full_video', True),
                "start_sec": 0.0 if getattr(self, 'logo_full_video', True) else getattr(self, 'logo_start_sec', 0.0),
                "duration_sec": 0.0 if getattr(self, 'logo_full_video', True) else getattr(self, 'logo_duration_sec', 0.0)
            },
            "burn_subtitle": {
                "enabled": self.burn_subtitle_enabled,
                "font_name": self.burn_subtitle_font_name,
                "font_size": self.burn_subtitle_font_size,
                "color_rgb": self.burn_subtitle_color_rgb,
                "bg_opacity": self.burn_subtitle_bg_opacity,
                "x_ratio": getattr(self, 'burn_sub_x_ratio', 0.50),
                "y_ratio": getattr(self, 'burn_sub_y_ratio', 0.85),
                "anim_type": getattr(self, 'burn_sub_anim_type', 'pop_bounce'),
                "anim_dur": getattr(self, 'burn_sub_anim_dur', 0.35),
                "template": getattr(self, 'burn_subtitle_template', None)
            },
            "segments": self._timeline_segments
        }

    def _apply_all_effects(self, frame):
        cur_sec = getattr(self, '_current_global_sec', None)
        if cur_sec is None:
            cur_sec = self.current_frame / max(1.0, self.fps)
        
        if self.blur_enabled:
            frame = self._apply_blur_to_frame(frame, cur_sec)
            frame = self._draw_blur_rectangle(frame, cur_sec)
        
        if self.text_overlay_enabled:
            frame = self._apply_text_overlay(frame, cur_sec)
        
        if self.logo_enabled:
            frame = self._apply_logo_overlay(frame, cur_sec)
            
        if self.burn_subtitle_enabled:
            frame = self._apply_burn_subtitle(frame, cur_sec)
        
        return frame

    def _update_display(self, force_reload=False):
        if not self.cap or not self.cap.isOpened():
            return
        if not force_reload and not self._is_playing and self._cached_clean_frame is not None:
            frame = self._cached_clean_frame.copy()
            frame = self._apply_all_effects(frame)
            self._show_frame_mat(frame)
        else:
            self._display_frame_at(self.current_frame)

    def _toggle_play(self):
        if not self.video_clips and (not self.video_path or not self.cap or not self.cap.isOpened()):
            return

        # Stop any standalone audition process if active
        if hasattr(self, '_audition_proc') and self._audition_proc and self._audition_proc.poll() is None:
            try:
                self._audition_proc.terminate()
            except Exception:
                pass

        if self._is_playing:
            self.play_timer.stop()
            self.player.pause()
            self._is_playing = False
            self._pending_play = False
            self.play_btn.setText("▶")
            logger.info("⏸ [VideoPreview] Playback paused.")
        else:
            if hasattr(self.player, 'source') and self.player.source().isEmpty():
                self._load_audio_for_player()

            import time
            global_sec = getattr(self, '_current_global_sec', 0.0)
            tot_dur = sum(c.get("duration", 0.0) for c in self.video_clips) if self.video_clips else (self.total_frames / max(1.0, self.fps))
            if tot_dur > 0 and global_sec >= tot_dur - 0.05:
                global_sec = 0.0
                self._current_global_sec = 0.0

            clip_idx, clip, local_sec = self.resolve_clip_at_time(global_sec)
            if clip_idx is not None:
                self.switch_to_clip(clip_idx)
                local_frame = int(local_sec * max(1.0, self.fps))
                if self.cap and self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, local_frame))
                    self.current_frame = local_frame

            self._playback_start_wall_time = time.monotonic()
            self._playback_start_sec = global_sec

            # Position timeline audio player to global timeline time
            pos_ms = int(global_sec * 1000)
            try:
                self.player.setPosition(pos_ms)
            except Exception:
                pass

            interval = max(10, int(1000.0 / max(10.0, self.fps)))
            self.play_timer.setTimerType(QtCore.Qt.PreciseTimer)
            self.play_timer.start(interval)

            try:
                if hasattr(self, 'audio_output'):
                    if hasattr(self.audio_output, 'setMuted'):
                        self.audio_output.setMuted(False)
                    if hasattr(self.audio_output, 'setVolume'):
                        vol = self.vol_slider.value() / 100.0 if hasattr(self, 'vol_slider') else 1.0
                        self.audio_output.setVolume(vol)
                self.player.play()
                if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                    self._pending_play = True
                logger.info(f"▶ [VideoPreview] Playback started at {global_sec:.2f}s (Volume: {self.audio_output.volume():.0%})")
            except Exception as e:
                logger.warning(f"Player play exception: {e}")
            self._is_playing = True
            self.play_btn.setText("⏸")

    def play(self):
        """Start playback if not already playing."""
        if not self._is_playing:
            self._toggle_play()

    def pause(self):
        """Pause playback if currently playing."""
        if self._is_playing:
            self._toggle_play()

    def _stop_video(self):
        if hasattr(self, '_dual_decoder') and self._dual_decoder:
            try:
                self._dual_decoder.close()
            except Exception:
                pass
            self._dual_decoder = None
        if hasattr(self, '_audition_proc') and self._audition_proc and self._audition_proc.poll() is None:
            try:
                self._audition_proc.terminate()
            except Exception:
                pass
        self.play_timer.stop()
        self.player.stop()
        try:
            self.player.setPosition(0)
        except Exception:
            pass
        self._is_playing = False
        self.play_btn.setText("▶")
        self._current_global_sec = 0.0
        self.seek_to_time_sec(0.0, immediate=True)
        self.playhead_moved.emit(0.0)

    def _try_render_transition_frame(self, target_global_sec: float):
        """Phase 3 live OpenCV transition rendering during preview playback and scrubbing."""
        transitions = getattr(self, 'transitions', [])
        if not transitions or not self.video_clips or len(self.video_clips) < 2:
            return None

        from core.transition_engine import resolve_transition_at_time
        from core.transition_renderer import render_transition_frame
        from core.dual_decoder_manager import DualDecoderManager

        for t in transitions:
            cut_t = float(getattr(t, "cut_time", 0.0))
            t_dur = float(getattr(t, "duration", 1.0))
            if abs(target_global_sec - cut_t) <= (t_dur + 0.1):
                for c_i in range(len(self.video_clips) - 1):
                    c_a = self.video_clips[c_i]
                    c_b = self.video_clips[c_i + 1]
                    a_id = str(c_a.get("id", c_i))
                    b_id = str(c_b.get("id", c_i + 1))
                    t_a = str(getattr(t, "clip_a_id", ""))
                    t_b = str(getattr(t, "clip_b_id", ""))
                    if (t_a == a_id and t_b == b_id) or getattr(t, "cut_index", None) == c_i:
                        t_dict = t if isinstance(t, dict) else {
                            "id": getattr(t, "id", f"t_{c_i}"),
                            "cut_time": getattr(t, "cut_time", cut_t),
                            "duration": getattr(t, "duration", t_dur),
                            "alignment": getattr(t, "alignment", "center"),
                            "type": getattr(t, "type", "cross_dissolve"),
                            "easing": getattr(t, "easing", "linear"),
                            "audio_mode": getattr(t, "audio_mode", "equal_power"),
                        }
                        med_a = float(c_a.get("media_file_duration", 0.0)) or (float(c_a.get("duration", 10.0)) * float(c_a.get("speed", 1.0)) + 2.0)
                        med_b = float(c_b.get("media_file_duration", 0.0)) or (float(c_b.get("duration", 10.0)) * float(c_b.get("speed", 1.0)) + 2.0)
                        c_a_dict = dict(c_a, media_file_duration=med_a)
                        c_b_dict = dict(c_b, media_file_duration=med_b)
                        res = resolve_transition_at_time(
                            global_time=target_global_sec,
                            transition=t_dict,
                            clip_a=c_a_dict,
                            clip_b=c_b_dict
                        )
                        if (res.is_active and res.is_valid_media_range) or res.status in ("VALID", "CLAMPED"):
                            if not hasattr(self, '_dual_decoder') or self._dual_decoder is None:
                                self._dual_decoder = DualDecoderManager()
                            ret_a, f_a, ret_b, f_b = self._dual_decoder.decode_transition_pair(
                                c_a, c_b, res.source_time_A, res.source_time_B
                            )
                            if ret_a and ret_b and f_a is not None and f_b is not None:
                                t_type = str(getattr(t, "type", "cross_dissolve"))
                                return render_transition_frame(f_a, f_b, t_type, res.progress)
                        break
        return None

    def _render_next_frame(self):
        if not self.video_clips:
            self._stop_video()
            return

        import time
        now = time.monotonic()
        start_wall = getattr(self, '_playback_start_wall_time', now)
        start_sec = getattr(self, '_playback_start_sec', 0.0)

        # 1. Master Precision Timeline Clock
        target_global_sec = start_sec + (now - start_wall)
        self._current_global_sec = target_global_sec
        tot_dur = sum(c.get("duration", 0.0) for c in self.video_clips)

        # Stop when global time reaches total duration
        if tot_dur > 0 and target_global_sec >= tot_dur:
            self._stop_video()
            return

        self._current_global_sec = target_global_sec

        # 2. Smooth Audio-Video Synchronization
        try:
            if hasattr(self, 'player') and self.player and self._is_playing:
                from PySide6.QtMultimedia import QMediaPlayer
                is_audio_playing = False
                if hasattr(QMediaPlayer, 'PlaybackState'):
                    is_audio_playing = (self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
                elif hasattr(QMediaPlayer, 'PlayingState'):
                    is_audio_playing = (self.player.state() == QMediaPlayer.PlayingState)

                if is_audio_playing:
                    a_pos_ms = self.player.position()
                    if a_pos_ms > 200:
                        a_pos_sec = a_pos_ms / 1000.0
                        drift = a_pos_sec - target_global_sec
                        if drift > 0.35:
                            slew = drift * 0.15
                            self._playback_start_sec += slew
                            target_global_sec += slew
                        elif drift < -0.40:
                            self.player.setPosition(int(target_global_sec * 1000))
                else:
                    # Auto-recover audio if player stalled or wasn't kicked off
                    if self.player.mediaStatus() in [QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia]:
                        if hasattr(self, 'audio_output'):
                            if hasattr(self.audio_output, 'setMuted'):
                                self.audio_output.setMuted(False)
                            if hasattr(self.audio_output, 'setVolume'):
                                self.audio_output.setVolume(self.vol_slider.value() / 100.0 if hasattr(self, 'vol_slider') else 1.0)
                        self.player.setPosition(int(target_global_sec * 1000))
                        self.player.play()
        except Exception:
            pass

        # 3. Resolve active clip and local time
        clip_idx, clip, local_sec = self.resolve_clip_at_time(target_global_sec)
        if clip_idx is None or clip is None:
            self._stop_video()
            return

        # 4. Handle seamless clip switching across boundaries
        if getattr(self, '_active_clip_idx', None) != clip_idx:
            logger.info(f"🎬 [Playback] Boundary transition: switching to Clip {clip_idx} at global {target_global_sec:.2f}s (local {local_sec:.2f}s)")
            if not self.switch_to_clip(clip_idx):
                self._stop_video()
                return

        if not self.cap or not self.cap.isOpened():
            self._stop_video()
            return

        expected_local_frame = int(local_sec * max(1.0, self.fps))
        frame_diff = expected_local_frame - self.current_frame

        # Pacing control: Never advance decoder ahead of wall clock
        if frame_diff <= 0:
            return

        ret = False
        frame = None
        t_frame = self._try_render_transition_frame(target_global_sec)
        if t_frame is not None:
            frame = t_frame
            ret = True
            self.current_frame = expected_local_frame
        elif frame_diff == 1:
            ret, frame = self.cap.read()
            self.current_frame += 1
        elif 2 <= frame_diff <= 8:
            for _ in range(frame_diff - 1):
                self.cap.grab()
            ret, frame = self.cap.read()
            self.current_frame = expected_local_frame
        elif 9 <= frame_diff <= 30:
            for _ in range(min(frame_diff - 1, 6)):
                self.cap.grab()
            ret, frame = self.cap.read()
            self.current_frame = self.current_frame + min(frame_diff, 7)
            self._playback_start_wall_time = now
            self._playback_start_sec = target_global_sec
        else:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, expected_local_frame))
            ret, frame = self.cap.read()
            self.current_frame = expected_local_frame
            self._playback_start_wall_time = now
            self._playback_start_sec = target_global_sec

        if not ret or frame is None:
            # If current clip reached EOF, seamlessly advance to next clip
            if clip_idx < len(self.video_clips) - 1:
                next_idx = clip_idx + 1
                self.switch_to_clip(next_idx)
                return
            self._stop_video()
            return

        frame = self._apply_all_effects(frame)
        self._show_frame_mat(frame)

        # 5. Throttle UI updates (~12Hz)
        now_ts = now
        last_ui_update = getattr(self, '_last_ui_slider_update', 0.0)
        if (now_ts - last_ui_update) >= 0.08:
            self._last_ui_slider_update = now_ts
            self.playhead_moved.emit(target_global_sec)
            if tot_dur > 0:
                val = int((target_global_sec / float(tot_dur)) * 1000)
                self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(val)
                self.seek_slider.blockSignals(False)
            global_frame = int(target_global_sec * max(1.0, self.fps))
            self._update_time_label(global_frame)

    def _display_frame_at(self, frame_idx: int):
        cur_g = getattr(self, '_current_global_sec', None)
        if cur_g is not None:
            t_frame = self._try_render_transition_frame(cur_g)
            if t_frame is not None:
                self.current_frame = frame_idx
                self._cached_clean_frame = t_frame.copy()
                frame = self._apply_all_effects(t_frame)
                self._show_frame_mat(frame)
                return

        if not self.cap or not self.cap.isOpened():
            return
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
        ret, frame = self.cap.read()
        if ret and frame is not None:
            self.current_frame = frame_idx
            self._cached_clean_frame = frame.copy()
            frame = self._apply_all_effects(frame)
            self._show_frame_mat(frame)

    def _show_frame_mat(self, frame):
        h, w, ch = frame.shape
        # Hardware-accelerated preview downscale for 1080p/4K high-res videos
        lbl_w = self.placeholder_lbl.width()
        lbl_h = self.placeholder_lbl.height()
        if lbl_w > 50 and lbl_h > 50 and (w > lbl_w * 1.4 or h > lbl_h * 1.4):
            scale = min((lbl_w * 1.2) / w, (lbl_h * 1.2) / h)
            if scale < 0.85:
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
                h, w, ch = frame.shape

        bytes_per_line = ch * w
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QtGui.QImage(rgb_frame.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
        pixmap = QtGui.QPixmap.fromImage(qimg)
        self.placeholder_lbl.setPixmap(pixmap)

    def _update_time_label(self, frame_idx: int):
        cur_sec = int(frame_idx / max(1.0, self.fps))
        tot_dur = sum(c.get("duration", 0.0) for c in getattr(self, 'video_clips', []))
        if tot_dur <= 0 and self.total_frames > 0:
            tot_dur = self.total_frames / max(1.0, self.fps)
        dur_sec = int(tot_dur)
        c_m, c_s = cur_sec // 60, cur_sec % 60
        d_m, d_s = dur_sec // 60, dur_sec % 60
        self.time_lbl.setText(f"{c_m:02d}:{c_s:02d} / {d_m:02d}:{d_s:02d}")

    def _execute_debounced_seek(self):
        """Executes the latest pending seek frame request, dropping obsolete intermediate frames."""
        if hasattr(self, '_pending_seek_frame') and self._pending_seek_frame is not None:
            f = self._pending_seek_frame
            s = getattr(self, '_pending_seek_sec', None)
            self._pending_seek_frame = None
            self._pending_seek_sec = None
            self._display_frame_at(f)
            if s is not None:
                pos_ms = int(s * 1000)
                try:
                    self.player.setPosition(pos_ms)
                except Exception:
                    pass

    def _on_seek_moved(self, value: int):
        tot_dur = sum(c.get("duration", 0.0) for c in getattr(self, 'video_clips', []))
        if tot_dur <= 0 and self.total_frames > 0:
            tot_dur = self.total_frames / max(1.0, self.fps)
        if tot_dur > 0:
            cur_sec = (value / 1000.0) * tot_dur
            self.seek_to_time_sec(cur_sec, immediate=False)
            self.playhead_moved.emit(cur_sec)

    def seek_to_time_sec(self, global_sec: float, immediate: bool = False):
        """Programmatic video frame seek with NLE-grade throttling to avoid decode queue buildup."""
        if not self.video_clips:
            return

        tot_dur = sum(c.get("duration", 0.0) for c in self.video_clips)
        global_sec = max(0.0, min(tot_dur, global_sec))
        self._current_global_sec = global_sec

        clip_idx, clip, local_sec = self.resolve_clip_at_time(global_sec)
        if clip_idx is None or clip is None:
            return

        if getattr(self, '_active_clip_idx', None) != clip_idx:
            self.switch_to_clip(clip_idx)

        local_frame = int(local_sec * max(1.0, self.fps))
        if self.cap and self.cap.isOpened():
            c_frames = self.cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if c_frames and c_frames > 0:
                local_frame = max(0, min(int(c_frames - 1), local_frame))

            import time
            self._playback_start_wall_time = time.monotonic()
            self._playback_start_sec = global_sec

            # Position audio player to global timeline time
            pos_ms = int(global_sec * 1000)
            try:
                self.player.setPosition(pos_ms)
            except Exception:
                pass

            # Update slider & time label
            if tot_dur > 0:
                val = int((global_sec / float(tot_dur)) * 1000)
                self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(val)
                self.seek_slider.blockSignals(False)
            global_frame = int(global_sec * max(1.0, self.fps))
            self._update_time_label(global_frame)

            if immediate or self._is_playing:
                self._display_frame_at(local_frame)
            else:
                self._pending_seek_frame = local_frame
                self._pending_seek_sec = global_sec
                if hasattr(self, '_seek_debounce_timer'):
                    self._seek_debounce_timer.start(20)
                else:
                    self._display_frame_at(local_frame)


# ==================== TOOLS PANEL ====================
class ToolsPanelWidget(QGroupBox):
    blur_enabled = Signal(bool)
    blur_intensity_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__("⚙ Tools", parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        row1 = QHBoxLayout()
        self.auto_sync_btn = QPushButton("⚡ Auto-Sync", self)
        self.auto_sync_btn.setProperty("class", "btn-orange")
        self.auto_speed_btn = QPushButton("Auto-Speed", self)
        self.auto_speed_btn.setProperty("class", "btn-orange")
        row1.addWidget(self.auto_sync_btn)
        row1.addWidget(self.auto_speed_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.video_sync_btn = QPushButton("🎬 Video Sync", self)
        self.video_sync_btn.setProperty("class", "btn-purple")
        self.cutter_btn = QPushButton(">< Cutter", self)
        self.cutter_btn.setProperty("class", "btn-purple")
        row2.addWidget(self.video_sync_btn)
        row2.addWidget(self.cutter_btn)
        layout.addLayout(row2)

        lic_lbl = QLabel("🔑 License: 379 ថ្ងៃ", self)
        lic_lbl.setStyleSheet("color: #0284c7; font-weight: bold; font-size: 12px; margin-top: 4px;")
        layout.addWidget(lic_lbl)

        self.chk_auto_speed = QCheckBox("Auto Sync Video Speed", self)
        self.chk_auto_speed.setChecked(True)
        self.chk_lock_speed = QCheckBox("Lock Speed (+25%)", self)
        self.chk_lock_speed.setChecked(True)
        self.chk_sync_tts = QCheckBox("Sync TTS to Original Video", self)
        self.chk_sync_tts.setChecked(True)
        self.chk_auto_vocal = QCheckBox("Auto remove Vocal", self)
        self.chk_auto_vocal.setChecked(True)

        layout.addWidget(self.chk_auto_speed)
        layout.addWidget(self.chk_lock_speed)
        layout.addWidget(self.chk_sync_tts)
        layout.addWidget(self.chk_auto_vocal)


# ==================== TIMELINE EDITOR ====================
class TimelineEditorWidget(QGroupBox):
    text_clip_changed = Signal(float, float)
    logo_clip_changed = Signal(float, float)
    subtitle_segment_adjusted = Signal(int, float, float)
    blur_item_selected = Signal(str)
    blur_item_timing_changed = Signal(str, float, float)
    blur_item_delete_requested = Signal(str)
    blur_item_duplicate_requested = Signal(str)
    text_selected = Signal(str)
    text_item_timing_changed = Signal(str, float, float)
    text_item_delete_requested = Signal(str)
    text_item_duplicate_requested = Signal(str)
    seek_requested = Signal(float)
    video_cut_requested = Signal(dict)
    video_split_requested = Signal(float)
    video_trim_left_requested = Signal(float)
    video_trim_right_requested = Signal(float)
    video_clip_selected = Signal(int)
    video_clip_delete_requested = Signal(int)
    video_clip_speed_changed = Signal(int, float)
    in_out_delete_requested = Signal(float, float)
    clip_dropped_on_timeline = Signal(str, float)      # (video_path, drop_sec) - CapCut Drag & Drop
    video_clips_reordered = Signal(list)               # (new_clips) - CapCut Drag & Drop Reorder
    transition_clicked = Signal(int)                   # (cut_idx) - Phase 3 Gate 4 Transitions

    def __init__(self, parent=None):
        super().__init__("Multi-Track Timeline Editor (Text • Logo • Subtitle • Blur)", parent)
        self.segments = []
        self._init_ui()

    def _init_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 6, 6, 6)

        # 1. Timeline Controls Toolbar (Clean 2-Row Responsive Layout - Zero Clipping)
        # Row 1: Editing Actions, In/Out Range & Track Visibility Toggles
        row1_lay = QHBoxLayout()
        row1_lay.setSpacing(5)
        row1_lay.setContentsMargins(2, 1, 2, 1)

        # Quick Edit Action Set: [✂️ Split (B)] [⇤ Trim L (Q)] [Trim R ⇥ (W)] [🗑️ Del (⌫)] [✂️ Video Studio]
        self.split_btn = QPushButton("✂️ Split (B)", self)
        self.split_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e1b4b;
                color: #a5b4fc;
                border: 1px solid #4338ca;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #4338ca;
                color: #ffffff;
            }
        """)
        self.split_btn.setToolTip("បំបែកវីដេអូត្រង់ Playhead (Shortcut: Ctrl+B / Cmd+B / B)")
        self.split_btn.clicked.connect(self._on_split_clicked)
        row1_lay.addWidget(self.split_btn)

        trim_btn_style = """
            QPushButton {
                background-color: #1e1b4b;
                color: #c7d2fe;
                border: 1px solid #4f46e5;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #3730a3;
                color: #ffffff;
                border-color: #6366f1;
            }
        """
        self.trim_left_btn = QPushButton("⇤ Trim L (Q)", self)
        self.trim_left_btn.setStyleSheet(trim_btn_style)
        self.trim_left_btn.setToolTip("Trim Left: កាត់ចោលផ្នែកខាងឆ្វេងពីដើមដល់ Playhead (CapCut Shortcut: Q)")
        self.trim_left_btn.clicked.connect(self._on_trim_left_clicked)
        row1_lay.addWidget(self.trim_left_btn)

        self.trim_right_btn = QPushButton("Trim R ⇥ (W)", self)
        self.trim_right_btn.setStyleSheet(trim_btn_style)
        self.trim_right_btn.setToolTip("Trim Right: កាត់ចោលផ្នែកខាងស្តាំពី Playhead ដល់ចុងបញ្ចប់ (CapCut Shortcut: W)")
        self.trim_right_btn.clicked.connect(self._on_trim_right_clicked)
        row1_lay.addWidget(self.trim_right_btn)

        del_btn_style = """
            QPushButton {
                background-color: #4c0519;
                color: #fda4af;
                border: 1px solid #be123c;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #9f1239;
                color: #ffffff;
                border-color: #f43f5e;
            }
        """
        self.delete_btn = QPushButton("🗑️ Del (⌫)", self)
        self.delete_btn.setStyleSheet(del_btn_style)
        self.delete_btn.setToolTip("លុប Clip ដែលបានជ្រើសរើស ឬលុបចន្លោះ In-Out (Shortcut: Delete / Backspace)")
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        row1_lay.addWidget(self.delete_btn)

        self.cut_video_btn = QPushButton("✂️ Cutter Studio", self)
        self.cut_video_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #0284c7);
                color: #ffffff;
                border: 1px solid #34d399;
                border-radius: 4px;
                padding: 3px 10px;
                font-weight: 800;
                font-size: 11px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #0ea5e9);
            }
        """)
        self.cut_video_btn.setToolTip("បើកផ្ទាំងកាត់តវីដេអូ (Video Cutter & Trimmer Studio) / Trim / Cut Out / Lossless")
        self.cut_video_btn.clicked.connect(self._on_cut_video_clicked)
        row1_lay.addWidget(self.cut_video_btn)

        self.trans_btn = QPushButton("⧓ Transition (T)", self)
        self.trans_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4338ca, stop:1 #6d28d9);
                color: #ffffff;
                border: 1px solid #818cf8;
                border-radius: 4px;
                padding: 3px 10px;
                font-weight: 800;
                font-size: 11px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4f46e5, stop:1 #7c3aed);
            }
        """)
        self.trans_btn.setToolTip("បន្ថែម ឬកែសម្រួល Transition រវាង Clips (CapCut Shortcut: T)")
        self.trans_btn.clicked.connect(self._on_transition_btn_clicked)
        row1_lay.addWidget(self.trans_btn)

        # In / Out Range Markers
        v_sep = QFrame(self)
        v_sep.setFrameShape(QFrame.VLine)
        v_sep.setStyleSheet("color: #334155; margin: 0 4px;")
        row1_lay.addWidget(v_sep)

        self.set_in_btn = QPushButton("[ In (I)", self)
        self.set_in_btn.setStyleSheet("""
            QPushButton {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #0284c7;
                color: #ffffff;
            }
        """)
        self.set_in_btn.setToolTip("កំណត់ចំណុចចាប់ផ្ដើម Mark In [ ត្រង់ Playhead (Shortcut: I)")
        self.set_in_btn.clicked.connect(lambda: self.waveform_canvas.set_in_point())
        row1_lay.addWidget(self.set_in_btn)

        self.set_out_btn = QPushButton("] Out (O)", self)
        self.set_out_btn.setStyleSheet("""
            QPushButton {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #0284c7;
                color: #ffffff;
            }
        """)
        self.set_out_btn.setToolTip("កំណត់ចំណុចបញ្ចប់ Mark Out ] ត្រង់ Playhead (Shortcut: O)")
        self.set_out_btn.clicked.connect(lambda: self.waveform_canvas.set_out_point())
        row1_lay.addWidget(self.set_out_btn)

        self.clear_in_out_btn = QPushButton("✕ Clear", self)
        self.clear_in_out_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #f1f5f9;
            }
        """)
        self.clear_in_out_btn.setToolTip("សម្អាត Mark In / Mark Out (Shortcut: Esc)")
        self.clear_in_out_btn.clicked.connect(lambda: self.waveform_canvas.clear_in_out())
        row1_lay.addWidget(self.clear_in_out_btn)

        self.in_out_badge = QLabel("[ --:-- - --:-- ]", self)
        self.in_out_badge.setStyleSheet("color: #64748b; font-family: 'Menlo', 'Courier New', monospace; font-size: 11px; padding: 2px 4px;")
        row1_lay.addWidget(self.in_out_badge)

        row1_lay.addStretch()

        # Track Toggles: [📝 Text] [🖼 Logo] [🔍 Blur]
        v_sep2 = QFrame(self)
        v_sep2.setFrameShape(QFrame.VLine)
        v_sep2.setStyleSheet("color: #334155; margin: 0 4px;")
        row1_lay.addWidget(v_sep2)

        tracks_lbl = QLabel("Tracks:", self)
        tracks_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
        row1_lay.addWidget(tracks_lbl)

        chip_style = """
            QPushButton {
                background-color: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: 600;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #f1f5f9;
            }
            QPushButton:checked {
                background-color: #0369a1;
                color: #ffffff;
                border: 1px solid #38bdf8;
            }
        """
        self.toggle_text_btn = QPushButton("📝 Text", self)
        self.toggle_text_btn.setCheckable(True)
        self.toggle_text_btn.setChecked(False)
        self.toggle_text_btn.setStyleSheet(chip_style)
        self.toggle_text_btn.setToolTip("បង្ហាញ/លាក់ Track Text Overlay លើ Timeline")
        self.toggle_text_btn.toggled.connect(lambda v: self.set_track_visible('text', v))
        row1_lay.addWidget(self.toggle_text_btn)

        self.toggle_logo_btn = QPushButton("🖼 Logo", self)
        self.toggle_logo_btn.setCheckable(True)
        self.toggle_logo_btn.setChecked(False)
        self.toggle_logo_btn.setStyleSheet(chip_style)
        self.toggle_logo_btn.setToolTip("បង្ហាញ/លាក់ Track Logo Overlay លើ Timeline")
        self.toggle_logo_btn.toggled.connect(lambda v: self.set_track_visible('logo', v))
        row1_lay.addWidget(self.toggle_logo_btn)

        self.toggle_blur_btn = QPushButton("🔍 Blur", self)
        self.toggle_blur_btn.setCheckable(True)
        self.toggle_blur_btn.setChecked(False)
        self.toggle_blur_btn.setStyleSheet(chip_style)
        self.toggle_blur_btn.setToolTip("បង្ហាញ/លាក់ Track Blur Mask លើ Timeline")
        self.toggle_blur_btn.toggled.connect(lambda v: self.set_track_visible('blur', v))
        row1_lay.addWidget(self.toggle_blur_btn)

        layout.addLayout(row1_lay)

        # Row 2: Viewport Navigation, Timecode & Zoom Controls
        row2_lay = QHBoxLayout()
        row2_lay.setSpacing(6)
        row2_lay.setContentsMargins(2, 1, 2, 1)

        # Timecode Display Badge
        self.timecode_lbl = QLabel("00:00.00 / 00:00.00", self)
        self.timecode_lbl.setStyleSheet("""
            QLabel {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #1e293b;
                border-radius: 4px;
                padding: 3px 8px;
                font-family: 'Menlo', 'Courier New', monospace;
                font-weight: bold;
                font-size: 11px;
            }
        """)
        row2_lay.addWidget(self.timecode_lbl)

        # Align to Playhead Button
        self.align_btn = QPushButton("🎯 Align Playhead", self)
        self.align_btn.setProperty("class", "btn-teal")
        self.align_btn.setIcon(get_svg_icon("align_playhead", "#ffffff", 14))
        self.align_btn.setToolTip("រំកិល Timeline ទៅចំកណ្តាលទីតាំង Playhead ក្រហម")
        self.align_btn.clicked.connect(self._align_to_playhead)
        row2_lay.addWidget(self.align_btn)

        # Fit to Window Button
        self.fit_btn = QPushButton("⤢ Fit Window", self)
        self.fit_btn.setProperty("class", "btn-gray")
        self.fit_btn.setIcon(get_svg_icon("fit_window", "#cbd5e1", 13))
        self.fit_btn.setToolTip("ពង្រីក/បង្រួម Timeline ឱ្យសមស្របពេញអេក្រង់ (Fit to Viewport)")
        self.fit_btn.clicked.connect(self._fit_to_window)
        row2_lay.addWidget(self.fit_btn)

        row2_lay.addStretch()

        # Zoom Controls: - [Slider] + [100%]
        zoom_lbl = QLabel("Zoom:", self)
        zoom_lbl.setStyleSheet("color: #64748b; font-size: 11px; font-weight: bold;")
        row2_lay.addWidget(zoom_lbl)

        self.zoom_out_btn = QPushButton("", self)
        self.zoom_out_btn.setFixedSize(24, 24)
        self.zoom_out_btn.setProperty("class", "btn-gray")
        self.zoom_out_btn.setIcon(get_svg_icon("minus", "#cbd5e1", 12))
        self.zoom_out_btn.setToolTip("Zoom Out Timeline (Cmd + -)")
        self.zoom_out_btn.clicked.connect(self._zoom_out)
        row2_lay.addWidget(self.zoom_out_btn)

        self.zoom_slider = QSlider(Qt.Horizontal, self)
        self.zoom_slider.setRange(10, 600)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(130)
        self.zoom_slider.setToolTip("Zoom Timeline (10% - 600% | Cmd+Wheel)")
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        row2_lay.addWidget(self.zoom_slider)

        self.zoom_in_btn = QPushButton("", self)
        self.zoom_in_btn.setFixedSize(24, 24)
        self.zoom_in_btn.setProperty("class", "btn-gray")
        self.zoom_in_btn.setIcon(get_svg_icon("plus", "#cbd5e1", 12))
        self.zoom_in_btn.setToolTip("Zoom In Timeline (Cmd + +)")
        self.zoom_in_btn.clicked.connect(self._zoom_in)
        row2_lay.addWidget(self.zoom_in_btn)

        # CapCut Zoom Percentage Badge
        self.zoom_val_lbl = QLabel("100%", self)
        self.zoom_val_lbl.setStyleSheet("""
            QLabel {
                color: #00f2fe;
                font-family: 'Menlo', 'Courier New', monospace;
                font-size: 11px;
                font-weight: 700;
                padding: 1px 4px;
                min-width: 38px;
            }
        """)
        row2_lay.addWidget(self.zoom_val_lbl)

        layout.addLayout(row2_lay)

        # 2. Timeline Body: Fixed Left Track Header Bar + Scrollable Waveform Canvas
        body_lay = QHBoxLayout()
        body_lay.setSpacing(0)
        body_lay.setContentsMargins(0, 0, 0, 0)

        # Fixed Left Column
        self.track_header = TimelineTrackHeaderWidget(self)
        body_lay.addWidget(self.track_header)

        # Scrollable Canvas
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setAcceptDrops(True)
        self.scroll_area.viewport().setAcceptDrops(True)
        self.scroll_area.viewport().installEventFilter(self)
        self.scroll_area.installEventFilter(self)
        self.scroll_area.setMinimumHeight(172)
        self.scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background-color: #0a0e1a;
                border: 1px solid #1a2238;
                border-left: none;
                border-radius: 0px 8px 8px 0px;
            }
        """)

        self.waveform_canvas = AudioWaveformCanvas(self.scroll_area)
        self.waveform_canvas.seek_requested.connect(self.seek_requested)
        self.waveform_canvas.text_clip_changed.connect(self.text_clip_changed)
        self.waveform_canvas.logo_clip_changed.connect(self.logo_clip_changed)
        self.waveform_canvas.subtitle_segment_adjusted.connect(self.subtitle_segment_adjusted)
        self.waveform_canvas.blur_item_selected.connect(self.blur_item_selected)
        self.waveform_canvas.blur_item_timing_changed.connect(self.blur_item_timing_changed)
        self.waveform_canvas.blur_item_delete_requested.connect(self.blur_item_delete_requested)
        self.waveform_canvas.blur_item_duplicate_requested.connect(self.blur_item_duplicate_requested)
        self.waveform_canvas.text_selected.connect(self.text_selected)
        self.waveform_canvas.text_item_timing_changed.connect(self.text_item_timing_changed)
        self.waveform_canvas.text_item_delete_requested.connect(self.text_item_delete_requested)
        self.waveform_canvas.text_item_duplicate_requested.connect(self.text_item_duplicate_requested)
        self.waveform_canvas.in_out_changed.connect(self._on_in_out_changed)
        self.waveform_canvas.split_requested.connect(self.video_split_requested)
        self.waveform_canvas.cut_requested.connect(self._on_cut_video_clicked)
        self.waveform_canvas.video_clip_selected.connect(self.video_clip_selected)
        self.waveform_canvas.video_clip_delete_requested.connect(self.video_clip_delete_requested)
        self.waveform_canvas.video_clip_speed_changed.connect(self.video_clip_speed_changed)
        self.waveform_canvas.in_out_delete_requested.connect(self.in_out_delete_requested)
        self.waveform_canvas.trim_left_requested.connect(self.video_trim_left_requested)
        self.waveform_canvas.trim_right_requested.connect(self.video_trim_right_requested)
        self.waveform_canvas.clip_dropped_on_timeline.connect(self.clip_dropped_on_timeline)
        self.waveform_canvas.video_clips_reordered.connect(self.video_clips_reordered)
        self.waveform_canvas.transition_clicked.connect(self.transition_clicked)
        self.scroll_area.setWidget(self.waveform_canvas)
        body_lay.addWidget(self.scroll_area, stretch=1)

        layout.addLayout(body_lay, stretch=1)

    def _on_transition_btn_clicked(self):
        v_clips = getattr(self.waveform_canvas, 'video_clips', [])
        if len(v_clips) < 2:
            return
        cur_sec = getattr(self.waveform_canvas, 'playhead_pos_sec', 0.0)
        target_cut = 0
        min_dist = float('inf')
        for c_i in range(1, len(v_clips)):
            c_st = v_clips[c_i].get("start", 0.0)
            dist = abs(cur_sec - c_st)
            if dist < min_dist:
                min_dist = dist
                target_cut = c_i - 1
        self.transition_clicked.emit(target_cut)

    def _on_delete_clicked(self):
        sel_idx = getattr(self.waveform_canvas, 'selected_clip_idx', None)
        in_s = getattr(self.waveform_canvas, 'in_point_sec', None)
        out_s = getattr(self.waveform_canvas, 'out_point_sec', None)
        if sel_idx is not None:
            self.video_clip_delete_requested.emit(sel_idx)
        elif in_s is not None and out_s is not None:
            self.in_out_delete_requested.emit(in_s, out_s)
        else:
            p_sec = getattr(self.waveform_canvas, 'playhead_pos_sec', 0.0)
            v_clips = getattr(self.waveform_canvas, 'video_clips', [])
            found = None
            for c_i, c in enumerate(v_clips):
                c_st = c.get("start", 0.0)
                c_dur = c.get("duration", 0.0)
                if c_st <= p_sec <= (c_st + c_dur):
                    found = c_i
                    break
            if found is not None:
                self.video_clip_delete_requested.emit(found)

    def _on_trim_left_clicked(self):
        playhead = getattr(self.waveform_canvas, 'playhead_pos_sec', 0.0)
        self.video_trim_left_requested.emit(playhead)

    def _on_trim_right_clicked(self):
        playhead = getattr(self.waveform_canvas, 'playhead_pos_sec', 0.0)
        self.video_trim_right_requested.emit(playhead)

    def _on_split_clicked(self):
        playhead = getattr(self.waveform_canvas, 'playhead_pos_sec', 0.0)
        self.video_split_requested.emit(playhead)

    def _on_cut_video_clicked(self):
        self.video_cut_requested.emit({
            "in_sec": getattr(self.waveform_canvas, 'in_point_sec', None),
            "out_sec": getattr(self.waveform_canvas, 'out_point_sec', None),
            "playhead_sec": getattr(self.waveform_canvas, 'playhead_pos_sec', 0.0)
        })

    def _on_in_out_changed(self, in_sec, out_sec):
        if not hasattr(self, 'in_out_badge'):
            return
        if in_sec is None and out_sec is None:
            self.in_out_badge.setText("[ --:-- - --:-- ]")
            self.in_out_badge.setStyleSheet("color: #64748b; font-family: 'Menlo', 'Courier New', monospace; font-size: 11px; padding: 2px 4px;")
        else:
            in_str = f"{int(in_sec // 60):02d}:{in_sec % 60:05.2f}" if in_sec is not None else "00:00.00"
            out_str = f"{int(out_sec // 60):02d}:{out_sec % 60:05.2f}" if out_sec is not None else "--:--"
            dur = (out_sec - (in_sec or 0.0)) if out_sec is not None else 0.0
            self.in_out_badge.setText(f"[ {in_str} ➔ {out_str} ({dur:.1f}s) ]")
            self.in_out_badge.setStyleSheet("""
                QLabel {
                    background-color: #082f49;
                    color: #38bdf8;
                    border: 1px solid #0284c7;
                    border-radius: 4px;
                    padding: 2px 6px;
                    font-family: 'Menlo', 'Courier New', monospace;
                    font-weight: bold;
                    font-size: 11px;
                }
            """)

    def set_track_visible(self, track_name: str, visible: bool):
        """Toggle track visibility (text, logo, blur) dynamically on timeline."""
        if track_name == 'text':
            self.waveform_canvas.show_text = visible
            if hasattr(self, 'toggle_text_btn') and self.toggle_text_btn.isChecked() != visible:
                self.toggle_text_btn.blockSignals(True)
                self.toggle_text_btn.setChecked(visible)
                self.toggle_text_btn.blockSignals(False)
        elif track_name == 'logo':
            self.waveform_canvas.show_logo = visible
            if hasattr(self, 'toggle_logo_btn') and self.toggle_logo_btn.isChecked() != visible:
                self.toggle_logo_btn.blockSignals(True)
                self.toggle_logo_btn.setChecked(visible)
                self.toggle_logo_btn.blockSignals(False)
        elif track_name == 'blur':
            self.waveform_canvas.show_blur = visible
            if hasattr(self, 'toggle_blur_btn') and self.toggle_blur_btn.isChecked() != visible:
                self.toggle_blur_btn.blockSignals(True)
                self.toggle_blur_btn.setChecked(visible)
                self.toggle_blur_btn.blockSignals(False)
                
        if hasattr(self, 'track_header'):
            self.track_header.update()
        if hasattr(self, 'waveform_canvas'):
            self.waveform_canvas.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'track_header'):
            self.track_header.update()

    def set_video_clips(self, clips: list, video_path: str = None):
        if hasattr(self, 'waveform_canvas'):
            self.waveform_canvas.set_video_clips(clips, video_path=video_path)
            self._update_timecode_display()

    def set_text_clip(self, start_sec: float, dur_sec: float, text: str = None):
        self.waveform_canvas.set_text_clip(start_sec, dur_sec, text)

    def set_text_items(self, items: list, active_id: str = None):
        self.waveform_canvas.set_text_items(items, active_id)

    @property
    def text_items(self):
        return getattr(self.waveform_canvas, 'text_items', [])

    @property
    def active_text_id(self):
        return getattr(self.waveform_canvas, 'active_text_id', None)

    def set_logo_clip(self, start_sec: float, dur_sec: float, name: str = None, full_video: bool = None):
        self.waveform_canvas.set_logo_clip(start_sec, dur_sec, name, full_video)

    def set_total_duration(self, total_duration: float):
        self.waveform_canvas.set_data(total_duration=total_duration)
        self._update_timecode_display()

    def set_blur_auto_speech(self, enabled: bool):
        self.waveform_canvas.set_blur_auto_speech(enabled)

    def set_blur_items(self, items: list, active_id: str = None):
        self.waveform_canvas.set_blur_items(items, active_id)

    def _align_to_playhead(self):
        if not hasattr(self, 'waveform_canvas') or not hasattr(self, 'scroll_area'):
            return
        pixels_per_sec = 40.0 * self.waveform_canvas.zoom_factor
        playhead_x = int(self.waveform_canvas.playhead_pos_sec * pixels_per_sec)
        viewport_w = self.scroll_area.viewport().width()
        target_scroll = max(0, playhead_x - viewport_w // 2)
        self.scroll_area.horizontalScrollBar().setValue(target_scroll)
        logger.info(f"📍 Aligned timeline view to playhead: {self.waveform_canvas.playhead_pos_sec:.2f}s (scroll={target_scroll})")

    def _fit_to_window(self):
        if not hasattr(self, 'waveform_canvas') or not hasattr(self, 'scroll_area'):
            return
        dur = getattr(self.waveform_canvas, 'total_duration_sec', 60.0)
        viewport_w = self.scroll_area.viewport().width()
        if dur > 0 and viewport_w > 100:
            target_pps = max(1.0, (viewport_w - 40) / float(dur))
            zoom = max(0.1, min(6.0, target_pps / 40.0))
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.setValue(int(round(zoom * 100)))
            self.zoom_slider.blockSignals(False)
            self._update_zoom_label(zoom)
            self.waveform_canvas.apply_zoom(zoom, anchor_canvas_x=0)
            self.scroll_area.horizontalScrollBar().setValue(0)
            logger.info(f"🔍 Timeline fit to window: zoom={zoom:.2f}x (dur={dur:.1f}s)")

    def _zoom_in(self):
        cur = self.zoom_slider.value()
        new_val = min(600, max(cur + 15, int(round(cur * 1.25))))
        self.zoom_slider.setValue(new_val)

    def _zoom_out(self):
        cur = self.zoom_slider.value()
        new_val = max(10, min(cur - 15, int(round(cur / 1.25))))
        self.zoom_slider.setValue(new_val)

    def _update_zoom_label(self, zoom: float):
        if hasattr(self, 'zoom_val_lbl') and self.zoom_val_lbl:
            pct = int(round(zoom * 100))
            self.zoom_val_lbl.setText(f"{pct}%")

    def set_segments(self, segments: list):
        self.segments = segments
        zoom = self.zoom_slider.value() / 100.0
        self.waveform_canvas.set_data(segments=segments, zoom=zoom)
        self._update_timecode_display()

    def set_playhead_position(self, playhead_sec: float):
        self.waveform_canvas.set_data(segments=None, playhead_sec=playhead_sec)
        self._update_timecode_display(playhead_sec)

        # CapCut Auto-scroll / Playhead Follow during playback:
        if getattr(self, 'auto_scroll_playhead', True) and hasattr(self, 'scroll_area'):
            sa = self.scroll_area
            sb = sa.horizontalScrollBar()
            pps = 40.0 * getattr(self.waveform_canvas, 'zoom_factor', 1.0)
            px = int(playhead_sec * pps)
            vw = sa.viewport().width()
            cur_s = sb.value()
            if px > (cur_s + vw * 0.85):
                sb.setValue(max(0, px - int(vw * 0.2)))
            elif px < cur_s:
                sb.setValue(max(0, px - int(vw * 0.1)))

    def _update_timecode_display(self, cur_sec: float = None):
        if not hasattr(self, 'timecode_lbl') or not hasattr(self, 'waveform_canvas'):
            return
        cur = self.waveform_canvas.playhead_pos_sec if cur_sec is None else cur_sec
        tot = getattr(self.waveform_canvas, 'total_duration_sec', 0.0)
        cur_m = int(cur // 60)
        cur_s = cur % 60
        tot_m = int(tot // 60)
        tot_s = tot % 60
        self.timecode_lbl.setText(f"{cur_m:02d}:{cur_s:05.2f} / {tot_m:02d}:{tot_s:05.2f}")

    def _on_zoom_changed(self, value: int):
        zoom = value / 100.0
        self._update_zoom_label(zoom)
        if hasattr(self, 'waveform_canvas') and self.waveform_canvas:
            self.waveform_canvas.apply_zoom(zoom)

    def scroll_to_sec(self, sec: float):
        """Smoothly scroll the timeline scroll area so the specified timestamp is visible."""
        if not hasattr(self, 'waveform_canvas') or not hasattr(self, 'scroll_area'):
            return
        pixels_per_sec = 40.0 * getattr(self.waveform_canvas, 'zoom_factor', 1.0)
        pos_x = int(sec * pixels_per_sec)
        viewport_w = self.scroll_area.viewport().width()
        target_scroll = max(0, pos_x - viewport_w // 4)
        self.scroll_area.horizontalScrollBar().setValue(target_scroll)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.DragEnter, QtCore.QEvent.DragMove, QtCore.QEvent.DragLeave, QtCore.QEvent.Drop):
            if hasattr(self, 'waveform_canvas') and self.waveform_canvas:
                if event.type() == QtCore.QEvent.DragEnter:
                    self.waveform_canvas.dragEnterEvent(event)
                elif event.type() == QtCore.QEvent.DragMove:
                    self.waveform_canvas.dragMoveEvent(event)
                elif event.type() == QtCore.QEvent.DragLeave:
                    self.waveform_canvas.dragLeaveEvent(event)
                elif event.type() == QtCore.QEvent.Drop:
                    self.waveform_canvas.dropEvent(event)
                return True
        elif event.type() == QtCore.QEvent.Wheel:
            if hasattr(self, 'waveform_canvas') and self.waveform_canvas:
                self.waveform_canvas.wheelEvent(event)
                return True
        elif event.type() == QtCore.QEvent.NativeGesture:
            if hasattr(self, 'waveform_canvas') and self.waveform_canvas:
                if self.waveform_canvas.event(event):
                    return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if hasattr(self, 'waveform_canvas'):
            self.waveform_canvas.dragEnterEvent(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if hasattr(self, 'waveform_canvas'):
            self.waveform_canvas.dragMoveEvent(event)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        if hasattr(self, 'waveform_canvas'):
            self.waveform_canvas.dragLeaveEvent(event)

    def dropEvent(self, event):
        if hasattr(self, 'waveform_canvas'):
            self.waveform_canvas.dropEvent(event)
        else:
            event.ignore()


# ==================== VIDEO EFFECTS WIDGET ====================
TEXT_STYLE_TEMPLATES = [
    # 1. TikTok & Trends (8)
    {
        "id": "tiktok_gold",
        "category": "TikTok & Trends",
        "name": "TikTok Gold",
        "font": "Kantumruy Pro",
        "size": 28,
        "color": "#FACC15",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#000000",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#facc15",
        "badge_fg": "#000000",
        "tip": "TikTok Gold: Bold yellow with bounce pop"
    },
    {
        "id": "viral_pop",
        "category": "TikTok & Trends",
        "name": "Viral Pop",
        "font": "Kantumruy Pro",
        "size": 28,
        "color": "#FF0055",
        "bg_color": None,
        "outline_color": "#FFFFFF",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#ff0055",
        "badge_fg": "#ffffff",
        "tip": "Viral Pop: High energy magenta with white stroke"
    },
    {
        "id": "neon_lime",
        "category": "TikTok & Trends",
        "name": "Neon Lime",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#22C55E",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#15803D",
        "shadow_offset": 3,
        "anim": "Slide Up",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#22c55e",
        "badge_fg": "#052e16",
        "tip": "Neon Lime: Vivid green with dark emerald box"
    },
    {
        "id": "hot_candy",
        "category": "TikTok & Trends",
        "name": "Hot Candy",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#F43F5E",
        "bg_color": None,
        "outline_color": "#FFFFFF",
        "outline_width": 2,
        "shadow_color": "#9F1239",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 1.5,
        "badge_bg": "#f43f5e",
        "badge_fg": "#ffffff",
        "tip": "Hot Candy: Sweet candy rose with rapid bounce"
    },
    {
        "id": "cyber_blue",
        "category": "TikTok & Trends",
        "name": "Cyber Blue",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#06B6D4",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#0E7490",
        "shadow_offset": 4,
        "anim": "Slide Left",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#06b6d4",
        "badge_fg": "#083344",
        "tip": "Cyber Blue: Vibrant cyan blue slide"
    },
    {
        "id": "sunset_glow",
        "category": "TikTok & Trends",
        "name": "Sunset Glow",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FB923C",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#C2410C",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#fb923c",
        "badge_fg": "#431407",
        "tip": "Sunset Glow: Warm orange glow bounce"
    },
    {
        "id": "ultra_yellow",
        "category": "TikTok & Trends",
        "name": "Ultra Yellow",
        "font": "Kantumruy Pro",
        "size": 28,
        "color": "#EAB308",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 4,
        "shadow_color": "#713F12",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.5,
        "badge_bg": "#eab308",
        "badge_fg": "#000000",
        "tip": "Ultra Yellow: Saturated headline gold"
    },
    {
        "id": "flash_white",
        "category": "TikTok & Trends",
        "name": "Flash White",
        "font": "Battambang",
        "size": 26,
        "color": "#FFFFFF",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 4,
        "shadow_color": "#000000",
        "shadow_offset": 6,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#000000",
        "badge_fg": "#ffffff",
        "tip": "Flash White: Heavy bold monochrome pop"
    },

    # 2. Breaking News (6)
    {
        "id": "breaking_red",
        "category": "Breaking News",
        "name": "Breaking Alert",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FFFFFF",
        "bg_color": None,
        "outline_color": "#7F1D1D",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 4,
        "anim": "Slide Left",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#dc2626",
        "badge_fg": "#ffffff",
        "tip": "Breaking Alert: Crimson red broadcast headline"
    },
    {
        "id": "news_headline",
        "category": "Breaking News",
        "name": "News Headline",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FEF08A",
        "bg_color": None,
        "outline_color": "#172554",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 3,
        "anim": "Slide Up",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#1e3a8a",
        "badge_fg": "#fef08a",
        "tip": "News Headline: Studio navy blue with bright headline"
    },
    {
        "id": "live_urgent",
        "category": "Breaking News",
        "name": "Live Urgent",
        "font": "Battambang",
        "size": 28,
        "color": "#FFFFFF",
        "bg_color": None,
        "outline_color": "#450A0A",
        "outline_width": 3,
        "shadow_color": "#000000",
        "shadow_offset": 5,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 1.5,
        "badge_bg": "#b91c1c",
        "badge_fg": "#ffffff",
        "tip": "Live Urgent: Urgent red flash badge"
    },
    {
        "id": "flash_report",
        "category": "Breaking News",
        "name": "Flash Report",
        "font": "Kantumruy Pro",
        "size": 25,
        "color": "#F8FAFC",
        "bg_color": None,
        "outline_color": "#38BDF8",
        "outline_width": 2,
        "shadow_color": "#0284C7",
        "shadow_offset": 3,
        "anim": "Slide Left",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#0f172a",
        "badge_fg": "#38bdf8",
        "tip": "Flash Report: Slate dark banner with sky blue border"
    },
    {
        "id": "exclusive_amber",
        "category": "Breaking News",
        "name": "Exclusive Amber",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#000000",
        "bg_color": None,
        "outline_color": "#B45309",
        "outline_width": 1,
        "shadow_color": "#78350F",
        "shadow_offset": 2,
        "anim": "Pop / Bounce",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#f59e0b",
        "badge_fg": "#000000",
        "tip": "Exclusive Amber: High-visibility warning alert"
    },
    {
        "id": "studio_red",
        "category": "Breaking News",
        "name": "Studio Red",
        "font": "Battambang",
        "size": 24,
        "color": "#FFFFFF",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#000000",
        "shadow_offset": 4,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.5,
        "badge_bg": "#991b1b",
        "badge_fg": "#ffffff",
        "tip": "Studio Red: Deep crimson formal broadcast tag"
    },

    # 3. Cinema & Film (6)
    {
        "id": "cinema_pure",
        "category": "Cinema & Film",
        "name": "Cinema Pure",
        "font": "Battambang",
        "size": 24,
        "color": "#FFFFFF",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 5,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 3.0,
        "badge_bg": "#1f2937",
        "badge_fg": "#ffffff",
        "tip": "Cinema Pure: Elegant cinematic white fade"
    },
    {
        "id": "golden_noir",
        "category": "Cinema & Film",
        "name": "Golden Noir",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FDE047",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 2,
        "shadow_color": "#713F12",
        "shadow_offset": 4,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 3.0,
        "badge_bg": "#18181b",
        "badge_fg": "#fde047",
        "tip": "Golden Noir: Hollywood luxury noir styling"
    },
    {
        "id": "vintage_sepia",
        "category": "Cinema & Film",
        "name": "Vintage Sepia",
        "font": "Battambang",
        "size": 24,
        "color": "#FEF3C7",
        "bg_color": None,
        "outline_color": "#78350F",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 3,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 3.0,
        "badge_bg": "#451a03",
        "badge_fg": "#fef3c7",
        "tip": "Vintage Sepia: Warm historical film tone"
    },
    {
        "id": "dramatic_silver",
        "category": "Cinema & Film",
        "name": "Dramatic Silver",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#E2E8F0",
        "bg_color": None,
        "outline_color": "#334155",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 6,
        "anim": "Slide Up",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.5,
        "badge_bg": "#020617",
        "badge_fg": "#e2e8f0",
        "tip": "Dramatic Silver: Dark cinematic block title"
    },
    {
        "id": "movie_title",
        "category": "Cinema & Film",
        "name": "Movie Title",
        "font": "Battambang",
        "size": 28,
        "color": "#F8FAFC",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 4,
        "shadow_color": "#000000",
        "shadow_offset": 6,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.0,
        "badge_bg": "#000000",
        "badge_fg": "#f8fafc",
        "tip": "Movie Title: Crisp widescreen feature film credit"
    },
    {
        "id": "film_noir",
        "category": "Cinema & Film",
        "name": "Film Noir",
        "font": "Battambang",
        "size": 24,
        "color": "#D1D5DB",
        "bg_color": None,
        "outline_color": "#374151",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 4,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.5,
        "badge_bg": "#111827",
        "badge_fg": "#d1d5db",
        "tip": "Film Noir: Classic monochrome thriller title"
    },

    # 4. Neon Cyber (6)
    {
        "id": "neon_cyber",
        "category": "Neon Cyber",
        "name": "Neon Cyber",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#00F0FF",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#00F0FF",
        "shadow_offset": 4,
        "anim": "Slide Up",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#0a192f",
        "badge_fg": "#00f0ff",
        "tip": "Neon Cyber: Electric cyber cyan glow"
    },
    {
        "id": "cyberpunk_pink",
        "category": "Neon Cyber",
        "name": "Cyberpunk Pink",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FF007F",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#FF007F",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#1a0b2e",
        "badge_fg": "#ff007f",
        "tip": "Cyberpunk Pink: Vibrant synthwave glow"
    },
    {
        "id": "laser_green",
        "category": "Neon Cyber",
        "name": "Laser Green",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#39FF14",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#39FF14",
        "shadow_offset": 4,
        "anim": "Slide Left",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#05200a",
        "badge_fg": "#39ff14",
        "tip": "Laser Green: Electric toxic neon glow"
    },
    {
        "id": "electric_violet",
        "category": "Neon Cyber",
        "name": "Electric Violet",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#C084FC",
        "bg_color": None,
        "outline_color": "#581C87",
        "outline_width": 2,
        "shadow_color": "#9333EA",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#1e0b36",
        "badge_fg": "#c084fc",
        "tip": "Electric Violet: Glowing purple futuristic title"
    },
    {
        "id": "acid_matrix",
        "category": "Neon Cyber",
        "name": "Acid Matrix",
        "font": "Kantumruy Pro",
        "size": 25,
        "color": "#4ADE80",
        "bg_color": None,
        "outline_color": "#064E3B",
        "outline_width": 3,
        "shadow_color": "#22C55E",
        "shadow_offset": 3,
        "anim": "Slide Up",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#022c22",
        "badge_fg": "#4ade80",
        "tip": "Acid Matrix: Sci-fi terminal green"
    },
    {
        "id": "synthwave_sun",
        "category": "Neon Cyber",
        "name": "Synthwave Sun",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FB7185",
        "bg_color": None,
        "outline_color": "#4C0519",
        "outline_width": 2,
        "shadow_color": "#E11D48",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#270722",
        "badge_fg": "#fb7185",
        "tip": "Synthwave Sun: 80s retro sunset glow"
    },

    # 5. Minimalist Vlog (6)
    {
        "id": "emerald_vibe",
        "category": "Minimalist Vlog",
        "name": "Fresh Mint",
        "font": "Noto Sans Khmer",
        "size": 25,
        "color": "#A7F3D0",
        "bg_color": None,
        "outline_color": "#022C22",
        "outline_width": 2,
        "shadow_color": "#047857",
        "shadow_offset": 2,
        "anim": "Slide Up",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.5,
        "badge_bg": "#064e3b",
        "badge_fg": "#a7f3d0",
        "tip": "Fresh Mint: Soothing green aesthetic vlog title"
    },
    {
        "id": "clean_white",
        "category": "Minimalist Vlog",
        "name": "Clean White",
        "font": "Kantumruy Pro",
        "size": 24,
        "color": "#FFFFFF",
        "bg_color": None,
        "outline_color": "#0F172A",
        "outline_width": 2,
        "shadow_color": "#1E293B",
        "shadow_offset": 3,
        "anim": "Fade In",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.5,
        "badge_bg": "#334155",
        "badge_fg": "#ffffff",
        "tip": "Clean White: Minimal modern slate vlog title"
    },
    {
        "id": "nordic_gray",
        "category": "Minimalist Vlog",
        "name": "Nordic Gray",
        "font": "Noto Sans Khmer",
        "size": 24,
        "color": "#F1F5F9",
        "bg_color": None,
        "outline_color": "#0F172A",
        "outline_width": 2,
        "shadow_color": "#000000",
        "shadow_offset": 3,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.0,
        "badge_bg": "#1e293b",
        "badge_fg": "#f1f5f9",
        "tip": "Nordic Gray: Calm Scandinavian aesthetic"
    },
    {
        "id": "warm_sand",
        "category": "Minimalist Vlog",
        "name": "Warm Sand",
        "font": "Kantumruy Pro",
        "size": 25,
        "color": "#FEF3C7",
        "bg_color": None,
        "outline_color": "#451A03",
        "outline_width": 2,
        "shadow_color": "#92400E",
        "shadow_offset": 2,
        "anim": "Slide Up",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#78350f",
        "badge_fg": "#fef3c7",
        "tip": "Warm Sand: Cozy coffee & travel vlog style"
    },
    {
        "id": "pastel_peach",
        "category": "Minimalist Vlog",
        "name": "Pastel Peach",
        "font": "Noto Sans Khmer",
        "size": 25,
        "color": "#FFE4E6",
        "bg_color": None,
        "outline_color": "#4C0519",
        "outline_width": 2,
        "shadow_color": "#BE123C",
        "shadow_offset": 3,
        "anim": "Pop / Bounce",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#9f1239",
        "badge_fg": "#ffe4e6",
        "tip": "Pastel Peach: Soft lifestyle and beauty title"
    },
    {
        "id": "soft_matcha",
        "category": "Minimalist Vlog",
        "name": "Soft Matcha",
        "font": "Kantumruy Pro",
        "size": 24,
        "color": "#D9F99D",
        "bg_color": None,
        "outline_color": "#1A2E05",
        "outline_width": 2,
        "shadow_color": "#4D7C0F",
        "shadow_offset": 2,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.5,
        "badge_bg": "#365314",
        "badge_fg": "#d9f99d",
        "tip": "Soft Matcha: Earthy peaceful wellness style"
    },

    # 6. Gaming & Esports (4)
    {
        "id": "gaming_crimson",
        "category": "Gaming & Esports",
        "name": "Gaming Crimson",
        "font": "Kantumruy Pro",
        "size": 28,
        "color": "#EF4444",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 4,
        "shadow_color": "#991B1B",
        "shadow_offset": 5,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 1.5,
        "badge_bg": "#18181b",
        "badge_fg": "#ef4444",
        "tip": "Gaming Crimson: Aggressive esports punch"
    },
    {
        "id": "poison_green",
        "category": "Gaming & Esports",
        "name": "Poison Green",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#10B981",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#059669",
        "shadow_offset": 4,
        "anim": "Slide Left",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#042f2e",
        "badge_fg": "#10b981",
        "tip": "Poison Green: High-action gamer highlight"
    },
    {
        "id": "royal_gamer",
        "category": "Gaming & Esports",
        "name": "Royal Gamer",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#A855F7",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#7E22CE",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#0f172a",
        "badge_fg": "#a855f7",
        "tip": "Royal Gamer: Twitch stream & montage title"
    },
    {
        "id": "mecha_orange",
        "category": "Gaming & Esports",
        "name": "Mecha Orange",
        "font": "Battambang",
        "size": 26,
        "color": "#F97316",
        "bg_color": None,
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow_color": "#C2410C",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 1.5,
        "badge_bg": "#1c1917",
        "badge_fg": "#f97316",
        "tip": "Mecha Orange: Industrial robotic action title"
    },

    # 7. Luxury & Gold (4)
    {
        "id": "royal_purple",
        "category": "Luxury & Gold",
        "name": "Royal Violet",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#F3E8FF",
        "bg_color": None,
        "outline_color": "#3B0764",
        "outline_width": 2,
        "shadow_color": "#7E22CE",
        "shadow_offset": 4,
        "anim": "Pop / Bounce",
        "speed": "0.5s (Normal)",
        "repeat_sec": 2.0,
        "badge_bg": "#581c87",
        "badge_fg": "#f3e8ff",
        "tip": "Royal Violet: Opulent royal palace styling"
    },
    {
        "id": "pure_platinum",
        "category": "Luxury & Gold",
        "name": "Pure Platinum",
        "font": "Battambang",
        "size": 24,
        "color": "#F8FAFC",
        "bg_color": None,
        "outline_color": "#0F172A",
        "outline_width": 3,
        "shadow_color": "#475569",
        "shadow_offset": 4,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 3.0,
        "badge_bg": "#334155",
        "badge_fg": "#f8fafc",
        "tip": "Pure Platinum: Premium silver luxury branding"
    },
    {
        "id": "champagne_gold",
        "category": "Luxury & Gold",
        "name": "Champagne Gold",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FEF08A",
        "bg_color": None,
        "outline_color": "#422006",
        "outline_width": 2,
        "shadow_color": "#A16207",
        "shadow_offset": 4,
        "anim": "Fade In",
        "speed": "0.8s (Smooth)",
        "repeat_sec": 2.5,
        "badge_bg": "#713f12",
        "badge_fg": "#fef08a",
        "tip": "Champagne Gold: High-end luxury gold title"
    },
    {
        "id": "rose_gold",
        "category": "Luxury & Gold",
        "name": "Rose Gold",
        "font": "Kantumruy Pro",
        "size": 26,
        "color": "#FFE4E6",
        "bg_color": None,
        "outline_color": "#4C0519",
        "outline_width": 2,
        "shadow_color": "#9F1239",
        "shadow_offset": 3,
        "anim": "Pop / Bounce",
        "speed": "0.3s (Fast)",
        "repeat_sec": 2.0,
        "badge_bg": "#881337",
        "badge_fg": "#ffe4e6",
        "tip": "Rose Gold: Chic jewelry and perfume branding"
    }
]

SUBTITLE_STYLE_TEMPLATES = [
    # Trending
    {"name": "💥 Pop White", "name_short": "Pop White", "category": "Trending", "font": "Kantumruy Pro", "size": 22, "color": "#FFFFFF", "stroke_color": "#000000", "stroke_width": 3, "has_3d": True, "shadow_color": "#000000", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#1e293b", "badge_fg": "#ffffff"},
    {"name": "⚡ TikTok Gold", "name_short": "TikTok Gold", "category": "Trending", "font": "Koulen", "size": 24, "color": "#FACC15", "stroke_color": "#713F12", "stroke_width": 4, "has_3d": True, "shadow_color": "#000000", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#713f12", "badge_fg": "#facc15"},
    {"name": "🔥 Fire Flame", "name_short": "Fire Flame", "category": "Trending", "font": "Bayon", "size": 22, "color": "#FF4500", "stroke_color": "#7C2D12", "stroke_width": 3, "glow_color": "#F97316", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#7c2d12", "badge_fg": "#fed7aa"},
    {"name": "🎬 Cinema Classic", "name_short": "Cinema Red", "category": "Trending", "font": "Battambang", "size": 22, "color": "#EF4444", "stroke_color": "#7F1D1D", "stroke_width": 3, "has_3d": True, "shadow_color": "#450A0A", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#991b1b", "badge_fg": "#fecaca"},
    {"name": "✨ Luxury Gold", "name_short": "Luxury Gold", "category": "Trending", "font": "Koulen", "size": 22, "color": "#FDE68A", "stroke_color": "#78350F", "stroke_width": 3, "glow_color": "#F59E0B", "opacity": 0, "anim": "magical_duel", "badge_bg": "#78350f", "badge_fg": "#fef3c7"},

    # 3D & Pop
    {"name": "💥 Comic 3D", "name_short": "Comic 3D", "category": "3D & Pop", "font": "Koulen", "size": 24, "color": "#FDE047", "stroke_color": "#B91C1C", "stroke_width": 4, "has_3d": True, "shadow_color": "#7F1D1D", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#b91c1c", "badge_fg": "#fef08a"},
    {"name": "🍃 Retro Mint", "name_short": "Retro Mint", "category": "3D & Pop", "font": "Preahvihear", "size": 22, "color": "#34D399", "stroke_color": "#064E3B", "stroke_width": 4, "has_3d": True, "shadow_color": "#022C22", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#064e3b", "badge_fg": "#a7f3d0"},
    {"name": "💎 3D Chrome", "name_short": "Chrome 3D", "category": "3D & Pop", "font": "Content", "size": 22, "color": "#F1F5F9", "stroke_color": "#0F172A", "stroke_width": 3, "has_3d": True, "shadow_color": "#334155", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#1e293b", "badge_fg": "#e2e8f0"},
    {"name": "🎀 Bubblegum", "name_short": "Bubblegum", "category": "3D & Pop", "font": "Kantumruy Pro", "size": 22, "color": "#F472B6", "stroke_color": "#831843", "stroke_width": 4, "has_3d": True, "shadow_color": "#500724", "opacity": 0, "anim": "pop_bounce", "badge_bg": "#831843", "badge_fg": "#fbcfe8"},

    # Glow & Neon
    {"name": "🌐 Cyber Neon", "name_short": "Cyber Cyan", "category": "Glow & Neon", "font": "Moul", "size": 22, "color": "#00F0FF", "stroke_color": "#083344", "stroke_width": 3, "glow_color": "#00F0FF", "opacity": 0, "anim": "cyber_field", "badge_bg": "#0e7490", "badge_fg": "#67e8f9"},
    {"name": "💜 Neon Violet", "name_short": "Neon Violet", "category": "Glow & Neon", "font": "Fasthand", "size": 24, "color": "#E879F9", "stroke_color": "#4C1D95", "stroke_width": 3, "glow_color": "#C026D3", "opacity": 0, "anim": "magical_duel", "badge_bg": "#4c1d95", "badge_fg": "#ddd6fe"},
    {"name": "🔓 Matrix Hacker", "name_short": "Matrix Code", "category": "Glow & Neon", "font": "Battambang", "size": 22, "color": "#22C55E", "stroke_color": "#052E16", "stroke_width": 3, "glow_color": "#16A34A", "opacity": 0, "anim": "frequency_decode", "badge_bg": "#14532d", "badge_fg": "#86efac"},
    {"name": "💢 Glitch Impact", "name_short": "Glitch Neon", "category": "Glow & Neon", "font": "Moul", "size": 22, "color": "#F43F5E", "stroke_color": "#1E1B4B", "stroke_width": 4, "glow_color": "#06B6D4", "opacity": 0, "anim": "glitch_shake", "badge_bg": "#881337", "badge_fg": "#fecdd3"},

    # Cinematic
    {"name": "🌊 Smooth Wave", "name_short": "Smooth Wave", "category": "Cinematic", "font": "Siemreap", "size": 22, "color": "#38BDF8", "stroke_color": "#0C4A6E", "stroke_width": 3, "opacity": 0, "anim": "wiping_in", "badge_bg": "#854d0e", "badge_fg": "#fef08a"},
    {"name": "🌫️ Blur Dissolve", "name_short": "Blur Focus", "category": "Cinematic", "font": "Noto Sans Khmer", "size": 13, "color": "#E2E8F0", "stroke_color": "#0F172A", "stroke_width": 2, "opacity": 5, "anim": "blur_dissolve", "badge_bg": "#0c4a6e", "badge_fg": "#7dd3fc"},
    {"name": "🎵 Karaoke Pulse", "name_short": "Karaoke", "category": "Cinematic", "font": "Kantumruy Pro", "size": 22, "color": "#FB7185", "stroke_color": "#881337", "stroke_width": 3, "opacity": 0, "anim": "karaoke_pulse", "badge_bg": "#4c0519", "badge_fg": "#f43f5e"},
]


class CapCutStyleCard(QFrame):
    """CapCut-style Visual Text Effect Card with stylized 'ART' preview tile, glowing hover, and 1-click apply."""
    clicked = Signal(dict)

    def __init__(self, template: dict, is_selected: bool = False, parent=None):
        super().__init__(parent)
        self.template = template
        self.is_selected = is_selected
        self.setFixedSize(84, 72)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{template['name']}\nFont: {template['font']}\nCategory: {template.get('category', 'Trending')}\nClick to apply CapCut text style")
        self._update_style()

    def set_selected(self, selected: bool):
        self.is_selected = selected
        self._update_style()
        self.update()

    def _update_style(self):
        border_col = "#38bdf8" if self.is_selected else "#1e293b"
        bg_col = "#142038" if self.is_selected else "#0b1122"
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_col};
                border: 1.5px solid {border_col};
                border-radius: 8px;
            }}
            QFrame:hover {{
                border: 1.5px solid #00f0ff;
                background-color: #111a2f;
            }}
        """)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.template)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing)

        w, h = self.width(), self.height()

        # 1. Mini CapCut Diamond/Sparkle Icon in top-left
        painter.setPen(Qt.NoPen)
        painter.setBrush(QtGui.QColor("#a78bfa"))
        diamond = QtGui.QPolygonF([
            QtCore.QPointF(9, 6),
            QtCore.QPointF(12, 9),
            QtCore.QPointF(9, 12),
            QtCore.QPointF(6, 9)
        ])
        painter.drawPolygon(diamond)

        # 2. Render stylized "ART" preview in center (identical to CapCut)
        sample_text = "ART"
        font_name = self.template.get("font", "Arial")
        font = QtGui.QFont(font_name, 14, QtGui.QFont.Black)
        painter.setFont(font)
        fm = QtGui.QFontMetrics(font)
        tw = fm.horizontalAdvance(sample_text)
        tx = max(4, (w - tw) // 2)
        ty = 36

        path = QtGui.QPainterPath()
        path.addText(tx, ty, font, sample_text)

        # Glow effect
        glow_col = self.template.get("glow_color")
        if glow_col:
            painter.setPen(QtGui.QPen(QtGui.QColor(glow_col), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        # 3D shadow
        if self.template.get("has_3d", False):
            shd_path = path.translated(2, 2)
            painter.fillPath(shd_path, QtGui.QColor(self.template.get("shadow_color", "#000000")))

        # Stroke outline
        stroke_col = QtGui.QColor(self.template.get("stroke_color", "#000000"))
        stroke_w = self.template.get("stroke_width", 3)
        painter.setPen(QtGui.QPen(stroke_col, stroke_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        # Fill text
        fill_col = QtGui.QColor(self.template.get("color", "#FFFFFF"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QtGui.QBrush(fill_col))
        painter.drawPath(path)

        # 3. Label name at bottom
        name_font = QtGui.QFont("Inter", 8, QtGui.QFont.Bold)
        painter.setFont(name_font)
        name_fm = QtGui.QFontMetrics(name_font)
        name = self.template.get("name_short", self.template.get("name", ""))
        nw = name_fm.horizontalAdvance(name)
        nx = max(2, (w - nw) // 2)
        painter.setPen(QtGui.QColor("#38bdf8" if self.is_selected else "#94a3b8"))
        painter.drawText(nx, h - 8, name)

        # 4. Checkmark circle if selected
        if self.is_selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QtGui.QColor("#0284c7"))
            painter.drawEllipse(QtCore.QRect(w - 14, 4, 10, 10))
            painter.setPen(QtGui.QPen(Qt.white, 1.5))
            painter.drawLine(w - 12, 9, w - 10, 11)
            painter.drawLine(w - 10, 11, w - 6, 6)

        painter.end()

class DraggablePresetButton(QPushButton):
    """Preset button that supports both normal clicking AND drag-and-drop directly onto the video preview canvas."""
    def __init__(self, text: str, preset_type: str, parent=None):
        super().__init__(text, parent)
        self.preset_type = preset_type
        self._drag_start_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or not self._drag_start_pos:
            super().mouseMoveEvent(event)
            return

        dist = (event.pos() - self._drag_start_pos).manhattanLength()
        if dist < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return

        drag = QtGui.QDrag(self)
        mime = QtCore.QMimeData()
        mime.setText(f"preset:{self.preset_type}")
        mime.setData("application/x-mask-preset", self.preset_type.encode("utf-8"))
        drag.setMimeData(mime)

        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.pos())

        if hasattr(drag, 'exec'):
            drag.exec(Qt.CopyAction | Qt.MoveAction)
        else:
            drag.exec_(Qt.CopyAction | Qt.MoveAction)
        self._drag_start_pos = None


class VideoEffectsWidget(QWidget):
    """Video effects control panel with Multi-Blur, Text Overlays, Logo, and Burn Subtitle"""
    
    blur_toggled = Signal(bool)
    blur_intensity_changed = Signal(int)
    blur_auto_speech_toggled = Signal(bool)
    reset_blur_requested = Signal()
    add_blur_requested = Signal()
    duplicate_blur_requested = Signal(str)
    delete_blur_requested = Signal(str)
    blur_selected = Signal(str)
    blur_rotation_changed = Signal(str, float)
    blur_type_changed = Signal(str, str)
    blur_full_video_toggled = Signal(bool)
    blur_mode_changed = Signal(str, str)
    blur_color_changed = Signal(str, str)
    blur_timing_changed = Signal(str, float, float)
    add_mask_preset_requested = Signal(str)

    text_toggled = Signal(bool)
    text_updated = Signal(str, str, int, str)
    text_bg_color_changed = Signal(str)
    text_position_changed = Signal(int, int)
    text_animation_changed = Signal(str, float, str, float, float, float)
    preview_text_anim_requested = Signal()
    add_text_requested = Signal()
    duplicate_text_requested = Signal(str)
    delete_text_requested = Signal(str)
    text_selected = Signal(str)
    text_outline_changed = Signal(str, int)
    text_shadow_changed = Signal(str, int)
    position_preset_requested = Signal(str)
    text_watermark_changed = Signal(bool, float, str, str, bool)
    logo_toggled = Signal(bool)
    logo_updated = Signal(str, int, int, int, int, bool)
    logo_full_video_toggled = Signal(bool)
    burn_subtitle_toggled = Signal(bool)
    burn_subtitle_updated = Signal(bool, str, int, str, float, str, float, object)
    burn_subtitle_position_changed = Signal(int, int)
    reset_burn_sub_requested = Signal()
    test_burn_sub_anim_requested = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_color = "#FFFFFF"
        self._current_bg_color = None
        self._current_outline_color = "#000000"
        self._current_outline_width = 2
        self._current_shadow_color = "#000000"
        self._current_shadow_offset = 3
        self._current_text = "ai 24 movei"
        self._current_size = 10
        blur_focus_tpl = next((tpl for tpl in SUBTITLE_STYLE_TEMPLATES if tpl.get("name_short") == "Blur Focus"), SUBTITLE_STYLE_TEMPLATES[0]) if SUBTITLE_STYLE_TEMPLATES else None
        self._current_sub_tpl = dict(blur_focus_tpl) if blur_focus_tpl else None
        self._burn_color = blur_focus_tpl.get("color", "#E2E8F0") if blur_focus_tpl else "#FFFFFF"
        self._text_x = 50
        self._text_y = 80
        self._text_start_sec = 0.0
        self._text_duration_sec = 0.0
        self._text_repeat_sec = 2.0
        self._is_watermark = False
        self._text_opacity = 100
        self._pos_zone = "mid"
        self._full_video = True
        self._text_speed_str = "medium"
        self.text_items = []
        self.active_text_id = "text_1"
        self._init_ui()

    def _init_ui(self):
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(2)

        self.effect_tabs = QTabWidget(self)
        tab_bar = self.effect_tabs.tabBar()
        tab_bar.setElideMode(Qt.ElideNone)
        tab_bar.setExpanding(False)
        tab_bar.setUsesScrollButtons(False)
        self.effect_tabs.setStyleSheet("""
            QTabWidget::pane {
                background-color: #0d1222;
                border: 1px solid #1a233a;
                border-radius: 8px;
                top: -1px;
            }
            QTabBar::tab {
                background-color: #090d18;
                color: #94a3b8;
                font-size: 11px;
                font-weight: 600;
                padding: 6px 14px;
                margin-right: 4px;
                border: 1px solid #1a233a;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                min-height: 18px;
            }
            QTabBar::tab:hover {
                background-color: #141c30;
                color: #e2e8f0;
            }
            QTabBar::tab:selected {
                background-color: #141c32;
                color: #38bdf8;
                border: 1px solid #243256;
                border-bottom: 2px solid #38bdf8;
            }
        """)

        # ---------------- 1. TAB: BLUR / MASK ----------------
        blur_tab = QWidget()
        blur_main_lay = QVBoxLayout(blur_tab)
        blur_main_lay.setContentsMargins(8, 6, 8, 6)
        blur_main_lay.setSpacing(6)

        # Row 1: Presets & Multi-Item Actions
        b_row1 = QHBoxLayout()
        b_row1.setSpacing(6)

        self.blur_checkbox = QCheckBox("Enable", self)
        self.blur_checkbox.setChecked(False)
        self.blur_checkbox.setStyleSheet("font-weight: 700; color: #38bdf8; margin-right: 4px;")
        self.blur_checkbox.toggled.connect(self._on_blur_toggled)
        b_row1.addWidget(self.blur_checkbox)

        sep_pre = QFrame(self)
        sep_pre.setFrameShape(QFrame.VLine)
        sep_pre.setStyleSheet("color: #334155; margin: 0 4px;")
        b_row1.addWidget(sep_pre)

        p_lbl = QLabel("Quick Presets:", self)
        p_lbl.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 11px;")
        b_row1.addWidget(p_lbl)

        self.btn_preset_logo = DraggablePresetButton("📌 បិត Logo", "logo", self)
        self.btn_preset_logo.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold;")
        self.btn_preset_logo.setToolTip("បិត Logo ចាស់ (អូសទៅដាក់លើវីដេអូ ឬចុចដើម្បីបន្ថែម / Inpaint)")
        self.btn_preset_logo.clicked.connect(lambda: self._on_preset_clicked("logo"))
        b_row1.addWidget(self.btn_preset_logo)

        self.btn_preset_sub = DraggablePresetButton("🎙️ បិត Subtitle", "subtitle", self)
        self.btn_preset_sub.setStyleSheet("background-color: #1e293b; color: #facc15; border: 1px solid #ca8a04; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold;")
        self.btn_preset_sub.setToolTip("បិត Subtitle ចាស់ (អូសទៅដាក់លើវីដេអូ ឬចុចដើម្បីបន្ថែម / Auto-Speech)")
        self.btn_preset_sub.clicked.connect(lambda: self._on_preset_clicked("subtitle"))
        b_row1.addWidget(self.btn_preset_sub)

        self.btn_preset_title = DraggablePresetButton("⏱️ បិត Title", "title", self)
        self.btn_preset_title.setStyleSheet("background-color: #1e293b; color: #4ade80; border: 1px solid #16a34a; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold;")
        self.btn_preset_title.setToolTip("បិត Title ចំណងជើងដើម (អូសទៅដាក់លើវីដេអូ ឬចុចដើម្បីបន្ថែម / 0s-15s)")
        self.btn_preset_title.clicked.connect(lambda: self._on_preset_clicked("title"))
        b_row1.addWidget(self.btn_preset_title)

        sep1 = QFrame(self)
        sep1.setFrameShape(QFrame.VLine)
        sep1.setStyleSheet("color: #334155;")
        b_row1.addWidget(sep1)

        self.add_blur_btn = QPushButton("+ New", self)
        self.add_blur_btn.setProperty("class", "btn-teal")
        self.add_blur_btn.setToolTip("Add new mask box (បង្កើតប្រអប់ Mask ថ្មី)")
        self.add_blur_btn.clicked.connect(self._on_add_blur_clicked)
        b_row1.addWidget(self.add_blur_btn)

        self.dup_blur_btn = QPushButton("Duplicate", self)
        self.dup_blur_btn.setProperty("class", "btn-gray")
        self.dup_blur_btn.setToolTip("Duplicate selected mask (ចម្លង Mask)")
        self.dup_blur_btn.clicked.connect(self._on_dup_blur_clicked)
        b_row1.addWidget(self.dup_blur_btn)

        self.del_blur_btn = QPushButton("Delete", self)
        self.del_blur_btn.setProperty("class", "btn-red")
        self.del_blur_btn.setToolTip("Delete selected mask (លុប Mask)")
        self.del_blur_btn.clicked.connect(self._on_del_blur_clicked)
        b_row1.addWidget(self.del_blur_btn)

        self.blur_selector = QComboBox(self)
        self.blur_selector.setMinimumWidth(110)
        self.blur_selector.setStyleSheet("""
            QComboBox { background-color: #0b1122; border: 1px solid #1e2942; border-radius: 4px; padding: 2px 6px; color: #38bdf8; font-weight: bold; }
        """)
        self.blur_selector.currentIndexChanged.connect(self._on_blur_selector_changed)
        b_row1.addWidget(self.blur_selector)

        b_row1.addStretch()
        blur_main_lay.addLayout(b_row1)

        # Row 2: Mask Mode, Timing Mode, Styling & Geometry
        b_row2 = QHBoxLayout()
        b_row2.setSpacing(6)

        b_row2.addWidget(QLabel("Mask Mode:", self))
        self.blur_type_combo = QComboBox(self)
        self.blur_type_combo.addItem("🌫️ Blur (Gaussian)", "blur")
        self.blur_type_combo.addItem("🧱 Pixelate (Mosaic)", "pixelate")
        self.blur_type_combo.addItem("⬛ Solid Cover", "solid")
        self.blur_type_combo.addItem("🌅 Gradient Cover", "gradient")
        self.blur_type_combo.addItem("🪄 Inpaint (Erase Logo)", "inpaint")
        self.blur_type_combo.addItem("🎨 Smart Fill (Blend)", "smart_fill")
        self.blur_type_combo.setStyleSheet("""
            QComboBox { background-color: #0b1122; border: 1px solid #1e2942; border-radius: 4px; padding: 2px 6px; color: #f8fafc; font-size: 11px; font-weight: 600; min-width: 145px; }
            QComboBox:hover { border-color: #38bdf8; }
        """)
        self.blur_type_combo.currentIndexChanged.connect(self._on_blur_type_combo_changed)
        b_row2.addWidget(self.blur_type_combo)

        # Color Swatch Button for Solid / Gradient Cover
        self.blur_color_btn = QPushButton("Color", self)
        self.blur_color_btn.setFixedSize(45, 22)
        self.blur_color_btn.setStyleSheet("background-color: #000000; color: #FFFFFF; font-size: 10px; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")
        self.blur_color_btn.setToolTip("ជ្រើសរើសពណ៌សម្រាប់ Solid Cover ឬ Gradient Cover")
        self.blur_color_btn.clicked.connect(self._pick_blur_color)
        b_row2.addWidget(self.blur_color_btn)

        sep2 = QFrame(self)
        sep2.setFrameShape(QFrame.VLine)
        sep2.setStyleSheet("color: #334155;")
        b_row2.addWidget(sep2)

        b_row2.addWidget(QLabel("Timing:", self))
        self.blur_mode_combo = QComboBox(self)
        self.blur_mode_combo.addItem("📌 ពេញវីដេអូ (Full Video)", "full")
        self.blur_mode_combo.addItem("🎙️ តាមសំឡេង (Auto-Speech)", "speech")
        self.blur_mode_combo.addItem("⏱️ កំណត់ម៉ោង (Custom Range)", "range")
        self.blur_mode_combo.setStyleSheet("""
            QComboBox { background-color: #0b1122; border: 1px solid #1e2942; border-radius: 4px; padding: 2px 6px; color: #4ade80; font-size: 11px; font-weight: 600; min-width: 155px; }
            QComboBox:hover { border-color: #4ade80; }
        """)
        self.blur_mode_combo.currentIndexChanged.connect(self._on_blur_mode_combo_changed)
        b_row2.addWidget(self.blur_mode_combo)

        self.blur_start_lbl = QLabel("Start:", self)
        self.blur_start_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        b_row2.addWidget(self.blur_start_lbl)

        self.blur_start_spin = QDoubleSpinBox(self)
        self.blur_start_spin.setRange(0.0, 99999.0)
        self.blur_start_spin.setSingleStep(0.5)
        self.blur_start_spin.setSuffix("s")
        self.blur_start_spin.setFixedWidth(60)
        self.blur_start_spin.valueChanged.connect(self._on_blur_timing_changed)
        b_row2.addWidget(self.blur_start_spin)

        self.blur_end_lbl = QLabel("End:", self)
        self.blur_end_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        b_row2.addWidget(self.blur_end_lbl)

        self.blur_end_spin = QDoubleSpinBox(self)
        self.blur_end_spin.setRange(0.0, 99999.0)
        self.blur_end_spin.setSingleStep(0.5)
        self.blur_end_spin.setValue(15.0)
        self.blur_end_spin.setSuffix("s")
        self.blur_end_spin.setFixedWidth(60)
        self.blur_end_spin.valueChanged.connect(self._on_blur_timing_changed)
        b_row2.addWidget(self.blur_end_spin)

        sep3 = QFrame(self)
        sep3.setFrameShape(QFrame.VLine)
        sep3.setStyleSheet("color: #334155;")
        b_row2.addWidget(sep3)

        b_row2.addWidget(QLabel("Power:", self))
        self.blur_slider = QSlider(Qt.Horizontal, self)
        self.blur_slider.setRange(1, 100)
        self.blur_slider.setValue(35)
        self.blur_slider.setFixedWidth(65)
        self.blur_slider.valueChanged.connect(self._on_blur_intensity_changed)
        b_row2.addWidget(self.blur_slider)

        self.blur_value_lbl = QLabel("35%", self)
        self.blur_value_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; min-width: 28px;")
        b_row2.addWidget(self.blur_value_lbl)

        b_row2.addWidget(QLabel("Rot:", self))
        self.blur_rot_slider = QSlider(Qt.Horizontal, self)
        self.blur_rot_slider.setRange(-180, 180)
        self.blur_rot_slider.setValue(0)
        self.blur_rot_slider.setFixedWidth(55)
        self.blur_rot_slider.valueChanged.connect(self._on_blur_rot_slider_changed)
        b_row2.addWidget(self.blur_rot_slider)

        self.blur_rot_lbl = QLabel("0°", self)
        self.blur_rot_lbl.setStyleSheet("color: #facc15; font-weight: bold; min-width: 25px;")
        b_row2.addWidget(self.blur_rot_lbl)

        self.reset_rot_btn = QPushButton("0°", self)
        self.reset_rot_btn.setProperty("class", "btn-gray")
        self.reset_rot_btn.setFixedSize(26, 22)
        self.reset_rot_btn.setToolTip("Reset rotation to 0°")
        self.reset_rot_btn.clicked.connect(self._on_reset_rot_clicked)
        b_row2.addWidget(self.reset_rot_btn)

        b_row2.addStretch()
        blur_main_lay.addLayout(b_row2)

        self.effect_tabs.addTab(blur_tab, "Blur / Mask")

        # ---------------- 2. TAB: TEXT OVERLAYS ----------------
        txt_tab = QWidget()
        txt_main_lay = QVBoxLayout(txt_tab)
        txt_main_lay.setContentsMargins(10, 8, 10, 8)
        txt_main_lay.setSpacing(8)

        # Row 1: Content & Typography (Enable, Text input, Font, Size, Color Picker & Swatches)
        t_row1 = QHBoxLayout()
        t_row1.setSpacing(8)

        self.text_checkbox = QCheckBox("Enable", self)
        self.text_checkbox.setChecked(True)
        self.text_checkbox.setStyleSheet("font-weight: 700; color: #38bdf8; font-size: 11px;")
        self.text_checkbox.setToolTip("បើក/បិទ Text Overlay លើវីដេអូ")
        self.text_checkbox.toggled.connect(self._on_text_toggled)
        t_row1.addWidget(self.text_checkbox)

        sep_t1 = QFrame(self)
        sep_t1.setFrameShape(QFrame.VLine)
        sep_t1.setStyleSheet("background-color: #1e2942; max-height: 20px;")
        t_row1.addWidget(sep_t1)

        lbl_text = QLabel("Text:", self)
        lbl_text.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row1.addWidget(lbl_text)

        self.text_input = QLineEdit("ai 24 movei", self)
        self.text_input.setMinimumWidth(180)
        self.text_input.setPlaceholderText("បញ្ចូលអក្សរចំណងជើងនៅទីនេះ...")
        self.text_input.setStyleSheet("""
            QLineEdit {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 4px 8px; color: #f8fafc; font-size: 12px;
            }
            QLineEdit:focus { border-color: #38bdf8; background-color: #0f172a; }
        """)
        self.text_input.textChanged.connect(self._on_text_changed)
        t_row1.addWidget(self.text_input, stretch=1)

        lbl_font = QLabel("Font:", self)
        lbl_font.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row1.addWidget(lbl_font)

        self.text_font_combo = QComboBox(self)
        self.text_font_combo.addItems(["Kantumruy Pro", "Noto Sans Khmer", "Battambang", "Khmer Sangam MN"])
        self.text_font_combo.setMinimumWidth(125)
        self.text_font_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 8px; color: #f1f5f9; font-size: 11px;
            }
            QComboBox:hover { border-color: #38bdf8; }
            QComboBox::drop-down { border: none; width: 14px; }
        """)
        self.text_font_combo.currentTextChanged.connect(self._on_font_changed)
        t_row1.addWidget(self.text_font_combo)

        lbl_size = QLabel("Size:", self)
        lbl_size.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row1.addWidget(lbl_size)

        self.size_spin = QSpinBox(self)
        self.size_spin.setRange(4, 120)
        self.size_spin.setValue(10)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setFixedWidth(64)
        self.size_spin.setStyleSheet("""
            QSpinBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 6px; color: #f1f5f9; font-size: 11px;
            }
        """)
        self.size_spin.valueChanged.connect(self._on_size_changed)
        t_row1.addWidget(self.size_spin)

        sep_t2 = QFrame(self)
        sep_t2.setFrameShape(QFrame.VLine)
        sep_t2.setStyleSheet("background-color: #1e2942; max-height: 20px;")
        t_row1.addWidget(sep_t2)

        lbl_color = QLabel("Color:", self)
        lbl_color.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row1.addWidget(lbl_color)

        # Quick Swatches (White, Yellow, Green, Cyan)
        swatches = [
            ("#FFFFFF", "White (ពណ៌ស)"),
            ("#FACC15", "Yellow (ពណ៌លឿង)"),
            ("#22C55E", "Green (ពណ៌បៃតង)"),
            ("#06B6D4", "Cyan (ពណ៌ផ្ទៃមេឃ)")
        ]
        for hex_col, tip in swatches:
            s_btn = QPushButton(self)
            s_btn.setFixedSize(22, 22)
            s_btn.setToolTip(tip)
            s_btn.setCursor(Qt.PointingHandCursor)
            s_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {hex_col};
                    border: 1.5px solid #475569;
                    border-radius: 11px;
                }}
                QPushButton:hover {{
                    border: 2px solid #38bdf8;
                }}
            """)
            s_btn.clicked.connect(lambda _, c=hex_col: self._set_text_color(c))
            t_row1.addWidget(s_btn)

        # Custom Color Picker Button
        self.color_btn = QPushButton("🎨", self)
        self.color_btn.setFixedSize(26, 24)
        self.color_btn.setToolTip("ជ្រើសរើសពណ៌ផ្សេងៗ (Custom Color Picker)")
        self.color_btn.setCursor(Qt.PointingHandCursor)
        self.color_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #ffffff; font-size: 12px;
                border: 1px solid #334155; border-radius: 6px;
            }
            QPushButton:hover { border-color: #38bdf8; background-color: #243256; }
        """)
        self.color_btn.clicked.connect(self._pick_color)
        t_row1.addWidget(self.color_btn)

        txt_main_lay.addLayout(t_row1)

        # Row 2: Anti-theft Motion, Speed, Position, Opacity, Full Video, and Apply
        t_row2 = QHBoxLayout()
        t_row2.setSpacing(8)

        lbl_motion = QLabel("Motion:", self)
        lbl_motion.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 11px;")
        lbl_motion.setToolTip("ចលនារត់អក្សរការពារគេលួច (Anti-theft Moving Watermark)")
        t_row2.addWidget(lbl_motion)

        self.text_motion_combo = QComboBox(self)
        self.text_motion_combo.addItems([
            "🔄 រត់ចុះឡើង (Up & Down)",
            "🌐 រត់ពាសពេញ (2D Float)",
            "⏹ នៅនឹង (Static)"
        ])
        self.text_motion_combo.setMinimumWidth(155)
        self.text_motion_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 8px; color: #38bdf8; font-size: 11px; font-weight: 600;
            }
            QComboBox:hover { border-color: #38bdf8; background-color: #111d38; }
            QComboBox::drop-down { border: none; width: 14px; }
        """)
        self.text_motion_combo.currentIndexChanged.connect(self._on_motion_combo_changed)
        t_row2.addWidget(self.text_motion_combo)

        lbl_spd = QLabel("Speed:", self)
        lbl_spd.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row2.addWidget(lbl_spd)

        self.text_speed_combo = QComboBox(self)
        self.text_speed_combo.addItems([
            "យឺត (Slow)",
            "ល្មម (Normal)",
            "លឿន (Fast)"
        ])
        self.text_speed_combo.setCurrentIndex(0)
        self.text_speed_combo.setMinimumWidth(95)
        self.text_speed_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 6px; color: #f1f5f9; font-size: 11px;
            }
            QComboBox:hover { border-color: #38bdf8; }
            QComboBox::drop-down { border: none; width: 14px; }
        """)
        self.text_speed_combo.currentIndexChanged.connect(self._on_text_anim_changed)
        t_row2.addWidget(self.text_speed_combo)

        sep_t3 = QFrame(self)
        sep_t3.setFrameShape(QFrame.VLine)
        sep_t3.setStyleSheet("background-color: #1e2942; max-height: 20px;")
        t_row2.addWidget(sep_t3)

        lbl_x = QLabel("X:", self)
        lbl_x.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row2.addWidget(lbl_x)

        self.pos_x = QSpinBox(self)
        self.pos_x.setRange(0, 2000)
        self.pos_x.setSingleStep(5)
        self.pos_x.setValue(getattr(self, '_text_x', 50))
        self.pos_x.setFixedWidth(54)
        self.pos_x.setStyleSheet("""
            QSpinBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 4px; color: #f1f5f9; font-size: 11px;
            }
            QSpinBox:hover { border-color: #38bdf8; }
        """)
        self.pos_x.setToolTip("ទីតាំងផ្តេក X (Horizontal Coordinate)")
        self.pos_x.valueChanged.connect(self._on_position_changed)
        t_row2.addWidget(self.pos_x)

        lbl_y = QLabel("Y:", self)
        lbl_y.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row2.addWidget(lbl_y)

        self.pos_y = QSpinBox(self)
        self.pos_y.setRange(0, 2000)
        self.pos_y.setSingleStep(5)
        self.pos_y.setValue(getattr(self, '_text_y', 80))
        self.pos_y.setFixedWidth(54)
        self.pos_y.setStyleSheet("""
            QSpinBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 4px; color: #f1f5f9; font-size: 11px;
            }
            QSpinBox:hover { border-color: #38bdf8; }
        """)
        self.pos_y.setToolTip("ទីតាំងបញ្ឈរ Y (Vertical Coordinate)")
        self.pos_y.valueChanged.connect(self._on_position_changed)
        t_row2.addWidget(self.pos_y)

        # Smart Alignment / Snap Menu
        self.align_combo = QComboBox(self)
        self.align_combo.addItem("🎯 Align...", "")
        self.align_combo.addItem("🎯 Center (ចំកណ្តាល)", "center")
        self.align_combo.addItem("⬆️ Top Center (លើ-កណ្តាល)", "top")
        self.align_combo.addItem("⬇️ Bottom Center (ក្រោម-កណ្តាល)", "bot")
        self.align_combo.addItem("↖️ Top Left (លើ-ឆ្វេង)", "top_left")
        self.align_combo.addItem("↗️ Top Right (លើ-ស្តាំ)", "top_right")
        self.align_combo.addItem("↙️ Bottom Left (ក្រោម-ឆ្វេង)", "bottom_left")
        self.align_combo.addItem("↘️ Bottom Right (ក្រោម-ស្តាំ)", "bottom_right")
        self.align_combo.addItem("⬅️ Left Center (ឆ្វេង-កណ្តាល)", "left")
        self.align_combo.addItem("➡️ Right Center (ស្តាំ-កណ្តាល)", "right")
        self.align_combo.setMinimumWidth(115)
        self.align_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 6px; color: #cbd5e1; font-size: 11px; font-weight: 600;
            }
            QComboBox:hover { border-color: #38bdf8; background-color: #111d38; color: #38bdf8; }
            QComboBox::drop-down { border: none; width: 14px; }
        """)
        self.align_combo.setToolTip("តម្រឹមទីតាំងស្វ័យប្រវត្តិតាមចំណុច Snap (Auto Align Presets)")
        self.align_combo.currentIndexChanged.connect(self._on_align_combo_changed)
        t_row2.addWidget(self.align_combo)

        sep_t4 = QFrame(self)
        sep_t4.setFrameShape(QFrame.VLine)
        sep_t4.setStyleSheet("background-color: #1e2942; max-height: 20px;")
        t_row2.addWidget(sep_t4)

        lbl_op = QLabel("Opacity:", self)
        lbl_op.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 11px;")
        t_row2.addWidget(lbl_op)

        self.opacity_spin = QSpinBox(self)
        self.opacity_spin.setRange(10, 100)
        self.opacity_spin.setSingleStep(5)
        self.opacity_spin.setValue(85)
        self.opacity_spin.setSuffix("%")
        self.opacity_spin.setFixedWidth(56)
        self.opacity_spin.setStyleSheet("""
            QSpinBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 6px;
                padding: 3px 4px; color: #f1f5f9; font-size: 11px;
            }
        """)
        self.opacity_spin.setToolTip("ភាពថ្លានៃអក្សរ (Transparency/Watermark)")
        self.opacity_spin.valueChanged.connect(self._on_opacity_changed)
        t_row2.addWidget(self.opacity_spin)

        self.full_video_checkbox = QCheckBox("Full Video", self)
        self.full_video_checkbox.setChecked(True)
        self.full_video_checkbox.setStyleSheet("font-weight: 600; color: #4ade80; font-size: 11px;")
        self.full_video_checkbox.setToolTip("រត់អត្ថបទពេញមួយវីដេអូ (Run for entire video)")
        self.full_video_checkbox.toggled.connect(self._on_full_video_toggled)
        t_row2.addWidget(self.full_video_checkbox)

        sep_t5 = QFrame(self)
        sep_t5.setFrameShape(QFrame.VLine)
        sep_t5.setStyleSheet("background-color: #1e2942; max-height: 20px;")
        t_row2.addWidget(sep_t5)

        apply_text_btn = QPushButton("✓ Apply Text", self)
        apply_text_btn.setProperty("class", "btn-primary")
        apply_text_btn.setToolTip("អនុវត្ត Text Overlay & ចលនារត់ទៅលើវីដេអូ")
        apply_text_btn.clicked.connect(self._apply_text)
        t_row2.addWidget(apply_text_btn)

        self.text_status = QLabel("Ready", self)
        self.text_status.setStyleSheet("color: #38bdf8; font-size: 11px; font-weight: 500;")
        t_row2.addWidget(self.text_status)

        t_row2.addStretch()
        txt_main_lay.addLayout(t_row2)

        # Smart defaults for outline and shadow
        self._current_outline_color = "#000000"
        self._current_outline_width = 2
        self._current_shadow_color = "#000000"
        self._current_shadow_offset = 3
        self._pos_zone = "top"

        # Compatibility dummy attributes if referenced elsewhere
        self.text_selector = QComboBox(self)
        self.text_selector.setVisible(False)
        self.watermark_checkbox = QCheckBox(self)
        self.watermark_checkbox.setVisible(False)
        self.dynamic_checkbox = QCheckBox(self)
        self.dynamic_checkbox.setVisible(False)
        self.duration_spin = QDoubleSpinBox(self)
        self.duration_spin.setVisible(False)

        self.effect_tabs.addTab(txt_tab, "Text Overlay")

        # ---------------- 3. TAB: LOGO OVERLAY ----------------
        logo_tab = QWidget()
        l_lay = QHBoxLayout(logo_tab)
        l_lay.setContentsMargins(12, 10, 12, 10)
        l_lay.setSpacing(10)

        self.logo_checkbox = QCheckBox("Enable", self)
        self.logo_checkbox.setChecked(False)
        self.logo_checkbox.toggled.connect(self._on_logo_toggled)
        l_lay.addWidget(self.logo_checkbox)

        self.logo_path_edit = QLineEdit("logo.png", self)
        self.logo_path_edit.setMinimumWidth(120)
        self.logo_path_edit.textChanged.connect(self._on_logo_path_changed)
        l_lay.addWidget(self.logo_path_edit)

        browse_btn = QPushButton("Browse...", self)
        browse_btn.setProperty("class", "btn-gray")
        browse_btn.clicked.connect(self._browse_logo)
        l_lay.addWidget(browse_btn)

        l_lay.addWidget(QLabel("X:", self))
        self.logo_x = QSpinBox(self)
        self.logo_x.setRange(0, 999)
        self.logo_x.setValue(233)
        self.logo_x.valueChanged.connect(self._on_logo_changed)
        l_lay.addWidget(self.logo_x)

        l_lay.addWidget(QLabel("Y:", self))
        self.logo_y = QSpinBox(self)
        self.logo_y.setRange(0, 999)
        self.logo_y.setValue(6)
        self.logo_y.valueChanged.connect(self._on_logo_changed)
        l_lay.addWidget(self.logo_y)

        l_lay.addWidget(QLabel("W:", self))
        self.logo_w = QSpinBox(self)
        self.logo_w.setRange(10, 500)
        self.logo_w.setValue(100)
        self.logo_w.valueChanged.connect(self._on_logo_changed)
        l_lay.addWidget(self.logo_w)

        l_lay.addWidget(QLabel("H:", self))
        self.logo_h = QSpinBox(self)
        self.logo_h.setRange(10, 500)
        self.logo_h.setValue(100)
        self.logo_h.valueChanged.connect(self._on_logo_changed)
        l_lay.addWidget(self.logo_h)

        self.logo_ratio_btn = QPushButton("Auto-Fit", self)
        self.logo_ratio_btn.setProperty("class", "btn-gray")
        self.logo_ratio_btn.setToolTip("Auto-fit height to original image aspect ratio (កំណត់សមាមាត្ររូបដើម កុំឱ្យខូចរាង)")
        self.logo_ratio_btn.clicked.connect(self._auto_fit_logo_ratio)
        l_lay.addWidget(self.logo_ratio_btn)

        self.logo_full_video_chk = QCheckBox("Full Video", self)
        self.logo_full_video_chk.setChecked(True)
        self.logo_full_video_chk.setStyleSheet("font-weight: 600; color: #38bdf8;")
        self.logo_full_video_chk.setToolTip("Run logo for full video (រត់ Logo ពេញមួយវីដេអូ)")
        self.logo_full_video_chk.toggled.connect(self._on_logo_full_video_toggled)
        l_lay.addWidget(self.logo_full_video_chk)

        self.remove_green_chk = QCheckBox("Key Green Screen", self)
        self.remove_green_chk.toggled.connect(self._on_logo_changed)
        l_lay.addWidget(self.remove_green_chk)

        apply_logo_btn = QPushButton("Apply Logo", self)
        apply_logo_btn.setProperty("class", "btn-primary")
        apply_logo_btn.clicked.connect(self._apply_logo)
        l_lay.addWidget(apply_logo_btn)

        self.logo_status = QLabel("Ready", self)
        self.logo_status.setStyleSheet("color: #38bdf8; font-size: 11px;")
        l_lay.addWidget(self.logo_status)

        self.effect_tabs.addTab(logo_tab, "Logo")

        # ---------------- 4. TAB: BURN SUBTITLE ----------------
        burn_tab = QWidget()
        burn_main_lay = QVBoxLayout(burn_tab)
        burn_main_lay.setContentsMargins(8, 6, 8, 6)
        burn_main_lay.setSpacing(6)

        # Row 1: CapCut-Style Filter Chips & Cards Gallery
        cat_row = QHBoxLayout()
        cat_row.setSpacing(6)
        cat_lbl = QLabel("🎨 Text Effects:", self)
        cat_lbl.setStyleSheet("color: #38bdf8; font-weight: 800; font-size: 11px;")
        cat_row.addWidget(cat_lbl)

        self._style_category_btns = {}
        categories = ["🔥 All", "✨ Trending", "💥 3D & Pop", "🌐 Glow & Neon", "🎬 Cinematic"]
        for cat in categories:
            c_btn = QPushButton(cat, self)
            c_btn.setCheckable(True)
            c_btn.setChecked(cat == "🔥 All")
            c_btn.setStyleSheet("""
                QPushButton {
                    background-color: #0b1122; color: #94a3b8; font-size: 10px; font-weight: bold;
                    border: 1px solid #1e293b; border-radius: 11px; padding: 2px 10px;
                }
                QPushButton:hover { color: #38bdf8; border-color: #38bdf8; }
                QPushButton:checked {
                    background-color: #1e293b; color: #38bdf8; border-color: #0284c7;
                }
            """)
            c_btn.clicked.connect(lambda checked, c=cat: self._filter_style_cards(c))
            cat_row.addWidget(c_btn)
            self._style_category_btns[cat] = c_btn

        cat_row.addStretch()
        burn_main_lay.addLayout(cat_row)

        # CapCut Visual Cards Gallery (Scrollable Horizontal Carousel)
        cards_scroll = QScrollArea(self)
        cards_scroll.setFixedHeight(82)
        cards_scroll.setWidgetResizable(True)
        cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cards_scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:horizontal {
                height: 4px;
                background: #090e17;
                border-radius: 2px;
            }
            QScrollBar::handle:horizontal {
                background: #1e293b;
                border-radius: 2px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #38bdf8;
            }
        """)

        cards_container = QWidget()
        cards_container.setStyleSheet("background: transparent;")
        self.cards_lay = QHBoxLayout(cards_container)
        self.cards_lay.setContentsMargins(0, 0, 0, 0)
        self.cards_lay.setSpacing(8)

        self._style_cards = []
        for i, tpl in enumerate(SUBTITLE_STYLE_TEMPLATES):
            is_sel = (tpl.get("name_short") == "Blur Focus" or tpl.get("anim") == "blur_dissolve")
            card = CapCutStyleCard(tpl, is_selected=is_sel, parent=self)
            card.clicked.connect(self._on_style_card_clicked)
            self.cards_lay.addWidget(card)
            self._style_cards.append((card, tpl))

        self.cards_lay.addStretch()
        cards_scroll.setWidget(cards_container)
        burn_main_lay.addWidget(cards_scroll)

        # Row 2: Subtitle Animations (CapCut Style)
        sub_row2 = QHBoxLayout()
        sub_row2.setSpacing(8)

        anim_lbl = QLabel("Animation:", self)
        anim_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 11px;")
        sub_row2.addWidget(anim_lbl)

        self.burn_sub_anim_combo = QComboBox(self)
        self.burn_sub_anim_combo.addItem("💥 Pop / Bounce (CapCut)", "pop_bounce")
        self.burn_sub_anim_combo.addItem("🌐 Cyber Field (Glitch/Neon)", "cyber_field")
        self.burn_sub_anim_combo.addItem("⚡ Frequency Decode (Hacker)", "frequency_decode")
        self.burn_sub_anim_combo.addItem("🌊 Wiping In (Smooth Wipe)", "wiping_in")
        self.burn_sub_anim_combo.addItem("✨ Magical Duel (Aura Pulse)", "magical_duel")
        self.burn_sub_anim_combo.addItem("🌫️ Blur Dissolve (Cinematic)", "blur_dissolve")
        self.burn_sub_anim_combo.addItem("🎵 Karaoke Pulse (Wave)", "karaoke_pulse")
        self.burn_sub_anim_combo.addItem("💢 Glitch Shake (Impact)", "glitch_shake")
        self.burn_sub_anim_combo.addItem("⏹ នៅនឹង (Static / None)", "none")
        self.burn_sub_anim_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 4px;
                padding: 3px 8px; color: #38bdf8; font-size: 11px; font-weight: bold; min-width: 175px;
            }
            QComboBox:hover { border-color: #38bdf8; }
        """)
        # Default to Blur Dissolve
        for i in range(self.burn_sub_anim_combo.count()):
            if self.burn_sub_anim_combo.itemData(i) == "blur_dissolve":
                self.burn_sub_anim_combo.setCurrentIndex(i)
                break
        self.burn_sub_anim_combo.currentIndexChanged.connect(self._on_burn_sub_changed)
        sub_row2.addWidget(self.burn_sub_anim_combo)

        speed_lbl = QLabel("Speed:", self)
        speed_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        sub_row2.addWidget(speed_lbl)

        self.burn_sub_speed_combo = QComboBox(self)
        self.burn_sub_speed_combo.addItem("0.20s (Fast)", 0.20)
        self.burn_sub_speed_combo.addItem("0.35s (Normal)", 0.35)
        self.burn_sub_speed_combo.addItem("0.50s (Smooth)", 0.50)
        self.burn_sub_speed_combo.setCurrentIndex(1)
        self.burn_sub_speed_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 4px;
                padding: 3px 8px; color: #94a3b8; font-size: 11px; font-weight: bold;
            }
        """)
        self.burn_sub_speed_combo.currentIndexChanged.connect(self._on_burn_sub_changed)
        sub_row2.addWidget(self.burn_sub_speed_combo)

        self.test_sub_anim_btn = QPushButton("👁️ Test FX", self)
        self.test_sub_anim_btn.setProperty("class", "btn-primary")
        self.test_sub_anim_btn.setStyleSheet("font-size: 11px; font-weight: bold; padding: 3px 10px;")
        self.test_sub_anim_btn.setToolTip("មើលចលនាអក្សរផ្ទាល់ (Live Animation Preview)")
        self.test_sub_anim_btn.clicked.connect(self._on_test_sub_anim_clicked)
        sub_row2.addWidget(self.test_sub_anim_btn)

        sub_row2.addStretch()
        burn_main_lay.addLayout(sub_row2)

        # Row 3: Standard Subtitle Controls & Typography (Clean spacing & no cutoffs)
        burn_lay = QHBoxLayout()
        burn_lay.setSpacing(8)

        self.burn_sub_checkbox = QCheckBox("Enable", self)
        self.burn_sub_checkbox.setChecked(True)
        self.burn_sub_checkbox.toggled.connect(self._on_burn_sub_toggled)
        burn_lay.addWidget(self.burn_sub_checkbox)

        burn_lay.addSpacing(6)

        burn_lay.addWidget(QLabel("Font:", self))
        self.burn_sub_font_combo = QComboBox(self)
        self.burn_sub_font_combo.addItems(AVAILABLE_KHMER_FONTS)
        self.burn_sub_font_combo.setStyleSheet("""
            QComboBox {
                background-color: #0b1122; border: 1px solid #1e2942; border-radius: 4px;
                padding: 3px 8px; color: #f8fafc; font-size: 11px; font-weight: 500; min-width: 130px;
            }
            QComboBox:hover { border-color: #38bdf8; }
        """)
        # Match Blur Focus font Noto Sans Khmer if available
        f_idx = self.burn_sub_font_combo.findText("Noto Sans Khmer")
        if f_idx >= 0:
            self.burn_sub_font_combo.setCurrentIndex(f_idx)
        self.burn_sub_font_combo.currentTextChanged.connect(self._on_burn_sub_changed)
        burn_lay.addWidget(self.burn_sub_font_combo)

        burn_lay.addWidget(QLabel("Size:", self))
        self.burn_sub_size_spin = QSpinBox(self)
        self.burn_sub_size_spin.setRange(8, 80)
        self.burn_sub_size_spin.setValue(13)
        self.burn_sub_size_spin.valueChanged.connect(self._on_burn_sub_changed)
        burn_lay.addWidget(self.burn_sub_size_spin)

        burn_lay.addWidget(QLabel("Color:", self))
        self.burn_sub_color_btn = QPushButton("🎨", self)
        self.burn_sub_color_btn.setFixedSize(30, 24)
        self.burn_sub_color_btn.setStyleSheet("background-color: #E2E8F0; border: 1.5px solid #64748b; border-radius: 5px;")
        self.burn_sub_color_btn.setToolTip("ជ្រើសរើសពណ៌អក្សរ (Color Picker)")
        self.burn_sub_color_btn.clicked.connect(self._pick_burn_sub_color)
        burn_lay.addWidget(self.burn_sub_color_btn)

        burn_lay.addWidget(QLabel("BG Box:", self))
        self.burn_sub_bg_slider = QSlider(Qt.Horizontal, self)
        self.burn_sub_bg_slider.setRange(0, 100)
        self.burn_sub_bg_slider.setValue(5)
        self.burn_sub_bg_slider.setFixedWidth(70)
        self.burn_sub_bg_slider.setToolTip("កម្រិតពណ៌ប្រអប់ខ្មៅពីក្រោយអក្សរ (5% = ស្រមោលប្រអប់ខ្មៅស្ដើង)")
        self.burn_sub_bg_slider.valueChanged.connect(self._on_burn_sub_changed)
        burn_lay.addWidget(self.burn_sub_bg_slider)

        burn_lay.addWidget(QLabel("Pos Y:", self))
        self.burn_sub_pos_slider = QSlider(Qt.Horizontal, self)
        self.burn_sub_pos_slider.setRange(5, 95)
        self.burn_sub_pos_slider.setValue(85)
        self.burn_sub_pos_slider.setFixedWidth(70)
        self.burn_sub_pos_slider.valueChanged.connect(self._on_burn_sub_pos_changed)
        burn_lay.addWidget(self.burn_sub_pos_slider)

        self.burn_sub_pos_val_lbl = QLabel("85%", self)
        self.burn_sub_pos_val_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; min-width: 30px;")
        burn_lay.addWidget(self.burn_sub_pos_val_lbl)

        reset_sub_pos_btn = QPushButton("Reset Pos", self)
        reset_sub_pos_btn.setProperty("class", "btn-gray")
        reset_sub_pos_btn.setToolTip("Reset subtitle to bottom center (85%)")
        reset_sub_pos_btn.clicked.connect(self._reset_burn_sub_pos)
        burn_lay.addWidget(reset_sub_pos_btn)

        self.burn_sub_status = QLabel("Enabled", self)
        self.burn_sub_status.setStyleSheet("color: #10b981; font-weight: bold; font-size: 11px;")
        burn_lay.addWidget(self.burn_sub_status)

        drag_hint = QLabel("Drag subtitle on video", self)
        drag_hint.setStyleSheet("color: #64748b; font-size: 11px;")
        burn_lay.addWidget(drag_hint)

        burn_lay.addStretch()
        burn_main_lay.addLayout(burn_lay)

        self.effect_tabs.addTab(burn_tab, "Burn Subtitle")

        main_lay.addWidget(self.effect_tabs)

    # ==================== BLUR SIGNALS ====================
    def _on_blur_toggled(self, checked: bool):
        self.blur_toggled.emit(checked)
        self.blur_value_lbl.setText(f"{self.blur_slider.value()}%" if checked else "Disabled")
        if checked and hasattr(self, 'add_blur_requested') and self.blur_selector.count() == 0:
            self.add_blur_requested.emit()

    def _on_add_blur_clicked(self):
        self.blur_checkbox.setChecked(True)
        self.add_blur_requested.emit()

    def _on_dup_blur_clicked(self):
        cur_id = self.blur_selector.currentData()
        if cur_id:
            self.duplicate_blur_requested.emit(cur_id)

    def _on_del_blur_clicked(self):
        cur_id = self.blur_selector.currentData()
        if cur_id:
            self.delete_blur_requested.emit(cur_id)

    def _on_blur_selector_changed(self, idx):
        cur_id = self.blur_selector.currentData()
        if cur_id:
            self.blur_selected.emit(cur_id)

    def _on_preset_clicked(self, p_type: str):
        self.blur_checkbox.setChecked(True)
        self.add_mask_preset_requested.emit(p_type)

    def _on_blur_type_combo_changed(self, idx):
        cur_id = self.blur_selector.currentData()
        if cur_id:
            m_type = self.blur_type_combo.currentData()
            self.blur_type_changed.emit(cur_id, m_type)
            is_color = m_type in ("solid", "gradient")
            self.blur_color_btn.setVisible(is_color)

    def _on_blur_mode_combo_changed(self, idx):
        cur_id = self.blur_selector.currentData()
        if cur_id:
            mode = self.blur_mode_combo.currentData()
            self.blur_mode_changed.emit(cur_id, mode)
            is_range = (mode == "range")
            self.blur_start_lbl.setVisible(is_range)
            self.blur_start_spin.setVisible(is_range)
            self.blur_end_lbl.setVisible(is_range)
            self.blur_end_spin.setVisible(is_range)

    def _on_blur_timing_changed(self):
        cur_id = self.blur_selector.currentData()
        if cur_id:
            st = float(self.blur_start_spin.value())
            et = float(self.blur_end_spin.value())
            self.blur_timing_changed.emit(cur_id, st, et)

    def _pick_blur_color(self):
        cur_id = self.blur_selector.currentData()
        col = QColorDialog.getColor(QtGui.QColor("#000000"), self, "ជ្រើសរើសពណ៌បិត Mask (Cover Color)")
        if col.isValid():
            hex_c = col.name()
            self.blur_color_btn.setStyleSheet(f"background-color: {hex_c}; color: {'#000' if col.lightness() > 128 else '#fff'}; font-size: 10px; font-weight: bold; border-radius: 4px;")
            if cur_id:
                self.blur_color_changed.emit(cur_id, hex_c)

    def _on_blur_intensity_changed(self, value: int):
        self.blur_value_lbl.setText(f"{value}%")
        self.blur_intensity_changed.emit(value)

    def _on_blur_rot_slider_changed(self, val):
        self.blur_rot_lbl.setText(f"{val}°")
        cur_id = self.blur_selector.currentData()
        if cur_id:
            self.blur_rotation_changed.emit(cur_id, float(val))

    def _on_reset_rot_clicked(self):
        self.blur_rot_slider.setValue(0)

    def _on_blur_auto_speech_toggled(self, checked: bool):
        self.blur_auto_speech_toggled.emit(checked)

    def _on_blur_full_video_toggled(self, checked: bool):
        self.blur_full_video_toggled.emit(checked)

    def _reset_blur_position(self):
        self.reset_blur_requested.emit()

    def sync_blur_items(self, blur_items: list, active_id: str):
        self.blur_selector.blockSignals(True)
        self.blur_selector.clear()
        active_idx = 0
        for i, b in enumerate(blur_items):
            m_type = b.get("type", "blur")
            self.blur_selector.addItem(f"🔍 {b.get('name', 'Mask')} [{m_type}]", b.get("id"))
            if b.get("id") == active_id:
                active_idx = i
        if blur_items:
            self.blur_checkbox.blockSignals(True)
            self.blur_checkbox.setChecked(True)
            self.blur_checkbox.blockSignals(False)

            self.blur_selector.setCurrentIndex(active_idx)
            cur_blur = next((b for b in blur_items if b.get("id") == active_id), blur_items[0])

            # 1. Mask Mode (type)
            m_type = cur_blur.get("type", "blur")
            self.blur_type_combo.blockSignals(True)
            t_idx = self.blur_type_combo.findData(m_type)
            if t_idx >= 0:
                self.blur_type_combo.setCurrentIndex(t_idx)
            self.blur_type_combo.blockSignals(False)
            self.blur_color_btn.setVisible(m_type in ("solid", "gradient"))

            # 2. Cover Color
            hex_c = cur_blur.get("solid_color", "#000000")
            q_col = QtGui.QColor(hex_c)
            self.blur_color_btn.setStyleSheet(f"background-color: {hex_c}; color: {'#000' if q_col.lightness() > 128 else '#fff'}; font-size: 10px; font-weight: bold; border-radius: 4px;")

            # 3. Timing Mode
            mode = cur_blur.get("mode")
            if not mode:
                if cur_blur.get("auto_speech"):
                    mode = "speech"
                elif not cur_blur.get("full_video", True):
                    mode = "range"
                else:
                    mode = "full"
            self.blur_mode_combo.blockSignals(True)
            m_idx = self.blur_mode_combo.findData(mode)
            if m_idx >= 0:
                self.blur_mode_combo.setCurrentIndex(m_idx)
            self.blur_mode_combo.blockSignals(False)

            is_range = (mode == "range")
            self.blur_start_lbl.setVisible(is_range)
            self.blur_start_spin.setVisible(is_range)
            self.blur_end_lbl.setVisible(is_range)
            self.blur_end_spin.setVisible(is_range)

            self.blur_start_spin.blockSignals(True)
            self.blur_start_spin.setValue(float(cur_blur.get("start_sec", 0.0)))
            self.blur_start_spin.blockSignals(False)

            self.blur_end_spin.blockSignals(True)
            self.blur_end_spin.setValue(float(cur_blur.get("end_sec", 15.0)))
            self.blur_end_spin.blockSignals(False)

            # 4. Power / Intensity
            self.blur_slider.blockSignals(True)
            self.blur_slider.setValue(int(cur_blur.get("intensity", 35)))
            self.blur_value_lbl.setText(f"{int(cur_blur.get('intensity', 35))}%")
            self.blur_slider.blockSignals(False)

            # 5. Rotation
            self.blur_rot_slider.blockSignals(True)
            rot = int(cur_blur.get("rotation", 0))
            self.blur_rot_slider.setValue(rot)
            self.blur_rot_lbl.setText(f"{rot}°")
            self.blur_rot_slider.blockSignals(False)
        self.blur_selector.blockSignals(False)

    # ==================== TEXT OVERLAY SIGNALS ====================
    def _on_text_category_changed(self, cat: str):
        for btn in self.text_tpl_buttons:
            b_cat = btn.property("category")
            btn.setVisible(cat.startswith("All") or b_cat == cat)

    def _on_add_text_clicked(self):
        self.text_checkbox.setChecked(True)
        self.add_text_requested.emit()

    def _on_dup_text_clicked(self):
        cur_id = self.text_selector.currentData()
        if cur_id:
            self.duplicate_text_requested.emit(cur_id)

    def _on_del_text_clicked(self):
        cur_id = self.text_selector.currentData()
        if cur_id:
            self.delete_text_requested.emit(cur_id)

    def _on_text_selector_changed(self, idx):
        cur_id = self.text_selector.currentData()
        if cur_id:
            self.text_selected.emit(cur_id)

    def _set_text_color(self, hex_color: str):
        self._current_color = hex_color
        self._update_swatch_buttons()
        if self.text_checkbox.isChecked():
            self.text_updated.emit(self._current_text, hex_color, self._current_size, self._current_font)
        self.text_status.setText(f"Color: {hex_color}")

    def _update_pos_buttons_ui(self, active_zone: str):
        self._pos_zone = active_zone.lower() if active_zone else "mid"
        z = self._pos_zone
        
        act_bg = "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0284c7, stop:1 #0369a1);"
        act_fg = "color: #ffffff; font-weight: 800; border-color: #38bdf8;"
        inact_bg = "background-color: #111827;"
        inact_fg = "color: #94a3b8; font-weight: 600; border-color: #1f293d;"
        
        configs = []
        if hasattr(self, 'pos_top_btn'):
            configs.append((self.pos_top_btn, "top", "border-top-left-radius: 5px; border-bottom-left-radius: 5px; border-right: none;"))
        if hasattr(self, 'pos_mid_btn'):
            configs.append((self.pos_mid_btn, "mid", "border-radius: 0px; border-right: none;"))
        if hasattr(self, 'pos_bot_btn'):
            configs.append((self.pos_bot_btn, "bot", "border-top-right-radius: 5px; border-bottom-right-radius: 5px;"))
        if hasattr(self, 'pos_full_btn'):
            configs.append((self.pos_full_btn, "full", "border-radius: 5px;"))
        
        for btn, name, corner in configs:
            is_active = (z == name or (name == "mid" and z in ["mid", "middle"]) or (name == "bot" and z in ["bot", "bottom"]))
            bg = act_bg if is_active else inact_bg
            fg = act_fg if is_active else inact_fg
            btn.setStyleSheet(f"""
                QPushButton {{
                    {bg}
                    {fg}
                    {corner}
                    border-width: 1px;
                    border-style: solid;
                    padding: 3px 10px;
                    font-size: 11px;
                    min-width: 44px;
                    max-height: 24px;
                }}
                QPushButton:hover {{
                    border-color: #38bdf8;
                    color: #e0f2fe;
                }}
            """)

    def _update_swatch_buttons(self):
        c = self._current_color or "#FFFFFF"
        fg_c = '#000000' if c.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0', '#00F0FF', '#FFEDD5', '#F3E8FF'] else '#ffffff'
        if hasattr(self, 'color_btn'):
            self.color_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {c}; color: {fg_c}; font-weight: 800; font-size: 11px;
                    border: 2px solid #64748b; border-radius: 5px; min-width: 26px; max-width: 26px;
                    min-height: 22px; max-height: 22px; padding: 0;
                }}
                QPushButton:hover {{ border-color: #38bdf8; }}
            """)
            self.color_btn.setToolTip(f"Font Color: {c} (Click to change)")

        oc = self._current_outline_color or "#000000"
        fg_oc = '#000000' if oc.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0', '#00F0FF'] else '#ffffff'
        if hasattr(self, 'outline_color_btn'):
            self.outline_color_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {oc}; color: {fg_oc}; font-weight: 800; font-size: 10px;
                    border: 2px solid #64748b; border-radius: 5px; min-width: 24px; max-width: 24px;
                    min-height: 22px; max-height: 22px; padding: 0;
                }}
                QPushButton:hover {{ border-color: #38bdf8; }}
            """)
            self.outline_color_btn.setToolTip(f"Outline Color: {oc} (Click to change)")

        sc = self._current_shadow_color or "#000000"
        fg_sc = '#000000' if sc.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0', '#00F0FF'] else '#ffffff'
        if hasattr(self, 'shadow_color_btn'):
            self.shadow_color_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {sc}; color: {fg_sc}; font-weight: 800; font-size: 10px;
                    border: 2px solid #64748b; border-radius: 5px; min-width: 24px; max-width: 24px;
                    min-height: 22px; max-height: 22px; padding: 0;
                }}
                QPushButton:hover {{ border-color: #38bdf8; }}
            """)
            self.shadow_color_btn.setToolTip(f"Shadow Color: {sc} (Click to change)")

    def _pick_outline_color(self):
        initial = QtGui.QColor(self._current_outline_color) if hasattr(QtGui, 'QColor') else QtGui.QColor("#000000")
        color = QColorDialog.getColor(initial, self, "Select Outline Color")
        if color.isValid():
            self._current_outline_color = color.name()
            self._update_swatch_buttons()
            self._emit_outline_shadow()

    def _pick_shadow_color(self):
        initial = QtGui.QColor(self._current_shadow_color) if hasattr(QtGui, 'QColor') else QtGui.QColor("#000000")
        color = QColorDialog.getColor(initial, self, "Select Shadow Color")
        if color.isValid():
            self._current_shadow_color = color.name()
            self._update_swatch_buttons()
            self._emit_outline_shadow()

    def _on_outline_changed(self, val: int):
        self._current_outline_width = val
        self._emit_outline_shadow()

    def _on_shadow_changed(self, val: int):
        self._current_shadow_offset = val
        self._emit_outline_shadow()

    def _emit_outline_shadow(self):
        self.text_outline_changed.emit(self._current_outline_color, self._current_outline_width)
        self.text_shadow_changed.emit(self._current_shadow_color, self._current_shadow_offset)

    def _on_motion_combo_changed(self, idx: int):
        if idx == 0:
            # 🔄 រត់ចុះឡើង (Up & Down)
            self._pos_zone = "up_down"
            if hasattr(self, 'dynamic_checkbox'):
                self.dynamic_checkbox.setChecked(True)
            self.position_preset_requested.emit("up_down")
        elif idx == 1:
            # 🌐 រត់ពាសពេញ (2D Float)
            self._pos_zone = "full"
            if hasattr(self, 'dynamic_checkbox'):
                self.dynamic_checkbox.setChecked(True)
            self.position_preset_requested.emit("full")
        else:
            # ⏹ នៅនឹង (Static)
            self._pos_zone = "mid"
            if hasattr(self, 'dynamic_checkbox'):
                self.dynamic_checkbox.setChecked(False)
            self.position_preset_requested.emit("mid")
        self._emit_text_animation()
        self.preview_text_anim_requested.emit()

    def _on_align_combo_changed(self, idx: int):
        if idx <= 0:
            return
        preset = self.align_combo.itemData(idx)
        if preset:
            if hasattr(self, 'text_motion_combo'):
                self.text_motion_combo.blockSignals(True)
                self.text_motion_combo.setCurrentIndex(2)  # Static
                self.text_motion_combo.blockSignals(False)
            if hasattr(self, 'dynamic_checkbox'):
                self.dynamic_checkbox.setChecked(False)
            self._pos_zone = preset
            self.position_preset_requested.emit(preset)
            self._emit_text_animation()
            self.preview_text_anim_requested.emit()
        self.align_combo.blockSignals(True)
        self.align_combo.setCurrentIndex(0)
        self.align_combo.blockSignals(False)

    def _on_pos_top_clicked(self):
        self._pos_zone = "top"
        if hasattr(self, 'text_motion_combo'):
            self.text_motion_combo.blockSignals(True)
            self.text_motion_combo.setCurrentIndex(2)  # Static
            self.text_motion_combo.blockSignals(False)
        if hasattr(self, 'dynamic_checkbox'):
            self.dynamic_checkbox.setChecked(False)
        self._update_pos_buttons_ui("top")
        self.position_preset_requested.emit("top")
        self._emit_text_animation()
        self.preview_text_anim_requested.emit()

    def _on_pos_mid_clicked(self):
        self._pos_zone = "mid"
        if hasattr(self, 'text_motion_combo'):
            self.text_motion_combo.blockSignals(True)
            self.text_motion_combo.setCurrentIndex(2)  # Static
            self.text_motion_combo.blockSignals(False)
        if hasattr(self, 'dynamic_checkbox'):
            self.dynamic_checkbox.setChecked(False)
        self._update_pos_buttons_ui("mid")
        self.position_preset_requested.emit("mid")
        self._emit_text_animation()
        self.preview_text_anim_requested.emit()

    def _on_pos_bot_clicked(self):
        self._pos_zone = "bot"
        if hasattr(self, 'text_motion_combo'):
            self.text_motion_combo.blockSignals(True)
            self.text_motion_combo.setCurrentIndex(2)  # Static
            self.text_motion_combo.blockSignals(False)
        if hasattr(self, 'dynamic_checkbox'):
            self.dynamic_checkbox.setChecked(False)
        self._update_pos_buttons_ui("bot")
        self.position_preset_requested.emit("bot")
        self._emit_text_animation()
        self.preview_text_anim_requested.emit()

    def _on_pos_full_clicked(self):
        self._pos_zone = "full"
        if hasattr(self, 'text_motion_combo'):
            self.text_motion_combo.blockSignals(True)
            self.text_motion_combo.setCurrentIndex(1)  # 2D Float
            self.text_motion_combo.blockSignals(False)
        self._update_pos_buttons_ui("full")
        if hasattr(self, 'dynamic_checkbox') and not self.dynamic_checkbox.isChecked():
            self.dynamic_checkbox.setChecked(True)
        self.position_preset_requested.emit("full")
        self._emit_text_animation()
        self.preview_text_anim_requested.emit()

    def _on_dynamic_toggled(self, checked: bool):
        self._emit_text_animation()

    def _on_watermark_toggled(self, checked: bool):
        self._is_watermark = checked
        if checked:
            if not self.text_checkbox.isChecked():
                self.text_checkbox.setChecked(True)
            if self.opacity_spin.value() == 100:
                self.opacity_spin.setValue(60)
            if hasattr(self, 'dynamic_checkbox') and not self.dynamic_checkbox.isChecked():
                self.dynamic_checkbox.setChecked(True)
            self.full_video_checkbox.setChecked(True)
        else:
            if self.opacity_spin.value() == 60:
                self.opacity_spin.setValue(100)
        self._emit_text_animation()

    def _on_opacity_changed(self, val: int):
        self._text_opacity = val
        self._emit_text_animation()

    def _on_full_video_toggled(self, checked: bool):
        self._full_video = checked
        self.duration_spin.setEnabled(not checked)
        self._emit_text_animation()

    def sync_text_items(self, text_items: list, active_id: str):
        self.text_items = text_items or []
        self.active_text_id = active_id
        self.text_selector.blockSignals(True)
        self.text_selector.clear()
        active_idx = 0
        for i, it in enumerate(self.text_items):
            clean_preview = (it.get("text", "Text")[:15] + "...") if len(it.get("text", "")) > 15 else it.get("text", "Text")
            self.text_selector.addItem(f"{it.get('name', 'Text')}: {clean_preview}", it.get("id"))
            if it.get("id") == active_id:
                active_idx = i
        if self.text_items:
            self.text_selector.setCurrentIndex(active_idx)
            cur_item = next((it for it in self.text_items if it.get("id") == active_id), self.text_items[0])
            
            self._current_text = cur_item.get("text", "")
            self._current_font = cur_item.get("font", "Kantumruy Pro")
            self._current_size = int(cur_item.get("size", 12))
            self._current_color = cur_item.get("color", "#FFFFFF")
            self._current_bg_color = cur_item.get("bg_color", "#000000")
            self._current_outline_color = cur_item.get("outline_color", "#000000")
            self._current_outline_width = int(cur_item.get("outline_width", 2))
            self._current_shadow_color = cur_item.get("shadow_color", "#000000")
            self._current_shadow_offset = int(cur_item.get("shadow_offset", 3))
            self._text_start_sec = float(cur_item.get("start", 0.0))
            self._text_duration_sec = float(cur_item.get("duration", 0.0))
            self._text_repeat_sec = float(cur_item.get("repeat_sec", 2.0))
            self._is_watermark = bool(cur_item.get("is_watermark", False))
            self._text_opacity = int(cur_item.get("opacity", 1.0) * 100)
            self._pos_zone = cur_item.get("pos_zone", "mid")
            self._full_video = bool(cur_item.get("full_video", True))
            self._text_speed_str = cur_item.get("speed_str", "medium")

            self.text_input.blockSignals(True)
            self.text_input.setText(self._current_text)
            self.text_input.blockSignals(False)

            self.size_spin.blockSignals(True)
            self.size_spin.setValue(self._current_size)
            self.size_spin.blockSignals(False)

            self.text_font_combo.blockSignals(True)
            idx_f = self.text_font_combo.findText(self._current_font)
            if idx_f >= 0:
                self.text_font_combo.setCurrentIndex(idx_f)
            self.text_font_combo.blockSignals(False)

            fg_c = '#000' if self._current_color.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0', '#00F0FF', '#FFEDD5', '#F3E8FF'] else '#fff'
            self.color_btn.setStyleSheet(f"background-color: {self._current_color}; color: {fg_c}; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")

            if hasattr(self, 'bg_color_btn') and self.bg_color_btn:
                fg_bg = '#fff' if self._current_bg_color and self._current_bg_color.upper() != '#FFFFFF' else '#000'
                self.bg_color_btn.setStyleSheet(f"background-color: {self._current_bg_color or '#000000'}; color: {fg_bg}; font-size: 14px; border: 1px solid #64748b; border-radius: 4px;")

            if hasattr(self, 'watermark_checkbox'):
                self.watermark_checkbox.blockSignals(True)
                self.watermark_checkbox.setChecked(self._is_watermark)
                self.watermark_checkbox.blockSignals(False)

            if hasattr(self, 'opacity_spin'):
                self.opacity_spin.blockSignals(True)
                self.opacity_spin.setValue(self._text_opacity)
                self.opacity_spin.blockSignals(False)

            if hasattr(self, 'full_video_checkbox'):
                self.full_video_checkbox.blockSignals(True)
                self.full_video_checkbox.setChecked(self._full_video)
                self.full_video_checkbox.blockSignals(False)

            if hasattr(self, 'duration_spin'):
                self.duration_spin.blockSignals(True)
                self.duration_spin.setValue(float(cur_item.get("duration", 5.0)))
                self.duration_spin.setEnabled(not self._full_video)
                self.duration_spin.blockSignals(False)

            if hasattr(self, 'text_motion_combo'):
                self.text_motion_combo.blockSignals(True)
                cur_zone = cur_item.get("pos_zone", "up_down")
                cur_anim = cur_item.get("anim_type", "dynamic")
                if cur_anim in ["dynamic", "dynamic_watermark", "watermark"]:
                    if cur_zone in ["up_down", "vertical", "bounce", "run_up_down"]:
                        self.text_motion_combo.setCurrentIndex(0)
                    else:
                        self.text_motion_combo.setCurrentIndex(1)
                else:
                    self.text_motion_combo.setCurrentIndex(2)
                self.text_motion_combo.blockSignals(False)

            if hasattr(self, 'text_speed_combo'):
                self.text_speed_combo.blockSignals(True)
                sp_txt = self._text_speed_str.lower()
                if "fast" in sp_txt or "លឿន" in sp_txt:
                    self.text_speed_combo.setCurrentIndex(2)
                elif "slow" in sp_txt or "យឺត" in sp_txt:
                    self.text_speed_combo.setCurrentIndex(0)
                else:
                    self.text_speed_combo.setCurrentIndex(1)
                self.text_speed_combo.blockSignals(False)

            if hasattr(self, 'dynamic_checkbox'):
                self.dynamic_checkbox.blockSignals(True)
                cur_anim = cur_item.get("anim_type", "pop")
                self.dynamic_checkbox.setChecked(cur_anim in ["dynamic", "dynamic_watermark", "watermark"])
                self.dynamic_checkbox.blockSignals(False)

            if hasattr(self, 'outline_spin'):
                self.outline_spin.blockSignals(True)
                self.outline_spin.setValue(self._current_outline_width)
                self.outline_spin.blockSignals(False)

            if hasattr(self, 'shadow_spin'):
                self.shadow_spin.blockSignals(True)
                self.shadow_spin.setValue(self._current_shadow_offset)
                self.shadow_spin.blockSignals(False)

            self._update_swatch_buttons()
            self._update_pos_buttons_ui(self._pos_zone)

            self._text_x = int(cur_item.get("x", 50))
            self._text_y = int(cur_item.get("y", 80))
            if hasattr(self, 'pos_x'):
                self.pos_x.blockSignals(True)
                self.pos_x.setValue(self._text_x)
                self.pos_x.blockSignals(False)
            if hasattr(self, 'pos_y'):
                self.pos_y.blockSignals(True)
                self.pos_y.setValue(self._text_y)
                self.pos_y.blockSignals(False)
        self.text_selector.blockSignals(False)

    def _on_text_toggled(self, checked: bool):
        self.text_toggled.emit(checked)
        self.text_status.setText("Status: Enabled ✓" if checked else "Status: Disabled")
        if checked:
            self._apply_text()

    def _on_text_changed(self, text: str):
        self._current_text = text
        if self.text_checkbox.isChecked():
            self.text_updated.emit(text, self._current_color, self._current_size, self._current_font)

    def _on_font_changed(self, font_name: str):
        self._current_font = font_name
        self.text_status.setText(f"Font: {font_name}")
        if self.text_checkbox.isChecked():
            self.text_updated.emit(self._current_text, self._current_color, self._current_size, font_name)

    def _on_size_changed(self, size: int):
        self._current_size = size
        if hasattr(self, 'text_items') and self.text_items:
            cur = next((it for it in self.text_items if it.get("id") == self.active_text_id), self.text_items[0])
            if cur:
                cur["size"] = size
                cur["size_pt"] = size
        if self.text_checkbox.isChecked():
            self.text_updated.emit(self._current_text, self._current_color, size, self._current_font)

    def _on_position_changed(self):
        x = self.pos_x.value() if hasattr(self, 'pos_x') else getattr(self, '_text_x', 50)
        y = self.pos_y.value() if hasattr(self, 'pos_y') else getattr(self, '_text_y', 80)
        self.text_position_changed.emit(x, y)

    def update_text_position_spinboxes(self, x: int, y: int):
        self._text_x = x
        self._text_y = y
        if hasattr(self, 'pos_x'):
            self.pos_x.blockSignals(True)
            self.pos_x.setValue(x)
            self.pos_x.blockSignals(False)
        if hasattr(self, 'pos_y'):
            self.pos_y.blockSignals(True)
            self.pos_y.setValue(y)
            self.pos_y.blockSignals(False)

    def set_text_timing(self, start_sec: float, dur_sec: float):
        """Called when text overlay item is dragged/resized on the timeline."""
        self._text_start_sec = max(0.0, start_sec)
        self._text_duration_sec = max(0.5, dur_sec)
        if hasattr(self, 'text_mode_combo'):
            self.text_mode_combo.blockSignals(True)
            self.text_mode_combo.setCurrentIndex(3)  # Custom (Timeline)
            self.text_mode_combo.blockSignals(False)
        self.text_status.setText(f"Timeline: {start_sec:.1f}s - {start_sec + dur_sec:.1f}s")
        self._emit_text_animation()

    def update_logo_spinboxes(self, x: int, y: int, w: int, h: int):
        self.logo_x.blockSignals(True)
        self.logo_y.blockSignals(True)
        self.logo_w.blockSignals(True)
        self.logo_h.blockSignals(True)
        self.logo_x.setValue(x)
        self.logo_y.setValue(y)
        self.logo_w.setValue(w)
        self.logo_h.setValue(h)
        self.logo_x.blockSignals(False)
        self.logo_y.blockSignals(False)
        self.logo_w.blockSignals(False)
        self.logo_h.blockSignals(False)
        self.logo_status.setText(f"Position: ({x}, {y}) {w}x{h} [Dragged]")

    def _pick_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            hex_color = color.name()
            self._current_color = hex_color
            fg_color = '#000' if hex_color.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0', '#00F0FF', '#FFEDD5', '#F3E8FF'] else '#fff'
            self.color_btn.setStyleSheet(f"background-color: {hex_color}; color: {fg_color}; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")
            if self.text_checkbox.isChecked():
                self.text_updated.emit(self._current_text, hex_color, self._current_size, self._current_font)

    def _pick_bg_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            hex_color = color.name()
            self._current_bg_color = hex_color
            fg_color = '#fff' if hex_color.upper() != '#FFFFFF' else '#000'
            self.bg_color_btn.setStyleSheet(f"background-color: {hex_color}; color: {fg_color}; font-size: 14px; border: 1px solid #64748b; border-radius: 4px;")
            self.text_bg_color_changed.emit(hex_color)
            if self.text_checkbox.isChecked():
                self.preview_text_anim_requested.emit()

    def _apply_text_template(self, tpl: dict):
        """Applies a designer style preset and triggers an immediate live preview animation."""
        self._current_color = tpl.get("color", "#FFFFFF")
        self._current_bg_color = tpl.get("bg_color", "#000000")
        self._current_size = tpl.get("size", 12)
        self._current_font = tpl.get("font", "Kantumruy Pro")
        self._current_outline_color = tpl.get("outline_color", "#000000")
        self._current_outline_width = int(tpl.get("outline_width", 2))
        self._current_shadow_color = tpl.get("shadow_color", "#000000")
        self._current_shadow_offset = int(tpl.get("shadow_offset", 3))
        
        if hasattr(self, 'size_spin'):
            self.size_spin.blockSignals(True)
            self.size_spin.setValue(self._current_size)
            self.size_spin.blockSignals(False)
            
        if hasattr(self, 'text_font_combo'):
            self.text_font_combo.blockSignals(True)
            idx = self.text_font_combo.findText(self._current_font)
            if idx >= 0:
                self.text_font_combo.setCurrentIndex(idx)
            self.text_font_combo.blockSignals(False)
            
        if hasattr(self, 'color_btn'):
            fg_c = '#000' if self._current_color.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0', '#00F0FF', '#FFEDD5', '#F3E8FF'] else '#fff'
            self.color_btn.setStyleSheet(f"background-color: {self._current_color}; color: {fg_c}; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")
            
        if hasattr(self, 'bg_color_btn'):
            fg_bg = '#fff' if self._current_bg_color.upper() != '#FFFFFF' else '#000'
            self.bg_color_btn.setStyleSheet(f"background-color: {self._current_bg_color}; color: {fg_bg}; font-size: 14px; border: 1px solid #64748b; border-radius: 4px;")

        if hasattr(self, 'outline_spin'):
            self.outline_spin.blockSignals(True)
            self.outline_spin.setValue(self._current_outline_width)
            self.outline_spin.blockSignals(False)
            fg_out = '#000' if self._current_outline_color.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0'] else '#fff'
            self.outline_color_btn.setStyleSheet(f"background-color: {self._current_outline_color}; color: {fg_out}; font-size: 10px; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")

        if hasattr(self, 'shadow_spin'):
            self.shadow_spin.blockSignals(True)
            self.shadow_spin.setValue(self._current_shadow_offset)
            self.shadow_spin.blockSignals(False)

        self._update_swatch_buttons()
            
        if hasattr(self, 'text_speed_combo'):
            self.text_speed_combo.blockSignals(True)
            sp_val = "Medium"
            if "Fast" in tpl.get("speed", ""):
                sp_val = "Fast"
            elif "Slow" in tpl.get("speed", ""):
                sp_val = "Slow"
            idx = self.text_speed_combo.findText(sp_val)
            if idx >= 0:
                self.text_speed_combo.setCurrentIndex(idx)
            self.text_speed_combo.blockSignals(False)

        if tpl.get("align"):
            self.position_preset_requested.emit(tpl["align"])
            
        if not self.text_checkbox.isChecked():
            self.text_checkbox.setChecked(True)
        else:
            self.text_toggled.emit(True)
            
        self.text_bg_color_changed.emit(self._current_bg_color)
        self.text_outline_changed.emit(self._current_outline_color, self._current_outline_width)
        self.text_shadow_changed.emit(self._current_shadow_color, self._current_shadow_offset)
        self.text_updated.emit(self._current_text, self._current_color, self._current_size, self._current_font)
        self._emit_text_animation()
        
        # Trigger immediate live animation preview!
        self.preview_text_anim_requested.emit()
        self.text_status.setText(f"Preset '{tpl['name']}' Live Previewing! ✓")

    def _filter_style_cards(self, selected_category: str):
        """Filter CapCut visual text effect cards by category."""
        for cat, btn in getattr(self, '_style_category_btns', {}).items():
            btn.blockSignals(True)
            btn.setChecked(cat == selected_category)
            btn.blockSignals(False)

        cat_clean = selected_category.replace("🔥 ", "").replace("✨ ", "").replace("💥 ", "").replace("🌐 ", "").replace("🎬 ", "").strip()
        for card, tpl in getattr(self, '_style_cards', []):
            if cat_clean == "All" or tpl.get("category", "") == cat_clean:
                card.setVisible(True)
            else:
                card.setVisible(False)

    def _on_style_card_clicked(self, tpl: dict):
        """User clicked a CapCut visual card."""
        for card, t in getattr(self, '_style_cards', []):
            card.set_selected(t.get("name") == tpl.get("name"))
        self._apply_sub_template(tpl)

    def _apply_sub_template(self, tpl: dict):
        """Applies a subtitle style preset instantly."""
        self._current_sub_tpl = dict(tpl)
        self._burn_color = tpl.get("color", "#FFFFFF")
        font_name = tpl.get("font", "Kantumruy Pro")
        size = tpl.get("size", 22)
        opacity = tpl.get("opacity", 0)
        
        # Highlight matching card in the gallery
        for card, t in getattr(self, '_style_cards', []):
            card.set_selected(t.get("name") == tpl.get("name"))

        if hasattr(self, 'burn_sub_font_combo'):
            self.burn_sub_font_combo.blockSignals(True)
            idx = self.burn_sub_font_combo.findText(font_name)
            if idx >= 0:
                self.burn_sub_font_combo.setCurrentIndex(idx)
            self.burn_sub_font_combo.blockSignals(False)
            
        if hasattr(self, 'burn_sub_size_spin'):
            self.burn_sub_size_spin.blockSignals(True)
            self.burn_sub_size_spin.setValue(size)
            self.burn_sub_size_spin.blockSignals(False)
            
        if hasattr(self, 'burn_sub_bg_slider'):
            self.burn_sub_bg_slider.blockSignals(True)
            self.burn_sub_bg_slider.setValue(opacity)
            self.burn_sub_bg_slider.blockSignals(False)
            
        if hasattr(self, 'burn_sub_color_btn'):
            self.burn_sub_color_btn.setStyleSheet(f"background-color: {self._burn_color}; border: 1.5px solid #64748b; border-radius: 5px;")
            
        anim_type = tpl.get("anim", "pop_bounce")
        if hasattr(self, 'burn_sub_anim_combo'):
            self.burn_sub_anim_combo.blockSignals(True)
            for i in range(self.burn_sub_anim_combo.count()):
                if self.burn_sub_anim_combo.itemData(i) == anim_type:
                    self.burn_sub_anim_combo.setCurrentIndex(i)
                    break
            self.burn_sub_anim_combo.blockSignals(False)

        if not self.burn_sub_checkbox.isChecked():
            self.burn_sub_checkbox.setChecked(True)
            
        self._on_burn_sub_changed()
        self.burn_sub_status.setText(f"Preset '{tpl['name']}' ✓")
        self.test_burn_sub_anim_requested.emit()

    def _on_text_anim_changed(self):
        self._emit_text_animation()

    def _on_test_anim_clicked(self):
        self._emit_text_animation()
        self.preview_text_anim_requested.emit()

    def _emit_text_animation(self):
        motion_idx = self.text_motion_combo.currentIndex() if hasattr(self, 'text_motion_combo') else 0
        if motion_idx == 0:
            anim_type = "dynamic"
            zone = "up_down"
        elif motion_idx == 1:
            anim_type = "dynamic"
            zone = "full"
        else:
            anim_type = "none"
            zone = getattr(self, '_pos_zone', 'mid')
        
        speed_text = self.text_speed_combo.currentText().lower() if hasattr(self, 'text_speed_combo') else "normal"
        if "fast" in speed_text or "លឿន" in speed_text:
            speed_val = 0.3
            speed_str = "fast"
        elif "slow" in speed_text or "យឺត" in speed_text:
            speed_val = 1.0
            speed_str = "slow"
        else:
            speed_val = 0.5
            speed_str = "medium"
            
        full_video = self.full_video_checkbox.isChecked() if hasattr(self, 'full_video_checkbox') else True
        mode = "always" if full_video else "custom"
        st = getattr(self, '_text_start_sec', 0.0)
        dur = 0.0 if full_video else (self.duration_spin.value() if hasattr(self, 'duration_spin') else 5.0)
        repeat_sec = 0.0
        
        opacity = (self.opacity_spin.value() / 100.0) if hasattr(self, 'opacity_spin') else 0.85
        is_watermark = (motion_idx in [0, 1]) or (opacity < 1.0)
        
        self.text_animation_changed.emit(anim_type, speed_val, mode, st, dur, repeat_sec)
        self.text_watermark_changed.emit(is_watermark, opacity, zone, speed_str, full_video)

    def _apply_text(self):
        text = self.text_input.text()
        color = self._current_color
        size = self.size_spin.value()
        font_name = self.text_font_combo.currentText()
        x = self.pos_x.value() if hasattr(self, 'pos_x') else getattr(self, '_text_x', 50)
        y = self.pos_y.value() if hasattr(self, 'pos_y') else getattr(self, '_text_y', 80)
        
        self._current_text = text
        self._current_size = size
        self._current_font = font_name
        
        self.text_status.setText(f"Applied: '{text[:18]}...' ✓")
        self.text_position_changed.emit(x, y)
        self.text_bg_color_changed.emit(self._current_bg_color)
        self.text_outline_changed.emit(getattr(self, '_current_outline_color', '#000000'), getattr(self, '_current_outline_width', 2))
        self.text_shadow_changed.emit(getattr(self, '_current_shadow_color', '#000000'), getattr(self, '_current_shadow_offset', 3))
        self.text_updated.emit(text, color, size, font_name)
        self._emit_text_animation()
        
        if not self.text_checkbox.isChecked():
            self.text_checkbox.setChecked(True)

        self.preview_text_anim_requested.emit()

    # ==================== LOGO SIGNALS ====================
    def _on_logo_toggled(self, checked: bool):
        self.logo_toggled.emit(checked)
        self.logo_status.setText("Status: Enabled ✓" if checked else "Status: Disabled")
        if checked:
            self._apply_logo()

    def _on_logo_path_changed(self, path: str):
        if path and os.path.exists(path):
            self.logo_status.setText(f"Status: Loaded {os.path.basename(path)}")
        else:
            self.logo_status.setText("Status: File not found")
        if self.logo_checkbox.isChecked() and path:
            self._apply_logo()

    def _on_logo_changed(self):
        if self.logo_checkbox.isChecked():
            self._apply_logo()

    def _on_logo_full_video_toggled(self, checked: bool):
        self.logo_full_video_toggled.emit(checked)
        if self.logo_checkbox.isChecked():
            self._apply_logo()

    def _auto_fit_logo_ratio(self):
        path = self.logo_path_edit.text().strip(' "\'')
        if not path:
            return
        from utils.file_utils import get_temp_path
        base_name = os.path.basename(path)
        candidates = [path, get_temp_path(f"safe_logo_{base_name}"), os.path.join(os.getcwd(), base_name)]
        valid_path = next((p for p in candidates if os.path.exists(p)), None)
        if not valid_path:
            return
        try:
            import cv2
            img = cv2.imread(valid_path)
            if img is not None:
                oh, ow = img.shape[:2]
                if ow > 0 and oh > 0:
                    cur_w = self.logo_w.value()
                    new_h = max(10, min(500, int(cur_w * oh / float(ow))))
                    self.logo_h.setValue(new_h)
                    self._apply_logo()
                    self.logo_status.setText(f"Ratio: {ow}x{oh} -> {cur_w}x{new_h} ✓")
        except Exception as e:
            logger.warning(f"Auto-fit logo ratio: {e}")

    def _apply_logo(self):
        path = self.logo_path_edit.text().strip(' "\'')
        x = self.logo_x.value()
        y = self.logo_y.value()
        w = self.logo_w.value()
        h = self.logo_h.value()
        remove_green = self.remove_green_chk.isChecked()
        
        if not path:
            self.logo_status.setText("Status: No file selected")
            return
            
        base_name = os.path.basename(path)
        from utils.file_utils import get_temp_path
        safe_copy = get_temp_path(f"safe_logo_{base_name}")
        local_copy = os.path.join(os.getcwd(), base_name)
        
        # Check accessible candidate
        resolved_path = path
        if not os.path.exists(resolved_path) or not os.access(resolved_path, os.R_OK):
            if os.path.exists(safe_copy) and os.access(safe_copy, os.R_OK):
                resolved_path = safe_copy
            elif os.path.exists(local_copy) and os.access(local_copy, os.R_OK):
                resolved_path = local_copy
            else:
                self.logo_status.setText("Status: Error opening logo")
                QMessageBox.warning(
                    self, "Logo Image Warning",
                    f"មិនអាចបើករូបភាព Logo បានទេ:\n{path}\n\n"
                    "សូមពិនិត្យមើលថាតើ File រូបភាពនេះមានពិតប្រាកដ និងអាចអានបានឬទេ។"
                )
                return
                
        path = resolved_path
        self.logo_path_edit.setText(path)
        self.logo_status.setText(f"Applied: {os.path.basename(path)} ✓")
        self.logo_checkbox.blockSignals(True)
        self.logo_checkbox.setChecked(True)
        self.logo_checkbox.blockSignals(False)
        self.logo_toggled.emit(True)
        self.logo_updated.emit(path, x, y, w, h, remove_green)
        if hasattr(self, 'logo_full_video_chk'):
            self.logo_full_video_toggled.emit(self.logo_full_video_chk.isChecked())

    def _browse_logo(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Logo Image", "", "Image Files (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        )
        if file_path:
            file_path = file_path.strip(' "\'')
            workspace_dir = os.path.abspath(os.getcwd())
            resolved = os.path.abspath(file_path)
            
            # If outside workspace, try to make a safe cached copy inside temp
            if not resolved.startswith(workspace_dir):
                from utils.file_utils import get_temp_path
                base_name = os.path.basename(file_path)
                safe_path = get_temp_path(f"safe_logo_{base_name}")
                copied = False
                try:
                    import shutil
                    shutil.copyfile(file_path, safe_path)
                    copied = True
                except Exception:
                    pass
                if copied and os.path.exists(safe_path) and os.path.getsize(safe_path) > 0:
                    file_path = safe_path
                    
            # Auto-detect aspect ratio and set proportional width/height
            try:
                import cv2
                img_probe = cv2.imread(file_path)
                if img_probe is not None:
                    oh, ow = img_probe.shape[:2]
                    if ow > 0 and oh > 0:
                        max_dim = 120
                        if ow >= oh:
                            w = max_dim
                            h = max(15, int(max_dim * oh / float(ow)))
                        else:
                            h = max_dim
                            w = max(15, int(max_dim * ow / float(oh)))
                        self.logo_w.blockSignals(True)
                        self.logo_h.blockSignals(True)
                        self.logo_w.setValue(w)
                        self.logo_h.setValue(h)
                        self.logo_w.blockSignals(False)
                        self.logo_h.blockSignals(False)
            except Exception:
                pass

            self.logo_path_edit.setText(file_path)
            self._apply_logo()

    # ==================== BURN SUBTITLE SIGNALS ====================
    def _on_burn_sub_toggled(self, checked: bool):
        self.burn_subtitle_toggled.emit(checked)
        self.burn_sub_status.setText("Status: Enabled ✓" if checked else "Status: Disabled")
        self._emit_burn_sub_updated()

    def _on_burn_sub_changed(self):
        self._emit_burn_sub_updated()

    def _pick_burn_sub_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            hex_color = color.name()
            self._burn_color = hex_color
            self.burn_sub_color_btn.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #666;")
            self._emit_burn_sub_updated()

    def _emit_burn_sub_updated(self):
        enabled = self.burn_sub_checkbox.isChecked()
        font_name = self.burn_sub_font_combo.currentText()
        size = self.burn_sub_size_spin.value()
        color_hex = self._burn_color
        bg_opacity = self.burn_sub_bg_slider.value() / 100.0 if hasattr(self, 'burn_sub_bg_slider') else 0.0
        anim_type = self.burn_sub_anim_combo.currentData() if hasattr(self, 'burn_sub_anim_combo') else "pop_bounce"
        anim_dur = float(self.burn_sub_speed_combo.currentData()) if hasattr(self, 'burn_sub_speed_combo') else 0.35
        tpl = getattr(self, '_current_sub_tpl', None)
        self.burn_subtitle_updated.emit(enabled, font_name, size, color_hex, bg_opacity, anim_type, anim_dur, tpl)

    def _on_test_sub_anim_clicked(self):
        self._emit_burn_sub_updated()
        self.test_burn_sub_anim_requested.emit()

    def _on_burn_sub_pos_changed(self, value: int):
        if hasattr(self, 'burn_sub_pos_val_lbl'):
            self.burn_sub_pos_val_lbl.setText(f"{value}%")
        self.burn_subtitle_position_changed.emit(value, 50)

    def _reset_burn_sub_pos(self):
        if hasattr(self, 'burn_sub_pos_slider'):
            self.burn_sub_pos_slider.blockSignals(True)
            self.burn_sub_pos_slider.setValue(85)
            self.burn_sub_pos_slider.blockSignals(False)
        if hasattr(self, 'burn_sub_pos_val_lbl'):
            self.burn_sub_pos_val_lbl.setText("85%")
        self.reset_burn_sub_requested.emit()

    def update_burn_sub_position(self, y_percent: int, x_percent: int = 50):
        if hasattr(self, 'burn_sub_pos_slider'):
            self.burn_sub_pos_slider.blockSignals(True)
            self.burn_sub_pos_slider.setValue(y_percent)
            self.burn_sub_pos_slider.blockSignals(False)
        if hasattr(self, 'burn_sub_pos_val_lbl'):
            self.burn_sub_pos_val_lbl.setText(f"{y_percent}%")
        if hasattr(self, 'burn_sub_status'):
            self.burn_sub_status.setText(f"Pos: ({x_percent}%, {y_percent}%) [Dragged]")

    def get_effects_config(self, preview_widget=None, segments=None) -> dict:
        """
        Build a 100% comprehensive, self-contained effects configuration from all 4 tabs:
        1. Blur Mask
        2. Text Overlay
        3. Logo
        4. Burn Subtitle
        Guarantees that any effect configured or enabled in any of the 4 tabs is captured into the final exported video.
        """
        if preview_widget and hasattr(preview_widget, '_get_canvas_size'):
            pw, ph = preview_widget._get_canvas_size()
        elif preview_widget and hasattr(preview_widget, 'screen_frame'):
            pw = max(1, preview_widget.screen_frame.width())
            ph = max(1, preview_widget.screen_frame.height())
        else:
            pw, ph = (640, 360)
        pw = max(1, pw)
        ph = max(1, ph)

        def _parse_rgb(hex_str, default=(255, 255, 255)):
            if not hex_str:
                return default
            h = str(hex_str).lstrip('#')
            if len(h) == 6:
                try:
                    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
                except Exception:
                    pass
            return default

        # ---------------- 1. BLUR MASK ----------------
        blur_enabled = (
            self.blur_checkbox.isChecked()
            or (preview_widget and getattr(preview_widget, 'blur_enabled', False))
        )
        if preview_widget and getattr(preview_widget, 'blur_items', None) and getattr(preview_widget, 'blur_enabled', False):
            blur_enabled = True

        blurs = []
        if preview_widget and hasattr(preview_widget, 'blur_items') and preview_widget.blur_items:
            blurs = [dict(b) for b in preview_widget.blur_items]

        # If user enabled blur in tab but no blur items exist yet, synthesize default region
        if blur_enabled and not blurs:
            blur_rect = getattr(preview_widget, 'blur_rect', None)
            cur_type = self.blur_type_combo.currentData() if hasattr(self, 'blur_type_combo') else "blur"
            cur_mode = self.blur_mode_combo.currentData() if hasattr(self, 'blur_mode_combo') else "full"
            if blur_rect:
                blurs = [{
                    "id": "blur_1",
                    "name": "Mask 1",
                    "x": blur_rect.x(),
                    "y": blur_rect.y(),
                    "width": blur_rect.width(),
                    "height": blur_rect.height(),
                    "rotation": float(self.blur_rot_slider.value()),
                    "intensity": int(self.blur_slider.value()),
                    "type": cur_type,
                    "mode": cur_mode,
                    "auto_speech": (cur_mode == "speech"),
                    "full_video": (cur_mode == "full"),
                    "start_sec": float(self.blur_start_spin.value()) if hasattr(self, 'blur_start_spin') else 0.0,
                    "end_sec": float(self.blur_end_spin.value()) if hasattr(self, 'blur_end_spin') else 999999.0
                }]
            else:
                blurs = [{
                    "id": "blur_1",
                    "name": "Mask 1",
                    "x": max(10, int(pw * 0.1)),
                    "y": max(10, int(ph * 0.1)),
                    "width": max(60, int(pw * 0.4)),
                    "height": max(40, int(ph * 0.2)),
                    "rotation": float(self.blur_rot_slider.value()),
                    "intensity": int(self.blur_slider.value()),
                    "type": cur_type,
                    "mode": cur_mode,
                    "auto_speech": (cur_mode == "speech"),
                    "full_video": (cur_mode == "full"),
                    "start_sec": float(self.blur_start_spin.value()) if hasattr(self, 'blur_start_spin') else 0.0,
                    "end_sec": float(self.blur_end_spin.value()) if hasattr(self, 'blur_end_spin') else 999999.0
                }]
        else:
            for b in blurs:
                if "mode" not in b:
                    if b.get("auto_speech", False):
                        b["mode"] = "speech"
                    elif not b.get("full_video", True):
                        b["mode"] = "range"
                    else:
                        b["mode"] = "full"

        blur_cfg = {
            "enabled": blur_enabled and bool(blurs),
            "intensity": self.blur_slider.value(),
            "rect": getattr(preview_widget, 'blur_rect', None),
            "blurs": blurs,
            "active_blur_id": self.blur_selector.currentData() or (blurs[0]["id"] if blurs else "blur_1")
        }

        # ---------------- 2. TEXT OVERLAY ----------------
        text_enabled = self.text_checkbox.isChecked()
        cur_text = self.text_input.text().strip()
        text_color_rgb = _parse_rgb(self._current_color, (255, 255, 255))
        text_bg_rgb = _parse_rgb(self._current_bg_color, (0, 0, 0)) if self._current_bg_color else None
        text_out_rgb = _parse_rgb(self._current_outline_color, (0, 0, 0))
        text_shd_rgb = _parse_rgb(self._current_shadow_color, (0, 0, 0))

        out_width = self.outline_spin.value() if hasattr(self, 'outline_spin') else getattr(self, '_current_outline_width', 2)
        shd_offset = self.shadow_spin.value() if hasattr(self, 'shadow_spin') else getattr(self, '_current_shadow_offset', 3)

        motion_idx = self.text_motion_combo.currentIndex() if hasattr(self, 'text_motion_combo') else 0
        if motion_idx == 0:
            anim_type = "dynamic"
            pos_zone = "up_down"
        elif motion_idx == 1:
            anim_type = "dynamic"
            pos_zone = "full"
        else:
            anim_type = "none"
            pos_zone = getattr(self, '_pos_zone', 'mid')

        speed_text = self.text_speed_combo.currentText().lower() if hasattr(self, 'text_speed_combo') else "normal"
        if "fast" in speed_text or "លឿន" in speed_text:
            anim_speed = 0.3
            speed_str = "fast"
        elif "slow" in speed_text or "យឺត" in speed_text:
            anim_speed = 1.0
            speed_str = "slow"
        else:
            anim_speed = 0.5
            speed_str = "medium"

        full_video = self.full_video_checkbox.isChecked() if hasattr(self, 'full_video_checkbox') else True
        anim_mode = "always" if full_video else "custom"
        st_sec = getattr(self, '_text_start_sec', 0.0)
        dur_sec = 0.0 if full_video else (self.duration_spin.value() if hasattr(self, 'duration_spin') else 5.0)
        opacity = max(0.1, min(1.0, (self.opacity_spin.value() / 100.0) if hasattr(self, 'opacity_spin') else 0.85))
        is_watermark = (motion_idx in [0, 1]) or (opacity < 1.0)
        tx = self.pos_x.value() if hasattr(self, 'pos_x') else getattr(self, '_text_x', 50)
        ty = self.pos_y.value() if hasattr(self, 'pos_y') else getattr(self, '_text_y', 80)

        # Multi-text items
        items = []
        if preview_widget and hasattr(preview_widget, 'text_items') and preview_widget.text_items:
            items = [dict(it) for it in preview_widget.text_items]
        elif self.text_items:
            items = [dict(it) for it in self.text_items]

        if not items and cur_text:
            items = [{
                "id": "text_1",
                "name": "Text 1",
                "text": cur_text,
                "font": self.text_font_combo.currentText(),
                "font_name": self.text_font_combo.currentText(),
                "size": self.size_spin.value(),
                "size_pt": self.size_spin.value(),
                "color": self._current_color,
                "color_rgb": text_color_rgb,
                "bg_color": self._current_bg_color,
                "bg_color_rgb": text_bg_rgb,
                "outline_color": self._current_outline_color,
                "outline_color_rgb": text_out_rgb,
                "outline_width": out_width,
                "shadow_color": self._current_shadow_color,
                "shadow_color_rgb": text_shd_rgb,
                "shadow_offset": shd_offset,
                "x": tx,
                "y": ty,
                "position": (tx, ty),
                "anim_type": anim_type,
                "anim_speed": anim_speed,
                "anim_repeat_sec": getattr(self, '_text_repeat_sec', 2.0),
                "anim_mode": anim_mode,
                "start_sec": st_sec,
                "duration_sec": dur_sec,
                "full_video": full_video,
                "opacity": opacity,
                "pos_zone": pos_zone,
                "is_watermark": is_watermark,
                "speed_str": speed_str,
                "enabled": True
            }]
        else:
            curr_spin_size = self.size_spin.value()
            active_id = self.text_selector.currentData() if hasattr(self, 'text_selector') and self.text_selector.currentData() else None
            for it in items:
                if active_id and it.get("id") == active_id:
                    it["size"] = curr_spin_size
                    it["size_pt"] = curr_spin_size
                else:
                    s_val = it.get("size") or it.get("size_pt") or curr_spin_size
                    it["size"] = int(s_val)
                    it["size_pt"] = int(s_val)
                if "anim_type" not in it or it["anim_type"] in ["none", "dynamic", "dynamic_watermark", "watermark"]:
                    it["anim_type"] = anim_type
                if "pos_zone" not in it:
                    it["pos_zone"] = pos_zone
                if "speed_str" not in it:
                    it["speed_str"] = speed_str
                if "outline_width" not in it:
                    it["outline_width"] = out_width
                if "shadow_offset" not in it:
                    it["shadow_offset"] = shd_offset
                if "full_video" not in it:
                    it["full_video"] = full_video
                if "opacity" not in it:
                    it["opacity"] = opacity

        text_cfg = {
            "enabled": text_enabled and (bool(cur_text) or any(bool(t.get("text")) for t in items)),
            "text": cur_text,
            "font_name": self.text_font_combo.currentText(),
            "color_rgb": text_color_rgb,
            "bg_color_rgb": text_bg_rgb,
            "outline_color_rgb": text_out_rgb,
            "outline_width": out_width,
            "shadow_color_rgb": text_shd_rgb,
            "shadow_offset": shd_offset,
            "size_pt": self.size_spin.value(),
            "size": self.size_spin.value(),
            "position": (tx, ty),
            "animation": {
                "type": anim_type,
                "speed": anim_speed,
                "mode": anim_mode,
                "start_sec": st_sec,
                "duration_sec": dur_sec,
                "repeat_sec": getattr(self, '_text_repeat_sec', 2.0)
            },
            "items": items,
            "active_id": (self.text_selector.currentData() if hasattr(self, 'text_selector') and self.text_selector.currentData() else "text_1")
        }

        # ---------------- 3. LOGO ----------------
        logo_enabled = self.logo_checkbox.isChecked()
        raw_logo_path = self.logo_path_edit.text().strip(' "\'')
        resolved_logo_path = None
        if raw_logo_path:
            if os.path.exists(raw_logo_path):
                resolved_logo_path = os.path.abspath(raw_logo_path)
            else:
                from utils.file_utils import get_temp_path
                base_name = os.path.basename(raw_logo_path)
                safe_copy = get_temp_path(f"safe_logo_{base_name}")
                local_copy = os.path.join(os.getcwd(), base_name)
                if os.path.exists(safe_copy):
                    resolved_logo_path = safe_copy
                elif os.path.exists(local_copy):
                    resolved_logo_path = local_copy
                elif preview_widget and hasattr(preview_widget, 'logo_path') and preview_widget.logo_path and os.path.exists(preview_widget.logo_path):
                    resolved_logo_path = preview_widget.logo_path

        logo_cfg = {
            "enabled": logo_enabled and bool(resolved_logo_path and os.path.exists(resolved_logo_path)),
            "path": resolved_logo_path,
            "x": self.logo_x.value(),
            "y": self.logo_y.value(),
            "width": self.logo_w.value(),
            "height": self.logo_h.value(),
            "remove_green": self.remove_green_chk.isChecked(),
            "full_video": self.logo_full_video_chk.isChecked(),
            "start_sec": 0.0 if self.logo_full_video_chk.isChecked() else (getattr(preview_widget, 'logo_start_sec', 0.0) if preview_widget else 0.0),
            "duration_sec": 0.0 if self.logo_full_video_chk.isChecked() else (getattr(preview_widget, 'logo_duration_sec', 0.0) if preview_widget else 0.0)
        }

        # ---------------- 4. BURN SUBTITLE ----------------
        burn_enabled = self.burn_sub_checkbox.isChecked()
        burn_color_rgb = _parse_rgb(self._burn_color, (255, 255, 255))
        y_ratio = self.burn_sub_pos_slider.value() / 100.0
        x_ratio = getattr(preview_widget, 'burn_sub_x_ratio', 0.50) if preview_widget else 0.50

        final_segments = segments
        if final_segments is None and preview_widget and hasattr(preview_widget, '_timeline_segments'):
            final_segments = preview_widget._timeline_segments
        if final_segments is None:
            final_segments = []

        anim_type = self.burn_sub_anim_combo.currentData() if hasattr(self, 'burn_sub_anim_combo') else "pop_bounce"
        anim_dur = float(self.burn_sub_speed_combo.currentData()) if hasattr(self, 'burn_sub_speed_combo') else 0.35

        burn_cfg = {
            "enabled": burn_enabled and bool(final_segments),
            "font_name": self.burn_sub_font_combo.currentText(),
            "font_size": self.burn_sub_size_spin.value(),
            "color_rgb": burn_color_rgb,
            "bg_opacity": self.burn_sub_bg_slider.value() / 100.0 if hasattr(self, 'burn_sub_bg_slider') else 0.0,
            "x_ratio": x_ratio,
            "y_ratio": y_ratio,
            "anim_type": anim_type,
            "anim_dur": anim_dur,
            "template": getattr(self, '_current_sub_tpl', None) or (preview_widget and getattr(preview_widget, 'burn_subtitle_template', None))
        }

        return {
            "preview_size": (pw, ph),
            "blur": blur_cfg,
            "text_overlay": text_cfg,
            "logo": logo_cfg,
            "burn_subtitle": burn_cfg,
            "segments": final_segments
        }

    def get_state(self) -> dict:
        """Serialize current effects settings for project file saving."""
        return {
            "blur_enabled": self.blur_checkbox.isChecked(),
            "blur_intensity": self.blur_slider.value(),
            "blur_full_video": self.blur_full_video_chk.isChecked() if hasattr(self, 'blur_full_video_chk') else True,
            "text_enabled": self.text_checkbox.isChecked(),
            "text_is_watermark": self.watermark_checkbox.isChecked() if hasattr(self, 'watermark_checkbox') else False,
            "text_opacity": self.opacity_spin.value() if hasattr(self, 'opacity_spin') else 100,
            "text_full_video": self.full_video_checkbox.isChecked() if hasattr(self, 'full_video_checkbox') else True,
            "text_duration": self.duration_spin.value() if hasattr(self, 'duration_spin') else 5.0,
            "text_pos_zone": getattr(self, '_pos_zone', 'mid'),
            "text_content": self.text_input.text(),
            "text_font": self.text_font_combo.currentText(),
            "text_size": self.size_spin.value(),
            "text_color": self._current_color,
            "text_bg_color": self._current_bg_color,
            "text_outline_color": self._current_outline_color,
            "text_outline_width": self._current_outline_width,
            "text_shadow_color": self._current_shadow_color,
            "text_shadow_offset": self._current_shadow_offset,
            "text_items": self.text_items,
            "active_text_id": self.active_text_id,
            "text_x": self.pos_x.value() if hasattr(self, 'pos_x') else getattr(self, '_text_x', 50),
            "text_y": self.pos_y.value() if hasattr(self, 'pos_y') else getattr(self, '_text_y', 80),
            "text_is_dynamic": self.dynamic_checkbox.isChecked() if hasattr(self, 'dynamic_checkbox') else False,
            "text_speed_index": self.text_speed_combo.currentIndex() if hasattr(self, 'text_speed_combo') else 1,
            "logo_enabled": self.logo_checkbox.isChecked(),
            "logo_path": self.logo_path_edit.text(),
            "logo_x": self.logo_x.value(),
            "logo_y": self.logo_y.value(),
            "logo_w": self.logo_w.value(),
            "logo_h": self.logo_h.value(),
            "logo_remove_green": self.remove_green_chk.isChecked(),
            "logo_full_video": self.logo_full_video_chk.isChecked() if hasattr(self, 'logo_full_video_chk') else True,
            "burn_sub_enabled": self.burn_sub_checkbox.isChecked(),
            "burn_sub_font": self.burn_sub_font_combo.currentText(),
            "burn_sub_size": self.burn_sub_size_spin.value() if hasattr(self, 'burn_sub_size_spin') else 18,
            "burn_sub_color": self._burn_color,
            "burn_sub_opacity": self.burn_sub_bg_slider.value() if hasattr(self, 'burn_sub_bg_slider') else 0,
            "burn_sub_pos_y": self.burn_sub_pos_slider.value() if hasattr(self, 'burn_sub_pos_slider') else 85,
            "burn_sub_anim_type": self.burn_sub_anim_combo.currentData() if hasattr(self, 'burn_sub_anim_combo') else "pop_bounce",
            "burn_sub_anim_dur": float(self.burn_sub_speed_combo.currentData()) if hasattr(self, 'burn_sub_speed_combo') else 0.35,
        }

    def set_state(self, state: dict):
        """Restore effects settings from a saved project file."""
        if not isinstance(state, dict):
            return
        if "blur_enabled" in state:
            self.blur_checkbox.setChecked(bool(state["blur_enabled"]))
        if "blur_intensity" in state:
            self.blur_slider.setValue(int(state["blur_intensity"]))
        if "blur_full_video" in state and hasattr(self, 'blur_full_video_chk'):
            self.blur_full_video_chk.setChecked(bool(state["blur_full_video"]))
        if "text_enabled" in state:
            self.text_checkbox.setChecked(bool(state["text_enabled"]))
        if "text_is_watermark" in state and hasattr(self, 'watermark_checkbox'):
            self.watermark_checkbox.setChecked(bool(state["text_is_watermark"]))
        if "text_is_dynamic" in state and hasattr(self, 'dynamic_checkbox'):
            self.dynamic_checkbox.setChecked(bool(state["text_is_dynamic"]))
        elif "text_anim_index" in state and hasattr(self, 'dynamic_checkbox'):
            self.dynamic_checkbox.setChecked(int(state["text_anim_index"]) == 5)
        if "text_opacity" in state and hasattr(self, 'opacity_spin'):
            self.opacity_spin.setValue(int(state["text_opacity"]))
        if "text_full_video" in state and hasattr(self, 'full_video_checkbox'):
            self.full_video_checkbox.setChecked(bool(state["text_full_video"]))
        if "text_duration" in state and hasattr(self, 'duration_spin'):
            self.duration_spin.setValue(float(state["text_duration"]))
        if "text_pos_zone" in state:
            self._pos_zone = str(state["text_pos_zone"])
        if "text_content" in state:
            self.text_input.setText(str(state["text_content"]))
        if "text_font" in state:
            idx = self.text_font_combo.findText(state["text_font"])
            if idx >= 0:
                self.text_font_combo.setCurrentIndex(idx)
        if "text_size" in state:
            self.size_spin.setValue(int(state["text_size"]))
        if "text_color" in state:
            self._current_color = str(state["text_color"])
            self.color_btn.setStyleSheet(f"background-color: {self._current_color}; border: 1px solid #64748b; border-radius: 4px;")
        if "text_bg_color" in state:
            self._current_bg_color = str(state["text_bg_color"])
            if hasattr(self, 'bg_color_btn'):
                self.bg_color_btn.setStyleSheet(f"background-color: {self._current_bg_color}; border: 1px solid #64748b; border-radius: 4px;")
        if "text_outline_color" in state:
            self._current_outline_color = str(state["text_outline_color"])
            if hasattr(self, 'outline_color_btn'):
                fg_out = '#000' if self._current_outline_color.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0'] else '#fff'
                self.outline_color_btn.setStyleSheet(f"background-color: {self._current_outline_color}; color: {fg_out}; font-size: 10px; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")
        if "text_outline_width" in state and hasattr(self, 'outline_spin'):
            self.outline_spin.setValue(int(state["text_outline_width"]))
        if "text_shadow_color" in state:
            self._current_shadow_color = str(state["text_shadow_color"])
            if hasattr(self, 'shadow_color_btn'):
                fg_shd = '#000' if self._current_shadow_color.upper() in ['#FFFFFF', '#FACC15', '#A7F3D0'] else '#fff'
                self.shadow_color_btn.setStyleSheet(f"background-color: {self._current_shadow_color}; color: {fg_shd}; font-size: 10px; font-weight: bold; border: 1px solid #64748b; border-radius: 4px;")
        if "text_shadow_offset" in state and hasattr(self, 'shadow_spin'):
            self.shadow_spin.setValue(int(state["text_shadow_offset"]))
        if "text_items" in state and isinstance(state["text_items"], list) and state["text_items"]:
            self.sync_text_items(state["text_items"], state.get("active_text_id", state["text_items"][0].get("id", "text_1")))
        if "text_x" in state:
            self._text_x = int(state["text_x"])
            if hasattr(self, 'pos_x'):
                self.pos_x.setValue(self._text_x)
        if "text_y" in state:
            self._text_y = int(state["text_y"])
            if hasattr(self, 'pos_y'):
                self.pos_y.setValue(self._text_y)
        if "text_speed_index" in state and hasattr(self, 'text_speed_combo'):
            self.text_speed_combo.setCurrentIndex(int(state["text_speed_index"]))
        if "logo_enabled" in state:
            self.logo_checkbox.setChecked(bool(state["logo_enabled"]))
        if "logo_path" in state:
            self.logo_path_edit.setText(str(state["logo_path"]))
        if "logo_x" in state:
            self.logo_x.setValue(int(state["logo_x"]))
        if "logo_y" in state:
            self.logo_y.setValue(int(state["logo_y"]))
        if "logo_w" in state:
            self.logo_w.setValue(int(state["logo_w"]))
        if "logo_h" in state:
            self.logo_h.setValue(int(state["logo_h"]))
        if "logo_remove_green" in state:
            self.remove_green_chk.setChecked(bool(state["logo_remove_green"]))
        if "logo_full_video" in state and hasattr(self, 'logo_full_video_chk'):
            self.logo_full_video_chk.setChecked(bool(state["logo_full_video"]))
        if "burn_sub_enabled" in state:
            self.burn_sub_checkbox.setChecked(bool(state["burn_sub_enabled"]))
        if "burn_sub_font" in state:
            idx = self.burn_sub_font_combo.findText(state["burn_sub_font"])
            if idx >= 0:
                self.burn_sub_font_combo.setCurrentIndex(idx)
        if "burn_sub_size" in state and hasattr(self, 'burn_sub_size_spin'):
            self.burn_sub_size_spin.setValue(int(state["burn_sub_size"]))
        if "burn_sub_color" in state:
            self._burn_color = str(state["burn_sub_color"])
            if hasattr(self, 'burn_sub_color_btn'):
                self.burn_sub_color_btn.setStyleSheet(f"background-color: {self._burn_color}; border: 1px solid #666;")
        if "burn_sub_opacity" in state and hasattr(self, 'burn_sub_bg_slider'):
            self.burn_sub_bg_slider.setValue(int(state["burn_sub_opacity"]))
        if "burn_sub_pos_y" in state and hasattr(self, 'burn_sub_pos_slider'):
            pos_y = int(state["burn_sub_pos_y"])
            self.burn_sub_pos_slider.setValue(pos_y)
            if hasattr(self, 'burn_sub_pos_val_lbl'):
                self.burn_sub_pos_val_lbl.setText(f"{pos_y}%")
            self.burn_subtitle_position_changed.emit(pos_y, 50)
        if "burn_sub_anim_type" in state and hasattr(self, 'burn_sub_anim_combo'):
            for i in range(self.burn_sub_anim_combo.count()):
                if self.burn_sub_anim_combo.itemData(i) == state["burn_sub_anim_type"]:
                    self.burn_sub_anim_combo.setCurrentIndex(i)
                    break
        if "burn_sub_anim_dur" in state and hasattr(self, 'burn_sub_speed_combo'):
            for i in range(self.burn_sub_speed_combo.count()):
                if abs(float(self.burn_sub_speed_combo.itemData(i)) - float(state["burn_sub_anim_dur"])) < 0.01:
                    self.burn_sub_speed_combo.setCurrentIndex(i)
                    break
        
        # Re-apply updated configurations
        if self.text_checkbox.isChecked():
            self._apply_text()
        if self.logo_checkbox.isChecked() and self.logo_path_edit.text():
            self._apply_logo()
        self._emit_burn_sub_updated()


# ==================== DROP ZONE ====================
class DropZoneWidget(QFrame):
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.selected_path = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setAlignment(Qt.AlignCenter)

        self.icon_label = QLabel("🎬", self)
        self.icon_label.setStyleSheet("font-size: 38px;")
        self.icon_label.setAlignment(Qt.AlignCenter)

        self.info_label = QLabel("Drag & Drop Video Here or Click to Browse", self)
        self.info_label.setStyleSheet("font-weight: 700; font-size: 14px; color: #ffffff;")
        self.info_label.setAlignment(Qt.AlignCenter)

        self.browse_btn = QPushButton("📁 Select Video File...", self)
        self.browse_btn.setFixedWidth(180)
        self.browse_btn.clicked.connect(self._browse_file)

        layout.addWidget(self.icon_label)
        layout.addWidget(self.info_label)
        layout.addWidget(self.browse_btn, alignment=Qt.AlignCenter)

    def _browse_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.webm)"
        )
        if file_path:
            self.set_file_path(file_path)

    def set_file_path(self, path: str):
        self.selected_path = path
        self.file_dropped.emit(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                self.set_file_path(file_path)


# ==================== SEGMENT EDITOR ====================
class SegmentEditorWidget(QWidget):
    segments_updated = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segments = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        tools_lay = QHBoxLayout()
        
        tools_lbl = QLabel("Transcript Segments Editor", self)
        tools_lbl.setStyleSheet("font-weight: bold; color: #8fa0c0; font-size: 12px;")
        tools_lay.addWidget(tools_lbl)
        tools_lay.addStretch()

        self.add_row_btn = QPushButton("➕ Add Row", self)
        self.add_row_btn.clicked.connect(self._add_empty_row)
        
        self.del_row_btn = QPushButton("🗑 Delete Row", self)
        self.del_row_btn.clicked.connect(self._delete_selected_row)

        self.import_srt_btn = QPushButton("📥 Import SRT", self)
        self.import_srt_btn.clicked.connect(self._import_srt)

        self.export_srt_btn = QPushButton("📤 Export SRT", self)
        self.export_srt_btn.clicked.connect(self._export_srt)

        tools_lay.addWidget(self.add_row_btn)
        tools_lay.addWidget(self.del_row_btn)
        tools_lay.addWidget(self.import_srt_btn)
        tools_lay.addWidget(self.export_srt_btn)

        layout.addLayout(tools_lay)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["TIMELINE", "ORIGINAL SPEECH (STT)", "KHMER TRANSLATION 🇰🇭 (DOUBLE-CLICK TO EDIT)", "ACTION"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)

        layout.addWidget(self.table)

    def set_segments(self, segments: list):
        self.segments = segments
        self.table.setRowCount(0)

        for i, seg in enumerate(segments):
            self._insert_segment_row(i, seg)

    def _insert_segment_row(self, row: int, seg: dict):
        self.table.insertRow(row)
        
        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        time_str = f"⏱ [{start:05.2f}s → {end:05.2f}s]"
        t_item = QTableWidgetItem(time_str)
        t_item.setFlags(t_item.flags() & ~Qt.ItemIsEditable)
        t_item.setForeground(Qt.GlobalColor.cyan)
        self.table.setItem(row, 0, t_item)

        orig_text = seg.get("original_text", seg.get("text", ""))
        orig_item = QTableWidgetItem(orig_text)
        orig_item.setFlags(orig_item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, 1, orig_item)

        khmer_text = seg.get("khmer_text", "")
        khmer_item = QTableWidgetItem(khmer_text)
        self.table.setItem(row, 2, khmer_item)

        preview_btn = QPushButton("🔊 Test Voice", self)
        preview_btn.setFixedWidth(95)
        preview_btn.clicked.connect(lambda _, r=row: self._preview_segment_tts(r))
        self.table.setCellWidget(row, 3, preview_btn)

    def _add_empty_row(self):
        row_count = self.table.rowCount()
        last_end = self.segments[-1]["end"] if self.segments else 0.0
        new_seg = {
            "start": round(last_end, 2),
            "end": round(last_end + 3.0, 2),
            "original_text": "New transcript line...",
            "khmer_text": "អត្ថបទខ្មែរថ្មី..."
        }
        self.segments.append(new_seg)
        self._insert_segment_row(row_count, new_seg)

    def _delete_selected_row(self):
        curr_row = self.table.currentRow()
        if curr_row >= 0:
            self.table.removeRow(curr_row)
            if curr_row < len(self.segments):
                self.segments.pop(curr_row)

    def get_updated_segments(self) -> list:
        updated = []
        for i in range(self.table.rowCount()):
            time_item = self.table.item(i, 0)
            orig_item = self.table.item(i, 1)
            khmer_item = self.table.item(i, 2)
            
            orig_seg = self.segments[i] if i < len(self.segments) else {}
            updated.append({
                "start": orig_seg.get("start", i * 3.0),
                "end": orig_seg.get("end", (i + 1) * 3.0),
                "original_text": orig_item.text() if orig_item else "",
                "khmer_text": khmer_item.text() if khmer_item else ""
            })
        return updated

    def _preview_segment_tts(self, row: int):
        khmer_item = self.table.item(row, 2)
        if not khmer_item or not khmer_item.text().strip():
            return
        
        khmer_text = khmer_item.text().strip()
        from utils.file_utils import get_temp_path
        from gui.main_window import clean_speaker_tag
        clean_text, spk, gender, voice = clean_speaker_tag(khmer_text)
        
        svc = VoxCPMService(voice_name=voice)
        out_wav = get_temp_path(f"preview_row_{row}.wav")
        if svc.synthesize(clean_text, out_wav):
            try:
                subprocess.run(["afplay", out_wav], check=False)
            except Exception:
                pass

    def _import_srt(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import SRT Subtitle File", "", "Subtitle Files (*.srt)")
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                from core.srt_translator import SRTTranslator
                translator = SRTTranslator()
                sub_segs = translator.parse_srt(content)
                
                new_segments = []
                for seg in sub_segs:
                    new_segments.append({
                        "start": seg.start_seconds,
                        "end": seg.end_seconds,
                        "original_text": seg.text,
                        "khmer_text": seg.text
                    })
                self.set_segments(new_segments)
            except Exception as e:
                print(f"Error importing SRT: {e}")

    def _export_srt(self):
        updated = self.get_updated_segments()
        if not updated:
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Translated SRT", "khmer_subtitles.srt", "Subtitle Files (*.srt)")
        if file_path:
            try:
                from core.srt_translator import SRTTranslator
                translator = SRTTranslator()
                sub_segs = translator.parse_segment_dicts(updated)
                translator.save_srt(sub_segs, file_path)
            except Exception as e:
                print(f"Error exporting SRT: {e}")


# ==================== LOG CONSOLE ====================
class LogConsoleWidget(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logConsole")
        self.setReadOnly(True)

    def append_log(self, text: str):
        self.append(text)
        sb = self.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())


# ==================== VOICE PROMPT & CHARACTER DESIGNER ====================
class VoicePromptEditor(QWidget):
    """
    Voice Prompt Editor for Character Voice Design.
    Extracts structured voice profile from natural language prompts.
    """
    voice_profile_updated = Signal(object)
    
    def __init__(self, parent=None, character_name: str = None):
        super().__init__(parent)
        self.character_name = character_name or "New Character"
        self.current_profile = None
        self._init_ui()
    
    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Character Name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("👤 Character Name:", self))
        self.name_input = QLineEdit(self.character_name, self)
        self.name_input.setPlaceholderText("e.g., Grandfather, Young Narrator, Police Officer...")
        self.name_input.textChanged.connect(self._on_name_changed)
        name_layout.addWidget(self.name_input, 1)
        layout.addLayout(name_layout)
        
        # Voice Prompt Input
        prompt_layout = QVBoxLayout()
        prompt_layout.addWidget(QLabel("🎭 Voice Prompt (Natural Language):", self))
        self.prompt_input = QTextEdit(self)
        self.prompt_input.setPlaceholderText(
            "Describe the voice characteristics in English or Khmer...\n\n"
            "Examples:\n"
            "- Cambodian male, deep voice, calm narrator, slow and confident\n"
            "- Young female, soft and warm, friendly conversational tone\n"
            "- មនុស្សប្រុស សំឡេងធំ គ្រលរ និយាយរឿងនិទាន ស្ងប់ស្ងាត់\n"
            "- ក្មេងស្រី សំឡេងខ្ពស់ រីករាយ រហ័សរហួន"
        )
        self.prompt_input.setMinimumHeight(100)
        self.prompt_input.textChanged.connect(self._on_prompt_changed)
        prompt_layout.addWidget(self.prompt_input)
        layout.addLayout(prompt_layout)
        
        # Quick Presets
        presets_box = QGroupBox("⚡ Quick Voice Prompt Presets", self)
        presets_box.setStyleSheet("QGroupBox { font-size: 11px; font-weight: bold; color: #38bdf8; }")
        presets_layout = QHBoxLayout(presets_box)
        presets_layout.setSpacing(6)
        
        preset_buttons = [
            ("🎙️ Narrator", "Male, deep voice, calm, professional narrator, medium speed"),
            ("👩 Friendly Female", "Female, warm, friendly, conversational, natural Khmer"),
            ("😡 Intense/Angry", "Male, intense, angry, fast speaking, powerful delivery"),
            ("👧 Little Girl", "Child female, cute, energetic, happy, high pitch"),
            ("👴 Grandfather", "Elderly male, slow, wise, gentle, deep voice"),
        ]
        
        for label, prompt in preset_buttons:
            btn = QPushButton(label, self)
            btn.setProperty("class", "btn-gray")
            btn.setStyleSheet("font-size: 10px; padding: 4px 8px;")
            btn.clicked.connect(lambda _, p=prompt: self.prompt_input.setText(p))
            presets_layout.addWidget(btn)
        
        presets_layout.addStretch()
        layout.addWidget(presets_box)
        
        # Profile Preview Group
        self.profile_preview = QGroupBox("📋 Extracted Character Profile Parameters", self)
        self.profile_preview.setStyleSheet("""
            QGroupBox {
                background-color: #0c101d;
                border: 1px solid #1a233a;
                border-radius: 8px;
                padding: 10px;
                font-weight: bold;
                color: #38bdf8;
            }
        """)
        preview_layout = QVBoxLayout(self.profile_preview)
        self.profile_text = QLabel("Type a prompt above or pick a preset to extract profile parameters.", self)
        self.profile_text.setWordWrap(True)
        self.profile_text.setStyleSheet("color: #94a3b8; font-size: 11px; font-family: 'Menlo', 'Courier New', monospace;")
        preview_layout.addWidget(self.profile_text)
        layout.addWidget(self.profile_preview)
        
        # Reference Audio (Optional for voice cloning)
        ref_group = QGroupBox("🎵 Reference Audio Sample (Optional for Zero-Shot Clone)", self)
        ref_group.setStyleSheet("QGroupBox { font-size: 11px; font-weight: bold; color: #a855f7; }")
        ref_layout = QHBoxLayout(ref_group)
        
        self.ref_path_input = QLineEdit(self)
        self.ref_path_input.setPlaceholderText("Optional: reference audio path for speaker timbre cloning...")
        ref_layout.addWidget(self.ref_path_input, 1)
        
        browse_ref_btn = QPushButton("📂 Browse", self)
        browse_ref_btn.setProperty("class", "btn-gray")
        browse_ref_btn.clicked.connect(self._browse_reference)
        ref_layout.addWidget(browse_ref_btn)
        
        layout.addWidget(ref_group)

        # Test Generation Row
        test_box = QGroupBox("🔊 Live Voice Test Synthesis", self)
        test_box.setStyleSheet("QGroupBox { font-size: 11px; font-weight: bold; color: #10b981; }")
        test_lay = QHBoxLayout(test_box)
        
        self.test_text_input = QLineEdit("សួស្តីអ្នកទាំងអស់គ្នា! នេះជាសំឡេងដែលបានបង្កើតចេញពី Voice Prompt។", self)
        test_lay.addWidget(self.test_text_input, 1)
        
        self.test_btn = QPushButton("▶ Test Voice", self)
        self.test_btn.setProperty("class", "btn-gold")
        self.test_btn.clicked.connect(self._test_voice)
        test_lay.addWidget(self.test_btn)
        
        layout.addWidget(test_box)

    def _on_name_changed(self, text: str):
        self.character_name = text.strip() or "New Character"
        self._extract_profile()

    def _on_prompt_changed(self):
        prompt = self.prompt_input.toPlainText().strip()
        if len(prompt) >= 3:
            self._extract_profile()

    def _extract_profile(self):
        from services.voice_prompt_processor import VoicePromptProcessor
        prompt = self.prompt_input.toPlainText().strip()
        if not prompt:
            self.profile_text.setText("⚠️ Please enter a voice prompt.")
            self.current_profile = None
            return
        
        try:
            profile = VoicePromptProcessor.process_prompt(
                prompt,
                name=self.character_name
            )
            self.current_profile = profile
            
            ref_path = self.ref_path_input.text().strip()
            if ref_path and os.path.exists(ref_path):
                self.current_profile.reference_audio = ref_path
                self.current_profile.is_clone = True
            
            preview_text = (
                f"👤 Name: {profile.name}\n"
                f"├─ Gender: {profile.gender.upper()}  |  Age: {profile.age.upper()}\n"
                f"├─ Emotion: {profile.emotion.upper()}  |  Style: {profile.style.upper()}\n"
                f"├─ Pitch: {profile.pitch.upper()} (Shift: {profile.pitch_shift:.2f}x)  |  Speed: {profile.speed:.2f}x\n"
                f"└─ Acoustic Model: {'Zero-Shot Clone' if profile.is_clone else 'Neural Voice Design'}\n\n"
                f"📝 Description: {profile.voice_description[:160]}..."
            )
            self.profile_text.setText(preview_text)
            self.voice_profile_updated.emit(profile)
        except Exception as e:
            self.profile_text.setText(f"❌ Extraction error: {e}")

    def _browse_reference(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Reference Audio/Video",
            "",
            "All Supported Files (*.wav *.mp3 *.m4a *.flac *.mp4 *.mov *.mkv)"
        )
        if file_path:
            self.ref_path_input.setText(file_path)
            if self.current_profile:
                self.current_profile.reference_audio = file_path
                self.current_profile.is_clone = True
                self._extract_profile()

    def _test_voice(self):
        from utils.file_utils import get_temp_path
        from services.voxcpm_service import VoxCPM2Runner
        import subprocess

        if not self.current_profile:
            self._extract_profile()

        if not self.current_profile:
            return

        test_text = self.test_text_input.text().strip() or "សួស្តីអ្នកទាំងអស់គ្នា!"
        out_wav = get_temp_path("test_character_voice.wav")
        
        self.test_btn.setEnabled(False)
        self.test_btn.setText("⏳ Generating...")
        QApplication.processEvents()

        try:
            runner = VoxCPM2Runner()
            success = runner.generate_with_profile(
                text=test_text,
                profile=self.current_profile,
                reference_audio=self.current_profile.reference_audio,
                reference_text=self.current_profile.reference_text,
                output_path=out_wav,
                mode="full_c"
            )
            if success and os.path.exists(out_wav):
                try:
                    subprocess.Popen(["afplay", out_wav])
                except Exception:
                    pass
        except Exception as e:
            print(f"Error during voice test: {e}")
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("▶ Test Voice")


class VoicePromptDialog(QDialog):
    """Dialog wrapping VoicePromptEditor to save characters to storage"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🎭 Voice Prompt & Character Voice Designer")
        self.setMinimumSize(640, 680)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        self.editor = VoicePromptEditor(self)
        layout.addWidget(self.editor, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setProperty("class", "btn-gray")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("💾 Save Character Profile", self)
        self.save_btn.setProperty("class", "btn-primary")
        self.save_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.save_btn.clicked.connect(self._save_and_accept)
        btn_row.addWidget(self.save_btn)

        layout.addLayout(btn_row)

    def _save_and_accept(self):
        from services.character_voice_manager import CharacterVoiceManager
        from services.voxcpm_service import add_custom_voice_preset
        
        profile = self.editor.current_profile
        if not profile:
            self.editor._extract_profile()
            profile = self.editor.current_profile

        if profile:
            manager = CharacterVoiceManager()
            manager._save_character(profile)
            manager._characters_cache[profile.name] = profile
            
            preset_dict = manager.get_voxcpm_preset(profile.name)
            if preset_dict:
                add_custom_voice_preset(profile.name, preset_dict)
            
            QMessageBox.information(
                self,
                "Character Saved",
                f"✅ Character '{profile.name}' has been saved and is ready for dubbing!"
            )
            self.accept()
        else:
            QMessageBox.warning(self, "Warning", "Please provide a valid voice prompt first.")


# ==================== 🎙️ AI VOICE STUDIO (PROMPT -> TEXT -> GENERATE -> RESULT) ====================
class VoiceStudioWorker(QtCore.QThread):
    """Background Worker for Asynchronous Voice Generation with Real-Time Percentage"""
    progress = Signal(int, str)  # (percent, status_message)
    finished = Signal(str, object)  # (out_wav_path, profile)
    error = Signal(str)

    def __init__(self, prompt_text: str, speech_text: str, parent=None):
        super().__init__(parent)
        self.prompt_text = prompt_text
        self.speech_text = speech_text

    def run(self):
        try:
            from services.voice_prompt_processor import VoicePromptProcessor
            from services.voxcpm_service import VoxCPM2Runner
            from utils.file_utils import get_temp_path
            import time

            # Step 1: Parse prompt
            self.progress.emit(15, "15% • Parsing Voice Prompt & Identity...")
            time.sleep(0.15)
            profile = VoicePromptProcessor.process_prompt(self.prompt_text or "Female Khmer voice", name="Studio Character")
            
            # Step 2: Acoustic Conditioning
            self.progress.emit(35, f"35% • Conditioning Pitch ({profile.pitch_shift:.2f}x) & Timbre...")
            time.sleep(0.15)
            
            # Step 3: VoxCPM2 Neural Synthesis
            self.progress.emit(65, "65% • Synthesizing Neural Speech (VoxCPM2)...")
            out_wav = get_temp_path("ai_voice_studio_output.wav")
            runner = VoxCPM2Runner()
            success = runner.generate_with_profile(
                text=self.speech_text,
                profile=profile,
                output_path=out_wav,
                mode="full_c"
            )

            # Step 4: Mastering & Export
            self.progress.emit(88, "88% • Mastering Audio & Normalizing Loudness...")
            time.sleep(0.1)

            if success and os.path.exists(out_wav):
                self.progress.emit(100, "100% • Generated Successfully! 🎉")
                self.finished.emit(out_wav, profile)
            else:
                self.error.emit("Failed to synthesize audio output.")
        except Exception as e:
            self.error.emit(str(e))


class AIVoiceStudioWidget(QWidget):
    """
    Dedicated AI Character Voice Design Studio:
    - 🎭 Character Voice Attributes (Name, Gender, Age, Voice Type, Tone, Emotion, Style, Speed, Additional)
    - 🤖 Auto-Prompt Builder (Live AI Prompt Generation)
    - 📝 Text to Speak (Khmer Text Input)
    - [ 🎙️ GENERATE VOICE ] (Action Button + 0%-100% Percentage Progress)
    - 🔊 Result (Audio Player with ▶/⏸, Progress Bar, Time, and Save Profile)
    """
    voice_generated = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_audio_path = None
        self.current_profile = None
        self.audio_duration_sec = 0.0
        self.current_pos_sec = 0.0
        self._proc = None
        self._is_playing = False
        self._worker = None
        self._custom_prompt_edited = False
        
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._update_playback_progress)
        
        self._init_ui()
        self._build_prompt_from_form()

    def _init_ui(self):
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(16, 16, 16, 16)
        main_lay.setSpacing(10)

        # Studio Container Card with Scroll Area to support all screens
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        card = QFrame()
        card.setStyleSheet("""
            QFrame#studioCard {
                background-color: #080c16;
                border: 1px solid #1e2942;
                border-radius: 12px;
                padding: 14px;
            }
        """)
        card.setObjectName("studioCard")
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(14, 14, 14, 14)
        c_lay.setSpacing(10)

        # Header: 🎙️ AI Voice Studio
        header_lay = QHBoxLayout()
        icon_lbl = QLabel("🎙️", card)
        icon_lbl.setStyleSheet("font-size: 22px;")
        
        title_box = QVBoxLayout()
        title_lbl = QLabel("AI Character Voice Studio", card)
        title_lbl.setStyleSheet("color: #38bdf8; font-size: 16px; font-weight: 800; letter-spacing: 0.5px;")
        sub_lbl = QLabel("Character Attributes → Auto-Prompt Builder → VoxCPM2 Neural Speech", card)
        sub_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        title_box.addWidget(title_lbl)
        title_box.addWidget(sub_lbl)
        
        header_lay.addWidget(icon_lbl)
        header_lay.addLayout(title_box)
        header_lay.addStretch()
        
        c_lay.addLayout(header_lay)

        # Divider
        sep1 = QFrame(card)
        sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet("background-color: #1a233a; max-height: 1px;")
        c_lay.addWidget(sep1)

        # ==================== 1. CHARACTER ATTRIBUTES FORM ====================
        attr_box = QGroupBox("🎭 Create Character Voice Attributes", card)
        attr_box.setStyleSheet("""
            QGroupBox {
                background-color: #0c101d;
                border: 1px solid #1e2942;
                border-radius: 8px;
                padding: 12px;
                margin-top: 6px;
                font-weight: bold;
                color: #38bdf8;
                font-size: 12px;
            }
        """)
        a_lay = QVBoxLayout(attr_box)
        a_lay.setSpacing(8)

        # Row 1: Character Name, Gender, Age
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Name:", attr_box))
        self.name_input = QLineEdit("Dara", attr_box)
        self.name_input.setPlaceholderText("Character Name (e.g. Dara)")
        self.name_input.textChanged.connect(self._on_form_changed)
        r1.addWidget(self.name_input, stretch=2)

        r1.addWidget(QLabel("Gender:", attr_box))
        self.gender_combo = QComboBox(attr_box)
        self.gender_combo.addItems(["Male", "Female"])
        self.gender_combo.currentIndexChanged.connect(self._on_form_changed)
        r1.addWidget(self.gender_combo, stretch=1)

        r1.addWidget(QLabel("Age:", attr_box))
        self.age_spin = QSpinBox(attr_box)
        self.age_spin.setRange(5, 95)
        self.age_spin.setValue(30)
        self.age_spin.valueChanged.connect(self._on_form_changed)
        r1.addWidget(self.age_spin, stretch=1)

        a_lay.addLayout(r1)

        # Row 2: Voice Type, Tone, Emotion
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Voice Type:", attr_box))
        self.type_combo = QComboBox(attr_box)
        self.type_combo.addItems(["Deep", "Soft", "Raspy", "Crisp", "Resonant", "Warm", "Bright"])
        self.type_combo.currentIndexChanged.connect(self._on_form_changed)
        r2.addWidget(self.type_combo, 1)

        r2.addWidget(QLabel("Tone:", attr_box))
        self.tone_combo = QComboBox(attr_box)
        self.tone_combo.addItems(["Warm", "Bright", "Dark", "Calm", "Gentle", "Authoritative"])
        self.tone_combo.currentIndexChanged.connect(self._on_form_changed)
        r2.addWidget(self.tone_combo, 1)

        r2.addWidget(QLabel("Emotion:", attr_box))
        self.emotion_combo = QComboBox(attr_box)
        self.emotion_combo.addItems(["Calm", "Friendly", "Confident", "Happy", "Intense/Angry", "Sad", "Mysterious"])
        self.emotion_combo.currentIndexChanged.connect(self._on_form_changed)
        r2.addWidget(self.emotion_combo, 1)

        a_lay.addLayout(r2)

        # Row 3: Speaking Style, Speed, Additional Description
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Style:", attr_box))
        self.style_combo = QComboBox(attr_box)
        self.style_combo.addItems(["Natural Conversation", "Professional Narrator", "Storytelling", "Dramatic Movie", "Casual Dialogue"])
        self.style_combo.currentIndexChanged.connect(self._on_form_changed)
        r3.addWidget(self.style_combo, stretch=2)

        r3.addWidget(QLabel("Speed:", attr_box))
        self.speed_combo = QComboBox(attr_box)
        self.speed_combo.addItems(["Slow (0.85x)", "Medium (1.0x)", "Fast (1.15x)"])
        self.speed_combo.setCurrentIndex(1)
        self.speed_combo.currentIndexChanged.connect(self._on_form_changed)
        r3.addWidget(self.speed_combo, stretch=1)

        a_lay.addLayout(r3)

        # Row 4: Additional Description
        r4 = QHBoxLayout()
        r4.addWidget(QLabel("Details:", attr_box))
        self.additional_input = QLineEdit("Professional Cambodian narrator", attr_box)
        self.additional_input.setPlaceholderText("Additional voice details (optional)...")
        self.additional_input.textChanged.connect(self._on_form_changed)
        r4.addWidget(self.additional_input, 1)
        a_lay.addLayout(r4)

        c_lay.addWidget(attr_box)

        # ==================== 2. GENERATED PROMPT VIEWER ====================
        p_hdr = QHBoxLayout()
        prompt_lbl = QLabel("🎙️ Generated Natural Language Prompt", card)
        prompt_lbl.setStyleSheet("color: #38bdf8; font-weight: 700; font-size: 12px;")
        p_hdr.addWidget(prompt_lbl)
        p_hdr.addStretch()

        self.edit_prompt_toggle = QPushButton("✏️ Custom Edit", card)
        self.edit_prompt_toggle.setProperty("class", "btn-gray")
        self.edit_prompt_toggle.setStyleSheet("font-size: 10px; padding: 2px 8px;")
        def _toggle_edit():
            self._custom_prompt_edited = True
            self.prompt_input.setReadOnly(False)
            self.prompt_input.setFocus()
        self.edit_prompt_toggle.clicked.connect(_toggle_edit)
        p_hdr.addWidget(self.edit_prompt_toggle)
        c_lay.addLayout(p_hdr)

        self.prompt_input = QTextEdit(card)
        self.prompt_input.setPlaceholderText("Auto-generated prompt will appear here...")
        self.prompt_input.setMinimumHeight(60)
        self.prompt_input.setMaximumHeight(75)
        self.prompt_input.setStyleSheet("""
            QTextEdit {
                background-color: #0c101d;
                border: 1px solid #1e2942;
                border-radius: 8px;
                padding: 6px 10px;
                color: #f1f5f9;
                font-size: 11px;
                font-family: 'Menlo', 'Courier New', monospace;
            }
            QTextEdit:focus {
                border: 1px solid #38bdf8;
            }
        """)
        c_lay.addWidget(self.prompt_input)

        # ==================== 3. TARGET TEXT TO SPEAK ====================
        t_hdr = QLabel("📝 Text to Speak (Khmer Script)", card)
        t_hdr.setStyleSheet("color: #f1f5f9; font-size: 12px; font-weight: 700; margin-top: 2px;")
        c_lay.addWidget(t_hdr)

        self.text_input = QTextEdit(card)
        self.text_input.setPlaceholderText("បញ្ចូលអត្ថបទខ្មែរដែលត្រូវបង្កើតសំឡេង (Khmer text to synthesize)...")
        self.text_input.setText("សួស្តីអ្នកទាំងអស់គ្នា! ថ្ងៃនេះខ្ញុំនឹងបង្ហាញអំពីបច្ចេកវិទ្យាបង្កើតសំឡេងតាម AI Voice Studio។")
        self.text_input.setMinimumHeight(60)
        self.text_input.setMaximumHeight(75)
        self.text_input.setStyleSheet("""
            QTextEdit {
                background-color: #0c101d;
                border: 1px solid #1e2942;
                border-radius: 8px;
                padding: 6px 10px;
                color: #f1f5f9;
                font-size: 13px;
            }
            QTextEdit:focus {
                border: 1px solid #2563eb;
            }
        """)
        c_lay.addWidget(self.text_input)

        # ==================== 4. GENERATE 🔊 ACTION ====================
        gen_box = QHBoxLayout()
        gen_box.addStretch()
        
        self.generate_btn = QPushButton("🎙️ GENERATE VOICE 🔊", card)
        self.generate_btn.setMinimumHeight(44)
        self.generate_btn.setMinimumWidth(240)
        self.generate_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2563eb, stop:1 #1d4ed8);
                border: 1px solid #3b82f6;
                border-radius: 8px;
                color: #ffffff;
                font-size: 14px;
                font-weight: 800;
                padding: 10px 24px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3b82f6, stop:1 #2563eb);
                border-color: #60a5fa;
            }
            QPushButton:pressed {
                background-color: #1e40af;
            }
            QPushButton:disabled {
                background-color: #1e293b;
                border-color: #334155;
                color: #64748b;
            }
        """)
        self.generate_btn.clicked.connect(self._run_generation)
        gen_box.addWidget(self.generate_btn)
        gen_box.addStretch()
        
        c_lay.addLayout(gen_box)

        # Percentage Progress Bar
        self.progress_bar = QProgressBar(card)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("Ready (0%)")
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #0c101d;
                border: 1px solid #1e2942;
                border-radius: 8px;
                height: 20px;
                text-align: center;
                color: #f1f5f9;
                font-size: 11px;
                font-weight: 700;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #38bdf8);
                border-radius: 7px;
            }
        """)
        self.progress_bar.setVisible(False)
        c_lay.addWidget(self.progress_bar)

        # ==================== 5. RESULT AUDIO PLAYER ====================
        sec4_box = QFrame(card)
        sec4_box.setStyleSheet("""
            QFrame {
                background-color: #0c101d;
                border: 1px solid #1a233a;
                border-radius: 10px;
                padding: 8px 12px;
            }
        """)
        s4_lay = QVBoxLayout(sec4_box)
        s4_lay.setContentsMargins(6, 6, 6, 6)
        s4_lay.setSpacing(6)

        r_header_lay = QHBoxLayout()
        r_lbl = QLabel("🔊 Generated Audio Result", sec4_box)
        r_lbl.setStyleSheet("color: #38bdf8; font-weight: 700; font-size: 12px;")
        r_header_lay.addWidget(r_lbl)
        
        self.badge_lbl = QLabel("Ready to generate", sec4_box)
        self.badge_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        r_header_lay.addStretch()
        r_header_lay.addWidget(self.badge_lbl)
        s4_lay.addLayout(r_header_lay)

        # Player Row: [ ▶ Play ] [ ━━━━━━━━━━━━ Progress Slider ] [ 00:00 / 00:04 ]
        player_lay = QHBoxLayout()
        player_lay.setSpacing(10)

        self.play_pause_btn = QPushButton("▶", sec4_box)
        self.play_pause_btn.setFixedSize(36, 36)
        self.play_pause_btn.setStyleSheet("""
            QPushButton {
                background-color: #2563eb;
                border: 1px solid #3b82f6;
                border-radius: 18px;
                color: #ffffff;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3b82f6;
            }
            QPushButton:disabled {
                background-color: #1e293b;
                border-color: #334155;
                color: #64748b;
            }
        """)
        self.play_pause_btn.setEnabled(False)
        self.play_pause_btn.clicked.connect(self._toggle_playback)
        player_lay.addWidget(self.play_pause_btn)

        self.seek_slider = QSlider(Qt.Horizontal, sec4_box)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setValue(0)
        self.seek_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 6px;
                background: #1e2942;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #38bdf8;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 2px solid #38bdf8;
                width: 14px;
                margin-top: -4px;
                margin-bottom: -4px;
                border-radius: 7px;
            }
        """)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        player_lay.addWidget(self.seek_slider, stretch=1)

        self.time_lbl = QLabel("00:00 / 00:00", sec4_box)
        self.time_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-family: 'Menlo', 'Courier New', monospace; min-width: 80px;")
        player_lay.addWidget(self.time_lbl)

        s4_lay.addLayout(player_lay)

        # Actions below player: Save to Profile & Export
        action_row = QHBoxLayout()
        action_row.addStretch()

        self.save_char_btn = QPushButton("💾 Save Character Profile", sec4_box)
        self.save_char_btn.setProperty("class", "btn-primary")
        self.save_char_btn.setStyleSheet("font-size: 11px; padding: 5px 14px; font-weight: bold;")
        self.save_char_btn.setEnabled(False)
        self.save_char_btn.clicked.connect(self._save_to_character_profile)
        action_row.addWidget(self.save_char_btn)

        self.export_audio_btn = QPushButton("📤 Download WAV", sec4_box)
        self.export_audio_btn.setProperty("class", "btn-gray")
        self.export_audio_btn.setStyleSheet("font-size: 11px; padding: 5px 12px;")
        self.export_audio_btn.setEnabled(False)
        self.export_audio_btn.clicked.connect(self._export_audio)
        action_row.addWidget(self.export_audio_btn)

        s4_lay.addLayout(action_row)
        c_lay.addWidget(sec4_box)

        scroll.setWidget(card)
        main_lay.addWidget(scroll)

    def _on_form_changed(self):
        if not self._custom_prompt_edited:
            self._build_prompt_from_form()

    def _build_prompt_from_form(self):
        from services.voice_prompt_processor import VoicePromptProcessor
        prompt = VoicePromptProcessor.build_natural_prompt(
            gender=self.gender_combo.currentText(),
            age=self.age_spin.value(),
            voice_type=self.type_combo.currentText(),
            tone=self.tone_combo.currentText(),
            emotion=self.emotion_combo.currentText(),
            style=self.style_combo.currentText(),
            speed=self.speed_combo.currentText().split()[0],
            additional=self.additional_input.text().strip()
        )
        self.prompt_input.setPlainText(prompt)

    def _run_generation(self):
        prompt_text = self.prompt_input.toPlainText().strip()
        speech_text = self.text_input.toPlainText().strip()

        if not speech_text:
            QMessageBox.warning(self, "Warning", "Please enter speech text to synthesize.")
            return

        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("⏳ Generating...")
        
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(5)
        self.progress_bar.setFormat("5% • Initializing Engine...")
        
        self.badge_lbl.setText("Starting generation...")
        self.badge_lbl.setStyleSheet("color: #38bdf8;")

        char_name = self.name_input.text().strip() or "Dara"
        self._worker = VoiceStudioWorker(prompt_text, speech_text, self)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._worker.start()

    def _on_worker_progress(self, percent: int, msg: str):
        self.progress_bar.setValue(percent)
        self.progress_bar.setFormat(msg)
        self.badge_lbl.setText(msg)

    def _on_worker_finished(self, out_wav: str, profile):
        import wave
        self.current_audio_path = out_wav
        self.current_profile = profile
        char_name = self.name_input.text().strip() or "Dara"
        self.current_profile.name = char_name

        try:
            with wave.open(out_wav, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                self.audio_duration_sec = frames / float(rate)
        except Exception:
            self.audio_duration_sec = 4.0

        self.badge_lbl.setText(f"✓ Generated • {self.audio_duration_sec:.1f}s • {profile.gender.capitalize()} {profile.age} (Pitch: {profile.pitch_shift:.2f}x)")
        self.badge_lbl.setStyleSheet("color: #10b981; font-weight: bold;")
        
        self.play_pause_btn.setEnabled(True)
        self.save_char_btn.setEnabled(True)
        self.export_audio_btn.setEnabled(True)
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("🎙️ GENERATE VOICE 🔊")
        
        # Reset player & start audition
        self._stop_playback()
        self._start_playback()
        self.voice_generated.emit(out_wav, profile)

    def _on_worker_error(self, err_msg: str):
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("🎙️ GENERATE VOICE 🔊")
        self.badge_lbl.setText(f"Error: {err_msg}")
        self.badge_lbl.setStyleSheet("color: #ef4444;")
        self.progress_bar.setFormat("Generation Failed ❌")
        QMessageBox.critical(self, "Error", f"Voice generation error: {err_msg}")

    def _toggle_playback(self):
        if self._is_playing:
            self._pause_playback()
        else:
            self._start_playback()

    def _start_playback(self):
        if not self.current_audio_path or not os.path.exists(self.current_audio_path):
            return
        
        try:
            if self._proc and self._proc.poll() is None:
                try: self._proc.terminate()
                except Exception: pass
            
            if sys.platform == "darwin":
                self._proc = subprocess.Popen(["afplay", "-v", "1", self.current_audio_path])
            elif sys.platform.startswith("linux"):
                self._proc = subprocess.Popen(["aplay", self.current_audio_path])

            self._is_playing = True
            self.play_pause_btn.setText("⏸")
            self._timer.start()
        except Exception as e:
            print(f"Playback error: {e}")

    def _pause_playback(self):
        if self._proc and self._proc.poll() is None:
            try: self._proc.terminate()
            except Exception: pass
        self._is_playing = False
        self.play_pause_btn.setText("▶")
        self._timer.stop()

    def _stop_playback(self):
        self._pause_playback()
        self.current_pos_sec = 0.0
        self.seek_slider.setValue(0)
        self._update_time_label()

    def _update_playback_progress(self):
        if not self._is_playing:
            return
        
        if self._proc and self._proc.poll() is not None:
            # Playback finished
            self._stop_playback()
            return

        self.current_pos_sec += 0.1
        if self.audio_duration_sec > 0:
            val = int((self.current_pos_sec / self.audio_duration_sec) * 1000)
            self.seek_slider.setValue(min(1000, val))
        self._update_time_label()

    def _on_seek_moved(self, value: int):
        if self.audio_duration_sec > 0:
            self.current_pos_sec = (value / 1000.0) * self.audio_duration_sec
            self._update_time_label()

    def _update_time_label(self):
        pos_m = int(self.current_pos_sec // 60)
        pos_s = int(self.current_pos_sec % 60)
        dur_m = int(self.audio_duration_sec // 60)
        dur_s = int(self.audio_duration_sec % 60)
        self.time_lbl.setText(f"{pos_m:02d}:{pos_s:02d} / {dur_m:02d}:{dur_s:02d}")

    def _save_to_character_profile(self):
        if not self.current_profile:
            return
        
        from qt_compat import QInputDialog
        from services.character_voice_manager import CharacterVoiceManager
        from services.voxcpm_service import add_custom_voice_preset

        char_name, ok = QInputDialog.getText(self, "Save Character", "Enter Character Name:", QLineEdit.Normal, self.current_profile.name)
        if ok and char_name.strip():
            self.current_profile.name = char_name.strip()
            manager = CharacterVoiceManager()
            manager._save_character(self.current_profile)
            manager._characters_cache[self.current_profile.name] = self.current_profile
            
            preset = manager.get_voxcpm_preset(self.current_profile.name)
            if preset:
                add_custom_voice_preset(self.current_profile.name, preset)
            
            QMessageBox.information(self, "Saved", f"✅ Character '{self.current_profile.name}' saved to Character Profiles & Presets!")

    def _export_audio(self):
        if not self.current_audio_path or not os.path.exists(self.current_audio_path):
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Synthesized Audio", "ai_voice_studio.wav", "WAV Audio (*.wav);;MP3 Audio (*.mp3)")
        if file_path:
            try:
                import shutil
                shutil.copyfile(self.current_audio_path, file_path)
                QMessageBox.information(self, "Exported", f"✅ Audio exported to:\n{file_path}")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to export: {e}")


class AIVoiceStudioDialog(QDialog):
    """Unified Dialog for AI Voice Studio & Character Designer"""
    def __init__(self, parent=None, initial_tab: int = 0):
        super().__init__(parent)
        self.setWindowTitle("🎙️ AI Voice Studio & Character Voice Designer")
        self.setMinimumSize(680, 720)
        self.resize(720, 740)
        self.setStyleSheet("""
            QDialog {
                background-color: #080c16;
                color: #f1f5f9;
                font-family: 'Segoe UI', 'Kantumruy Pro', 'Khmer OS Battambang', sans-serif;
            }
            QTabWidget::pane {
                border: 1px solid #1e2942;
                border-radius: 8px;
                background-color: #0a0e1a;
                top: -1px;
            }
            QTabBar::tab {
                background: #080c16;
                color: #94a3b8;
                border: 1px solid #1e2942;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 2px;
                font-weight: 600;
                font-size: 12px;
            }
            QTabBar::tab:selected {
                background: #0a0e1a;
                color: #38bdf8;
                border-color: #2563eb;
                border-bottom: 2px solid #38bdf8;
            }
        """)
        
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(12, 12, 12, 12)
        main_lay.setSpacing(10)

        self.tabs = QTabWidget(self)
        
        # Tab 1: AI Voice Studio
        self.studio = AIVoiceStudioWidget(self)
        self.tabs.addTab(self.studio, "🎙️ AI Voice Studio (Prompt → Audio)")

        # Tab 2: Character Profile Designer
        self.designer_container = QWidget()
        d_lay = QVBoxLayout(self.designer_container)
        d_lay.setContentsMargins(8, 8, 8, 8)
        self.editor = VoicePromptEditor(self.designer_container)
        d_lay.addWidget(self.editor, 1)

        d_actions = QHBoxLayout()
        d_actions.addStretch()
        
        save_profile_btn = QPushButton("💾 Save Character Profile", self.designer_container)
        save_profile_btn.setProperty("class", "btn-primary")
        save_profile_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        def _save_designer_profile():
            from services.character_voice_manager import CharacterVoiceManager
            from services.voxcpm_service import add_custom_voice_preset
            
            profile = self.editor.current_profile
            if not profile:
                self.editor._extract_profile()
                profile = self.editor.current_profile

            if profile:
                manager = CharacterVoiceManager()
                manager._save_character(profile)
                manager._characters_cache[profile.name] = profile
                
                preset_dict = manager.get_voxcpm_preset(profile.name)
                if preset_dict:
                    add_custom_voice_preset(profile.name, preset_dict)
                
                QMessageBox.information(
                    self,
                    "Character Saved",
                    f"✅ Character '{profile.name}' has been saved and is ready for dubbing!"
                )
                self.accept()
            else:
                QMessageBox.warning(self, "Warning", "Please provide a valid voice prompt first.")

        save_profile_btn.clicked.connect(_save_designer_profile)
        d_actions.addWidget(save_profile_btn)
        d_lay.addLayout(d_actions)

        self.tabs.addTab(self.designer_container, "🎭 Character Profile Designer")
        self.tabs.setCurrentIndex(initial_tab)

        main_lay.addWidget(self.tabs, 1)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch()
        close_btn = QPushButton("Close", self)
        close_btn.setProperty("class", "btn-gray")
        close_btn.setStyleSheet("padding: 6px 18px; font-weight: bold;")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        main_lay.addLayout(footer)


# ==================== GEMINI API KEY DIALOG ====================
class GeminiApiKeyDialog(QDialog):
    """
    Dedicated dialog for persistent Google Gemini API Key configuration,
    supporting Multi-Account API Key Pool (Load Balancing & Auto-Failover),
    Rate Limit / Quota Dashboard, dedicated Add Key input, and individual key management.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔑 Google Gemini API Key - Multi-Account Pool & Rate Limit Dashboard")
        self.resize(680, 530)
        self.setStyleSheet("""
            QDialog {
                background-color: #0b0f19;
                color: #e2e8f0;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            }
        """)
        from utils.config_manager import get_gemini_api_keys
        self.keys = get_gemini_api_keys()
        self._key_status_labels = {}
        self._init_ui()

    def _init_ui(self):
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(22, 18, 22, 18)
        main_lay.setSpacing(12)

        # Header Title
        title_lbl = QLabel("🌐 Google Gemini AI Translation & Multi-Account Key Pool", self)
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 800; color: #38bdf8;")
        main_lay.addWidget(title_lbl)

        # 1. RATE LIMIT & QUOTA DASHBOARD
        self.dashboard_frame = QFrame(self)
        self.dashboard_frame.setStyleSheet("""
            QFrame {
                background-color: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        dash_lay = QHBoxLayout(self.dashboard_frame)
        dash_lay.setContentsMargins(10, 8, 10, 8)
        dash_lay.setSpacing(12)

        # Metric 1: Key Count
        self.m1_box = self._create_metric_card("🔑 Key Pool", "1 Account", "#38bdf8", "Auto-Rotation")
        dash_lay.addWidget(self.m1_box)

        # Metric 2: Rate Limit (RPM)
        self.m2_box = self._create_metric_card("⚡ Combined Rate Limit", "15 RPM", "#a855f7", "Requests / Minute")
        dash_lay.addWidget(self.m2_box)

        # Metric 3: Daily Quota (RPD)
        self.m3_box = self._create_metric_card("📊 Daily Quota Limit", "~1,500 RPD", "#10b981", "Requests / Day")
        dash_lay.addWidget(self.m3_box)

        # Metric 4: Health Status
        self.m4_box = self._create_metric_card("🛡️ Status", "🟢 Healthy", "#34d399", "Zero Failures")
        dash_lay.addWidget(self.m4_box)

        main_lay.addWidget(self.dashboard_frame)

        # 2. DEDICATED INPUT SECTION TO ADD NEW KEY
        add_box = QFrame(self)
        add_box.setStyleSheet("background-color: #060911; border: 1px solid #1e2942; border-radius: 8px;")
        add_lay = QHBoxLayout(add_box)
        add_lay.setContentsMargins(10, 6, 10, 6)
        add_lay.setSpacing(8)

        add_icon = QLabel("➕", add_box)
        add_lay.addWidget(add_icon)

        self.new_key_input = QLineEdit(add_box)
        self.new_key_input.setPlaceholderText("Paste new Gemini API Key here (e.g. AIzaSy... ឬចុះបន្ទាត់ដាក់ច្រើន...)")
        self.new_key_input.setStyleSheet("background: transparent; border: none; color: #00e676; font-size: 13px; font-family: 'Menlo', 'Courier New', monospace;")
        self.new_key_input.returnPressed.connect(self._add_key)
        add_lay.addWidget(self.new_key_input, 1)

        add_btn = QPushButton("➕ Add Key", add_box)
        add_btn.setStyleSheet("""
            QPushButton {
                background-color: #0284c7;
                color: #ffffff;
                font-weight: 700;
                font-size: 12px;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background-color: #0369a1;
            }
        """)
        add_btn.clicked.connect(self._add_key)
        add_lay.addWidget(add_btn)

        main_lay.addWidget(add_box)

        # 3. LIST OF ADDED KEYS WITH RATE LIMIT STATUS
        list_header_lay = QHBoxLayout()
        list_title = QLabel("📋 Active Key Pool List (បណ្តុំ API Keys សកម្ម):", self)
        list_title.setStyleSheet("font-size: 12px; font-weight: 700; color: #94a3b8;")
        list_header_lay.addWidget(list_title)
        list_header_lay.addStretch()

        self.key_counter_lbl = QLabel(f"សរុប: {len(self.keys)} Keys", self)
        self.key_counter_lbl.setStyleSheet("font-size: 11px; font-weight: 700; color: #38bdf8;")
        list_header_lay.addWidget(self.key_counter_lbl)
        main_lay.addLayout(list_header_lay)

        # Scroll Area for Key Cards
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { background-color: #080d1a; border: 1px solid #161f36; border-radius: 6px; }")
        
        self.keys_container = QWidget()
        self.keys_container.setStyleSheet("background-color: transparent;")
        self.keys_vbox = QVBoxLayout(self.keys_container)
        self.keys_vbox.setContentsMargins(8, 8, 8, 8)
        self.keys_vbox.setSpacing(8)
        self.keys_vbox.addStretch()

        self.scroll_area.setWidget(self.keys_container)
        main_lay.addWidget(self.scroll_area, 1)

        # Get Free Key Link
        link_lbl = QLabel(
            "<a href='https://aistudio.google.com/app/apikey' style='color: #60a5fa; text-decoration: none; font-weight: 600;'>👉 ចុចទីនេះដើម្បីយក Free Gemini API Key (Google AI Studio)</a>",
            self
        )
        link_lbl.setOpenExternalLinks(True)
        link_lbl.setStyleSheet("font-size: 11px;")
        main_lay.addWidget(link_lbl)

        # 4. ACTION BUTTONS
        btn_lay = QHBoxLayout()
        self.test_all_btn = QPushButton("⚡ Test All Keys", self)
        self.test_all_btn.setStyleSheet("font-weight: 700; padding: 6px 14px; background-color: #6366f1; color: white; border-radius: 6px; font-size: 12px;")
        self.test_all_btn.clicked.connect(self._test_all_keys)
        btn_lay.addWidget(self.test_all_btn)

        btn_lay.addStretch()

        cancel_btn = QPushButton("Cancel", self)
        cancel_btn.setProperty("class", "btn-gray")
        cancel_btn.setStyleSheet("background-color: #334155; color: white; border-radius: 6px; padding: 6px 14px;")
        cancel_btn.clicked.connect(self.reject)
        btn_lay.addWidget(cancel_btn)

        save_btn = QPushButton("💾 Save Key Pool", self)
        save_btn.setStyleSheet("font-weight: 700; padding: 6px 20px; background-color: #10b981; color: white; border-radius: 6px; font-size: 12px;")
        save_btn.clicked.connect(self._save_keys)
        btn_lay.addWidget(save_btn)

        main_lay.addLayout(btn_lay)

        # Initial Render
        self._render_key_list()
        self._update_dashboard()

    def _create_metric_card(self, title: str, value: str, color: str, sub: str) -> QFrame:
        card = QFrame(self)
        card.setStyleSheet(f"background-color: #1e293b; border-radius: 6px; border: 1px solid #334155; padding: 6px;")
        v = QVBoxLayout(card)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(2)

        t_lbl = QLabel(title, card)
        t_lbl.setStyleSheet("font-size: 10px; font-weight: 700; color: #94a3b8;")
        v.addWidget(t_lbl)

        val_lbl = QLabel(value, card)
        val_lbl.setObjectName("val_lbl")
        val_lbl.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {color};")
        v.addWidget(val_lbl)

        sub_lbl = QLabel(sub, card)
        sub_lbl.setObjectName("sub_lbl")
        sub_lbl.setStyleSheet("font-size: 9px; color: #64748b;")
        v.addWidget(sub_lbl)
        return card

    def _update_dashboard(self):
        count = len(self.keys)
        # Update Metric 1: Count
        v1 = self.m1_box.findChild(QLabel, "val_lbl")
        s1 = self.m1_box.findChild(QLabel, "sub_lbl")
        if v1:
            v1.setText(f"{count} Account{'s' if count != 1 else ''}")
        if s1:
            s1.setText("Auto-Rotation Active" if count > 1 else "Single Account")

        # Update Metric 2: RPM
        v2 = self.m2_box.findChild(QLabel, "val_lbl")
        s2 = self.m2_box.findChild(QLabel, "sub_lbl")
        rpm = count * 15
        if v2:
            v2.setText(f"{rpm} RPM")
        if s2:
            s2.setText(f"({count} × 15 RPM Free Tier)" if count > 0 else "0 RPM")

        # Update Metric 3: RPD
        v3 = self.m3_box.findChild(QLabel, "val_lbl")
        s3 = self.m3_box.findChild(QLabel, "sub_lbl")
        rpd = count * 1500
        if v3:
            v3.setText(f"~{rpd:,} RPD")
        if s3:
            s3.setText(f"({count} × 1,500 RPD Limit)")

        # Update Metric 4: Health
        v4 = self.m4_box.findChild(QLabel, "val_lbl")
        s4 = self.m4_box.findChild(QLabel, "sub_lbl")
        if count == 0:
            if v4: v4.setText("⚠️ No Key")
            if s4: s4.setText("Key Required")
        elif count == 1:
            if v4: v4.setText("🟢 Normal")
            if s4: s4.setText("Single Pool")
        else:
            if v4: v4.setText(f"🚀 {count}x Quota")
            if s4: s4.setText("Auto-Failover Ready")

        self.key_counter_lbl.setText(f"សរុប: {count} Keys")

    def _render_key_list(self):
        # Clear existing items
        self._key_status_labels.clear()
        while self.keys_vbox.count() > 1:
            item = self.keys_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.keys:
            empty_lbl = QLabel("⚠️ មិនទាន់មាន Key នៅក្នុង Pool នៅឡើយ។ សូម Paste និងចុច '➕ Add Key' ខាងលើ។", self.keys_container)
            empty_lbl.setStyleSheet("color: #64748b; font-size: 11px; padding: 12px; font-style: italic;")
            self.keys_vbox.insertWidget(0, empty_lbl)
            return

        for idx, k in enumerate(self.keys):
            card = QFrame(self.keys_container)
            card.setStyleSheet("""
                QFrame {
                    background-color: #0f172a;
                    border: 1px solid #1e293b;
                    border-radius: 6px;
                    padding: 4px;
                }
                QFrame:hover {
                    border: 1px solid #38bdf8;
                }
            """)
            c_lay = QHBoxLayout(card)
            c_lay.setContentsMargins(10, 6, 10, 6)
            c_lay.setSpacing(10)

            # Index badge
            idx_badge = QLabel(f"#{idx+1}", card)
            idx_badge.setStyleSheet("font-size: 11px; font-weight: 800; color: #38bdf8; background-color: #0369a1; border-radius: 4px; padding: 2px 6px;")
            c_lay.addWidget(idx_badge)

            # Key preview (Masked)
            masked = k[:8] + "..." + k[-6:] if len(k) > 16 else k
            k_lbl = QLabel(masked, card)
            k_lbl.setToolTip(k)
            k_lbl.setStyleSheet("font-family: 'Menlo', 'Courier New', monospace; font-size: 12px; color: #00e676; font-weight: 700;")
            c_lay.addWidget(k_lbl)

            # Rate Limit Badge
            rl_badge = QLabel("⚡ 15 RPM / 1.5k RPD", card)
            rl_badge.setStyleSheet("font-size: 10px; color: #94a3b8; background-color: #1e293b; border-radius: 4px; padding: 2px 6px;")
            c_lay.addWidget(rl_badge)

            c_lay.addStretch()

            # Status Label
            stat_lbl = QLabel("🟢 Ready", card)
            stat_lbl.setStyleSheet("font-size: 11px; color: #34d399; font-weight: 600;")
            self._key_status_labels[k] = stat_lbl
            c_lay.addWidget(stat_lbl)

            # Individual Test Button
            test_b = QPushButton("⚡ Test", card)
            test_b.setStyleSheet("background-color: #4338ca; color: white; border: none; border-radius: 4px; padding: 3px 8px; font-size: 10px; font-weight: 700;")
            test_b.clicked.connect(lambda checked=False, target_key=k, target_lbl=stat_lbl: self._test_single_key(target_key, target_lbl))
            c_lay.addWidget(test_b)

            # Remove Button
            del_b = QPushButton("🗑️", card)
            del_b.setToolTip("លុប Key នេះចេញពី Pool")
            del_b.setStyleSheet("background-color: #991b1b; color: white; border: none; border-radius: 4px; padding: 3px 8px; font-size: 11px;")
            del_b.clicked.connect(lambda checked=False, target_key=k: self._remove_key(target_key))
            c_lay.addWidget(del_b)

            self.keys_vbox.insertWidget(idx, card)

    def _add_key(self):
        from utils.config_manager import parse_key_string
        raw = self.new_key_input.text().strip()
        if not raw:
            return

        new_keys = parse_key_string(raw)
        added_count = 0
        for nk in new_keys:
            if nk and nk not in self.keys:
                self.keys.append(nk)
                added_count += 1

        self.new_key_input.clear()
        self._render_key_list()
        self._update_dashboard()

    def _remove_key(self, target_key: str):
        if target_key in self.keys:
            self.keys.remove(target_key)
            self._render_key_list()
            self._update_dashboard()

    def _test_single_key(self, target_key: str, stat_lbl: QLabel):
        stat_lbl.setText("⏳ Testing...")
        stat_lbl.setStyleSheet("color: #38bdf8; font-size: 11px;")
        QApplication.processEvents()

        from utils.config_manager import test_gemini_api_key
        ok, msg = test_gemini_api_key(target_key)
        if ok:
            stat_lbl.setText("✅ 200 OK (Active)")
            stat_lbl.setStyleSheet("color: #34d399; font-weight: bold; font-size: 11px;")
        else:
            stat_lbl.setText("❌ Failed")
            stat_lbl.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 11px;")
            QMessageBox.warning(self, "API Key Error", f"Key មិនអាចភ្ជាប់បានទេ:\n{msg}")

    def _test_all_keys(self):
        if not self.keys:
            QMessageBox.warning(self, "Warning", "មិនទាន់មាន Key នៅក្នុង Pool សម្រាប់តេស្តទេ។")
            return

        from utils.config_manager import test_gemini_api_key
        for k in self.keys:
            lbl = self._key_status_labels.get(k)
            if lbl:
                lbl.setText("⏳ Testing...")
                lbl.setStyleSheet("color: #38bdf8; font-size: 11px;")

        QApplication.processEvents()

        all_ok = True
        for k in self.keys:
            lbl = self._key_status_labels.get(k)
            ok, msg = test_gemini_api_key(k)
            if ok:
                if lbl:
                    lbl.setText("✅ 200 OK (Active)")
                    lbl.setStyleSheet("color: #34d399; font-weight: bold; font-size: 11px;")
            else:
                all_ok = False
                if lbl:
                    lbl.setText("❌ Error")
                    lbl.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 11px;")

        if all_ok:
            QMessageBox.information(
                self, "Pool Test Succeeded",
                f"🎉 Key ទាំងអស់ ({len(self.keys)} Accounts) បានផ្ទៀងផ្ទាត់ជោគជ័យ!\n\n"
                f"⚡ Combined Rate Limit: {len(self.keys) * 15} RPM\n"
                f"📊 Combined Quota: ~{len(self.keys) * 1500:,} RPD\n"
                f"🔄 Key Rotation & Auto-Failover ត្រៀមជាស្រេច!"
            )

    def _save_keys(self):
        if not self.keys:
            QMessageBox.warning(self, "Warning", "សូមបញ្ចូល API Key យ៉ាងហោចណាស់មួយមុននឹង Save!")
            return

        from utils.config_manager import save_gemini_api_keys
        if save_gemini_api_keys(self.keys):
            count = len(self.keys)
            QMessageBox.information(
                self, "Key Pool Saved",
                f"🎉 បានរក្សាទុក {count} Gemini API Key(s) ទៅកាន់ប្រព័ន្ធ Key Pool ដោយជោគជ័យ!\n\n"
                f"• Rate Limit សរុប: {count * 15} Requests/នាទី\n"
                f"• Quota សរុប: ~{count * 1500:,} Requests/ថ្ងៃ\n"
                f"• ប្រព័ន្ធ Load Balancing ត្រូវបានបើកដំណើរការ!"
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "មិនអាចរក្សាទុក API Key Pool បានទេ។")
            return

        if save_gemini_api_keys(keys):
            count = len(keys)
            msg = f"🎉 បានរក្សាទុក {count} Gemini API Key(s) ដោយជោគជ័យ!"
            if count > 1:
                msg += f"\nប្រព័ន្ធ Key Pool ត្រូវបានបើកដំណើរការ (Quota & ល្បឿនកើន {count}x ដង)!"
            QMessageBox.information(self, "Success", msg)
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "មិនអាចរក្សាទុក API Key បានទេ។")


# ==================== EXPORT VIDEO SETTINGS DIALOG ====================
class ExportSettingsDialog(QDialog):
    """
    CapCut Desktop Pro Exact Replica Export Dialog (Image 1: media_1790773904413.png):
    - Left Column:
      * Video Cover Preview with '✏️ Edit cover' button
      * Story Folder & Episode metadata
    - Right Column:
      * Export timeline: Timeline 01
      * Name: Video project name (editable)
      * Export to: Destination folder path + folder picker button
      * Video Options:
        - Remove watermark 💎 (toggle switch)
        - Resolution dropdown (1080P, 4K, 2K, 720P, 480P, Original)
        - Bit rate dropdown (Recommended, Higher, Lower)
        - Codec dropdown (H.264, HEVC / H.265, ProRes)
        - Format dropdown (mp4, mov, mkv)
        - Frame rate dropdown (30fps, 60fps, 24fps, 25fps)
        - Color space label (Rec. 709 SDR)
      * Sync exported videos to space checkbox
    - Bottom Footer:
      * Dynamic duration & size badge: "Duration: 4m 28s | Size: about 410 MB"
      * Cancel button (dark gray) and Export button (CapCut signature cyan #00b8b8)
    """
    def __init__(
        self,
        parent=None,
        video_path: str = "",
        initial_res: str = "1080P",
        initial_mode: str = "Balanced",
        initial_bgm: int = 30,
        story_folder: str = "My_Story",
        episode: int = 1,
        output_dir: str = "",
        timeline_name: str = "Timeline 01"
    ):
        super().__init__(parent)
        self.video_path = video_path or ""
        self.initial_res = initial_res or "1080P"
        self.initial_mode = initial_mode or "Balanced"
        self.initial_bgm = initial_bgm if initial_bgm is not None else 30
        self.story_folder = story_folder or "My_Story"
        self.episode = episode or 1
        self.output_dir = output_dir or os.path.abspath("output")
        self.timeline_name = timeline_name or "Timeline 01"

        # Determine default project name
        base_name = os.path.splitext(os.path.basename(self.video_path))[0] if self.video_path else time.strftime("%m%d (1)")
        if not base_name or base_name == "safe_input":
            base_name = time.strftime("%m%d (1)")
        self.project_name = base_name

        self.setWindowTitle(f"Export-{self.project_name}")
        self.setFixedSize(780, 560)
        self.setStyleSheet("""
            QDialog {
                background-color: #18191c;
                color: #e2e8f0;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            }
            QLabel {
                color: #e2e8f0;
                font-size: 12px;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #23252b;
                border: 1px solid #323640;
                border-radius: 6px;
                color: #ffffff;
                padding: 5px 10px;
                font-size: 12px;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 1px solid #00b8b8;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QCheckBox {
                color: #e2e8f0;
                font-size: 12px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #4a5260;
                border-radius: 4px;
                background-color: #23252b;
            }
            QCheckBox::indicator:checked {
                background-color: #00b8b8;
                border-color: #00b8b8;
                image: none;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
        """)

        # Video metadata probe
        self.orig_w = 1920
        self.orig_h = 1080
        self.fps = 30.0
        self.duration_sec = 0.0
        self.cover_pixmap = None

        if self.video_path and os.path.exists(self.video_path):
            try:
                cap = cv2.VideoCapture(self.video_path)
                if cap.isOpened():
                    self.orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
                    self.orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
                    self.fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
                    frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
                    self.duration_sec = (frames / self.fps) if self.fps > 0 else 0.0
                    
                    # Extract cover frame at 1.0s or 0.0s
                    cap.set(cv2.CAP_PROP_POS_MSEC, min(1000, self.duration_sec * 500))
                    ret, frame = cap.read()
                    if not ret:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = cap.read()
                    cap.release()
                    if ret and frame is not None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        h, w, ch = rgb.shape
                        qimg = QtGui.QImage(rgb.data, w, h, ch * w, QtGui.QImage.Format_RGB888)
                        self.cover_pixmap = QtGui.QPixmap.fromImage(qimg)
            except Exception:
                pass

        if not self.cover_pixmap or self.cover_pixmap.isNull():
            # Fallback placeholder cover
            self.cover_pixmap = QtGui.QPixmap(320, 180)
            self.cover_pixmap.fill(QtGui.QColor("#1e293b"))

        self._init_ui()

    def _init_ui(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(20, 16, 20, 16)
        root_lay.setSpacing(14)

        # ---------------- TOP TITLE BAR ----------------
        top_bar = QHBoxLayout()
        top_title = QLabel(f"Export-{self.project_name}", self)
        top_title.setStyleSheet("font-size: 13px; font-weight: 700; color: #f8fafc;")
        top_bar.addWidget(top_title)
        top_bar.addStretch()
        root_lay.addLayout(top_bar)

        # ---------------- MAIN 2-COLUMN BODY ----------------
        body_lay = QHBoxLayout()
        body_lay.setSpacing(24)

        # ===== LEFT COLUMN: COVER & METADATA =====
        left_col = QVBoxLayout()
        left_col.setSpacing(12)

        # Video Cover Box with 'Edit cover' Overlay
        cover_container = QFrame(self)
        cover_container.setFixedSize(320, 190)
        cover_container.setStyleSheet("""
            QFrame {
                background-color: #0b0f19;
                border: 1px solid #2d3139;
                border-radius: 8px;
            }
        """)
        c_inner_lay = QVBoxLayout(cover_container)
        c_inner_lay.setContentsMargins(0, 0, 0, 0)

        self.cover_lbl = QLabel(cover_container)
        self.cover_lbl.setAlignment(Qt.AlignCenter)
        self.cover_lbl.setScaledContents(True)
        self.cover_lbl.setStyleSheet("border-radius: 8px;")
        c_inner_lay.addWidget(self.cover_lbl)

        # Overlay Button: 'Edit cover' (Placed at top-left of cover)
        self.edit_cover_btn = QPushButton("✏️ Edit cover", cover_container)
        self.edit_cover_btn.setGeometry(10, 10, 95, 26)
        self.edit_cover_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(24, 25, 28, 0.78);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.25);
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: rgba(38, 41, 48, 0.95);
                border-color: rgba(255, 255, 255, 0.5);
            }
        """)
        self.edit_cover_btn.clicked.connect(self._on_edit_cover_clicked)

        left_col.addWidget(cover_container)

        # Left Column Secondary Settings: Story & Episode
        story_box = QFrame(self)
        story_box.setStyleSheet("""
            QFrame {
                background-color: #1e2025;
                border: 1px solid #2a2d35;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        sb_lay = QGridLayout(story_box)
        sb_lay.setContentsMargins(6, 6, 6, 6)
        sb_lay.setHorizontalSpacing(10)
        sb_lay.setVerticalSpacing(8)

        st_lbl = QLabel("Folder រឿង:", story_box)
        st_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600;")
        self.story_edit = QLineEdit(self.story_folder, story_box)
        self.story_edit.setPlaceholderText("Story Folder Name")
        sb_lay.addWidget(st_lbl, 0, 0)
        sb_lay.addWidget(self.story_edit, 0, 1)

        ep_lbl = QLabel("ភាគ (EP):", story_box)
        ep_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600;")
        self.ep_spin = QSpinBox(story_box)
        self.ep_spin.setRange(1, 9999)
        self.ep_spin.setValue(self.episode)
        self.ep_spin.setPrefix("EP ")
        sb_lay.addWidget(ep_lbl, 1, 0)
        sb_lay.addWidget(self.ep_spin, 1, 1)

        bgm_lbl = QLabel("BGM Vol:", story_box)
        bgm_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600;")
        self.bgm_spin = QSpinBox(story_box)
        self.bgm_spin.setRange(0, 100)
        self.bgm_spin.setValue(self.initial_bgm)
        self.bgm_spin.setSuffix("%")
        self.bgm_spin.valueChanged.connect(self._update_estimates)
        sb_lay.addWidget(bgm_lbl, 2, 0)
        sb_lay.addWidget(self.bgm_spin, 2, 1)

        left_col.addWidget(story_box)
        left_col.addStretch()
        body_lay.addLayout(left_col, 0)

        # ===== RIGHT COLUMN: EXPORT PARAMETERS =====
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        # Row 1: Export timeline
        r1 = QHBoxLayout()
        r1_lbl = QLabel("Export timeline", self)
        r1_lbl.setFixedWidth(110)
        r1_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        r1_val = QLabel(self.timeline_name, self)
        r1_val.setStyleSheet("color: #f1f5f9; font-weight: 600; font-size: 12px;")
        r1.addWidget(r1_lbl)
        r1.addWidget(r1_val)
        r1.addStretch()
        right_col.addLayout(r1)

        # Row 2: Name
        r2 = QHBoxLayout()
        r2_lbl = QLabel("Name", self)
        r2_lbl.setFixedWidth(110)
        r2_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.name_edit = QLineEdit(self.project_name, self)
        self.name_edit.textChanged.connect(lambda t: self.setWindowTitle(f"Export-{t}"))
        r2.addWidget(r2_lbl)
        r2.addWidget(self.name_edit)
        right_col.addLayout(r2)

        # Row 3: Export to
        r3 = QHBoxLayout()
        r3_lbl = QLabel("Export to", self)
        r3_lbl.setFixedWidth(110)
        r3_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.export_to_edit = QLineEdit(self.output_dir, self)
        self.browse_dir_btn = QPushButton("📁", self)
        self.browse_dir_btn.setFixedSize(32, 28)
        self.browse_dir_btn.setStyleSheet("""
            QPushButton {
                background-color: #23252b;
                border: 1px solid #323640;
                border-radius: 6px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #323640;
            }
        """)
        self.browse_dir_btn.clicked.connect(self._on_browse_output_dir)
        r3.addWidget(r3_lbl)
        r3.addWidget(self.export_to_edit)
        r3.addWidget(self.browse_dir_btn)
        right_col.addLayout(r3)

        # Section: Video ⌃
        vid_sec = QFrame(self)
        vid_sec.setStyleSheet("background-color: transparent;")
        v_sec_lay = QVBoxLayout(vid_sec)
        v_sec_lay.setContentsMargins(0, 4, 0, 4)
        v_sec_lay.setSpacing(8)

        v_head = QHBoxLayout()
        self.video_chk = QCheckBox("Video ⌃", vid_sec)
        self.video_chk.setChecked(True)
        self.video_chk.setStyleSheet("font-weight: 700; font-size: 12px; color: #f1f5f9;")
        v_head.addWidget(self.video_chk)
        v_head.addStretch()
        v_sec_lay.addLayout(v_head)

        # Video Parameters Grid
        v_grid = QGridLayout()
        v_grid.setHorizontalSpacing(14)
        v_grid.setVerticalSpacing(8)

        # Remove watermark
        wm_lbl = QLabel("Remove watermark 💎", vid_sec)
        wm_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.remove_wm_chk = QCheckBox(vid_sec)
        self.remove_wm_chk.setChecked(True)
        self.remove_wm_chk.setToolTip("លុប Watermark ដោយស្វ័យប្រវត្តិ (No Watermark)")
        v_grid.addWidget(wm_lbl, 0, 0)
        v_grid.addWidget(self.remove_wm_chk, 0, 1, Qt.AlignLeft)

        # Resolution
        res_lbl = QLabel("Resolution", vid_sec)
        res_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.res_combo = QComboBox(vid_sec)
        self.res_combo.addItems([
            "1080P",
            "4K",
            "2K",
            "720P",
            "480P",
            "Original"
        ])
        # Select initial res
        c_init = self.initial_res.upper()
        if "4K" in c_init or "3840" in c_init:
            self.res_combo.setCurrentText("4K")
        elif "2K" in c_init or "2560" in c_init:
            self.res_combo.setCurrentText("2K")
        elif "720" in c_init:
            self.res_combo.setCurrentText("720P")
        elif "480" in c_init:
            self.res_combo.setCurrentText("480P")
        elif "ORIG" in c_init:
            self.res_combo.setCurrentText("Original")
        else:
            self.res_combo.setCurrentText("1080P")
        self.res_combo.currentIndexChanged.connect(self._update_estimates)
        v_grid.addWidget(res_lbl, 1, 0)
        v_grid.addWidget(self.res_combo, 1, 1)

        # Bit rate
        br_lbl = QLabel("Bit rate", vid_sec)
        br_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.bitrate_combo = QComboBox(vid_sec)
        self.bitrate_combo.addItems(["Recommended", "Higher", "Lower"])
        self.bitrate_combo.setCurrentText("Recommended")
        self.bitrate_combo.currentIndexChanged.connect(self._update_estimates)
        v_grid.addWidget(br_lbl, 2, 0)
        v_grid.addWidget(self.bitrate_combo, 2, 1)

        # Codec
        codec_lbl = QLabel("Codec", vid_sec)
        codec_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.codec_combo = QComboBox(vid_sec)
        self.codec_combo.addItems(["H.264", "HEVC", "ProRes"])
        self.codec_combo.setCurrentText("H.264")
        v_grid.addWidget(codec_lbl, 3, 0)
        v_grid.addWidget(self.codec_combo, 3, 1)

        # Format
        fmt_lbl = QLabel("Format", vid_sec)
        fmt_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.fmt_combo = QComboBox(vid_sec)
        self.fmt_combo.addItems(["mp4", "mov", "mkv"])
        self.fmt_combo.setCurrentText("mp4")
        v_grid.addWidget(fmt_lbl, 4, 0)
        v_grid.addWidget(self.fmt_combo, 4, 1)

        # Frame rate
        fps_lbl = QLabel("Frame rate", vid_sec)
        fps_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        self.fps_combo = QComboBox(vid_sec)
        self.fps_combo.addItems(["30fps", "60fps", "24fps", "25fps"])
        self.fps_combo.setCurrentText(f"{int(round(self.fps))}fps" if round(self.fps) in (24, 25, 30, 60) else "30fps")
        v_grid.addWidget(fps_lbl, 5, 0)
        v_grid.addWidget(self.fps_combo, 5, 1)

        # Color space
        cs_lbl = QLabel("Color space", vid_sec)
        cs_lbl.setStyleSheet("color: #88909c; font-size: 11px;")
        cs_val = QLabel("Rec. 709 SDR", vid_sec)
        cs_val.setStyleSheet("color: #cbd5e1; font-size: 11px;")
        v_grid.addWidget(cs_lbl, 6, 0)
        v_grid.addWidget(cs_val, 6, 1)

        v_sec_lay.addLayout(v_grid)
        right_col.addWidget(vid_sec)

        # Sync exported videos to space
        sync_box = QVBoxLayout()
        sync_box.setSpacing(2)
        self.sync_chk = QCheckBox("Sync exported videos to space  ⓘ", self)
        self.sync_chk.setChecked(True)
        self.sync_chk.setToolTip("Auto-open output folder upon export completion")
        sync_sub = QLabel("Get 3 GB of free storage for 1 month when you turn it on.", self)
        sync_sub.setStyleSheet("color: #64748b; font-size: 10px; margin-left: 24px;")
        sync_box.addWidget(self.sync_chk)
        sync_box.addWidget(sync_sub)
        right_col.addLayout(sync_box)

        right_col.addStretch()
        body_lay.addLayout(right_col, 1)

        root_lay.addLayout(body_lay, 1)

        # ---------------- BOTTOM FOOTER ----------------
        footer_line = QFrame(self)
        footer_line.setFrameShape(QFrame.HLine)
        footer_line.setStyleSheet("background-color: #262930; max-height: 1px;")
        root_lay.addWidget(footer_line)

        footer_lay = QHBoxLayout()
        footer_lay.setContentsMargins(0, 4, 0, 0)
        footer_lay.setSpacing(12)

        # Left: Duration & Size badge
        self.estimates_lbl = QLabel("🎞 Duration: 0s | Size: about 0 MB", self)
        self.estimates_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 500;")
        footer_lay.addWidget(self.estimates_lbl)

        footer_lay.addStretch()

        # Right: Cancel & Export Buttons
        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #2b2e35;
                color: #cbd5e1;
                border-radius: 4px;
                padding: 6px 18px;
                font-weight: 600;
                font-size: 12px;
                border: none;
            }
            QPushButton:hover {
                background-color: #383d47;
                color: #ffffff;
            }
        """)
        self.cancel_btn.clicked.connect(self.reject)
        footer_lay.addWidget(self.cancel_btn)

        self.export_btn = QPushButton("Export", self)
        self.export_btn.setStyleSheet("""
            QPushButton {
                background-color: #00b8b8;
                color: #ffffff;
                border-radius: 4px;
                padding: 6px 24px;
                font-weight: 800;
                font-size: 12px;
                border: none;
            }
            QPushButton:hover {
                background-color: #00d2d2;
            }
            QPushButton:pressed {
                background-color: #009e9e;
            }
        """)
        self.export_btn.clicked.connect(self.accept)
        footer_lay.addWidget(self.export_btn)

        root_lay.addLayout(footer_lay)

        # Display initial cover and estimates
        self._update_cover_display()
        self._update_estimates()

    def _update_cover_display(self):
        if self.cover_pixmap and not self.cover_pixmap.isNull():
            scaled = self.cover_pixmap.scaled(
                320, 190, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            # Crop to exact 320x190
            rect = QtCore.QRect((scaled.width() - 320) // 2, (scaled.height() - 190) // 2, 320, 190)
            cropped = scaled.copy(rect)
            self.cover_lbl.setPixmap(cropped)

    def _on_edit_cover_clicked(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Select Cover Image", "", "Images (*.png *.jpg *.jpeg *.webp);;All Files (*)"
        )
        if p and os.path.exists(p):
            pix = QtGui.QPixmap(p)
            if not pix.isNull():
                self.cover_pixmap = pix
                self._update_cover_display()

    def _on_browse_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Folder", self.export_to_edit.text())
        if d:
            self.export_to_edit.setText(d)

    def _update_estimates(self):
        dur = max(1.0, self.duration_sec)
        m = int(dur // 60)
        s = int(dur % 60)
        dur_str = f"{m}m {s:02d}s" if m > 0 else f"{s}s"

        res = self.res_combo.currentText()
        br = self.bitrate_combo.currentText()

        # Bitrate mapping (Mbps)
        br_map = {
            "4K": {"Recommended": 35.0, "Higher": 50.0, "Lower": 22.0},
            "2K": {"Recommended": 20.0, "Higher": 28.0, "Lower": 14.0},
            "1080P": {"Recommended": 12.0, "Higher": 18.0, "Lower": 8.0},
            "720P": {"Recommended": 6.0, "Higher": 9.0, "Lower": 4.0},
            "480P": {"Recommended": 3.0, "Higher": 4.5, "Lower": 2.0},
            "Original": {"Recommended": 12.0, "Higher": 18.0, "Lower": 8.0}
        }
        mbps = br_map.get(res, br_map["1080P"]).get(br, 12.0)
        size_mb = (dur * mbps * 125000) / (1024 * 1024)

        if size_mb >= 1024:
            size_str = f"about {size_mb / 1024:.1f} GB"
        else:
            size_str = f"about {max(1, int(round(size_mb)))} MB"

        self.estimates_lbl.setText(f"🎞 Duration: {dur_str} | Size: {size_str}")

    def get_selected_resolution(self) -> str:
        res = self.res_combo.currentText()
        is_portrait = getattr(self, 'orig_h', 1080) > getattr(self, 'orig_w', 1920)
        if is_portrait:
            mapping = {
                "4K": "2160×3840 — 4K (Vertical)",
                "2K": "1440×2560 — 2K (Vertical)",
                "1080P": "1080×1920 — Full HD (Vertical) ⭐",
                "720P": "720×1280 — HD (Vertical)",
                "480P": "480×854 — SD (Vertical)",
                "Original": "Original — ទំហំដើម"
            }
            return mapping.get(res, "1080×1920 — Full HD (Vertical) ⭐")
        else:
            mapping = {
                "4K": "3840×2160 — 4K",
                "2K": "2560×1440 — 2K",
                "1080P": "1920×1080 — Full HD ⭐",
                "720P": "1280×720 — HD",
                "480P": "854×480 — SD",
                "Original": "Original — ទំហំដើម"
            }
            return mapping.get(res, "1920×1080 — Full HD ⭐")

    def get_settings(self) -> dict:
        return {
            "resolution": self.get_selected_resolution(),
            "res_tag": self.res_combo.currentText(),
            "mode": self.bitrate_combo.currentText().upper(),
            "bgm_volume": self.bgm_spin.value(),
            "name": self.name_edit.text().strip(),
            "export_to": self.export_to_edit.text().strip(),
            "story_folder": self.story_edit.text().strip(),
            "episode": self.ep_spin.value(),
            "format": self.fmt_combo.currentText(),
            "codec": self.codec_combo.currentText(),
            "fps": self.fps_combo.currentText(),
            "remove_watermark": self.remove_wm_chk.isChecked(),
            "sync_space": self.sync_chk.isChecked()
        }


# ==================== DEDICATED VIDEO CUTTER & TRIMMER DIALOG ====================
class VideoCutterDialog(QDialog):
    """
    Dedicated Professional Video Trimmer & Cutter Studio.
    Supports Lossless stream copy (<0.5s) and Frame-Accurate cutting.
    Three cutting modes:
      1. ✂️ Trim / Keep Selection [In -> Out] (កាត់យកតែចន្លោះនេះ)
      2. 🗑️ Cut Out Selection [In -> Out] (កាត់ចោលចន្លោះនេះ ភ្ជាប់មុខក្រោយ)
      3. ✂️ Split at Playhead (បំបែកជា ២ ត្រង់ Playhead)
    """
    def __init__(
        self,
        video_path: str,
        total_duration: float = 0.0,
        current_playhead_sec: float = 0.0,
        in_point_sec: float = None,
        out_point_sec: float = None,
        parent=None
    ):
        super().__init__(parent)
        self.video_path = video_path
        self.current_playhead_sec = current_playhead_sec or 0.0
        
        self.total_duration = total_duration
        self.video_width = 0
        self.video_height = 0
        try:
            from utils.ffmpeg import get_video_info
            info = get_video_info(video_path)
            if self.total_duration <= 0:
                self.total_duration = float(info.get("duration", 0.0))
            self.video_width = info.get("width", 0)
            self.video_height = info.get("height", 0)
        except Exception:
            pass
            
        if self.total_duration <= 0:
            self.total_duration = 60.0

        # Initial In / Out points
        self.in_point_sec = max(0.0, in_point_sec if in_point_sec is not None else 0.0)
        self.out_point_sec = min(self.total_duration, out_point_sec if out_point_sec is not None else self.total_duration)
        if self.out_point_sec <= self.in_point_sec:
            self.out_point_sec = self.total_duration

        self.setWindowTitle("✂️ កាត់តវីដេអូ (Video Cutter & Trimmer Studio)")
        self.resize(650, 640)
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #0b1120;
                color: #f8fafc;
            }
            QLabel {
                color: #e2e8f0;
            }
            QGroupBox {
                border: 1px solid #1e293b;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 10px;
                font-weight: bold;
                color: #38bdf8;
                background-color: #0f172a;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }
            QRadioButton {
                color: #f1f5f9;
                font-size: 12px;
                spacing: 6px;
            }
            QRadioButton::indicator:checked {
                background-color: #00f0ff;
                border: 2px solid #ffffff;
                border-radius: 6px;
            }
            QDoubleSpinBox {
                background-color: #020617;
                color: #38bdf8;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 6px;
                font-family: 'Menlo', 'Courier New', monospace;
                font-weight: bold;
                font-size: 13px;
            }
            QLineEdit {
                background-color: #020617;
                color: #f1f5f9;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 6px 8px;
                font-size: 11px;
            }
            QCheckBox {
                color: #cbd5e1;
                font-size: 12px;
                spacing: 6px;
            }
            QCheckBox::indicator:checked {
                background-color: #10b981;
                border: 1px solid #34d399;
                border-radius: 3px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # 1. Header Banner
        head_box = QHBoxLayout()
        icon_lbl = QLabel("✂️")
        icon_lbl.setStyleSheet("font-size: 26px;")
        head_box.addWidget(icon_lbl)

        txt_col = QVBoxLayout()
        txt_col.setSpacing(2)
        t_title = QLabel("កាត់តវីដេអូ (Video Cutter & Trimmer Studio)")
        t_title.setStyleSheet("font-size: 16px; font-weight: 800; color: #38bdf8;")
        t_sub = QLabel("កាត់តវីដេអូបានលឿន Lossless (<0.5s) មិនបាត់បង់គុណភាព និងមិនចាំបាច់ Render យូរ")
        t_sub.setStyleSheet("font-size: 11px; color: #94a3b8;")
        txt_col.addWidget(t_title)
        txt_col.addWidget(t_sub)
        head_box.addLayout(txt_col, 1)
        layout.addLayout(head_box)

        # 2. File Metadata Card
        fname = os.path.basename(self.video_path) if self.video_path else "No Video"
        dur_m = int(self.total_duration // 60)
        dur_s = self.total_duration % 60
        res_str = f"{self.video_width} × {self.video_height} px" if self.video_width > 0 else "Original"

        info_card = QFrame(self)
        info_card.setStyleSheet("background-color: #0f172a; border: 1px solid #1e293b; border-radius: 6px; padding: 6px 10px;")
        info_lay = QHBoxLayout(info_card)
        info_lay.setContentsMargins(4, 4, 4, 4)

        f_lbl = QLabel(f"📹 <b>{fname}</b>")
        f_lbl.setStyleSheet("color: #f1f5f9; font-size: 12px;")
        d_lbl = QLabel(f"⏱ ប្រវែងសរុប: <b>{dur_m:02d}:{dur_s:05.2f} ({self.total_duration:.1f}s)</b>")
        d_lbl.setStyleSheet("color: #38bdf8; font-size: 12px;")
        r_lbl = QLabel(f"📐 <b>{res_str}</b>")
        r_lbl.setStyleSheet("color: #94a3b8; font-size: 12px;")

        info_lay.addWidget(f_lbl, 2)
        info_lay.addWidget(d_lbl, 2)
        info_lay.addWidget(r_lbl, 1)
        layout.addWidget(info_card)

        # 3. Trimming Mode Selector
        mode_grp = QGroupBox("១. ជ្រើសរើសទម្រង់កាត់ត (Select Cut Mode)", self)
        mode_lay = QVBoxLayout(mode_grp)
        mode_lay.setSpacing(8)

        self.radio_trim = QRadioButton("✂️ កាត់យកតែចន្លោះនេះ (Trim / Keep Selection [In -> Out])")
        self.radio_trim.setChecked(True)
        self.radio_trim.setToolTip("លុបផ្នែកខាងមុខ (0 -> In) និងផ្នែកខាងក្រោយ (Out -> End) ចោល រក្សាទុកតែចន្លោះកណ្តាល")
        mode_lay.addWidget(self.radio_trim)

        self.radio_cut_out = QRadioButton("🗑️ កាត់ចោលចន្លោះនេះ (Cut Out / Delete Selection [In -> Out])")
        self.radio_cut_out.setToolTip("លុបផ្នែកចន្លោះកណ្តាលចោល ហើយយកផ្នែកដើម (0 -> In) មកភ្ជាប់ជាមួយផ្នែកចុង (Out -> End)")
        mode_lay.addWidget(self.radio_cut_out)

        self.radio_split = QRadioButton("✂️ បំបែកជា ២ ត្រង់ Playhead (Split Video into Part 1 & Part 2)")
        self.radio_split.setToolTip("បំបែកវីដេអូជា 2 Files ដាច់ដោយឡែកត្រង់ទីតាំង Playhead")
        mode_lay.addWidget(self.radio_split)

        self.radio_trim.toggled.connect(self._on_mode_changed)
        self.radio_cut_out.toggled.connect(self._on_mode_changed)
        self.radio_split.toggled.connect(self._on_mode_changed)
        layout.addWidget(mode_grp)

        # 4. Interactive Time Controls Group
        self.time_grp = QGroupBox("២. កំណត់ពេលវេលាកាត់ (Timing Controls)", self)
        time_lay = QVBoxLayout(self.time_grp)
        time_lay.setSpacing(10)

        # --- In Point Row ---
        self.in_row = QHBoxLayout()
        in_lbl = QLabel("ចំណុចចាប់ផ្ដើម [In]:", self.time_grp)
        in_lbl.setFixedWidth(120)
        in_lbl.setStyleSheet("font-weight: bold; color: #38bdf8;")
        self.in_row.addWidget(in_lbl)

        self.in_spin = QDoubleSpinBox(self.time_grp)
        self.in_spin.setRange(0.0, self.total_duration)
        self.in_spin.setDecimals(2)
        self.in_spin.setSingleStep(0.5)
        self.in_spin.setValue(self.in_point_sec)
        self.in_spin.setFixedWidth(90)
        self.in_spin.valueChanged.connect(self._on_time_changed)
        self.in_row.addWidget(self.in_spin)

        self.in_timecode_lbl = QLabel("00:00.00", self.time_grp)
        self.in_timecode_lbl.setStyleSheet("font-family: 'Menlo', 'Courier New', monospace; font-size: 11px; color: #64748b; min-width: 60px;")
        self.in_row.addWidget(self.in_timecode_lbl)

        btn_in_playhead = QPushButton(f"[ Playhead ({self.current_playhead_sec:.2f}s)", self.time_grp)
        btn_in_playhead.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        btn_in_playhead.clicked.connect(lambda: self.in_spin.setValue(self.current_playhead_sec))
        self.in_row.addWidget(btn_in_playhead)

        btn_in_start = QPushButton("0s (ដើម)", self.time_grp)
        btn_in_start.setStyleSheet("background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        btn_in_start.clicked.connect(lambda: self.in_spin.setValue(0.0))
        self.in_row.addWidget(btn_in_start)

        for delta, txt in [(-1.0, "-1s"), (-0.1, "-0.1s"), (0.1, "+0.1s"), (1.0, "+1s")]:
            btn = QPushButton(txt, self.time_grp)
            btn.setStyleSheet("background-color: #1e293b; color: #94a3b8; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px; max-width: 38px;")
            btn.clicked.connect(lambda _, d=delta: self.in_spin.setValue(max(0.0, self.in_spin.value() + d)))
            self.in_row.addWidget(btn)

        self.in_row.addStretch()
        time_lay.addLayout(self.in_row)

        # --- Out Point Row ---
        self.out_row = QHBoxLayout()
        out_lbl = QLabel("ចំណុចបញ្ចប់ [Out]:", self.time_grp)
        out_lbl.setFixedWidth(120)
        out_lbl.setStyleSheet("font-weight: bold; color: #38bdf8;")
        self.out_row.addWidget(out_lbl)

        self.out_spin = QDoubleSpinBox(self.time_grp)
        self.out_spin.setRange(0.0, self.total_duration)
        self.out_spin.setDecimals(2)
        self.out_spin.setSingleStep(0.5)
        self.out_spin.setValue(self.out_point_sec)
        self.out_spin.setFixedWidth(90)
        self.out_spin.valueChanged.connect(self._on_time_changed)
        self.out_row.addWidget(self.out_spin)

        self.out_timecode_lbl = QLabel("00:00.00", self.time_grp)
        self.out_timecode_lbl.setStyleSheet("font-family: 'Menlo', 'Courier New', monospace; font-size: 11px; color: #64748b; min-width: 60px;")
        self.out_row.addWidget(self.out_timecode_lbl)

        btn_out_playhead = QPushButton(f"] Playhead ({self.current_playhead_sec:.2f}s)", self.time_grp)
        btn_out_playhead.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        btn_out_playhead.clicked.connect(lambda: self.out_spin.setValue(self.current_playhead_sec))
        self.out_row.addWidget(btn_out_playhead)

        btn_out_end = QPushButton("Max (ចុង)", self.time_grp)
        btn_out_end.setStyleSheet("background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        btn_out_end.clicked.connect(lambda: self.out_spin.setValue(self.total_duration))
        self.out_row.addWidget(btn_out_end)

        for delta, txt in [(-1.0, "-1s"), (-0.1, "-0.1s"), (0.1, "+0.1s"), (1.0, "+1s")]:
            btn = QPushButton(txt, self.time_grp)
            btn.setStyleSheet("background-color: #1e293b; color: #94a3b8; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px; max-width: 38px;")
            btn.clicked.connect(lambda _, d=delta: self.out_spin.setValue(min(self.total_duration, self.out_spin.value() + d)))
            self.out_row.addWidget(btn)

        self.out_row.addStretch()
        time_lay.addLayout(self.out_row)

        # --- Split Point Row (Shown only in Split Mode) ---
        self.split_row_w = QWidget(self.time_grp)
        split_row = QHBoxLayout(self.split_row_w)
        split_row.setContentsMargins(0, 0, 0, 0)
        sp_lbl = QLabel("ទីតាំងបំបែក (Split at):", self.split_row_w)
        sp_lbl.setFixedWidth(120)
        sp_lbl.setStyleSheet("font-weight: bold; color: #a5b4fc;")
        split_row.addWidget(sp_lbl)

        self.split_spin = QDoubleSpinBox(self.split_row_w)
        self.split_spin.setRange(0.1, self.total_duration - 0.1)
        self.split_spin.setDecimals(2)
        self.split_spin.setSingleStep(0.5)
        self.split_spin.setValue(self.current_playhead_sec if self.current_playhead_sec > 0.1 else self.total_duration / 2.0)
        self.split_spin.setFixedWidth(90)
        self.split_spin.valueChanged.connect(self._on_time_changed)
        split_row.addWidget(self.split_spin)

        self.split_timecode_lbl = QLabel("00:00.00", self.split_row_w)
        self.split_timecode_lbl.setStyleSheet("font-family: 'Menlo', 'Courier New', monospace; font-size: 11px; color: #64748b; min-width: 60px;")
        split_row.addWidget(self.split_timecode_lbl)

        btn_sp_playhead = QPushButton("យក Playhead", self.split_row_w)
        btn_sp_playhead.setStyleSheet("background-color: #1e1b4b; color: #a5b4fc; border: 1px solid #4338ca; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        btn_sp_playhead.clicked.connect(lambda: self.split_spin.setValue(self.current_playhead_sec))
        split_row.addWidget(btn_sp_playhead)
        split_row.addStretch()
        self.split_row_w.setVisible(False)
        time_lay.addWidget(self.split_row_w)

        # Duration Result Badge
        self.duration_badge = QLabel("ប្រវែងលទ្ធផល: 00:00.00", self.time_grp)
        self.duration_badge.setStyleSheet("""
            QLabel {
                background-color: #082f49;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 6px;
                padding: 6px 12px;
                font-family: 'Menlo', 'Courier New', monospace;
                font-weight: bold;
                font-size: 12px;
            }
        """)
        time_lay.addWidget(self.duration_badge)
        layout.addWidget(self.time_grp)

        # 5. Output Settings & Options
        opt_grp = QGroupBox("៣. ជម្រើសកាត់ត & Output (Settings)", self)
        opt_lay = QVBoxLayout(opt_grp)
        opt_lay.setSpacing(6)

        self.lossless_chk = QCheckBox("⚡ កាត់ល្បឿនលឿន Lossless (<0.5 វិនាទី មិនបាត់បង់គុណភាព Stream Copy)")
        self.lossless_chk.setChecked(True)
        self.lossless_chk.setToolTip("ដំណើរការកាត់ដោយប្រើ FFmpeg Stream Copy ដែលមិនបាច់ Decode/Encode ឡើងវិញ មិនបាត់បង់គុណភាពសូម្បីតែបន្តិច")
        opt_lay.addWidget(self.lossless_chk)

        self.auto_reload_chk = QCheckBox("🔄 បញ្ចូលវីដេអូកាត់រួចទៅក្នុង Project ភ្លាមៗ (Auto-reload into Project)")
        self.auto_reload_chk.setChecked(True)
        self.auto_reload_chk.setToolTip("នៅពេលកាត់រួច វានឹងបើកវីដេអូថ្មីនោះចូលមកក្នុង Timeline & Player ភ្លាមៗដោយស្វ័យប្រវត្តិ")
        opt_lay.addWidget(self.auto_reload_chk)

        # Destination path row
        dest_lay = QHBoxLayout()
        dest_lbl = QLabel("📂 រក្សាទុកនៅ:")
        dest_lbl.setFixedWidth(80)
        self.dest_edit = QLineEdit(self)
        base = os.path.splitext(self.video_path)[0] if self.video_path else "cut_video"
        ext = os.path.splitext(self.video_path)[1] if self.video_path else ".mp4"
        self.dest_edit.setText(f"{base}_cut{ext}")

        browse_btn = QPushButton("Browse...", self)
        browse_btn.setStyleSheet("background-color: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 4px; padding: 4px 8px; font-size: 11px;")
        browse_btn.clicked.connect(self._on_browse_dest)

        dest_lay.addWidget(dest_lbl)
        dest_lay.addWidget(self.dest_edit, 1)
        dest_lay.addWidget(browse_btn)
        opt_lay.addLayout(dest_lay)
        layout.addWidget(opt_grp)

        # 6. Action Buttons
        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)

        self.cancel_btn = QPushButton("បោះបង់ (Cancel)", self)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #334155;
            }
        """)
        self.cancel_btn.clicked.connect(self.reject)

        self.apply_btn = QPushButton("✂️ ចាប់ផ្ដើមកាត់ (Apply Cut)", self)
        self.apply_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #0284c7);
                color: #ffffff;
                border: 1px solid #38bdf8;
                border-radius: 6px;
                padding: 8px 24px;
                font-weight: 800;
                font-size: 13px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #0ea5e9);
            }
        """)
        self.apply_btn.clicked.connect(self.accept)

        btn_box.addWidget(self.cancel_btn)
        btn_box.addWidget(self.apply_btn, 1)
        layout.addLayout(btn_box)

        # Trigger initial calculation
        self._on_time_changed()

    def _on_mode_changed(self):
        is_split = self.radio_split.isChecked()
        self.in_row.itemAt(0).widget().parentWidget().setVisible(not is_split)
        self.out_row.itemAt(0).widget().parentWidget().setVisible(not is_split)
        self.split_row_w.setVisible(is_split)
        self._on_time_changed()

    def _format_time(self, sec: float) -> str:
        sec = max(0.0, float(sec))
        m = int(sec // 60)
        s = sec % 60
        return f"{m:02d}:{s:05.2f}"

    def _on_time_changed(self):
        in_v = self.in_spin.value()
        out_v = self.out_spin.value()
        sp_v = self.split_spin.value()

        self.in_timecode_lbl.setText(self._format_time(in_v))
        self.out_timecode_lbl.setText(self._format_time(out_v))
        self.split_timecode_lbl.setText(self._format_time(sp_v))

        if self.radio_split.isChecked():
            dur1 = sp_v
            dur2 = max(0.0, self.total_duration - sp_v)
            self.duration_badge.setText(
                f"✂️ បំបែកជា: ភាគ ១ = {self._format_time(dur1)} ({dur1:.1f}s)  |  ភាគ ២ = {self._format_time(dur2)} ({dur2:.1f}s)"
            )
        elif self.radio_cut_out.isChecked():
            removed = max(0.0, out_v - in_v)
            remained = max(0.0, self.total_duration - removed)
            self.duration_badge.setText(
                f"🗑️ កាត់ចោល: {self._format_time(removed)} ({removed:.1f}s)  ➔  ប្រវែងសល់ក្រោយកាត់: {self._format_time(remained)} ({remained:.1f}s)"
            )
        else: # Trim / Keep selection
            kept = max(0.0, out_v - in_v)
            self.duration_badge.setText(
                f"✂️ ប្រវែងដែលនឹងទទួលបាន (Trimmed Length): {self._format_time(kept)} ({kept:.1f}s) — ពី {self._format_time(in_v)} ដល់ {self._format_time(out_v)}"
            )

    def _on_browse_dest(self):
        from PySide6.QtWidgets import QFileDialog
        init_path = self.dest_edit.text()
        chosen, _ = QFileDialog.getSaveFileName(self, "រក្សាទុកវីដេអូកាត់រួចនៅ", init_path, "Video Files (*.mp4 *.mkv *.mov)")
        if chosen:
            self.dest_edit.setText(chosen)

    def get_settings(self) -> dict:
        if self.radio_split.isChecked():
            action = "split"
        elif self.radio_cut_out.isChecked():
            action = "cut_out"
        else:
            action = "trim"

        out_path = self.dest_edit.text().strip()
        base, ext = os.path.splitext(out_path)
        out_path2 = f"{base}_part2{ext}"

        return {
            "action": action,
            "start_sec": self.in_spin.value(),
            "end_sec": self.out_spin.value(),
            "split_sec": self.split_spin.value(),
            "lossless": self.lossless_chk.isChecked(),
            "auto_reload": self.auto_reload_chk.isChecked(),
            "output_path": out_path,
            "output_path2": out_path2
        }


# ==================== CAPCUT-STYLE MULTI-VIDEO MERGER DIALOG ====================
class MultiVideoMergerDialog(QDialog):
    """
    CapCut-Style Multi-Video Sequence Builder & Merger Dialog.
    Allows user to import multiple video clips (3 to 10+ clips),
    reorder them sequentially, view total combined duration, and
    stitch/merge them into a single unified video file for the project timeline.
    """
    def __init__(self, video_paths: list, parent=None):
        super().__init__(parent)
        self.raw_paths = list(video_paths)
        self.clips = []
        self._action = "cancel"

        self.setWindowTitle("🔗 ភ្ជាប់វីដេអូច្រើនជាតែមួយ (Multi-Video Sequence Builder)")
        self.resize(720, 560)
        self._load_clips_info(self.raw_paths)
        self._init_ui()

    def _load_clips_info(self, paths: list):
        from utils.ffmpeg import get_video_info
        from utils.file_utils import ensure_accessible_video_file
        self.clips = []
        for p in paths:
            if not os.path.exists(p):
                continue
            safe_p = ensure_accessible_video_file(p)
            inf = get_video_info(safe_p)
            dur = inf.get("duration", 0.0)
            w = inf.get("width", 0)
            h = inf.get("height", 0)
            size_mb = os.path.getsize(safe_p) / (1024 * 1024)
            self.clips.append({
                "path": safe_p,
                "orig_path": p,
                "name": os.path.basename(p),
                "duration": dur,
                "width": w,
                "height": h,
                "size_mb": size_mb
            })

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #0b1120;
                color: #f8fafc;
            }
            QLabel {
                color: #e2e8f0;
            }
            QGroupBox {
                border: 1px solid #1e293b;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 10px;
                font-weight: bold;
                color: #38bdf8;
                background-color: #0f172a;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }
            QTableWidget {
                background-color: #020617;
                border: 1px solid #1e293b;
                border-radius: 6px;
                color: #e2e8f0;
                gridline-color: #0f172a;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 4px;
            }
            QTableWidget::item:selected {
                background-color: #1e293b;
                color: #38bdf8;
            }
            QHeaderView::section {
                background-color: #0f1526;
                color: #94a3b8;
                font-weight: bold;
                border: none;
                border-bottom: 1px solid #1e2942;
                padding: 5px;
            }
            QLineEdit {
                background-color: #020617;
                color: #f1f5f9;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 6px 8px;
                font-size: 11px;
            }
            QCheckBox {
                color: #cbd5e1;
                font-size: 12px;
                spacing: 6px;
            }
            QCheckBox::indicator:checked {
                background-color: #10b981;
                border: 1px solid #34d399;
                border-radius: 3px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # 1. Header Banner
        head_box = QHBoxLayout()
        icon_lbl = QLabel("🎬")
        icon_lbl.setStyleSheet("font-size: 28px;")
        head_box.addWidget(icon_lbl)

        txt_col = QVBoxLayout()
        txt_col.setSpacing(2)
        t_title = QLabel("ភ្ជាប់វីដេអូច្រើនជាតែមួយ (CapCut-Style Multi-Video Merger)")
        t_title.setStyleSheet("font-size: 16px; font-weight: 800; color: #38bdf8;")
        t_sub = QLabel("តម្រៀបលំដាប់លំដោយ (Clip 1, 2, 3...) និងភ្ជាប់វីដេអូជាច្រើនចូលគ្នាជា Timeline រួមតែមួយ")
        t_sub.setStyleSheet("font-size: 11px; color: #94a3b8;")
        txt_col.addWidget(t_title)
        txt_col.addWidget(t_sub)
        head_box.addLayout(txt_col, 1)
        layout.addLayout(head_box)

        # 2. Clips List Group
        list_grp = QGroupBox(f"បញ្ជីវីដេអូត្រូវភ្ជាប់ ({len(self.clips)} Clips)", self)
        self.list_grp = list_grp
        list_lay = QVBoxLayout(list_grp)
        list_lay.setSpacing(6)

        # Table
        self.table = QTableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["#", "🎬 ឈ្មោះ Clip វីដេអូ", "⏱ ប្រវែង", "📐 Resolution", "💾 ទំហំ File"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        list_lay.addWidget(self.table)

        # Sequence Control Toolbar
        seq_bar = QHBoxLayout()
        seq_bar.setSpacing(8)

        self.up_btn = QPushButton("⬆ រំកិលឡើង (Move Up)", self)
        self.up_btn.setStyleSheet("background-color: #1e293b; color: #f1f5f9; border: 1px solid #334155; border-radius: 4px; padding: 4px 10px; font-size: 11px; font-weight: bold;")
        self.up_btn.clicked.connect(self._move_up)
        seq_bar.addWidget(self.up_btn)

        self.down_btn = QPushButton("⬇ រំកិលចុះ (Move Down)", self)
        self.down_btn.setStyleSheet("background-color: #1e293b; color: #f1f5f9; border: 1px solid #334155; border-radius: 4px; padding: 4px 10px; font-size: 11px; font-weight: bold;")
        self.down_btn.clicked.connect(self._move_down)
        seq_bar.addWidget(self.down_btn)

        self.sort_btn = QPushButton("🔄 តម្រៀបឈ្មោះ (A-Z)", self)
        self.sort_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #0284c7; border-radius: 4px; padding: 4px 10px; font-size: 11px;")
        self.sort_btn.clicked.connect(self._sort_by_name)
        seq_bar.addWidget(self.sort_btn)

        self.add_btn = QPushButton("➕ បន្ថែម Clips ទៀត...", self)
        self.add_btn.setStyleSheet("background-color: #1e293b; color: #34d399; border: 1px solid #059669; border-radius: 4px; padding: 4px 10px; font-size: 11px; font-weight: bold;")
        self.add_btn.clicked.connect(self._add_more_clips)
        seq_bar.addWidget(self.add_btn)

        self.del_btn = QPushButton("🗑 លុបចេញ", self)
        self.del_btn.setStyleSheet("background-color: #1e293b; color: #f87171; border: 1px solid #dc2626; border-radius: 4px; padding: 4px 10px; font-size: 11px;")
        self.del_btn.clicked.connect(self._remove_selected)
        seq_bar.addWidget(self.del_btn)

        seq_bar.addStretch()
        list_lay.addLayout(seq_bar)
        layout.addWidget(list_grp, 1)

        # 3. Live Summary Card
        self.summary_badge = QLabel("សរុប: 0 Clips | ប្រវែង: 00:00", self)
        self.summary_badge.setStyleSheet("""
            QLabel {
                background-color: #082f49;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 6px;
                padding: 6px 12px;
                font-family: 'Menlo', 'Courier New', monospace;
                font-weight: bold;
                font-size: 12px;
            }
        """)
        layout.addWidget(self.summary_badge)

        # 4. Settings
        opt_lay = QVBoxLayout()
        opt_lay.setSpacing(6)

        self.lossless_chk = QCheckBox("⚡ ដំណើរការភ្ជាប់លឿន Lossless (< 1 វិនាទី មិនបាត់បង់គុណភាព Stream Copy)")
        self.lossless_chk.setChecked(True)
        opt_lay.addWidget(self.lossless_chk)

        # Destination path
        dest_lay = QHBoxLayout()
        dest_lbl = QLabel("📂 រក្សាទុកនៅ:")
        dest_lbl.setFixedWidth(80)
        self.dest_edit = QLineEdit(self)
        
        # Default destination file name based on first clip
        from utils.file_utils import get_temp_path
        first_name = os.path.splitext(self.clips[0]["name"])[0] if self.clips else "story"
        self.dest_edit.setText(get_temp_path(f"merged_{first_name}_{len(self.clips)}clips.mp4"))

        browse_btn = QPushButton("Browse...", self)
        browse_btn.setStyleSheet("background-color: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 4px; padding: 4px 8px; font-size: 11px;")
        browse_btn.clicked.connect(self._on_browse_dest)

        dest_lay.addWidget(dest_lbl)
        dest_lay.addWidget(self.dest_edit, 1)
        dest_lay.addWidget(browse_btn)
        opt_lay.addLayout(dest_lay)
        layout.addLayout(opt_lay)

        # 5. Action Buttons
        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)

        self.cancel_btn = QPushButton("បោះបង់ (Cancel)", self)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #334155;
            }
        """)
        self.cancel_btn.clicked.connect(self.reject)

        self.media_bin_btn = QPushButton("📁 បញ្ចូលជា Clips ដាច់ដោយឡែក (Media Bin)", self)
        self.media_bin_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e1b4b;
                color: #a5b4fc;
                border: 1px solid #4338ca;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #4338ca;
                color: #ffffff;
            }
        """)
        self.media_bin_btn.clicked.connect(self._on_media_bin_clicked)

        self.merge_btn = QPushButton("🚀 ភ្ជាប់ជាវីដេអូតែមួយ & ចូលក្នុង Studio (Merge into 1)", self)
        self.merge_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #0284c7);
                color: #ffffff;
                border: 1px solid #38bdf8;
                border-radius: 6px;
                padding: 8px 20px;
                font-weight: 800;
                font-size: 13px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #0ea5e9);
            }
        """)
        self.merge_btn.clicked.connect(self._on_merge_clicked)

        btn_box.addWidget(self.cancel_btn)
        btn_box.addWidget(self.media_bin_btn)
        btn_box.addWidget(self.merge_btn, 1)
        layout.addLayout(btn_box)

        self._refresh_table()

    def _refresh_table(self):
        self.table.setRowCount(len(self.clips))
        self.list_grp.setTitle(f"បញ្ជីវីដេអូត្រូវភ្ជាប់ ({len(self.clips)} Clips)")

        total_dur = 0.0
        ref_w = 0
        ref_h = 0

        for row, clip in enumerate(self.clips):
            dur = clip.get("duration", 0.0)
            total_dur += dur
            w = clip.get("width", 0)
            h = clip.get("height", 0)
            if row == 0:
                ref_w, ref_h = w, h

            # #
            idx_item = QTableWidgetItem(f"{row+1:02d}")
            idx_item.setTextAlignment(Qt.AlignCenter)
            idx_item.setForeground(QtGui.QColor("#38bdf8"))
            self.table.setItem(row, 0, idx_item)

            # Name
            name_item = QTableWidgetItem(f"🎬 {clip['name']}")
            name_item.setToolTip(clip["path"])
            self.table.setItem(row, 1, name_item)

            # Duration
            m = int(dur // 60)
            s = dur % 60
            dur_item = QTableWidgetItem(f"{m:02d}:{s:05.2f}")
            dur_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 2, dur_item)

            # Resolution
            res_item = QTableWidgetItem(f"{w} × {h}" if w > 0 else "Unknown")
            res_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 3, res_item)

            # Size
            size_item = QTableWidgetItem(f"{clip['size_mb']:.1f} MB")
            size_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 4, size_item)

        tot_m = int(total_dur // 60)
        tot_s = total_dur % 60
        self.summary_badge.setText(
            f"📹 ចំនួនវីដេអូ: {len(self.clips)} Clips  |  ⏱ ប្រវែងសរុបរួម: {tot_m:02d}:{tot_s:05.2f} ({total_dur:.1f}s)  |  📐 ទំហំគោល: {ref_w}×{ref_h} px"
        )

    def _move_up(self):
        row = self.table.currentRow()
        if row > 0:
            self.clips[row], self.clips[row - 1] = self.clips[row - 1], self.clips[row]
            self._refresh_table()
            self.table.selectRow(row - 1)

    def _move_down(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.clips) - 1:
            self.clips[row], self.clips[row + 1] = self.clips[row + 1], self.clips[row]
            self._refresh_table()
            self.table.selectRow(row + 1)

    def _sort_by_name(self):
        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s["name"])]
        self.clips.sort(key=natural_sort_key)
        self._refresh_table()

    def _remove_selected(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.clips):
            self.clips.pop(row)
            self._refresh_table()
            if self.clips:
                self.table.selectRow(min(row, len(self.clips) - 1))

    def _add_more_clips(self):
        from PySide6.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "ជ្រើសរើសវីដេអូបន្ថែម", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.m4v)"
        )
        if paths:
            existing = {c["path"] for c in self.clips}
            from utils.ffmpeg import get_video_info
            for p in paths:
                if p not in existing and os.path.exists(p):
                    inf = get_video_info(p)
                    self.clips.append({
                        "path": p,
                        "name": os.path.basename(p),
                        "duration": inf.get("duration", 0.0),
                        "width": inf.get("width", 0),
                        "height": inf.get("height", 0),
                        "size_mb": os.path.getsize(p) / (1024 * 1024)
                    })
            self._refresh_table()

    def _on_browse_dest(self):
        from PySide6.QtWidgets import QFileDialog
        chosen, _ = QFileDialog.getSaveFileName(self, "រក្សាទុកវីដេអូភ្ជាប់រួចនៅ", self.dest_edit.text(), "Video Files (*.mp4)")
        if chosen:
            self.dest_edit.setText(chosen)

    def _on_media_bin_clicked(self):
        self._action = "media_bin"
        self.accept()

    def _on_merge_clicked(self):
        if not self.clips:
            return
        self._action = "merge"
        self.accept()

    def get_result(self) -> dict:
        return {
            "action": self._action,
            "clips": self.clips,
            "video_paths": [c["path"] for c in self.clips],
            "output_path": self.dest_edit.text().strip(),
            "lossless": self.lossless_chk.isChecked()
        }


# ==================== CAPCUT DETAILS INSPECTOR WIDGET ====================
class VideoDetailsWidget(QWidget):
    """
    CapCut-Style Details Inspector Widget.
    Displays comprehensive metadata, technical specs, and timeline sequence info matching CapCut desktop UI.
    Provides interactive controls for Selected Clip Speed (0.5x - 2.0x), Volume, and Mute.
    """
    clip_speed_changed = Signal(int, float)            # (clip_idx, speed)
    clip_volume_changed = Signal(int, float, bool)     # (clip_idx, volume, muted)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_selected_clip_idx = None
        self._init_ui()

    def _init_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # Header Title
        hdr_lay = QHBoxLayout()
        title_lbl = QLabel("Details", self)
        title_lbl.setStyleSheet("font-size: 13px; font-weight: 800; color: #38bdf8;")
        hdr_lay.addWidget(title_lbl)
        hdr_lay.addStretch()
        lay.addLayout(hdr_lay)

        # Separator line
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background-color: #1e293b; max-height: 1px;")
        lay.addWidget(sep)

        # Scroll Area for Form Fields
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        container = QWidget(scroll)
        container.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)

        self._labels = {}
        fields = [
            ("name", "Name:"),
            ("path", "Path:"),
            ("timeline", "Timeline:"),
            ("aspect", "Aspect Ratio:"),
            ("resolution", "Resolution:"),
            ("fps", "Frame Rate:"),
            ("duration", "Duration:"),
            ("frames", "Total Frames:"),
            ("audio", "Audio Specs:"),
            ("size", "File Size:"),
            ("clips", "Timeline Clips:"),
            ("proxy", "Proxy:"),
        ]

        for row, (key, title) in enumerate(fields):
            t_lbl = QLabel(title, container)
            t_lbl.setStyleSheet("color: #64748b; font-weight: 600; font-size: 11px;")
            val_lbl = QLabel("—", container)
            val_lbl.setStyleSheet("color: #f1f5f9; font-weight: 500; font-size: 11px;")
            val_lbl.setWordWrap(True)
            val_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._labels[key] = val_lbl
            grid.addWidget(t_lbl, row, 0, Qt.AlignLeft | Qt.AlignTop)
            grid.addWidget(val_lbl, row, 1, Qt.AlignLeft | Qt.AlignTop)

        grid.setColumnStretch(1, 1)
        c_lay.addLayout(grid)

        # Clip Speed & Audio Controls Section
        sep2 = QFrame(self)
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("background-color: #1e293b; max-height: 1px; margin-top: 8px; margin-bottom: 8px;")
        c_lay.addWidget(sep2)

        clip_sec_lbl = QLabel("⚡ Selected Clip Speed & Audio", container)
        clip_sec_lbl.setStyleSheet("font-size: 12px; font-weight: bold; color: #38bdf8;")
        c_lay.addWidget(clip_sec_lbl)

        clip_grid = QGridLayout()
        clip_grid.setHorizontalSpacing(16)
        clip_grid.setVerticalSpacing(8)

        # Selected clip label
        lbl_c = QLabel("Clip:", container)
        lbl_c.setStyleSheet("color: #64748b; font-weight: 600; font-size: 11px;")
        self.selected_clip_name_lbl = QLabel("No clip selected (Click clip on timeline)", container)
        self.selected_clip_name_lbl.setStyleSheet("color: #f1f5f9; font-size: 11px; font-weight: 500;")
        self.selected_clip_name_lbl.setWordWrap(True)
        clip_grid.addWidget(lbl_c, 0, 0, Qt.AlignLeft | Qt.AlignTop)
        clip_grid.addWidget(self.selected_clip_name_lbl, 0, 1, Qt.AlignLeft | Qt.AlignTop)

        # Speed Dropdown
        lbl_s = QLabel("Speed:", container)
        lbl_s.setStyleSheet("color: #64748b; font-weight: 600; font-size: 11px;")
        self.clip_speed_combo = QComboBox(container)
        self.clip_speed_combo.addItems(["0.50x", "0.75x", "1.00x", "1.25x", "1.50x", "2.00x"])
        self.clip_speed_combo.setCurrentText("1.00x")
        self.clip_speed_combo.setStyleSheet("""
            QComboBox {
                font-size: 11px;
                padding: 3px 6px;
                background-color: #0b1120;
                border: 1px solid #1e293b;
                border-radius: 4px;
                color: #e2e8f0;
            }
        """)
        self.clip_speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        clip_grid.addWidget(lbl_s, 1, 0, Qt.AlignLeft | Qt.AlignVCenter)
        clip_grid.addWidget(self.clip_speed_combo, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)

        # Volume & Mute
        lbl_v = QLabel("Audio:", container)
        lbl_v.setStyleSheet("color: #64748b; font-weight: 600; font-size: 11px;")
        vol_box = QHBoxLayout()
        vol_box.setContentsMargins(0, 0, 0, 0)
        vol_box.setSpacing(6)

        self.clip_vol_slider = QSlider(Qt.Horizontal, container)
        self.clip_vol_slider.setRange(0, 200)
        self.clip_vol_slider.setValue(100)
        self.clip_vol_slider.setFixedWidth(70)
        self.clip_vol_slider.valueChanged.connect(self._on_vol_changed)

        self.clip_vol_lbl = QLabel("100%", container)
        self.clip_vol_lbl.setStyleSheet("color: #94a3b8; font-size: 10.5px; font-family: monospace;")
        self.clip_vol_lbl.setFixedWidth(36)

        self.clip_mute_chk = QCheckBox("Mute", container)
        self.clip_mute_chk.setStyleSheet("color: #e2e8f0; font-size: 11px;")
        self.clip_mute_chk.toggled.connect(self._on_vol_changed)

        vol_box.addWidget(self.clip_vol_slider)
        vol_box.addWidget(self.clip_vol_lbl)
        vol_box.addWidget(self.clip_mute_chk)
        vol_box.addStretch()

        clip_grid.addWidget(lbl_v, 2, 0, Qt.AlignLeft | Qt.AlignVCenter)
        clip_grid.addLayout(vol_box, 2, 1, Qt.AlignLeft | Qt.AlignVCenter)

        clip_grid.setColumnStretch(1, 1)
        c_lay.addLayout(clip_grid)

        c_lay.addStretch()
        scroll.setWidget(container)
        lay.addWidget(scroll, stretch=1)

    def set_selected_clip(self, clip_idx: int, clip_data: dict):
        self._current_selected_clip_idx = clip_idx
        if not clip_data:
            self.selected_clip_name_lbl.setText("No clip selected")
            return
        c_name = clip_data.get("name", os.path.basename(clip_data.get("path", ""))) or f"Clip {clip_idx + 1}"
        spd = float(clip_data.get("speed", 1.0))
        vol = float(clip_data.get("volume", 1.0))
        muted = bool(clip_data.get("muted", False))
        dur = float(clip_data.get("duration", 0.0))
        self.selected_clip_name_lbl.setText(f"Clip {clip_idx + 1}: {c_name} ({dur:.2f}s)")

        self.clip_speed_combo.blockSignals(True)
        spd_text = f"{spd:.2f}x"
        idx = self.clip_speed_combo.findText(spd_text)
        if idx >= 0:
            self.clip_speed_combo.setCurrentIndex(idx)
        else:
            self.clip_speed_combo.addItem(spd_text)
            self.clip_speed_combo.setCurrentText(spd_text)
        self.clip_speed_combo.blockSignals(False)

        self.clip_vol_slider.blockSignals(True)
        self.clip_vol_slider.setValue(int(vol * 100))
        self.clip_vol_lbl.setText(f"{int(vol * 100)}%")
        self.clip_vol_slider.blockSignals(False)

        self.clip_mute_chk.blockSignals(True)
        self.clip_mute_chk.setChecked(muted)
        self.clip_mute_chk.blockSignals(False)

    def _on_speed_changed(self):
        if self._current_selected_clip_idx is None:
            return
        txt = self.clip_speed_combo.currentText().replace("x", "").strip()
        try:
            spd = float(txt)
            self.clip_speed_changed.emit(self._current_selected_clip_idx, spd)
        except ValueError:
            pass

    def _on_vol_changed(self):
        if self._current_selected_clip_idx is None:
            return
        vol = self.clip_vol_slider.value() / 100.0
        self.clip_vol_lbl.setText(f"{self.clip_vol_slider.value()}%")
        muted = self.clip_mute_chk.isChecked()
        self.clip_volume_changed.emit(self._current_selected_clip_idx, vol, muted)

    def update_details(self, meta: dict):
        if not meta:
            return
        if "name" in meta:
            self._labels["name"].setText(str(meta.get("name", "—")))
        if "path" in meta:
            p = str(meta.get("path", "—"))
            self._labels["path"].setText(p)
            self._labels["path"].setToolTip(p)
        if "timeline" in meta:
            self._labels["timeline"].setText(str(meta.get("timeline", "Sequence 01")))
        if "width" in meta and "height" in meta:
            w = meta.get("width", 0)
            h = meta.get("height", 0)
            self._labels["resolution"].setText(f"{w} × {h}")
            if w > 0 and h > 0:
                aspect = "9:16 (Vertical)" if h > w else ("16:9 (Landscape)" if w > h else "1:1 (Square)")
                self._labels["aspect"].setText(aspect)
        if "fps" in meta:
            fps_val = meta.get("fps")
            try:
                self._labels["fps"].setText(f"{float(fps_val):.2f} FPS")
            except (ValueError, TypeError):
                self._labels["fps"].setText(str(fps_val))
        if "duration" in meta:
            try:
                dur = float(meta.get("duration", 0.0))
                m = int(dur // 60)
                s = int(dur % 60)
                ms = int((dur % 1.0) * 100)
                self._labels["duration"].setText(f"{m:02d}:{s:02d}.{ms:02d} ({dur:.1f}s)")
            except (ValueError, TypeError):
                self._labels["duration"].setText("—")
        if "frames" in meta:
            try:
                self._labels["frames"].setText(f"{int(meta.get('frames', 0)):,} frames")
            except (ValueError, TypeError):
                self._labels["frames"].setText("—")
        if "audio" in meta:
            self._labels["audio"].setText(str(meta.get("audio", "44.1 kHz, Stereo")))
        if "size_mb" in meta:
            self._labels["size"].setText(f"{float(meta.get('size_mb', 0.0)):.1f} MB")
        if "clips_count" in meta:
            self._labels["clips"].setText(f"{int(meta.get('clips_count', 1))} Clips")
        if "proxy" in meta:
            self._labels["proxy"].setText(str(meta.get("proxy", "Turned off")))





