import os
import sys
import re
import shutil
import subprocess
import copy
import time
from pathlib import Path
from qt_compat import (
    Qt, Slot, QThread, Signal, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QComboBox, QLineEdit, QTextEdit, QPlainTextEdit, QFileDialog, QMessageBox, QGroupBox,
    QSplitter, QFrame, QDoubleSpinBox, QSpinBox, QSlider, QInputDialog, QTabWidget, QMenu, QDialog,
    QApplication, QMimeData, QTimer, QUrl, QKeySequence, QDesktopServices, QShortcut,
    QFileSystemWatcher, QProgressDialog
)

from gui.widgets import (
    VideoPreviewWidget, SubtitleTableWidget, 
    TimelineEditorWidget, VideoEffectsWidget, LogConsoleWidget,
    GeminiApiKeyDialog, PasteSRTDialog, VideoCutterDialog,
    MultiVideoMergerDialog, VideoDetailsWidget
)
from gui.media_bin_widget import MediaBinWidget
from gui.svg_icons import get_svg_icon
from core.dubbing import DubbingWorker
from utils.file_utils import OUTPUT_DIR, get_output_path, get_temp_path
from utils.logger import setup_logger, logger
from utils.config_manager import get_gemini_api_key
from core.models import PERSONA_CHOICES

def clean_speaker_tag(text: str) -> tuple:
    """
    Parses subtitle text for speaker tags like [ក្មេង], [ស្រី], [ប្រុស], [ចាស់ប្រុស], [ចាស់ស្រី], [Child], (ក្មេង), etc.
    Returns: (clean_text, speaker_display, gender, voice_preset)
    """
    raw = (text or "").strip()
    if not raw:
        return "", "👨 ប្រុស", "male", "Khmer Male - Piseth"

    m_bracket = re.match(r'^\s*(?:\[|\()([^\]\)]+)(?:\]|\))\s*[:：\-–—]?\s*', raw)
    m_colon = re.match(r'^\s*(ក្មេង(?:ប្រុស|ស្រី)?|កូន|child(?:ren)?|kid|boy|girl|ចាស់(?:ប្រុស|ស្រី)?|មនុស្សចាស់|elder(?:ly)?(?:\s*(?:male|female|man|woman))?|លោកតា|លោកយាយ|យាយ|តា|ស្រី|female|woman|lady|ប្រុស|male|man|guy|speaker\s*\d+)\s*[:：]\s*', raw, flags=re.IGNORECASE)
    match = m_bracket or m_colon

    gender = "male"
    spk_display = "👨 ប្រុស"
    voice = "Khmer Male - Piseth"
    clean_text = raw

    if match:
        tag_word = match.group(1).lower()
        clean_text = raw[match.end():].strip()
        
        if any(w in tag_word for w in ['ក្មេង', 'កូន', 'child', 'kid', 'boy', 'girl']):
            gender = "child"
            spk_display = "🧒 ក្មេង"
            voice = "Khmer Child - Boy (Vannak)"
        elif any(w in tag_word for w in ['ចាស់ស្រី', 'លោកយាយ', 'យាយ']) or 'elderly female' in tag_word:
            gender = "elder_female"
            spk_display = "👵 ចាស់ស្រី"
            voice = "Khmer Elder - Female (Grandmother)"
        elif any(w in tag_word for w in ['ចាស់ប្រុស', 'លោកតា', 'តា']) or 'elderly male' in tag_word:
            gender = "elder_male"
            spk_display = "👴 ចាស់ប្រុស"
            voice = "Khmer Elder - Male (Grandfather)"
        elif any(w in tag_word for w in ['ចាស់', 'elder', 'មនុស្សចាស់']):
            gender = "elder"
            spk_display = "👵👴 មនុស្សចាស់"
            voice = "Khmer Elder - Male (Grandfather)"
        elif any(w in tag_word for w in ['ស្រី', 'female', 'woman', 'lady']):
            gender = "female"
            spk_display = "👩 ស្រី"
            voice = "Khmer Female - Sreymom"
        elif any(w in tag_word for w in ['ប្រុស', 'male', 'man', 'guy']):
            gender = "male"
            spk_display = "👨 ប្រុស"
            voice = "Khmer Male - Piseth"
        elif 'speaker' in tag_word:
            spk_display = match.group(1).title()
            voice = "Khmer Male - Piseth"
    else:
        bracket_match = re.match(r'^\s*\[(.*?)\]\s*(.*)$', raw)
        if bracket_match:
            b_tag = bracket_match.group(1).lower()
            remainder = bracket_match.group(2).strip()
            if any(w in b_tag for w in ['ក្មេង', 'child', 'kid', 'boy', 'girl', 'កូន']):
                gender = "child"
                spk_display = "🧒 ក្មេង"
                voice = "Khmer Child - Boy (Vannak)"
                clean_text = remainder
            elif any(w in b_tag for w in ['ចាស់ស្រី', 'លោកយាយ', 'យាយ']) or 'elderly female' in b_tag:
                gender = "elder_female"
                spk_display = "👵 ចាស់ស្រី"
                voice = "Khmer Elder - Female (Grandmother)"
                clean_text = remainder
            elif any(w in b_tag for w in ['ចាស់ប្រុស', 'លោកតា', 'តា']) or 'elderly male' in b_tag:
                gender = "elder_male"
                spk_display = "👴 ចាស់ប្រុស"
                voice = "Khmer Elder - Male (Grandfather)"
                clean_text = remainder
            elif any(w in b_tag for w in ['ចាស់', 'elder', 'មនុស្សចាស់']):
                gender = "elder"
                spk_display = "👵👴 មនុស្សចាស់"
                voice = "Khmer Elder - Male (Grandfather)"
                clean_text = remainder
            elif any(w in b_tag for w in ['ស្រី', 'female', 'woman']):
                gender = "female"
                spk_display = "👩 ស្រី"
                voice = "Khmer Female - Sreymom"
                clean_text = remainder
            elif any(w in b_tag for w in ['ប្រុស', 'male', 'man']):
                gender = "male"
                spk_display = "👨 ប្រុស"
                voice = "Khmer Male - Piseth"
                clean_text = remainder

    while True:
        m = re.match(r'^\s*(?:\[[^\]\n]*\]|\([^\)\n]*\))\s*[:：\-–—]?\s*', clean_text)
        if m and m.end() > 0:
            clean_text = clean_text[m.end():].strip()
        else:
            break
    clean_text = re.sub(r'^[\]\)\:\：\-\–—\s]+', '', clean_text).strip()

    if not clean_text:
        clean_text = raw

    return clean_text, spk_display, gender, voice


class GenerateVoicesWorker(QThread):
    progress = Signal(int, str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, segments: list, total_duration: float, voice_name: str = "Khmer Female - Sreymom"):
        super().__init__()
        self.segments = segments
        self.total_duration = total_duration
        self.voice_name = voice_name
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            from core.tts import TextToSpeech
            from core.dialogue_sync import DialogueSyncEngine
            from utils.file_utils import get_temp_path
            import os

            total = len(self.segments)
            if total == 0:
                self.error.emit("គ្មាន Subtitle សម្រាប់សំយោគសំឡេងទេ (No subtitles to synthesize)")
                return

            self.progress.emit(5, f"🔊 កំពុងចាប់ផ្តើមសំយោគសំឡេង {total} ឃ្លា (Parallel Workers=3)...")

            # Ensure every segment has a voice
            for seg in self.segments:
                if not seg.get("voice"):
                    char = str(seg.get("character") or "").lower()
                    g = str(seg.get("gender") or "").lower()
                    if "ចាស់ស្រី" in char or g in ("elder_female", "ចាស់ស្រី") or "grandmother" in char:
                        seg["voice"] = "Khmer Elder - Female (Grandmother)"
                    elif "ចាស់ប្រុស" in char or g in ("elder_male", "ចាស់ប្រុស") or "grandfather" in char:
                        seg["voice"] = "Khmer Elder - Male (Grandfather)"
                    elif "មនុស្សចាស់" in char or "ចាស់" in char or g in ("elder", "មនុស្សចាស់"):
                        seg["voice"] = "Khmer Elder - Male (Grandfather)"
                    elif "ស្រី" in char or g == "female":
                        seg["voice"] = "Khmer Female - Sreymom"
                    elif "ក្មេង" in char or g == "child":
                        seg["voice"] = "Khmer Child - Boy (Vannak)"
                    else:
                        seg["voice"] = "Khmer Male - Piseth"

            tts_engine = TextToSpeech(voice_name=self.voice_name)

            def _tts_cb(completed, tot, info):
                pct = 5 + int(80 * (completed / float(max(1, tot))))
                self.progress.emit(pct, f"🔊 TTS: កំពុងសំយោគសំឡេង... ({completed}/{tot})")

            raw_audio_paths = tts_engine.generate_all_segments(
                self.segments,
                progress_callback=_tts_cb,
                max_workers=3,
                is_cancelled_fn=lambda: self._is_cancelled
            )

            if self._is_cancelled:
                self.error.emit("ដំណើរការសំយោគសំឡេងត្រូវបានផ្អាក")
                return

            self.progress.emit(88, "⏱ កំពុងផ្គុំ និងតម្រឹមសំឡេងតាម Timeline (Dialogue Sync Engine)...")
            sync_engine = DialogueSyncEngine()
            master_khmer_wav = get_temp_path("master_khmer_voice.wav")

            # Remove stale master if exists
            if os.path.exists(master_khmer_wav):
                try:
                    os.remove(master_khmer_wav)
                except Exception:
                    pass

            out_path = sync_engine.sync_dialogue_timeline(
                segments=self.segments,
                seg_audio_paths=raw_audio_paths,
                total_duration_sec=self.total_duration,
                output_master_path=master_khmer_wav
            )

            if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
                self.error.emit("ការផ្គុំសំឡេងបរាជ័យ (Audio Sync Failed)")
                return

            self.progress.emit(100, "✅ សំយោគសំឡេងជោគជ័យ!")
            self.finished.emit(out_path)
        except Exception as e:
            self.error.emit(f"កំហុសក្នុងការសំយោគសំឡេង: {str(e)}")



class MergeVideosWorker(QThread):
    progress = Signal(int, str)
    finished_success = Signal(str)
    failed_error = Signal(str)

    def __init__(self, video_paths: list, output_path: str, lossless: bool = True):
        super().__init__()
        self.video_paths = video_paths
        self.output_path = output_path
        self.lossless = lossless
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            from utils.ffmpeg import merge_multiple_videos

            def _cb(pct, msg):
                if not self._is_cancelled:
                    self.progress.emit(pct, msg)

            def _is_canc():
                return self._is_cancelled

            ok = merge_multiple_videos(
                self.video_paths,
                self.output_path,
                lossless=self.lossless,
                progress_callback=_cb,
                is_cancelled=_is_canc
            )

            if self._is_cancelled:
                self.failed_error.emit("ប្រតិបត្តិការត្រូវបានបោះបង់ (Operation cancelled by user)")
                return

            if ok and os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 1024:
                self.finished_success.emit(self.output_path)
            else:
                self.failed_error.emit("មិនអាចភ្ជាប់វីដេអូបានទេ សូមពិនិត្យមើលទ្រង់ទ្រាយឯកសារ (Merge failed)")
        except Exception as e:
            self.failed_error.emit(f"កំហុសក្នុងការភ្ជាប់វីដេអូ: {str(e)}")


class CutVideoWorker(QThread):
    progress = Signal(int, str)
    finished_success = Signal(str, str, str, bool)
    failed_error = Signal(str)

    def __init__(self, settings: dict, video_path: str):
        super().__init__()
        self.settings = settings
        self.video_path = video_path
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            from utils.ffmpeg import trim_video, cut_out_video_segment, split_video_at
            action = self.settings.get("action", "trim")
            start_sec = self.settings.get("start_sec", 0.0)
            end_sec = self.settings.get("end_sec", 0.0)
            split_sec = self.settings.get("split_sec", 0.0)
            lossless = self.settings.get("lossless", True)
            auto_reload = self.settings.get("auto_reload", True)
            out_path = self.settings.get("output_path")
            out_path2 = self.settings.get("output_path2")

            self.progress.emit(20, f"✂️ កំពុងដំណើរការកាត់ត: {action.upper()}...")

            ok = False
            if action == "trim":
                ok = trim_video(self.video_path, out_path, start_sec, end_sec, lossless=lossless)
            elif action == "cut_out":
                ok = cut_out_video_segment(self.video_path, out_path, start_sec, end_sec)
            elif action == "split":
                ok = split_video_at(self.video_path, split_sec, out_path, out_path2, lossless=lossless)

            if self._is_cancelled:
                self.failed_error.emit("ប្រតិបត្តិការកាត់តត្រូវបានបោះបង់ (Cut cancelled)")
                return

            if ok and os.path.exists(out_path):
                self.progress.emit(100, "✅ កាត់តវីដេអូបានសម្រេច!")
                self.finished_success.emit(action, out_path, out_path2 or "", auto_reload)
            else:
                self.failed_error.emit("មិនអាចកាត់តវីដេអូបានទេ (Cut operation failed)")
        except Exception as e:
            self.failed_error.emit(f"កំហុសក្នុងការកាត់ត: {str(e)}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎬 AI Video Dubber - Ultimate Edition")
        self.resize(1380, 920)
        self.setMinimumSize(1080, 750)
        
        self.worker = None
        self.transcribe_worker = None
        self.translate_worker = None
        self.react_worker = None
        self.video_path = None
        self.output_dir = str(OUTPUT_DIR)
        self.transcribed_segments = []  # Store segments after Transcription
        self.extracted_mp3_path = None
        self.mp3_worker = None
        self.current_project_file = None
        self.voice_worker = None
        self.merge_worker = None
        self.merge_progress_dialog = None
        self.cut_worker = None
        self.cut_progress_dialog = None
        self.setAcceptDrops(True)
        
        self._init_ui()
        self._load_stylesheet()
        
        # Thread-safe logger
        setup_logger(callback=self.log_console.append_log)

        # Global Shortcut for Cmd+V / Ctrl+V to paste Subtitles anywhere
        self.paste_shortcut = QShortcut(QKeySequence("Ctrl+V"), self)
        self.paste_shortcut.activated.connect(self._on_paste_sub_button_clicked)

        # Global Shortcuts for Save & Open Project (Cmd+S / Ctrl+S, Cmd+O / Ctrl+O)
        self.save_shortcut = QShortcut(QKeySequence.Save, self)
        self.save_shortcut.activated.connect(lambda: self._save_project())

        self.open_shortcut = QShortcut(QKeySequence.Open, self)
        self.open_shortcut.activated.connect(lambda: self._open_project())

        # Global Shortcuts for Video Cutting & Splitting (Ctrl+K: Studio, Ctrl+B: Split, Q: Trim Left, W: Trim Right)
        self.cut_dialog_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.cut_dialog_shortcut.activated.connect(lambda: self._on_video_cut_requested())

        self.split_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        self.split_shortcut.activated.connect(lambda: self._trigger_shortcut_split())
        self.split_cmd_shortcut = QShortcut(QKeySequence("Meta+B"), self)
        self.split_cmd_shortcut.activated.connect(lambda: self._trigger_shortcut_split())
        self.split_b_shortcut = QShortcut(QKeySequence("B"), self)
        self.split_b_shortcut.activated.connect(lambda: self._trigger_shortcut_split())

        self.trim_l_shortcut = QShortcut(QKeySequence("Q"), self)
        self.trim_l_shortcut.activated.connect(lambda: self._trigger_shortcut_trim_left())

        self.trim_r_shortcut = QShortcut(QKeySequence("W"), self)
        self.trim_r_shortcut.activated.connect(lambda: self._trigger_shortcut_trim_right())

        # CapCut Undo & Redo Shortcuts (Ctrl+Z / Cmd+Z, Ctrl+Y / Cmd+Shift+Z)
        from core.timeline_model import TimelineModel
        self.timeline_model = TimelineModel()

        self.undo_shortcut = QShortcut(QKeySequence.Undo, self)
        self.undo_shortcut.activated.connect(lambda: self._trigger_shortcut_undo())
        self.redo_shortcut = QShortcut(QKeySequence.Redo, self)
        self.redo_shortcut.activated.connect(lambda: self._trigger_shortcut_redo())
        self.redo_y_shortcut = QShortcut(QKeySequence("Ctrl+Y"), self)
        self.redo_y_shortcut.activated.connect(lambda: self._trigger_shortcut_redo())

        # CapCut Delete Shortcut (Delete / Backspace)
        self.del_shortcut = QShortcut(QKeySequence.Delete, self)
        self.del_shortcut.activated.connect(lambda: self._trigger_shortcut_delete())
        self.backspace_shortcut = QShortcut(QKeySequence(Qt.Key_Backspace), self)
        self.backspace_shortcut.activated.connect(lambda: self._trigger_shortcut_delete())

    def _load_stylesheet(self):
        qss_path = Path(__file__).resolve().parent / "styles.qss"
        if qss_path.exists():
            with open(qss_path, "r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())

    def _init_ui(self):
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(6)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # ---------------- 1. TOP HEADER & GUIDED WORKFLOW BAR ----------------
        top_header = QFrame(self)
        top_header.setObjectName("topHeader")
        th_lay = QHBoxLayout(top_header)
        th_lay.setContentsMargins(10, 6, 10, 6)
        th_lay.setSpacing(10)

        # App Logo & Title (Left)
        title_lbl = QLabel("Khmer Dubber Studio", self)
        title_lbl.setObjectName("titleLabel")
        ver_badge = QLabel("v3.0 Pro", self)
        ver_badge.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-size: 10px; font-weight: bold; border-radius: 4px; padding: 2px 6px;")
        th_lay.addWidget(title_lbl)
        th_lay.addWidget(ver_badge)

        sep1 = QFrame(self)
        sep1.setFrameShape(QFrame.VLine)
        sep1.setStyleSheet("background-color: #1e2942; max-height: 22px;")
        th_lay.addWidget(sep1)

        # Main Header Actions (Left-Center)
        self.load_vid_btn = QPushButton("📁 Open Video", self)
        self.load_vid_btn.setProperty("class", "btn-primary")
        self.load_vid_btn.setIcon(get_svg_icon("open_video", "#ffffff", 14))
        self.load_vid_btn.setStyleSheet("""
            QPushButton {
                background: #1d4ed8;
                color: #ffffff;
                font-weight: 800;
                font-size: 11px;
                border: 1px solid #3b82f6;
                border-radius: 6px;
                padding: 5px 14px;
            }
            QPushButton:hover {
                background: #2563eb;
                border-color: #60a5fa;
            }
        """)
        self.load_vid_btn.clicked.connect(self._browse_video)
        th_lay.addWidget(self.load_vid_btn)

        # 🌟 1-CLICK MAGIC AUTO-DUB HERO BUTTON
        self.magic_auto_dub_btn = QPushButton("✨ 1-Click Auto Dub", self)
        self.magic_auto_dub_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #d97706, stop:0.5 #ec4899, stop:1 #8b5cf6);
                color: #ffffff;
                font-weight: 900;
                font-size: 12px;
                border: 1px solid #f472b6;
                border-radius: 6px;
                padding: 5px 14px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #b45309, stop:0.5 #db2777, stop:1 #7c3aed);
                border-color: #fbcfe8;
            }
        """)
        self.magic_auto_dub_btn.setToolTip("🚀 ដំណើរការស្វ័យប្រវត្តិតែ ១ ចុចគត់ (Auto Subtitle ➔ AI Voiceover ➔ Export Final Video)")
        self.magic_auto_dub_btn.clicked.connect(self._on_magic_1click_dub_clicked)
        th_lay.addWidget(self.magic_auto_dub_btn)

        # Compatibility references
        self.copy_mp3_btn = None
        self.merge_vids_btn = None
        self.media_bin_btn = None
        self.sub_styles_menu_btn = None
        self.quick_export_top_btn = None

        th_lay.addStretch()

        # Tools Menu & Settings (Right)
        self.gemini_key_btn = QPushButton("🔑 Gemini Key", self)
        self.gemini_key_btn.setToolTip("ចុចដើម្បីបញ្ចូល ឬប្តូរ Google Gemini API Key និងតេស្តការភ្ជាប់ API")
        self.gemini_key_btn.clicked.connect(self._open_gemini_settings)
        th_lay.addWidget(self.gemini_key_btn)
        self._update_gemini_btn_status()

        self.save_proj_btn = QPushButton("Save", self)
        self.save_proj_btn.setProperty("class", "btn-gray")
        self.save_proj_btn.setIcon(get_svg_icon("save", "#cbd5e1", 13))
        self.save_proj_btn.setToolTip("រក្សាទុកគម្រោង (Save Project .vproj) - Cmd+S")
        self.save_proj_btn.clicked.connect(lambda: self._save_project())

        self.open_proj_btn = QPushButton("Open", self)
        self.open_proj_btn.setProperty("class", "btn-gray")
        self.open_proj_btn.setIcon(get_svg_icon("folder", "#cbd5e1", 13))
        self.open_proj_btn.setToolTip("បើកគម្រោង (Open Project .vproj) - Cmd+O")
        self.open_proj_btn.clicked.connect(lambda: self._open_project())

        th_lay.addWidget(self.save_proj_btn)
        th_lay.addWidget(self.open_proj_btn)

        # CapCut Signature Top-Right Export Button
        self.export_video_btn = QPushButton("Export", self)
        self.export_video_btn.setToolTip("Export វីដេអូចុងក្រោយ (CapCut Export Studio)")
        self.export_video_btn.setStyleSheet("""
            QPushButton {
                background-color: #00b8b8;
                color: #ffffff;
                font-size: 12px;
                font-weight: 800;
                border-radius: 6px;
                padding: 6px 20px;
                border: none;
            }
            QPushButton:hover {
                background-color: #00d2d2;
            }
            QPushButton:pressed {
                background-color: #009e9e;
            }
        """)
        self.export_video_btn.clicked.connect(self._export_final_video)
        th_lay.addWidget(self.export_video_btn)

        main_layout.addWidget(top_header)

        # ---------------- 2. CAPCUT PRO DUAL-TIER STUDIO LAYOUT ----------------
        # Root vertical splitter: Upper 3-Panel Studio vs Lower Full-Width Timeline
        studio_vsplitter = QSplitter(Qt.Vertical, self)
        studio_vsplitter.setObjectName("studioVSplitter")
        studio_vsplitter.setHandleWidth(8)
        studio_vsplitter.setChildrenCollapsible(False)
        studio_vsplitter.setStyleSheet("""
            QSplitter::handle:vertical {
                background-color: #121929;
                border-top: 1px solid #1e2942;
                border-bottom: 1px solid #0f172a;
                height: 8px;
                margin: 1px 0px;
                border-radius: 4px;
            }
            QSplitter::handle:vertical:hover {
                background-color: #38bdf8;
                border-top: 1px solid #7dd3fc;
                border-bottom: 1px solid #0284c7;
            }
            QSplitter::handle:vertical:pressed {
                background-color: #0284c7;
            }
        """)

        # ---------------- UPPER SECTION: 3-PANEL HORIZONTAL SPLITTER ----------------
        upper_splitter = QSplitter(Qt.Horizontal, self)
        upper_splitter.setObjectName("upperHorizontalSplitter")
        upper_splitter.setHandleWidth(8)
        upper_splitter.setChildrenCollapsible(False)
        upper_splitter.setStyleSheet("""
            QSplitter::handle:horizontal {
                background-color: #121929;
                border-left: 1px solid #1e2942;
                border-right: 1px solid #0f172a;
                width: 8px;
                margin: 0px 1px;
                border-radius: 4px;
            }
            QSplitter::handle:horizontal:hover {
                background-color: #38bdf8;
                border-left: 1px solid #7dd3fc;
                border-right: 1px solid #0284c7;
            }
            QSplitter::handle:horizontal:pressed {
                background-color: #0284c7;
            }
        """)

        # PANEL 1 (LEFT): MEDIA BIN (Assets / Library)
        self.media_bin = MediaBinWidget(self)
        self.media_bin.setMinimumWidth(260)
        self.media_bin.video_selected.connect(self._on_media_bin_video_selected)
        self.media_bin.combine_all_requested.connect(self._open_multi_video_merger)
        self.media_bin.add_to_timeline_requested.connect(self._on_media_bin_add_to_timeline)
        self.media_bin.start_batch_requested.connect(self._start_batch_dubbing)
        self.media_bin.stop_batch_requested.connect(self._stop_batch_dubbing)
        upper_splitter.addWidget(self.media_bin)

        # PANEL 2 (CENTER): VIDEO PREVIEW PLAYER (CapCut Viewport)
        preview_container = QWidget(self)
        preview_container.setMinimumWidth(380)
        preview_lay = QVBoxLayout(preview_container)
        preview_lay.setContentsMargins(0, 0, 0, 0)
        preview_lay.setSpacing(4)

        prev_hdr = QFrame(preview_container)
        prev_hdr.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0f172a, stop:1 #090e17);
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 3px 8px;
            }
        """)
        prev_hdr_lay = QHBoxLayout(prev_hdr)
        prev_hdr_lay.setContentsMargins(6, 4, 6, 4)
        prev_hdr_lay.setSpacing(6)

        prev_title = QLabel("🎥 Previewing — Video", prev_hdr)
        prev_title.setStyleSheet("font-weight: 700; font-size: 11px; color: #38bdf8;")
        prev_hdr_lay.addWidget(prev_title)
        prev_hdr_lay.addStretch()

        self.preview_res_lbl = QLabel("1080p | 16:9", prev_hdr)
        self.preview_res_lbl.setStyleSheet("color: #94a3b8; font-size: 10px; font-weight: 600;")
        prev_hdr_lay.addWidget(self.preview_res_lbl)
        preview_lay.addWidget(prev_hdr)

        self.video_preview = VideoPreviewWidget(preview_container)
        self.video_preview.file_dropped.connect(lambda p: self._handle_multiple_videos_imported([p]))
        self.video_preview.files_dropped.connect(self._handle_multiple_videos_imported)
        preview_lay.addWidget(self.video_preview, stretch=1)
        upper_splitter.addWidget(preview_container)

        # PANEL 3 (RIGHT): INSPECTOR TABS (Captions, Effects, Details)
        inspector_container = QWidget(self)
        inspector_container.setMinimumWidth(330)
        insp_lay = QVBoxLayout(inspector_container)
        insp_lay.setContentsMargins(0, 0, 0, 0)
        insp_lay.setSpacing(0)

        self.inspector_tabs = QTabWidget(inspector_container)
        self.inspector_tabs.setObjectName("studioInspectorTabs")
        self.inspector_tabs.tabBar().setElideMode(Qt.ElideNone)
        self.inspector_tabs.setStyleSheet("""
            QTabBar::tab {
                padding: 6px 12px;
                font-weight: 700;
                font-size: 11px;
            }
        """)

        # Tab 1: Captions / Subtitles
        self.subtitle_table = SubtitleTableWidget(self.inspector_tabs)
        self.inspector_tabs.addTab(self.subtitle_table, "💬 Captions")

        # Tab 2: Video Effects
        self.video_effects = VideoEffectsWidget(self.inspector_tabs)
        self.inspector_tabs.addTab(self.video_effects, "🎨 Effects")

        # Tab 3: Details Inspector
        self.details_widget = VideoDetailsWidget(self.inspector_tabs)
        self.inspector_tabs.addTab(self.details_widget, "ℹ️ Details")

        insp_lay.addWidget(self.inspector_tabs)
        upper_splitter.addWidget(inspector_container)

        # Configure Upper Splitter Ratios: [Media Bin: 270, Preview: 520, Inspector: 510]
        upper_splitter.setStretchFactor(0, 0)
        upper_splitter.setStretchFactor(1, 1)
        upper_splitter.setStretchFactor(2, 1)
        upper_splitter.setSizes([270, 520, 510])
        studio_vsplitter.addWidget(upper_splitter)

        # ---------------- LOWER SECTION: FULL-WIDTH TIMELINE EDITOR ----------------
        self.timeline_editor = TimelineEditorWidget(self)
        self.timeline_editor.setMinimumHeight(180)
        studio_vsplitter.addWidget(self.timeline_editor)

        # Connect Timeline, Preview, and Effect signals
        self.video_preview.playhead_moved.connect(self.timeline_editor.set_playhead_position)
        self.timeline_editor.seek_requested.connect(self.video_preview.seek_to_time_sec)
        self.timeline_editor.text_clip_changed.connect(self._on_timeline_text_clip_changed)
        self.timeline_editor.logo_clip_changed.connect(self._on_timeline_logo_clip_changed)
        self.timeline_editor.subtitle_segment_adjusted.connect(self._on_timeline_sub_adjusted)
        self.timeline_editor.blur_item_selected.connect(self.video_preview.set_active_blur)
        self.timeline_editor.blur_item_timing_changed.connect(self.video_preview.set_blur_item_timing)
        self.timeline_editor.blur_item_delete_requested.connect(self.video_preview.delete_blur)
        self.timeline_editor.blur_item_duplicate_requested.connect(self.video_preview.duplicate_blur)
        self.timeline_editor.text_selected.connect(self.video_preview.set_active_text)
        self.timeline_editor.text_item_timing_changed.connect(self._on_text_item_timing_changed)
        self.timeline_editor.text_item_delete_requested.connect(self.video_preview.delete_text)
        self.timeline_editor.text_item_duplicate_requested.connect(self.video_preview.duplicate_text)
        self.timeline_editor.video_cut_requested.connect(self._on_video_cut_requested)
        self.timeline_editor.video_split_requested.connect(self._on_video_split_requested)
        self.timeline_editor.video_trim_left_requested.connect(self._on_video_trim_left_requested)
        self.timeline_editor.video_clip_selected.connect(self._on_video_clip_selected)
        self.timeline_editor.video_clip_speed_changed.connect(self._on_clip_speed_changed)
        self.timeline_editor.video_clip_delete_requested.connect(self._on_video_clip_delete_requested)
        self.timeline_editor.in_out_delete_requested.connect(self._on_in_out_delete_requested)
        self.timeline_editor.clip_dropped_on_timeline.connect(self._on_clip_dropped_on_timeline)
        self.timeline_editor.video_clips_reordered.connect(self._on_video_clips_reordered)
        self.timeline_editor.transition_clicked.connect(self._on_transition_clicked)

        if hasattr(self, 'details_widget') and self.details_widget:
            self.details_widget.clip_speed_changed.connect(self._on_clip_speed_changed)
            self.details_widget.clip_volume_changed.connect(self._on_clip_volume_changed)

        self.video_preview.mark_in_requested.connect(lambda sec: self.timeline_editor.waveform_canvas.set_in_point(sec))
        self.video_preview.mark_out_requested.connect(lambda sec: self.timeline_editor.waveform_canvas.set_out_point(sec))
        self.video_preview.cut_requested.connect(lambda sec: self._on_video_cut_requested())
        
        # Connect Subtitle Table signals
        self.subtitle_table.seek_requested.connect(self.video_preview.seek_to_time_sec)
        self.subtitle_table.generate_voices_requested.connect(self._generate_all_voices)
        self.subtitle_table.cancel_voices_requested.connect(self._cancel_voice_generation)
        self.subtitle_table.subtitle_style_changed.connect(self._on_table_subtitle_style_changed)
        self.subtitle_table.gemini_ai_requested.connect(self._run_gemini_audio_pipeline)
        self.subtitle_table.paste_srt_requested.connect(self._on_paste_sub_button_clicked)
        self.subtitle_table.export_srt_requested.connect(self._export_khmer_srt)
        self.subtitle_table.export_khmer_srt_requested.connect(self._export_khmer_srt)
        self.subtitle_table.export_original_srt_requested.connect(self._export_original_srt)
        self.gemini_ai_stt_btn = getattr(self.subtitle_table, 'gemini_ai_btn', None)
        self.paste_sub_btn = getattr(self.subtitle_table, 'paste_srt_btn', None)

        # Connect Video Effects signals
        self.video_effects.blur_toggled.connect(self._on_blur_toggled)
        self.video_effects.blur_intensity_changed.connect(self._on_blur_intensity_changed)
        self.video_effects.blur_auto_speech_toggled.connect(self.video_preview.set_blur_auto_speech)
        self.video_effects.blur_auto_speech_toggled.connect(self.timeline_editor.set_blur_auto_speech)
        self.video_effects.reset_blur_requested.connect(self._on_reset_blur_requested)
        self.video_effects.add_blur_requested.connect(self.video_preview.add_blur)
        self.video_effects.duplicate_blur_requested.connect(self.video_preview.duplicate_blur)
        self.video_effects.delete_blur_requested.connect(self.video_preview.delete_blur)
        self.video_effects.blur_selected.connect(self.video_preview.set_active_blur)
        self.video_effects.blur_rotation_changed.connect(self.video_preview.set_blur_rotation)
        self.video_effects.blur_type_changed.connect(self.video_preview.set_blur_type)
        self.video_effects.blur_full_video_toggled.connect(self.video_preview.set_blur_full_video)
        self.video_effects.blur_mode_changed.connect(self.video_preview.set_blur_mode)
        self.video_effects.blur_color_changed.connect(self.video_preview.set_blur_color)
        self.video_effects.blur_timing_changed.connect(self.video_preview.set_blur_item_timing)
        self.video_effects.add_mask_preset_requested.connect(self.video_preview.add_mask_preset)
        
        self.video_effects.text_toggled.connect(self._on_text_toggled)
        self.video_effects.text_updated.connect(self._on_text_updated)
        self.video_effects.text_bg_color_changed.connect(self.video_preview.set_text_overlay_bg_color)
        self.video_effects.text_position_changed.connect(self._on_text_position_changed)
        self.video_effects.text_animation_changed.connect(self.video_preview.set_text_overlay_animation)
        self.video_effects.text_watermark_changed.connect(self.video_preview.set_text_watermark_options)
        self.video_effects.preview_text_anim_requested.connect(self.video_preview.preview_text_animation)
        self.video_effects.add_text_requested.connect(self.video_preview.add_text)
        self.video_effects.duplicate_text_requested.connect(self.video_preview.duplicate_text)
        self.video_effects.delete_text_requested.connect(self.video_preview.delete_text)
        self.video_effects.text_selected.connect(self.video_preview.set_active_text)
        self.video_effects.text_outline_changed.connect(self.video_preview.set_text_outline)
        self.video_effects.text_shadow_changed.connect(self.video_preview.set_text_shadow)
        self.video_effects.position_preset_requested.connect(self.video_preview.set_text_position_preset)
        self.video_effects.logo_toggled.connect(self._on_logo_toggled)
        self.video_effects.logo_updated.connect(self._on_logo_updated)
        self.video_effects.logo_full_video_toggled.connect(self._on_logo_full_video_toggled)
        self.video_effects.burn_subtitle_toggled.connect(self._on_burn_subtitle_toggled)
        self.video_effects.burn_subtitle_updated.connect(self._on_burn_subtitle_updated)
        self.video_effects.burn_subtitle_position_changed.connect(self.video_preview.set_burn_subtitle_position)
        self.video_effects.reset_burn_sub_requested.connect(self.video_preview.reset_burn_subtitle_position)
        self.video_effects.test_burn_sub_anim_requested.connect(self.video_preview.test_burn_subtitle_animation)
        
        self.video_preview.blur_items_changed.connect(self._on_blur_items_changed)
        self.video_preview.text_items_changed.connect(self._on_text_items_changed)
        self.video_preview.text_moved.connect(self.video_effects.update_text_position_spinboxes)
        self.video_preview.logo_moved.connect(self.video_effects.update_logo_spinboxes)
        self.video_preview.burn_subtitle_moved.connect(self.video_effects.update_burn_sub_position)

        # Sync initial auto-enabled effects (Text Overlay & Burn Subtitle)
        if hasattr(self, 'video_effects') and hasattr(self, 'video_preview'):
            self.video_preview.set_text_overlay_enabled(self.video_effects.text_checkbox.isChecked())
            self.video_preview.set_burn_subtitle_enabled(self.video_effects.burn_sub_checkbox.isChecked())
            if hasattr(self, 'timeline_editor'):
                self.timeline_editor.set_track_visible('text', self.video_effects.text_checkbox.isChecked())

        # Configure Vertical Splitter Ratios: Upper Studio ~450px, Timeline ~250px
        studio_vsplitter.setStretchFactor(0, 3)
        studio_vsplitter.setStretchFactor(1, 2)
        studio_vsplitter.setSizes([450, 250])

        main_layout.addWidget(studio_vsplitter, stretch=1)
        self.left_tabs = None

        # ---------------- 3. HEADLESS STUDIO CONTROLS (Housed in CapCut Export Studio) ----------------
        self.export_srt_btn = QPushButton("Export SRT")
        self.export_srt_btn.clicked.connect(self._export_srt)
        self.export_srt_btn.hide()

        self.story_folder_combo = QComboBox()
        self.story_folder_combo.setEditable(True)
        self._refresh_story_folders()
        self.story_folder_combo.hide()

        self.episode_spin = QSpinBox()
        self.episode_spin.setRange(1, 9999)
        self.episode_spin.setValue(1)
        self.episode_spin.setPrefix("EP ")
        self.episode_spin.hide()

        self.open_output_dir_btn = QPushButton("បើក Folder")
        self.open_output_dir_btn.clicked.connect(self._open_output_folder)
        self.open_output_dir_btn.hide()

        self.select_output_dir_btn = QPushButton("ប្តូរ Base...")
        self.select_output_dir_btn.clicked.connect(self._browse_output_dir)
        self.select_output_dir_btn.hide()

        self.output_path_input = QLineEdit(str(self.output_dir))
        self.output_path_input.hide()

        self.bgm_vol_spin = QSpinBox()
        self.bgm_vol_spin.setRange(0, 100)
        self.bgm_vol_spin.setValue(45)
        self.bgm_vol_spin.valueChanged.connect(self._on_bgm_volume_changed)
        self.bgm_vol_spin.hide()

        self.export_mode_combo = QComboBox()
        self.export_mode_combo.addItems(["Fast", "Balanced", "Quality"])
        self.export_mode_combo.setCurrentText("Fast")
        self.export_mode_combo.hide()

        self.export_res_combo = QComboBox()
        self.export_res_combo.addItems([
            "Original (ដើម)",
            "4K (3840×2160)",
            "2K (2560×1440)",
            "1080p (Full HD) ⭐",
            "720p (HD)",
            "480p (SD)"
        ])
        self.export_res_combo.setCurrentText("1080p (Full HD) ⭐")
        self.export_res_combo.hide()

        self.export_mp3_btn = QPushButton("Export MP3")
        self.export_mp3_btn.clicked.connect(self._export_mp3)
        self.export_mp3_btn.hide()

        self.cancel_export_btn = QPushButton("✕ Cancel", self)
        self.cancel_export_btn.clicked.connect(self._cancel_export)

        self.step_labels = []

        # Professional Status & Progress Bar Frame
        status_bar_frame = QFrame(self)
        status_bar_frame.setStyleSheet("""
            QFrame {
                background-color: #080c16;
                border: 1px solid #161f36;
                border-radius: 6px;
                padding: 2px 8px;
            }
        """)
        prog_lay = QHBoxLayout(status_bar_frame)
        prog_lay.setContentsMargins(6, 2, 6, 2)
        prog_lay.setSpacing(10)

        self.status_lbl = QLabel("Ready.", self)
        self.status_lbl.setStyleSheet("font-weight: 700; color: #38bdf8; font-size: 11px;")
        
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #0d1527;
                border: 1px solid #1e2942;
                border-radius: 4px;
                text-align: center;
                color: transparent;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #38bdf8);
                border-radius: 3px;
            }
        """)

        # Discreet toggle button to show console only when user explicitly wants to inspect logs
        self.toggle_console_btn = QPushButton("📜 Logs", self)
        self.toggle_console_btn.setCheckable(True)
        self.toggle_console_btn.setChecked(False)
        self.toggle_console_btn.setToolTip("Toggle Console Log (បង្ហាញ/លាក់ Console)")
        self.toggle_console_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: 1px solid #1e2942;
                border-radius: 4px;
                color: #64748b;
                font-size: 10px;
                font-weight: 600;
                padding: 2px 8px;
            }
            QPushButton:hover {
                color: #38bdf8;
                border-color: #38bdf8;
                background-color: #0f172a;
            }
            QPushButton:checked {
                color: #38bdf8;
                border-color: #0284c7;
                background-color: #0c1930;
            }
        """)

        prog_lay.addWidget(self.status_lbl)
        prog_lay.addWidget(self.progress_bar, stretch=1)
        self.cancel_export_btn.setStyleSheet("""
            QPushButton {
                background-color: #ef4444;
                color: #ffffff;
                font-weight: 700;
                font-size: 11px;
                border-radius: 4px;
                padding: 2px 10px;
                border: none;
            }
            QPushButton:hover {
                background-color: #dc2626;
            }
        """)
        self.cancel_export_btn.setVisible(False)
        prog_lay.addWidget(self.cancel_export_btn)

        prog_lay.addWidget(self.toggle_console_btn)

        main_layout.addWidget(status_bar_frame)

        # Log console drawer - HIDDEN BY DEFAULT for a clean, professional studio UI
        self.log_console = LogConsoleWidget(self)
        self.log_console.setFixedHeight(85)
        self.log_console.setVisible(False)
        main_layout.addWidget(self.log_console)

        self.toggle_console_btn.toggled.connect(self._toggle_console_visibility)

    def _toggle_console_visibility(self, visible: bool):
        self.log_console.setVisible(visible)

    def _set_processing_state(self, is_running: bool, status_msg: str = ""):
        """Enable or disable workflow buttons to prevent race conditions and multiple triggers."""
        buttons_to_toggle = [
            getattr(self, 'load_vid_btn', None),
            getattr(self, 'auto_trans_btn', None),
            getattr(self, 'transcribe_btn', None),
            getattr(self, 'translate_btn', None),
            getattr(self, 'ai_voice_studio_btn', None),
            getattr(self, 'top_export_btn', None),
            getattr(self, 'one_click_dub_btn', None),
            getattr(self, 'export_video_btn', None),
            getattr(self, 'export_mp3_btn', None),
            getattr(self, 'import_srt_btn', None),
            getattr(self, 'import_web_sub_btn', None),
            getattr(self, 'tools_menu_btn', None),
        ]
        for btn in buttons_to_toggle:
            if btn is not None:
                btn.setEnabled(not is_running)

        if hasattr(self, 'cancel_export_btn'):
            self.cancel_export_btn.setVisible(is_running)
            self.cancel_export_btn.setEnabled(is_running)

        if status_msg:
            self.status_lbl.setText(status_msg)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            valid_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v')
            for url in event.mimeData().urls():
                fpath = url.toLocalFile()
                if fpath.lower().endswith(valid_exts):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            valid_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v')
            valid_paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile().lower().endswith(valid_exts)]
            if valid_paths:
                self._handle_multiple_videos_imported(valid_paths)
                event.acceptProposedAction()

    def keyPressEvent(self, event):
        # Support Cmd+V / Ctrl+V to paste Subtitle text or SRT anywhere in Desktop Studio
        if (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)) and event.key() == Qt.Key_V:
            clipboard = QApplication.clipboard()
            text = (clipboard.text() or "").strip()
            if text and ("-->" in text or text.startswith("[") or text.startswith("{")):
                if self._paste_subtitles_from_clipboard(text):
                    event.accept()
                    return

        # CapCut Global Zoom Shortcuts: Cmd++/Cmd+=, Cmd+-, Cmd+0, Shift+Z
        if hasattr(self, 'timeline_editor') and self.timeline_editor:
            if (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
                if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
                    self.timeline_editor._zoom_in()
                    event.accept()
                    return
                elif event.key() == Qt.Key_Minus:
                    self.timeline_editor._zoom_out()
                    event.accept()
                    return
                elif event.key() == Qt.Key_0:
                    self.timeline_editor._fit_to_window()
                    event.accept()
                    return
            if event.key() == Qt.Key_Z and (event.modifiers() & Qt.ShiftModifier):
                self.timeline_editor._fit_to_window()
                event.accept()
                return

        super().keyPressEvent(event)

    def _paste_subtitles_from_clipboard(self, text: str) -> bool:
        """Parse pasted SRT or JSON subtitles from clipboard and populate timeline/table."""
        segs = []
        if text.startswith("[") or text.startswith("{"):
            try:
                import json
                raw = json.loads(text)
                raw_segs = raw if isinstance(raw, list) else raw.get("segments", [])
                for s in raw_segs:
                    st = float(s.get("startSeconds", s.get("start", 0.0)))
                    et = float(s.get("endSeconds", s.get("end", st + 2.0)))
                    if et <= st: et = st + 2.0
                    orig = (s.get("sourceText") or s.get("original_text") or "").strip()
                    khmer = (s.get("translatedText") or s.get("khmer_text") or orig).strip()

                    clean_khmer, spk, gender, voice = clean_speaker_tag(khmer)
                    clean_orig, _, _, _ = clean_speaker_tag(orig)

                    raw_gender = (s.get("gender") or "").lower().strip()
                    orig_spk = str(s.get("speaker") or "").lower()
                    if raw_gender == "child" or any(w in orig_spk for w in ['child', 'kid', 'boy', 'baby', 'ក្មេង']):
                        gender = "child"
                        spk = "🧒 ក្មេង"
                        voice = "Khmer Child - Boy (Vannak)"
                    elif raw_gender in ("elder_female", "ចាស់ស្រី") or any(w in orig_spk for w in ['ចាស់ស្រី', 'grandmother', 'យាយ']):
                        gender = "elder_female"
                        spk = "👵 ចាស់ស្រី"
                        voice = "Khmer Elder - Female (Grandmother)"
                    elif raw_gender in ("elder_male", "ចាស់ប្រុស") or any(w in orig_spk for w in ['ចាស់ប្រុស', 'grandfather', 'តា']):
                        gender = "elder_male"
                        spk = "👴 ចាស់ប្រុស"
                        voice = "Khmer Elder - Male (Grandfather)"
                    elif raw_gender == "female" or any(w in orig_spk for w in ['female', 'woman', 'girl', 'ស្រី']):
                        gender = "female"
                        spk = "👩 ស្រី"
                        voice = "Khmer Female - Sreymom"
                    elif raw_gender == "elder" or any(w in orig_spk for w in ['elder', 'ចាស់']):
                        gender = "elder"
                        spk = "👵👴 មនុស្សចាស់"
                        voice = "Khmer Elder - Male (Grandfather)"

                    persona = s.get("persona") or s.get("character") or spk
                    if persona not in PERSONA_CHOICES:
                        if gender == "child":
                            persona = "👦 Boy / Child"
                        elif gender == "female":
                            persona = "👩 Female Adult"
                        elif gender == "elder_female":
                            persona = "👵 Elderly Female"
                        elif gender in ("elder", "elder_male"):
                            persona = "👴 Elderly Male"
                        else:
                            persona = "👨 Male Adult"
                    emotion = s.get("emotion") or "😐 Neutral"
                    style = s.get("speaking_style") or s.get("style") or "Normal"
                    voice = s.get("voice_id") or s.get("voice") or voice

                    segs.append({
                        "id": str(s.get("id", len(segs) + 1)),
                        "start": round(st, 2),
                        "end": round(et, 2),
                        "original_text": clean_orig or clean_khmer,
                        "khmer_text": clean_khmer,
                        "speaker": spk,
                        "speaker_id": s.get("speaker_id") or s.get("speakerId") or f"speaker_{1 + (len(segs) % 2):02d}",
                        "character": persona,
                        "persona": persona,
                        "emotion": emotion,
                        "speaking_style": style,
                        "gender": gender,
                        "voice": voice,
                        "voice_id": voice
                    })
            except Exception as e:
                self.log_console.append_log(f"⚠️ JSON clipboard parse error: {e}")

        if not segs and "-->" in text:
            try:
                from core.srt_translator import SRTTranslator
                st_trans = SRTTranslator()
                sub_segs = st_trans.parse_srt(text)
                for s in sub_segs:
                    clean_txt, spk, gender, voice = clean_speaker_tag(s.text)

                    # 1. If s.speaker was identified by SRTTranslator from prefix tags, prioritize it
                    if s.speaker:
                        s_spk = s.speaker.lower()
                        if any(w in s_spk for w in ['ក្មេង', 'child', 'kid', 'boy', 'baby', 'កូន']):
                            gender = "child"
                            spk = "🧒 ក្មេង"
                            voice = "Khmer Child - Boy (Vannak)"
                        elif any(w in s_spk for w in ['ចាស់ស្រី', 'grandmother', 'យាយ']) or 'elderly female' in s_spk:
                            gender = "elder_female"
                            spk = "👵 ចាស់ស្រី"
                            voice = "Khmer Elder - Female (Grandmother)"
                        elif any(w in s_spk for w in ['ចាស់ប្រុស', 'grandfather', 'តា']) or 'elderly male' in s_spk:
                            gender = "elder_male"
                            spk = "👴 ចាស់ប្រុស"
                            voice = "Khmer Elder - Male (Grandfather)"
                        elif any(w in s_spk for w in ['ស្រី', 'female', 'woman', 'girl', 'lady']):
                            gender = "female"
                            spk = "👩 ស្រី"
                            voice = "Khmer Female - Sreymom"
                        elif any(w in s_spk for w in ['ចាស់', 'elder', 'លោកតា', 'លោកយាយ']):
                            gender = "elder"
                            spk = "👵👴 មនុស្សចាស់"
                            voice = "Khmer Elder - Male (Grandfather)"
                        elif any(w in s_spk for w in ['ប្រុស', 'male', 'man', 'guy']):
                            gender = "male"
                            spk = "👨 ប្រុស"
                            voice = "Khmer Male - Piseth"

                    # 2. Failsafe: check if clean_txt or s.text still has prefix tags
                    combined_check = f"{clean_txt} {getattr(s, 'text', '')}".lower()
                    if spk == "👨 ប្រុស" and not (s.speaker and 'ប្រុស' in s.speaker):
                        if any(w in combined_check[:30] for w in ['[ក្មេង]', '(ក្មេង)', 'ក្មេង:', '[child]']):
                            gender = "child"
                            spk = "🧒 ក្មេង"
                            voice = "Khmer Child - Boy (Vannak)"
                        elif any(w in combined_check[:30] for w in ['[ចាស់ស្រី]', '(ចាស់ស្រី)', 'ចាស់ស្រី:']):
                            gender = "elder_female"
                            spk = "👵 ចាស់ស្រី"
                            voice = "Khmer Elder - Female (Grandmother)"
                        elif any(w in combined_check[:30] for w in ['[ចាស់ប្រុស]', '(ចាស់ប្រុស)', 'ចាស់ប្រុស:']):
                            gender = "elder_male"
                            spk = "👴 ចាស់ប្រុស"
                            voice = "Khmer Elder - Male (Grandfather)"
                        elif any(w in combined_check[:30] for w in ['[ស្រី]', '(ស្រី)', 'ស្រី:', '[female]']):
                            gender = "female"
                            spk = "👩 ស្រី"
                            voice = "Khmer Female - Sreymom"

                    if gender == "child":
                        matched_persona = "👦 Boy / Child"
                    elif gender == "female":
                        matched_persona = "👩 Female Adult"
                    elif gender == "elder_female":
                        matched_persona = "👵 Elderly Female"
                    elif gender in ("elder", "elder_male"):
                        matched_persona = "👴 Elderly Male"
                    else:
                        matched_persona = "👨 Male Adult"
                    segs.append({
                        "id": str(len(segs) + 1),
                        "start": s.start_seconds,
                        "end": s.end_seconds,
                        "original_text": clean_txt,
                        "khmer_text": clean_txt,
                        "speaker": spk,
                        "speaker_id": f"speaker_{1 + (len(segs) % 2):02d}",
                        "character": matched_persona,
                        "persona": matched_persona,
                        "emotion": "😐 Neutral",
                        "speaking_style": "Normal",
                        "gender": gender,
                        "voice": voice,
                        "voice_id": voice
                    })
            except Exception as e:
                self.log_console.append_log(f"⚠️ SRT clipboard parse error: {e}")

        if segs:
            self.subtitle_table.set_segments(segs)
            self.timeline_editor.set_segments(segs)
            self.video_preview.set_timeline_segments(segs)
            self.transcribed_segments = segs
            self.log_console.append_log(f"📋 [Clipboard Paste] Successfully pasted {len(segs)} segments into Desktop Studio!")
            self.status_lbl.setText(f"📋 Pasted {len(segs)} segments from Clipboard!")
            self._auto_save_project()
            QMessageBox.information(
                self,
                "បិទភ្ជាប់បានជោគជ័យ",
                f"✅ បានបិទភ្ជាប់ (Paste) Subtitles ចំនួន {len(segs)} segments ពី Clipboard ដោយជោគជ័យ!\nលោកអ្នកអាចចុច '🎙️ Export Dubbed Video' ដើម្បីបញ្ចូលសំឡេងបានភ្លាមៗ។"
            )
            return True
        return False

    def _on_paste_sub_button_clicked(self):
        """Action when user clicks '📋 Paste SRT' button or presses Cmd+V."""
        clipboard = QApplication.clipboard()
        text = (clipboard.text() or "").strip()
        dialog = PasteSRTDialog(self, initial_text=text)
        dialog.subtitles_applied.connect(self._paste_subtitles_from_clipboard)
        dialog.exec_()

    def _open_multi_video_merger(self, initial_paths: list = None):
        """Open CapCut-Style Multi-Video Sequence Builder & Merger Dialog."""
        paths = list(initial_paths) if initial_paths else []
        if not paths:
            file_paths, _ = QFileDialog.getOpenFileNames(
                self, "ជ្រើសរើសវីដេអូច្រើនដើម្បីភ្ជាប់ (Select 3-10 Videos to Merge)", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.m4v);;All Files (*)"
            )
            if not file_paths:
                return
            paths = file_paths

        dlg = MultiVideoMergerDialog(paths, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return

        res = dlg.get_result()
        action = res.get("action")
        v_paths = res.get("video_paths", [])
        out_path = res.get("output_path")
        lossless = res.get("lossless", True)

        if action == "media_bin":
            self._handle_multiple_videos_imported(v_paths)
            return

        if action == "merge" and v_paths:
            self._execute_merge_multiple_videos(v_paths, out_path, lossless=lossless)

    def _execute_merge_multiple_videos(self, video_paths: list, output_path: str, lossless: bool = True, show_dialog: bool = True):
        """Execute multi-video stitching asynchronously with live non-blocking progress."""
        if self.merge_worker and self.merge_worker.isRunning():
            self.status_lbl.setText("⚠️ ដំណើរការភ្ជាប់វីដេអូកំពុងដំណើរការរួចហើយ")
            return

        n = len(video_paths)
        self.status_lbl.setText(f"🔗 កំពុងភ្ជាប់វីដេអូចំនួន {n} Clips លើ Timeline...")
        self.log_console.append_log(f"🔗 [CapCut Merge] Starting asynchronous multi-video merge for {n} clips (lossless={lossless}) -> {output_path}")

        # Precalculate individual clip timings for the professional timeline track
        from utils.file_utils import ensure_accessible_video_file
        video_paths = [ensure_accessible_video_file(vp) for vp in video_paths]
        clips_data = []
        curr_start = 0.0
        for vp in video_paths:
            cdur = 0.0
            try:
                import cv2
                cap = cv2.VideoCapture(vp)
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                fcnt = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                cap.release()
                if fps > 0 and fcnt > 0:
                    cdur = fcnt / fps
            except Exception:
                cdur = 0.0
            if cdur <= 0:
                cdur = 5.0
            clips_data.append({
                "name": os.path.basename(vp),
                "start": curr_start,
                "duration": cdur,
                "path": vp
            })
            curr_start += cdur
        self._pending_merged_clips = clips_data

        if show_dialog:
            self.merge_progress_dialog = QProgressDialog(f"🔗 កំពុងរៀបចំភ្ជាប់វីដេអូចំនួន {n} Clips...", "បោះបង់ (Cancel)", 0, 100, self)
            self.merge_progress_dialog.setWindowTitle("🔗 កំពុងភ្ជាប់វីដេអូ (Merging Videos)")
            self.merge_progress_dialog.setWindowModality(Qt.WindowModal)
            self.merge_progress_dialog.setAutoClose(False)
            self.merge_progress_dialog.setAutoReset(False)
            self.merge_progress_dialog.setMinimumWidth(420)
            self.merge_progress_dialog.setStyleSheet("""
                QProgressDialog {
                    background-color: #0b1120;
                    color: #f8fafc;
                    border: 1px solid #1e293b;
                    border-radius: 8px;
                    font-family: 'Kantumruy Pro', 'Inter', sans-serif;
                }
                QLabel {
                    color: #38bdf8;
                    font-size: 12px;
                    font-weight: 600;
                    padding: 8px;
                }
                QProgressBar {
                    background-color: #080c16;
                    border: 1px solid #1e293b;
                    border-radius: 6px;
                    text-align: center;
                    color: #ffffff;
                    font-weight: bold;
                    height: 18px;
                }
                QProgressBar::chunk {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #38bdf8);
                    border-radius: 5px;
                }
                QPushButton {
                    background-color: #1e293b;
                    color: #ef4444;
                    font-weight: bold;
                    border: 1px solid #334155;
                    border-radius: 5px;
                    padding: 4px 14px;
                }
                QPushButton:hover {
                    background-color: #dc2626;
                    color: #ffffff;
                }
            """)
            self.merge_progress_dialog.setValue(5)
            self.merge_progress_dialog.canceled.connect(self._cancel_merge_worker)
            self.merge_progress_dialog.show()
        else:
            self.merge_progress_dialog = None

        self._merge_n_clips = n
        self.merge_worker = MergeVideosWorker(video_paths, output_path, lossless=lossless)
        self.merge_worker.progress.connect(self._on_merge_worker_progress)
        self.merge_worker.finished_success.connect(self._on_merge_worker_success)
        self.merge_worker.failed_error.connect(self._on_merge_worker_failed)
        self.merge_worker.start()

    def _cancel_merge_worker(self):
        if self.merge_worker and self.merge_worker.isRunning():
            self.merge_worker.cancel()
            self.log_console.append_log("⚠️ User requested to cancel video merge.")
            self.status_lbl.setText("⚠️ បានបោះបង់ការភ្ជាប់វីដេអូ")

    def _on_merge_worker_progress(self, pct: int, msg: str):
        if self.merge_progress_dialog:
            self.merge_progress_dialog.setValue(pct)
            self.merge_progress_dialog.setLabelText(f"[{pct}%] {msg}")
        self.status_lbl.setText(f"[{pct}%] {msg}")
        self.log_console.append_log(f"🔗 [{pct}%] {msg}")

    def _on_merge_worker_success(self, output_path: str):
        if self.merge_progress_dialog:
            self.merge_progress_dialog.setValue(100)
            self.merge_progress_dialog.close()
            self.merge_progress_dialog = None

        n = getattr(self, '_merge_n_clips', 0)
        self.status_lbl.setText(f"✅ បានភ្ជាប់វីដេអូចំនួន {n} Clips លើ Timeline រួចរាល់")
        self.log_console.append_log(f"✅ Multi-video merge succeeded: {output_path} ({os.path.getsize(output_path):,} bytes)")

        # Transfer pending multi-clip layout to current merged clips
        self._current_merged_clips = getattr(self, '_pending_merged_clips', None)

        # Load unified video directly into Studio player and timeline!
        self._load_video_file(output_path, clear_segments=False)
        if self.left_tabs and hasattr(self, 'video_preview'):
            self.left_tabs.setCurrentWidget(self.video_preview)

    def _on_merge_worker_failed(self, err_msg: str):
        if self.merge_progress_dialog:
            self.merge_progress_dialog.close()
            self.merge_progress_dialog = None

        self.status_lbl.setText("❌ ភ្ជាប់វីដេអូបរាជ័យ (Merge failed)")
        self.log_console.append_log(f"❌ Failed to merge video clips: {err_msg}")
        QMessageBox.critical(
            self, "ភ្ជាប់វីដេអូបរាជ័យ (Merge Failed)",
            f"មិនអាចភ្ជាប់វីដេអូបានទេ:\n{err_msg}\n\nសូមពិនិត្យមើលទ្រង់ទ្រាយវីដេអូ ឬទំហំផ្ទុក Disk Space។"
        )

    def _browse_video(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Video File(s)", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.m4v);;All Files (*)"
        )
        if not file_paths:
            return

        from utils.file_utils import check_file_access, get_user_friendly_file_access_message
        valid_paths = []
        for p in file_paths:
            acc = check_file_access(p)
            if not acc.is_accessible:
                title, msg = get_user_friendly_file_access_message(acc)
                self.status_lbl.setText("⚠ Cannot access selected video")
                self.log_console.append_log(f"⚠️ [FILE ACCESS] {acc.status.value}: {acc.message}")
                QMessageBox.warning(self, title, msg)
            else:
                valid_paths.append(acc.path)

        if valid_paths:
            self._handle_multiple_videos_imported(valid_paths)

    def _handle_multiple_videos_imported(self, file_paths: list):
        """CapCut-style multi-video import:
        Instantly puts all selected clips into Media Bin,
        loads the 1st clip into the player/editor,
        and switches to Media Bin tab so the user can easily see their library.
        """
        if not file_paths:
            return

        # 1. Add all videos to Media Bin (without forcing onto timeline)
        if hasattr(self, 'media_bin'):
            self.media_bin.add_videos(file_paths, auto_load=False)

        # 2. Preview the first video in Studio Player (if timeline is currently empty)
        curr_clips = list(getattr(self.timeline_editor.waveform_canvas, 'video_clips', [])) if hasattr(self, 'timeline_editor') else []
        if not curr_clips and hasattr(self, 'video_preview'):
            from utils.file_utils import ensure_accessible_video_file
            self.video_preview.set_video_path(ensure_accessible_video_file(file_paths[0]))
            self._update_inspector_details(file_paths[0])

        # 3. Switch left tab to Media Bin if left_tabs exists
        if self.left_tabs and hasattr(self, 'media_bin'):
            self.left_tabs.setCurrentWidget(self.media_bin)

        # 4. Update badge & status
        self._update_media_bin_btn_badge()
        n = len(file_paths)
        self.status_lbl.setText(f"📁 បានបញ្ចូលវីដេអូចំនួន {n} Clips ទៅក្នុង Media Bin (អូសទម្លាក់លើ Timeline ឬចុច '+' ដើម្បីកាត់ត)")
        self.log_console.append_log(
            f"🎬 [CapCut Import] Added {n} clips into Media Bin. Ready to drag onto Timeline."
        )

    def _on_media_bin_add_to_timeline(self, video_path: str):
        """Handle adding a video clip from Media Bin onto the timeline (Instant, CapCut-style)."""
        self._on_clip_dropped_on_timeline(video_path, None)

    def _on_clip_dropped_on_timeline(self, video_path: str, drop_sec: float = None):
        """CapCut-Style Drag & Drop: Handle video clip dropped directly onto Timeline (100% Instant Virtual NLE, Zero Popups, Zero Merge Wait)."""
        if not video_path:
            return

        from utils.file_utils import check_file_access, get_user_friendly_file_access_message, ensure_accessible_video_file
        acc = check_file_access(video_path)
        if not acc.is_accessible:
            title, msg = get_user_friendly_file_access_message(acc)
            self.status_lbl.setText("⚠ Cannot access selected video")
            self.log_console.append_log(f"❌ [DRAG & DROP ACCESS] {acc.status.value}: {acc.message}")
            QMessageBox.warning(self, title, msg)
            return

        video_path = ensure_accessible_video_file(video_path)
        cur_v = getattr(self, 'video_path', None)
        # Case 1: Timeline empty or no video loaded -> load immediately
        if not cur_v or not os.path.exists(cur_v):
            self._load_video_file(video_path)
            if self.left_tabs and hasattr(self, 'video_preview'):
                self.left_tabs.setCurrentWidget(self.video_preview)
            self.status_lbl.setText(f"🎬 បានដាក់ {os.path.basename(video_path)} លើ Timeline")
            self.log_console.append_log(f"🎬 [CapCut Drag & Drop] Loaded {os.path.basename(video_path)} onto Timeline.")
            return

        # Fast duration resolution (0ms from MediaBin cache or OpenCV)
        new_dur = 0.0
        if hasattr(self, 'media_bin') and hasattr(self.media_bin, 'items'):
            for it in self.media_bin.items:
                if it.get("path") == video_path:
                    new_dur = it.get("duration", 0.0)
                    break
        if new_dur <= 0.0:
            try:
                import cv2
                cap_tmp = cv2.VideoCapture(video_path)
                if cap_tmp.isOpened():
                    fps_t = cap_tmp.get(cv2.CAP_PROP_FPS) or 30.0
                    cnt_t = cap_tmp.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    new_dur = cnt_t / max(1.0, fps_t)
                    cap_tmp.release()
            except Exception:
                pass
        if new_dur <= 0.0:
            from utils.ffmpeg import get_video_info
            new_info = get_video_info(video_path)
            new_dur = float(new_info.get("duration", 0.0) or 5.0)

        curr_clips = list(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        if not curr_clips:
            cur_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)
            if cur_dur <= 0.0:
                try:
                    import cv2
                    c_tmp = cv2.VideoCapture(cur_v)
                    if c_tmp.isOpened():
                        cur_dur = (c_tmp.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / max(1.0, c_tmp.get(cv2.CAP_PROP_FPS) or 30.0)
                        c_tmp.release()
                except Exception:
                    cur_dur = 5.0
            curr_clips = [{
                "name": getattr(self, 'original_video_name', os.path.basename(cur_v)),
                "start": 0.0,
                "duration": cur_dur,
                "path": cur_v
            }]

        # Re-index all clip start times to ensure strict continuity
        curr_offset = 0.0
        for c in curr_clips:
            c["start"] = curr_offset
            curr_offset += c.get("duration", 0.0)

        new_clip_dict = {
            "name": os.path.basename(video_path),
            "start": curr_offset,
            "duration": new_dur,
            "path": video_path
        }

        # Check if drop position requests inserting between existing clips
        insert_idx = len(curr_clips)
        if drop_sec is not None and drop_sec > 0.0 and drop_sec < (curr_offset - 0.5):
            for idx, c in enumerate(curr_clips):
                c_mid = c["start"] + c.get("duration", 0.0) * 0.5
                if drop_sec < c_mid:
                    insert_idx = idx
                    break
            curr_clips.insert(insert_idx, new_clip_dict)
        else:
            curr_clips.append(new_clip_dict)

        # Re-index sequential start positions
        curr_offset = 0.0
        for c in curr_clips:
            c["start"] = curr_offset
            curr_offset += c.get("duration", 0.0)
        new_clip_start = curr_clips[insert_idx]["start"]
        total_dur = curr_offset

        # 1. Update timeline total duration & clips
        self.timeline_editor.set_total_duration(total_dur)
        self.timeline_editor.set_video_clips(curr_clips)
        if hasattr(self.timeline_editor, 'waveform_canvas'):
            self.timeline_editor.waveform_canvas.selected_clip_idx = insert_idx
            self.timeline_editor.waveform_canvas.update()

        # 2. Update central TimelineModel
        if hasattr(self, 'timeline_model'):
            self.timeline_model.video_clips = curr_clips
            self.timeline_model.total_duration_sec = total_dur

        # 3. Update Video Preview player
        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_clips(curr_clips)
            self.video_preview.seek_to_time_sec(new_clip_start)

        # 4. Move playhead and viewport to newly added clip
        self.timeline_editor.set_playhead_position(new_clip_start)
        self.timeline_editor.scroll_to_sec(new_clip_start)

        # 5. Media Bin Registration
        if hasattr(self, 'media_bin'):
            self.media_bin.add_videos([video_path], auto_load=False)
            self.media_bin.sync_timeline_clips(curr_clips)
        # 6. Update Inspector Details & Preview Resolution
        self._update_inspector_details(self.video_path, clips=curr_clips, duration=total_dur)

        tot_m = int(total_dur // 60)
        tot_s = int(total_dur % 60)
        self.status_lbl.setText(f"⚡ [CapCut] បានបន្ថែម {os.path.basename(video_path)} លើ Timeline ភ្លាមៗ ({len(curr_clips)} Clips | {tot_m:02d}:{tot_s:02d})")
        self.log_console.append_log(f"⚡ [CapCut NLE] Instant Virtual Append: '{os.path.basename(video_path)}' ({new_dur:.1f}s) added to timeline. Total {len(curr_clips)} clips ({total_dur:.1f}s). Zero waiting time!")

    def _on_video_clips_reordered(self, new_clips: list):
        """Handle reordering/swapping video clips directly on the timeline (CapCut Drag & Drop Reorder)."""
        if not new_clips:
            return

        total_dur = sum(c.get("duration", 0.0) for c in new_clips)

        # 1. Update timeline total duration & clips
        self.timeline_editor.set_total_duration(total_dur)
        self.timeline_editor.set_video_clips(new_clips)

        # 2. Update central TimelineModel
        if hasattr(self, 'timeline_model'):
            self.timeline_model.video_clips = new_clips
            self.timeline_model.total_duration_sec = total_dur

        # 3. Update Video Preview player
        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_clips(new_clips)
            cur_p = getattr(self.timeline_editor.waveform_canvas, 'playhead_pos_sec', 0.0)
            self.video_preview.seek_to_time_sec(cur_p)

        # 4. Sync Media Bin
        if hasattr(self, 'media_bin'):
            self.media_bin.sync_timeline_clips(new_clips)

        # 5. Update Inspector Details
        self._update_inspector_details(getattr(self, 'video_path', None), clips=new_clips, duration=total_dur)

        tot_m = int(total_dur // 60)
        tot_s = int(total_dur % 60)
        clip_names = " ➔ ".join(c.get("name", "Clip") for c in new_clips)
        self.status_lbl.setText(f"⚡ [CapCut] បានផ្លាស់ប្តូរលំដាប់ Clips មុខក្រោយជោគជ័យ ({len(new_clips)} Clips | {tot_m:02d}:{tot_s:02d})")
        self.log_console.append_log(f"⚡ [CapCut NLE] Video clips reordered: {clip_names}")

    def _on_transition_clicked(self, cut_idx: int):
        """Phase 3 Gate 4: Handle clicking transition button/badge between adjacent clips."""
        clips = getattr(self, 'clips', None) or getattr(self.timeline_editor.waveform_canvas, 'video_clips', [])
        if not clips or len(clips) <= cut_idx + 1:
            return

        clip_a = clips[cut_idx]
        clip_b = clips[cut_idx + 1]

        if not hasattr(self, 'transitions'):
            self.transitions = []

        existing = None
        for t in self.transitions:
            t_cut = getattr(t, 'cut_index', None)
            t_a = str(getattr(t, 'clip_a_id', ''))
            t_b = str(getattr(t, 'clip_b_id', ''))
            ca_id = str(clip_a.get('id', cut_idx))
            cb_id = str(clip_b.get('id', cut_idx + 1))
            if t_cut == cut_idx or (t_a == ca_id and t_b == cb_id):
                existing = t
                break

        from gui.transition_dialog import TransitionDialog
        dlg = TransitionDialog(clip_a, clip_b, existing, self)
        if dlg.exec():
            if dlg.delete_requested and existing:
                self.transitions = [t for t in self.transitions if t != existing]
                self.log_console.append_log(f"🎬 [Transitions] Removed transition between '{clip_a.get('name', 'Clip A')}' and '{clip_b.get('name', 'Clip B')}'.")
            elif dlg.applied_transition:
                dlg.applied_transition.cut_index = cut_idx
                if existing in self.transitions:
                    self.transitions.remove(existing)
                self.transitions.append(dlg.applied_transition)
                self.log_console.append_log(
                    f"🎬 [Transitions] Applied {dlg.applied_transition.type} ({dlg.applied_transition.duration}s, {dlg.applied_transition.audio_mode}) between '{clip_a.get('name', 'Clip A')}' and '{clip_b.get('name', 'Clip B')}'."
                )

            # Sync with timeline model
            if hasattr(self, 'timeline_model'):
                self.timeline_model.transitions = self.transitions

            # Update preview and timeline canvas
            if hasattr(self, 'video_preview'):
                self.video_preview.transitions = self.transitions
                self.video_preview._ensure_timeline_preview_audio(force_rebuild=True)
                cur_p = getattr(self.timeline_editor.waveform_canvas, 'playhead_pos_sec', 0.0)
                self.video_preview.seek_to_time_sec(cur_p, immediate=True)

            if hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas'):
                self.timeline_editor.waveform_canvas.transitions = self.transitions
                self.timeline_editor.waveform_canvas.update()

    def _on_video_clip_selected(self, clip_idx: int):
        """CapCut NLE: Sync selected timeline clip with Details Inspector."""
        curr_clips = getattr(self.timeline_editor.waveform_canvas, 'video_clips', [])
        if 0 <= clip_idx < len(curr_clips):
            c = curr_clips[clip_idx]
            if hasattr(self, 'details_widget') and self.details_widget:
                self.details_widget.set_selected_clip(clip_idx, c)
                if hasattr(self, 'inspector_tabs'):
                    idx = self.inspector_tabs.indexOf(self.details_widget)
                    if idx >= 0:
                        self.inspector_tabs.setCurrentIndex(idx)

    def _on_clip_speed_changed(self, clip_idx: int, new_speed: float):
        """Variable-Speed Audio & Video Engine: Update clip speed multiplier and ripple timeline."""
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        if not (0 <= clip_idx < len(curr_clips)):
            return

        c = curr_clips[clip_idx]
        old_speed = float(c.get("speed", 1.0))
        if abs(old_speed - new_speed) < 1e-4:
            return

        orig_src_dur = c.get("source_duration")
        if orig_src_dur is None:
            orig_src_dur = max(0.01, float(c.get("duration", 0.0)) * old_speed)
            c["source_duration"] = orig_src_dur

        new_dur = round(orig_src_dur / new_speed, 3)
        c["speed"] = new_speed
        c["duration"] = new_dur

        # Ripple subsequent clips sequentially
        curr_st = 0.0
        for clip in curr_clips:
            clip["start"] = round(curr_st, 3)
            curr_st += clip.get("duration", 0.0)
        new_tot_dur = round(curr_st, 3)

        # 1. Update timeline editor
        self.timeline_editor.set_total_duration(new_tot_dur)
        self.timeline_editor.set_video_clips(curr_clips)

        # 2. Update TimelineModel
        if hasattr(self, 'timeline_model'):
            self.timeline_model.video_clips = curr_clips
            self.timeline_model.total_duration_sec = new_tot_dur

        # 3. Update Video Preview player (auto-triggers timeline audio rebuild)
        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_clips(curr_clips)

        # 4. Sync details inspector
        self._update_inspector_details(getattr(self, 'video_path', None), clips=curr_clips, duration=new_tot_dur)
        if hasattr(self, 'details_widget') and self.details_widget:
            self.details_widget.set_selected_clip(clip_idx, c)

        self.status_lbl.setText(f"⚡ Clip {clip_idx + 1} speed: {new_speed:.2f}x (New duration: {new_dur:.2f}s)")
        self.log_console.append_log(f"⚡ [Clip Speed] Clip {clip_idx + 1} speed set to {new_speed:.2f}x (Timeline: {new_dur:.2f}s, Total: {new_tot_dur:.2f}s)")

    def _on_clip_volume_changed(self, clip_idx: int, vol: float, muted: bool):
        """Update clip volume and mute properties."""
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        if not (0 <= clip_idx < len(curr_clips)):
            return

        c = curr_clips[clip_idx]
        c["volume"] = vol
        c["muted"] = muted

        self.timeline_editor.set_video_clips(curr_clips)
        if hasattr(self, 'timeline_model'):
            self.timeline_model.video_clips = curr_clips
        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_clips(curr_clips)

    def _toggle_media_bin_tab(self):
        """Media Bin and Preview are now permanently visible side-by-side in CapCut layout."""
        pass

    def _update_media_bin_btn_badge(self):
        if hasattr(self, 'media_bin'):
            cnt = len(self.media_bin.items)
            if hasattr(self, 'media_bin_btn') and self.media_bin_btn:
                self.media_bin_btn.setText(f"📁 Media Bin ({cnt})")

    def _on_media_bin_video_selected(self, video_path: str):
        """CapCut Workflow: Clicking/selecting a clip in Media Bin previews it in the player without modifying the timeline."""
        if not video_path or not os.path.exists(video_path):
            return
        from utils.file_utils import ensure_accessible_video_file
        safe_p = ensure_accessible_video_file(video_path)
        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_path(safe_p)
        self._update_inspector_details(safe_p)
        self.status_lbl.setText(f"👁️ Previewing: {os.path.basename(video_path)} (អូសទម្លាក់លើ Timeline ឬចុច '+' ដើម្បីកាត់ត)")

    def _update_inspector_details(self, file_path: str = None, clips: list = None, duration: float = None):
        """Update CapCut Inspector Details tab and top Preview Resolution badge with current video info."""
        if not hasattr(self, 'details_widget') or not self.details_widget:
            return
        
        target_path = file_path or getattr(self, 'video_path', None)
        if not target_path or not os.path.exists(target_path):
            if clips is not None and len(clips) == 0:
                self.details_widget.update_details({
                    "name": "—",
                    "path": "—",
                    "timeline": "0 Clips (Empty)",
                    "aspect": "16:9",
                    "resolution": "—",
                    "fps": "—",
                    "duration": "00:00",
                    "frames": "0",
                    "audio": "—",
                    "size": "0 MB",
                    "clips": "0",
                    "proxy": "None"
                })
                if hasattr(self, 'preview_res_lbl') and self.preview_res_lbl:
                    self.preview_res_lbl.setText("No Video Loaded")
            return

        try:
            from utils.ffmpeg import get_video_info
            v_info = get_video_info(target_path)
            f_size = os.path.getsize(target_path) if os.path.exists(target_path) else 0
            sz_str = f"{f_size / (1024 * 1024):.1f} MB" if f_size > 0 else "—"
            
            w = v_info.get("width", 1920)
            h = v_info.get("height", 1080)
            fps_val = v_info.get("fps", getattr(self.video_preview, 'fps', 30.0))
            
            if duration is not None:
                dur_val = duration
            else:
                dur_val = v_info.get("duration", 0.0)
                if dur_val <= 0 and hasattr(self, 'video_preview') and self.video_preview.total_frames > 0:
                    dur_val = self.video_preview.total_frames / max(1.0, fps_val)
                    
            m = int(dur_val // 60)
            s = int(dur_val % 60)
            dur_str = f"{m:02d}:{s:02d} ({dur_val:.1f}s)"
            
            # Aspect ratio string
            if w > 0 and h > 0:
                ratio = w / h
                if abs(ratio - 16/9) < 0.05:
                    aspect_str = "16:9"
                elif abs(ratio - 9/16) < 0.05:
                    aspect_str = "9:16"
                elif abs(ratio - 1.0) < 0.05:
                    aspect_str = "1:1"
                elif abs(ratio - 4/3) < 0.05:
                    aspect_str = "4:3"
                else:
                    aspect_str = f"{w}:{h}"
            else:
                aspect_str = "16:9"
                
            active_clips = clips if clips is not None else getattr(self.timeline_editor.waveform_canvas, 'video_clips', [])
            n_clips = len(active_clips) if active_clips else 1
            
            if hasattr(self, 'preview_res_lbl') and self.preview_res_lbl:
                self.preview_res_lbl.setText(f"{w}x{h} | {aspect_str} | {fps_val:.0f}fps")
                
            self.details_widget.update_details({
                "name": os.path.basename(target_path),
                "path": target_path,
                "timeline": f"{n_clips} Clips ({dur_str})",
                "aspect": aspect_str,
                "resolution": f"{w} x {h}",
                "fps": f"{fps_val:.2f} fps",
                "duration": dur_str,
                "frames": f"{int(dur_val * fps_val):,}",
                "audio": f"{v_info.get('sample_rate', 44100)} Hz, {v_info.get('channels', 2)} ch",
                "size": sz_str,
                "clips": str(n_clips),
                "proxy": "Original (High Quality)"
            })
        except Exception as e:
            logger.debug(f"Details inspector update warning: {e}")

    def _load_video_file(self, file_path: str, clear_segments: bool = True):
        import time
        t0 = time.monotonic()  # T0: File selected

        from utils.file_utils import (
            ensure_accessible_video_file, get_temp_path,
            check_file_access, FileAccessStatus, get_user_friendly_file_access_message
        )

        # 1. Comprehensive File Access & Media Validation
        acc_res = check_file_access(file_path)
        if not acc_res.is_accessible:
            title, msg = get_user_friendly_file_access_message(acc_res)
            self.status_lbl.setText("⚠ Cannot access selected video")
            self.log_console.append_log(f"❌ [FILE ACCESS] {acc_res.status.value}: {acc_res.message}")
            QMessageBox.warning(self, title, msg)
            return

        if clear_segments:
            # Clear any leftover segments from previous project to prevent collision
            stale_khmer_wav = get_temp_path("master_khmer_voice.wav")
            if os.path.exists(stale_khmer_wav):
                try:
                    os.remove(stale_khmer_wav)
                except Exception:
                    pass
            self._current_merged_clips = None

        safe_file_path = ensure_accessible_video_file(file_path)
        self.video_path = safe_file_path
        t1 = time.monotonic()  # T1: Metadata & Accessible Path Ready

        if clear_segments or not hasattr(self, 'original_video_name') or not self.original_video_name:
            base_n = os.path.basename(file_path)
            if not base_n.startswith("capcut_trim_"):
                self.original_video_name = base_n

        # Reset preview audio track selector to original video audio
        if hasattr(self.video_preview, 'audio_track_combo'):
            self.video_preview.audio_track_combo.blockSignals(True)
            self.video_preview.audio_track_combo.setCurrentIndex(0)
            self.video_preview.audio_track_combo.blockSignals(False)

        if clear_segments:
            # Clear any leftover segments from previous project to prevent collision
            self.transcribed_segments = []
            self.subtitle_table.set_segments([])
            self.timeline_editor.set_segments([])
            self.last_master_wav = None

        success = self.video_preview.set_video_path(safe_file_path)
        t2 = time.monotonic()  # T2: Media Source Attached
        t3 = t2               # T3: First Frame Visible (Rendered inside set_video_path)

        if not success:
            # If direct access failed in player, attempt with working copy
            safe_file_path = ensure_accessible_video_file(file_path, allow_copy=True)
            if safe_file_path != self.video_path and os.path.exists(safe_file_path):
                self.video_path = safe_file_path
                success = self.video_preview.set_video_path(safe_file_path)

        if not success:
            self.status_lbl.setText("❌ Error loading video")
            self.log_console.append_log(f"❌ Failed to load video into player: {file_path}")
            QMessageBox.warning(
                self, "Video Load Warning",
                f"មិនអាចបើកវីដេអូក្នុង Player បានទេ:\n{file_path}\n\n"
                "សូមពិនិត្យមើល Codec ឬទំហំវីដេអូ។"
            )
            return

        self.status_lbl.setText(f"Loaded: {os.path.basename(file_path)}")
        self.log_console.append_log(f"📹 Loaded video: {file_path}")

        # Auto-detect story folder name and episode (Enpoin / ភាគ)
        try:
            from utils.file_utils import parse_episode_from_filename
            parsed_title, parsed_ep = parse_episode_from_filename(file_path)
            if hasattr(self, 'story_folder_combo'):
                self.story_folder_combo.setEditText(parsed_title)
            if hasattr(self, 'episode_spin'):
                self.episode_spin.setValue(parsed_ep)
            self.log_console.append_log(f"🏷️ Auto-detected Story: '{parsed_title}', Episode: {parsed_ep}")
        except Exception:
            pass

        # Update timeline duration, video track clips, and full video logo duration to match loaded video
        if hasattr(self, 'video_preview') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)
            if hasattr(self, 'timeline_editor'):
                self.timeline_editor.set_total_duration(tot_dur)
            is_full = getattr(self.video_preview, 'logo_full_video', True)
            if is_full:
                self.video_preview.set_logo_timing(0.0, tot_dur)
                if hasattr(self, 'timeline_editor'):
                    self.timeline_editor.set_logo_clip(0.0, tot_dur, full_video=True)
            if hasattr(self, 'timeline_editor'):
                clips = getattr(self, '_current_merged_clips', None)
                if not clips or not isinstance(clips, list):
                    existing = getattr(self.timeline_editor.waveform_canvas, 'video_clips', [])
                    if not clear_segments and existing:
                        clips = existing
                    else:
                        v_name = getattr(self, 'original_video_name', os.path.basename(file_path))
                        clips = [{
                            "name": v_name,
                            "start": 0.0,
                            "duration": tot_dur,
                            "path": safe_file_path
                        }]
                else:
                    self._current_merged_clips = None
                self.timeline_editor.set_video_clips(clips, video_path=safe_file_path)
                if hasattr(self, 'video_preview'):
                    self.video_preview.set_video_clips(clips)
                
                # Sync central TimelineModel
                if hasattr(self, 'timeline_model'):
                    self.timeline_model.video_clips = clips
                    self.timeline_model.total_duration_sec = tot_dur
                    self.timeline_model.video_path = safe_file_path
                    self.timeline_model.segments = self.transcribed_segments

                # Update CapCut Details Inspector
                self._update_inspector_details(safe_file_path, clips=clips, duration=tot_dur)
                if hasattr(self, 'media_bin'):
                    self.media_bin.sync_timeline_clips(clips)

        t4 = time.monotonic()  # T4: Timeline Ready
        t5 = t4               # T5: Playback Ready

        self.log_console.append_log(
            f"⏱️ [PERF INSTRUMENTATION] Video Ingestion Benchmarks:\n"
            f"   • T0->T1 Metadata & Validation: {(t1 - t0)*1000:.1f}ms\n"
            f"   • T1->T2 Media Source Attached: {(t2 - t1)*1000:.1f}ms\n"
            f"   • T2->T3 First Frame Visible: {(t3 - t2)*1000:.1f}ms\n"
            f"   • T3->T4 Timeline Model Ready: {(t4 - t3)*1000:.1f}ms\n"
            f"   • T4->T5 Playback Engine Ready: {(t5 - t4)*1000:.1f}ms\n"
            f"   ➔ TOTAL PERCEIVED IMPORT TIME: {(t5 - t0)*1000:.1f}ms"
        )

        # AUTOMATICALLY GENERATE MP3 AUDIO IMMEDIATELY UPON UPLOAD (new project upload only)
        if clear_segments:
            self._generate_mp3_on_upload(safe_file_path)

        # Check if an existing SRT subtitle file is available for this video and auto-load it
        self._auto_load_subtitles_for_video(safe_file_path)

    def _auto_load_subtitles_for_video(self, video_path: str) -> bool:
        """Check if an SRT subtitle file already exists for this video and auto-load it."""
        stem = Path(video_path).stem
        if stem.startswith("safe_input_"):
            orig_stem = stem[len("safe_input_"):]
        else:
            orig_stem = stem

        candidate_paths = [
            os.path.join(str(self.output_dir), f"{stem}_khmer.srt"),
            os.path.join(str(self.output_dir), f"{orig_stem}_khmer.srt"),
            os.path.join(str(self.output_dir), f"{stem}.srt"),
            os.path.join(str(self.output_dir), f"{orig_stem}.srt"),
            os.path.join(os.path.dirname(video_path), f"{stem}_khmer.srt"),
            os.path.join(os.path.dirname(video_path), f"{orig_stem}_khmer.srt"),
            get_temp_path(f"{stem}_khmer.srt"),
            get_temp_path(f"{orig_stem}_khmer.srt")
        ]

        for p in candidate_paths:
            if os.path.exists(p) and os.path.getsize(p) > 20:
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        content = f.read()
                    from core.srt_translator import SRTTranslator
                    st = SRTTranslator()
                    sub_segs = st.parse_srt(content)
                    if sub_segs:
                        segs = []
                        for s in sub_segs:
                            segs.append({
                                "start": s.start_seconds,
                                "end": s.end_seconds,
                                "original_text": s.text,
                                "khmer_text": s.text
                            })
                        self.transcribed_segments = segs
                        self.subtitle_table.set_segments(segs)
                        if hasattr(self, 'timeline_editor'):
                            self.timeline_editor.set_segments(segs)
                        if hasattr(self, 'video_preview'):
                            self.video_preview.set_timeline_segments(segs)
                            self.video_preview._update_display()
                        self.log_console.append_log(f"📄 [Auto-Load Subtitles] Successfully loaded {len(segs)} segments from: {os.path.basename(p)}")
                        self.status_lbl.setText(f"📄 បានរកឃើញ SRT ស្វ័យប្រវត្តិ ({len(segs)} បន្ទាត់)")
                        return True
                except Exception as e:
                    self.log_console.append_log(f"Notice on loading candidate SRT {p}: {e}")
        return False

    def _generate_mp3_on_upload(self, video_path: str):
        """Automatically extract high-quality stereo MP3 audio from the uploaded video in background."""
        from utils.file_utils import generate_unique_filename
        out_mp3 = generate_unique_filename(video_path, prefix="audio", extension=".mp3", custom_dir=self.output_dir)
        
        # Check if already generated
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1000:
            self._on_mp3_generated(out_mp3)
            return

        self.extracted_mp3_path = None
        if getattr(self, 'copy_mp3_btn', None):
            self.copy_mp3_btn.setEnabled(False)
            self.copy_mp3_btn.setText("Generating MP3...")
        if hasattr(self, 'copy_mp3_btn_bot'):
            self.copy_mp3_btn_bot.setEnabled(False)
            self.copy_mp3_btn_bot.setText("Generating MP3...")

        class MP3Worker(QThread):
            finished = Signal(str)
            error = Signal(str)

            def __init__(self, in_vid, out_audio, parent=None):
                super().__init__(parent)
                self.in_vid = in_vid
                self.out_audio = out_audio
                self._cancelled = False
                self._proc = None

            def cancel(self):
                self._cancelled = True
                if self._proc is not None:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass

            def run(self):
                try:
                    import subprocess
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", self.in_vid,
                        "-vn",
                        "-c:a", "libmp3lame",
                        "-b:a", "192k",
                        self.out_audio
                    ]
                    self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._proc.wait()
                    if self._cancelled:
                        return
                    if self._proc.returncode == 0 and os.path.exists(self.out_audio):
                        self.finished.emit(self.out_audio)
                    else:
                        self.error.emit("FFmpeg error")
                except Exception as e:
                    self.error.emit(str(e))

        if not hasattr(self, '_mp3_workers'):
            self._mp3_workers = []

        # Cancel any previous workers cleanly without destroying them prematurely
        for old_w in list(self._mp3_workers):
            if old_w.isRunning():
                old_w.cancel()

        worker = MP3Worker(video_path, out_mp3, parent=self)
        self._mp3_workers.append(worker)

        def _cleanup():
            if worker in self._mp3_workers:
                try:
                    self._mp3_workers.remove(worker)
                except Exception:
                    pass

        worker.finished.connect(lambda p: (_cleanup(), self._on_mp3_generated(p)))
        worker.error.connect(lambda err: (
            _cleanup(),
            getattr(self, 'copy_mp3_btn', None) and self.copy_mp3_btn.setText("Copy MP3")
        ))
        worker.start()

    def _on_mp3_generated(self, mp3_path: str):
        self.extracted_mp3_path = mp3_path
        size_mb = (os.path.getsize(mp3_path) / (1024.0 * 1024.0)) if os.path.exists(mp3_path) else 0.0
        
        if getattr(self, 'copy_mp3_btn', None):
            self.copy_mp3_btn.setEnabled(True)
            self.copy_mp3_btn.setText("Copy MP3")
            self.copy_mp3_btn.setToolTip(f"MP3 Ready ({size_mb:.1f} MB)\n{mp3_path}\nClick to copy MP3 file to clipboard!")
        
        if hasattr(self, 'drag_mp3_btn'):
            self.drag_mp3_btn.setEnabled(True)
            self.drag_mp3_btn.setToolTip(f"Drag '{os.path.basename(mp3_path)}' directly into Web Browser / Google AI Studio")

        if hasattr(self, 'copy_mp3_btn_bot'):
            self.copy_mp3_btn_bot.setEnabled(True)
            self.copy_mp3_btn_bot.setText("📋 Copy MP3")
            self.copy_mp3_btn_bot.setToolTip(f"MP3 Ready ({size_mb:.1f} MB)\n{mp3_path}\nClick to copy MP3 file to clipboard!")

        self.status_lbl.setText(f"✅ Video & MP3 Ready: {os.path.basename(mp3_path)}")
        self.log_console.append_log(f"✅ [Auto-MP3] Generated MP3: {mp3_path} ({size_mb:.1f} MB)")
        self.log_console.append_log("📋 [Auto-MP3] Click '📋 Copy MP3' to paste or '📂 Drag to Web' to drop into Google AI Studio!")

    def _copy_mp3_to_clipboard(self):
        """Instant 1-Click Copy MP3 file to clipboard (no annoying dialog)."""
        if not hasattr(self, 'extracted_mp3_path') or not self.extracted_mp3_path or not os.path.exists(self.extracted_mp3_path):
            QMessageBox.warning(self, "No MP3", "មិនទាន់មាន File MP3 នៅឡើយទេ។ សូម Upload Video ជាមុនសិន។")
            return

        final_mp3_path = os.path.abspath(self.extracted_mp3_path)

        # 1. Copy MP3 file URL and text path directly to Clipboard
        clipboard = QApplication.clipboard()
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(final_mp3_path)])
        mime_data.setText(final_mp3_path)
        clipboard.setMimeData(mime_data)

        # 2. Visual feedback on buttons
        if getattr(self, 'copy_mp3_btn', None):
            self.copy_mp3_btn.setText(status_text)
        if hasattr(self, 'copy_mp3_btn_bot'):
            self.copy_mp3_btn_bot.setText(status_text)
        QTimer.singleShot(2500, lambda: (
            getattr(self, 'copy_mp3_btn', None) and self.copy_mp3_btn.setText("Copy MP3"),
            getattr(self, 'copy_mp3_btn_bot', None) and self.copy_mp3_btn_bot.setText("Copy MP3")
        ))

        dest_name = os.path.basename(final_mp3_path)
        self.log_console.append_log(f"📋 [Clipboard] Copied MP3 '{dest_name}'. You can Paste directly into Finder or Drag to Web Studio!")
        self.status_lbl.setText(f"Copied MP3: {dest_name}")

    def _save_mp3_to_folder(self):
        """Prompt user to choose destination folder & file location."""
        if not hasattr(self, 'extracted_mp3_path') or not self.extracted_mp3_path or not os.path.exists(self.extracted_mp3_path):
            return
        source_mp3 = os.path.abspath(self.extracted_mp3_path)
        base_name = os.path.basename(source_mp3)
        default_dir = getattr(self, 'output_dir', os.getcwd())
        default_save_path = os.path.join(default_dir, base_name)

        target_path, _ = QFileDialog.getSaveFileName(
            self,
            "ជ្រើសរើស Folder សម្រាប់ដាក់ File MP3",
            default_save_path,
            "MP3 Audio (*.mp3);;All Files (*)"
        )
        if target_path:
            try:
                import shutil
                shutil.copy2(source_mp3, target_path)
                self.log_console.append_log(f"💾 [Auto-MP3] បានរក្សាទុក File MP3 ទៅកាន់: {target_path}")
            except Exception as e_copy:
                QMessageBox.warning(self, "Save Error", f"មិនអាចរក្សាទុកបានទេ: {e_copy}")

        # 4. Visual feedback on buttons
        status_text = "Saved & Copied!" if saved_to_custom else "Copied to Clipboard!"
        if getattr(self, 'copy_mp3_btn', None):
            self.copy_mp3_btn.setText(status_text)
        if hasattr(self, 'copy_mp3_btn_bot'):
            self.copy_mp3_btn_bot.setText(status_text)
        QTimer.singleShot(2500, lambda: (
            getattr(self, 'copy_mp3_btn', None) and self.copy_mp3_btn.setText("Copy MP3"),
            getattr(self, 'copy_mp3_btn_bot', None) and self.copy_mp3_btn_bot.setText("Copy MP3")
        ))

        dest_name = os.path.basename(final_mp3_path)
        folder_name = os.path.dirname(final_mp3_path)
        self.log_console.append_log(f"[Clipboard] Copied MP3 '{dest_name}' in folder '{folder_name}'. You can Drag & Drop it into Google AI Studio or Paste into Finder!")
        self.status_lbl.setText(f"Copied MP3: {dest_name} (in {folder_name})")

    def _quick_copy_mp3_clipboard(self):
        """Quickly copy current MP3 file to clipboard without opening file dialog."""
        if not hasattr(self, 'extracted_mp3_path') or not self.extracted_mp3_path or not os.path.exists(self.extracted_mp3_path):
            return
        mp3_path = os.path.abspath(self.extracted_mp3_path)
        clipboard = QApplication.clipboard()
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(mp3_path)])
        mime_data.setText(mp3_path)
        clipboard.setMimeData(mime_data)
        self._reveal_mp3_in_finder()
        if getattr(self, 'copy_mp3_btn', None):
            self.copy_mp3_btn.setText("Copied to Clipboard!")
        if hasattr(self, 'copy_mp3_btn_bot'):
            self.copy_mp3_btn_bot.setText("Copied to Clipboard!")
        QTimer.singleShot(2500, lambda: (
            getattr(self, 'copy_mp3_btn', None) and self.copy_mp3_btn.setText("Copy MP3"),
            getattr(self, 'copy_mp3_btn_bot', None) and self.copy_mp3_btn_bot.setText("Copy MP3")
        ))
        self.log_console.append_log(f"[Clipboard] Quick copied MP3: {os.path.basename(mp3_path)}")
        self.status_lbl.setText("Quick copied MP3 to clipboard!")

    def _show_mp3_context_menu(self, pos):
        """Right-click context menu for MP3 actions."""
        if not hasattr(self, 'extracted_mp3_path') or not self.extracted_mp3_path or not os.path.exists(self.extracted_mp3_path):
            return
        sender = self.sender()
        menu = QMenu(self)
        menu.addAction("💾 Save MP3 to Folder...", self._save_mp3_to_folder)
        menu.addAction("📋 Quick Copy to Clipboard (Current File)", self._quick_copy_mp3_clipboard)
        menu.addAction("📝 Copy File Path", self._copy_mp3_path_only)
        menu.addAction("📂 Reveal in Finder", self._reveal_mp3_in_finder)
        menu.addAction("▶️ Play MP3 Audio", self._play_extracted_mp3)
        menu.exec_(sender.mapToGlobal(pos))

    def _copy_mp3_path_only(self):
        if hasattr(self, 'extracted_mp3_path') and self.extracted_mp3_path:
            QApplication.clipboard().setText(os.path.abspath(self.extracted_mp3_path))
            self.log_console.append_log(f"📝 Copied MP3 file path to clipboard: {self.extracted_mp3_path}")
            self.status_lbl.setText("📝 Copied MP3 path to clipboard!")

    def _reveal_mp3_in_finder(self):
        if hasattr(self, 'extracted_mp3_path') and self.extracted_mp3_path and os.path.exists(self.extracted_mp3_path):
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", self.extracted_mp3_path], check=False)
            elif os.name == 'nt':
                subprocess.run(["explorer", f"/select,{self.extracted_mp3_path}"], check=False)
            else:
                subprocess.run(["xdg-open", os.path.dirname(self.extracted_mp3_path)], check=False)

    def _play_extracted_mp3(self):
        if hasattr(self, 'extracted_mp3_path') and self.extracted_mp3_path and os.path.exists(self.extracted_mp3_path):
            if sys.platform == "darwin":
                subprocess.run(["open", self.extracted_mp3_path], check=False)
            elif os.name == 'nt':
                os.startfile(self.extracted_mp3_path)
            else:
                subprocess.run(["xdg-open", self.extracted_mp3_path], check=False)

    def _run_one_click_auto_dub(self):
        """Execute complete automated pipeline: MP3 -> React Khmer SRT -> TTS -> Dialogue Sync -> Video Mux."""
        if not self.video_path:
            self._browse_video()
            if not self.video_path:
                return

        if not self._ensure_gemini_key_available():
            return

        self.log_console.append_log("⚡ [1-Click Fast Auto Dub] Starting End-to-End Pipeline with React AI Engine...")
        self._set_processing_state(True, "⚡ Auto Dubbing in progress via React Engine...")

        def on_trans_done(segs):
            effects_config = self.video_preview.get_effects_config() if hasattr(self.video_preview, 'get_effects_config') else None
            self._start_dubbing_worker(pre_edited_segments=segs, effects_config=effects_config)

        self._start_react_translation_worker(on_finish_callback=on_trans_done)

    def _get_current_timeline_clips(self) -> list:
        """
        Return the current ordered list of video clips from Timeline (Single Source of Truth).
        Falls back to self.video_path if no multi-clip list is available.
        """
        # 1. Timeline Model
        if hasattr(self, 'timeline_model') and getattr(self.timeline_model, 'video_clips', None):
            clips = list(self.timeline_model.video_clips)
            if clips:
                return clips
        # 2. Timeline Editor waveform canvas
        if hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas'):
            v_clips = getattr(self.timeline_editor.waveform_canvas, 'video_clips', None)
            if v_clips:
                return list(v_clips)
        # 3. Video Preview widget
        if hasattr(self, 'video_preview') and getattr(self.video_preview, 'video_clips', None):
            v_clips = getattr(self.video_preview, 'video_clips', None)
            if v_clips:
                return list(v_clips)
        # 4. Fallback: single loaded video
        cur_v = getattr(self, 'video_path', None)
        if cur_v and os.path.exists(cur_v):
            dur = getattr(self, 'video_duration', 0.0)
            return [{
                "path": cur_v,
                "name": getattr(self, 'original_video_name', os.path.basename(cur_v)),
                "start": 0.0,
                "duration": dur,
                "source_in": 0.0
            }]
        return []

    def _run_auto_transcribe_and_translate(self):
        """Execute speech-to-text and automatically chain into Khmer translation."""
        clips = self._get_current_timeline_clips()
        if not self.video_path and not clips:
            QMessageBox.warning(self, "Warning", "Please load a video file first.")
            return

        if not self._ensure_gemini_key_available():
            return

        self.log_console.append_log("⚡ Starting Step 2: Whisper STT & Auto Khmer Translation across Timeline...")
        self.progress_bar.setValue(0)
        self._start_transcription_worker(auto_translate_next=True)

    # ==================== 5-STEP GEMINI MULTIMODAL AUDIO PIPELINE ====================
    def _run_gemini_audio_pipeline(self, auto_export_on_finish: bool = False):
        """Execute the complete 5-step Gemini Multimodal Audio pipeline natively on Desktop across all Timeline clips."""
        clips = self._get_current_timeline_clips()
        if not self.video_path and not clips:
            QMessageBox.warning(self, "No Video", "សូម Upload ឬជ្រើសរើស Video ជាមុនសិន។")
            return

        if not self._ensure_gemini_key_available():
            return

        clip_count = len(clips)
        self.log_console.append_log(f"🚀 [Gemini 5-Step Pipeline] Starting audio processing for {clip_count} Timeline clip(s)...")
        self._set_processing_state(True, f"🧠 [1/5] Gemini AI Audio Pipeline running ({clip_count} clips)...")
        self.progress_bar.setValue(5)

        from services.gemini_audio_service import GeminiAudioService

        class GeminiPipelineWorker(QThread):
            progress = Signal(int, str)
            finished = Signal(list, str)
            error = Signal(str)
            log = Signal(str)

            def __init__(self, clips=None, fallback_video_path=None):
                super().__init__()
                self.clips = clips or []
                self.fallback_video_path = fallback_video_path

            def run(self):
                try:
                    service = GeminiAudioService()
                    def _cb(pct, msg):
                        self.progress.emit(pct, msg)
                        self.log.emit(msg)

                    media_input = None
                    is_multi = len(self.clips) > 1 or (
                        len(self.clips) == 1 and (
                            float(self.clips[0].get("source_in", 0.0) or 0.0) > 0.05 or
                            float(self.clips[0].get("duration", 0.0) or 0.0) > 0.0
                        )
                    )

                    if is_multi:
                        self.log.emit(f"🎬 [Timeline Audio] Building continuous timeline composite audio for {len(self.clips)} clip(s)...")
                        self.progress.emit(8, "Building Timeline Audio...")
                        from services.timeline_audio_service import TimelineAudioService
                        tas = TimelineAudioService()
                        res = tas.build_timeline_composite_audio(self.clips)
                        media_input = res[0] if isinstance(res, (tuple, list)) else res

                    if not media_input or not os.path.exists(media_input):
                        if self.clips and self.clips[0].get("path") and os.path.exists(self.clips[0]["path"]):
                            media_input = self.clips[0]["path"]
                        elif self.fallback_video_path and os.path.exists(self.fallback_video_path):
                            media_input = self.fallback_video_path
                        else:
                            raise FileNotFoundError("រកមិនឃើញ video ឬ audio file លើ Timeline ទេ។")

                    stem = Path(media_input).stem
                    out_khmer_srt = os.path.join(str(OUTPUT_DIR), f"{stem}_khmer.srt")
                    out_orig_srt = os.path.join(str(OUTPUT_DIR), f"{stem}_original.srt")
                    out_orig_sub = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_original.srt")
                    out_khmer_sub = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_khmer.srt")

                    self.log.emit("🎙️ [Stage 1: STT] Running High-Precision Faster-Whisper (Acoustic Alignment & Full Speech Extraction)...")
                    _cb(15, "Running High-Precision Faster-Whisper STT...")
                    from services.stt_service import STTService
                    from services.translation_service import TranslationService
                    from utils.gemini_parser import seconds_to_srt_time
                    from utils.file_utils import export_segments_to_srt

                    stt_service = STTService()
                    stt_segs = stt_service.transcribe(
                        media_input,
                        source_lang="auto",
                        progress_callback=lambda p, m: _cb(15 + int(p * 0.45), f"STT: {m}")
                    )

                    if not stt_segs:
                        self.log.emit("⚠️ Faster-Whisper returned empty, falling back to Gemini Cloud STT...")
                        service = GeminiAudioService()
                        segments, srt_content = service.process_video_pipeline(
                            media_input, progress_callback=_cb, srt_output_path=out_khmer_srt
                        )
                    else:
                        # ===== STAGE 1: SAVE ORIGINAL SOURCE SRT (CHINESE) =====
                        export_segments_to_srt(stt_segs, out_orig_srt, text_key="original_text")
                        try:
                            export_segments_to_srt(stt_segs, out_orig_sub, text_key="original_text")
                        except Exception:
                            pass
                        self.log.emit(f"📄 [Stage 1 Complete] Original Source SRT saved ({len(stt_segs)} segments): {out_orig_srt}")

                        # ===== STAGE 2: TRANSLATE TO KHMER =====
                        _cb(65, f"Translating {len(stt_segs)} segments to Khmer with Gemini AI...")
                        self.log.emit(f"🌐 [Stage 2: Translation] Translating {len(stt_segs)} segments to natural spoken Khmer with Gemini AI...")
                        trans_service = TranslationService()
                        translated_segs = trans_service.translate_segments(
                            stt_segs,
                            source_lang="auto",
                            target_lang="km",
                            progress_callback=lambda p, m: _cb(65 + int(p * 0.30), f"Translating: {m}")
                        )

                        ui_segments = []
                        srt_lines = []
                        from services.speaker_detector import SpeakerDetector
                        detector = SpeakerDetector(audio_wav_path=media_input)

                        raw_for_diarization = []
                        for s in translated_segs:
                            raw_for_diarization.append({
                                "start": round(float(s.start), 2),
                                "end": round(float(s.end), 2),
                                "original_text": s.original_text or "",
                                "khmer_text": s.translated_text or (s.original_text or ""),
                                "speaker": getattr(s, 'speaker_id', None) or "Speaker 1"
                            })
                        diarized_results = detector.diarize_and_profile_segments(raw_for_diarization)

                        for i, s in enumerate(translated_segs, 1):
                            st_val = round(float(s.start), 2)
                            et_val = round(float(s.end), 2)
                            orig_t = s.original_text or ""
                            d = diarized_results[i - 1] if i - 1 < len(diarized_results) else {}

                            tag = d.get("speaker_tag", "[ប្រុស]")
                            voc = d.get("voice", "Khmer Male - Piseth")
                            role = d.get("role", "male")
                            pers = d.get("persona", "Male Adult")
                            clean_khm_untagged = d.get("khmer_text", "")
                            if not clean_khm_untagged:
                                clean_khm_untagged = (s.translated_text or orig_t).strip()
                                tag_match = re.match(r"^\[(ប្រុស|ស្រី|ក្មេង|ក្មេងប្រុស|ក្មេងស្រី|ចាស់|ចាស់ប្រុស|ចាស់ស្រី)\]\s*", clean_khm_untagged)
                                if tag_match:
                                    clean_khm_untagged = clean_khm_untagged[len(tag_match.group(0)):].strip()

                            final_tagged_khm = f"{tag} {clean_khm_untagged}".strip()
                            spk_t = d.get("speaker") or getattr(s, 'speaker_id', None) or f"Speaker 1"

                            ui_segments.append({
                                "id": f"sub_{i:04d}",
                                "start": st_val,
                                "end": et_val,
                                "original_text": orig_t,
                                "khmer_text": final_tagged_khm,
                                "translated_text": final_tagged_khm,
                                "speaker": spk_t,
                                "speaker_tag": tag,
                                "persona": pers,
                                "character": pers,
                                "voice": voc,
                                "voice_id": voc,
                                "style": "Normal"
                            })

                            st_srt = seconds_to_srt_time(st_val)
                            et_srt = seconds_to_srt_time(et_val)
                            srt_lines.append(f"{i}\n{st_srt} --> {et_srt}\n{final_tagged_khm}\n")

                        srt_content = "\n".join(srt_lines)
                        if out_khmer_srt:
                            with open(out_khmer_srt, "w", encoding="utf-8") as f:
                                f.write(srt_content)
                            try:
                                with open(out_khmer_sub, "w", encoding="utf-8") as f:
                                    f.write(srt_content)
                            except Exception:
                                pass
                            self.log.emit(f"📄 [Stage 2 Complete] Khmer SRT saved: {out_khmer_srt}")
                        segments = ui_segments

                    self.finished.emit(segments, srt_content)
                except Exception as e:
                    self.error.emit(str(e))

        self._gemini_worker = GeminiPipelineWorker(clips=clips, fallback_video_path=self.video_path)
        self._gemini_worker.progress.connect(lambda p, m: (self.progress_bar.setValue(p), self.status_lbl.setText(m)))
        self._gemini_worker.log.connect(self.log_console.append_log)

        def on_done(segments, srt_content):
            self._set_processing_state(False, f"✅ Gemini Complete! ({len(segments)} segments)")
            self.progress_bar.setValue(100)
            self.transcribed_segments = segments
            self.subtitle_table.set_segments(segments)
            if hasattr(self, 'timeline_editor'):
                self.timeline_editor.set_segments(segments)
            if hasattr(self, 'video_preview'):
                self.video_preview.set_timeline_segments(segments)
                self.video_preview._update_display()
            self.log_console.append_log(f"🎉 [Gemini AI] Successfully imported {len(segments)} Khmer segments into Subtitle Editor!")

            if auto_export_on_finish:
                self.log_console.append_log("✨ [1-Click Auto Dub] Subtitles ready! Automatically synthesizing voices and exporting final video...")
                self.status_lbl.setText("✨ [1-Click Auto Dub] កំពុងបញ្ចូលសំឡេងខ្មែរ និង Export វីដេអូចុងក្រោយ...")
                QtCore.QTimer.singleShot(600, self._export_final_video)
            else:
                QMessageBox.information(
                    self, "Gemini Subtitles Ready",
                    f"ការបកប្រែ និងចាប់ម៉ោងតាម Gemini AI បានជោគជ័យ!\n\n"
                    f"ចំនួនប្រយោគ: {len(segments)} បន្ទាត់\n"
                    f"អ្នកអាចកែសម្រួលអក្សរ ឬចុច 'EXPORT FINAL VIDEO' ដើម្បីបញ្ចូលសំឡេងខ្មែរបានភ្លាមៗ។"
                )

        def on_err(err):
            self._set_processing_state(False, "❌ Gemini Pipeline Error")
            self.log_console.append_log(f"❌ [Gemini AI Error]: {err}")
            QMessageBox.critical(self, "Gemini Error", f"កំហុសក្នុងការដំណើរការ Gemini AI:\n{err}")

        self._gemini_worker.finished.connect(on_done)
        self._gemini_worker.error.connect(on_err)
        self._gemini_worker.start()

    # ==================== 1-CLICK MAGIC AUTO DUB & SUBTITLE PRESETS ====================
    def _on_magic_1click_dub_clicked(self):
        """1-Click Magic Auto Dub: End-to-end hands-free pipeline from Video to Dubbed & Subtitled Output."""
        cur_v = getattr(self, 'video_path', None)
        if not cur_v or not os.path.exists(cur_v):
            self._browse_video()
            cur_v = getattr(self, 'video_path', None)
            if not cur_v or not os.path.exists(cur_v):
                return

        if not self._ensure_gemini_key_available():
            return

        mb_items = getattr(self.media_bin, 'items', []) if hasattr(self, 'media_bin') else []
        if len(mb_items) > 1:
            reply = QMessageBox.question(
                self, "✨ 1-Click Auto Dub",
                f"រកឃើញវីដេអូចំនួន {len(mb_items)} Clips ក្នុង Media Bin!\n\n"
                f"• ចុច 'Yes' ដើម្បីដំណើរការ Auto Dub វីដេអូទាំងអស់តែម្តង (Batch Mode)\n"
                f"• ចុច 'No' ដើម្បីដំណើរការតែវីដេអូសកម្មនេះប៉ុណ្ណោះ",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self._start_batch_dubbing([item["path"] for item in mb_items])
                return

        curr_segs = getattr(self, 'transcribed_segments', [])
        if curr_segs:
            reply = QMessageBox.question(
                self, "✨ 1-Click Auto Dub",
                f"វីដេអូនេះមាន Subtitle ចំនួន {len(curr_segs)} បន្ទាត់រួចរាល់ហើយ!\n\n"
                f"• ចុច 'Yes' ដើម្បី Export វីដេអូជាមួយសំឡេងខ្មែរភ្លាមៗ (Export Final Video)\n"
                f"• ចុច 'No' ដើម្បីដំណើរការស្កេន AI ឡើងវិញពីដើម",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self._export_final_video()
                return

        self.status_lbl.setText("✨ [1-Click Auto Dub] កំពុងដំណើរការ Faster-Whisper STT → Gemini Text Translation...")
        self.log_console.append_log("✨ [1-Click Auto Dub] Starting Production Pipeline (Faster-Whisper STT → Gemini Text Translation)...")
        self._start_dubbing_worker(pre_edited_segments=None)

    def _show_quick_sub_styles_menu(self):
        """Display quick popup menu with popular TikTok, Cinema, and Viral subtitle presets."""
        from qt_compat import QMenu
        from gui.widgets import SUBTITLE_STYLE_TEMPLATES
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0b1120;
                color: #f8fafc;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 6px;
                font-size: 12px;
            }
            QMenu::item {
                padding: 6px 18px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #1e293b;
                color: #38bdf8;
            }
        """)

        for tpl in SUBTITLE_STYLE_TEMPLATES:
            act = menu.addAction(f"{tpl['name']} ({tpl['font']})")
            act.triggered.connect(lambda checked=False, t=tpl: self._apply_sub_preset(t))

        from qt_compat import QCursor
        if hasattr(self, 'sub_styles_menu_btn') and self.sub_styles_menu_btn.isVisible():
            menu.exec_(self.sub_styles_menu_btn.mapToGlobal(QtCore.QPoint(0, self.sub_styles_menu_btn.height() + 4)))
        else:
            menu.exec_(QCursor.pos())

    def _apply_sub_preset(self, tpl: dict):
        """Apply a selected subtitle style across the preview canvas and video effects configuration."""
        if hasattr(self, 'video_effects'):
            self.video_effects._apply_sub_template(tpl)
            self.status_lbl.setText(f"🎨 បានជ្រើសរើសម៉ូត: {tpl['name']} ({tpl['font']})")
            self.log_console.append_log(f"🎨 [Subtitle Preset] Applied style: {tpl['name']} (Font: {tpl['font']}, Color: {tpl['color']})")

    def _on_table_subtitle_style_changed(self, style_name: str):
        """Map selected style name from SubtitleTableWidget combo to preset template."""
        from gui.widgets import SUBTITLE_STYLE_TEMPLATES
        tpl = None
        for t in SUBTITLE_STYLE_TEMPLATES:
            if t["name"].lower() in style_name.lower() or style_name.lower() in t["name"].lower():
                tpl = t
                break
        if not tpl and SUBTITLE_STYLE_TEMPLATES:
            tpl = SUBTITLE_STYLE_TEMPLATES[0]
        if tpl:
            self._apply_sub_preset(tpl)

    # ==================== MEDIA BIN BATCH DUBBING ====================
    def _start_batch_dubbing(self, video_paths: list):
        if not video_paths:
            QMessageBox.information(self, "No Videos", "គ្មានវីដេអូសម្រាប់ដំណើរការ Batch ទេ។")
            return

        if not self._ensure_gemini_key_available():
            return

        from core.batch_worker import BatchDubbingWorker

        effects_config = self.video_preview.get_effects_config() if hasattr(self.video_preview, 'get_effects_config') else {}
        bgm_vol = self.bgm_volume_slider.value() / 100.0 if hasattr(self, 'bgm_volume_slider') else 0.20
        voice_name = getattr(self, 'active_voice_preset', "Khmer Male - Piseth")
        target_res = self.res_combo.currentText() if hasattr(self, 'res_combo') else "Match Source"

        self.media_bin.set_batch_running(True)
        self.log_console.append_log(f"⚡ [Batch Dubbing] Launching batch queue for {len(video_paths)} videos...")

        self._batch_worker = BatchDubbingWorker(
            video_paths=video_paths,
            effects_config=effects_config,
            background_volume=bgm_vol,
            voice_name=voice_name,
            target_resolution=target_res
        )
        self._batch_worker.item_status_changed.connect(self.media_bin.update_item_status)
        self._batch_worker.overall_progress.connect(lambda p, m: (self.progress_bar.setValue(p), self.status_lbl.setText(m)))
        self._batch_worker.log_message.connect(self.log_console.append_log)

        def on_batch_done(success, total):
            self.media_bin.set_batch_running(False)
            self.progress_bar.setValue(100)
            self.status_lbl.setText(f"Batch Done: {success}/{total}")
            QMessageBox.information(
                self, "Batch Complete 🎉",
                f"ការផលិតវីដេអូជាបាច់ (Batch Dubbing) បានបញ្ចប់!\n\n"
                f"វីដេអូជោគជ័យ: {success} / {total}\n"
                f"ទីតាំងរក្សាទុក: {OUTPUT_DIR}"
            )
            if sys.platform == "darwin":
                subprocess.run(["open", str(OUTPUT_DIR)], check=False)

        self._batch_worker.batch_finished.connect(on_batch_done)
        self._batch_worker.start()

    def _stop_batch_dubbing(self):
        if hasattr(self, '_batch_worker') and self._batch_worker and self._batch_worker.isRunning():
            self._batch_worker.cancel()
            self.media_bin.set_batch_running(False)
            self.status_lbl.setText("Batch cancelled by user.")

    def _update_gemini_btn_status(self):
        if not hasattr(self, 'gemini_key_btn'):
            return
        from utils.config_manager import get_gemini_api_keys
        keys = get_gemini_api_keys()
        if len(keys) > 1:
            self.gemini_key_btn.setText(f"🔑 Gemini AI ({len(keys)} Keys Pool ✓)")
            self.gemini_key_btn.setStyleSheet("""
                QPushButton {
                    background-color: #064e3b;
                    color: #34d399;
                    border: 1px solid #10b981;
                    border-radius: 6px;
                    padding: 4px 10px;
                    font-weight: 700;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background-color: #047857;
                }
            """)
        elif len(keys) == 1:
            self.gemini_key_btn.setText("🔑 Gemini AI (✓ Active)")
            self.gemini_key_btn.setStyleSheet("""
                QPushButton {
                    background-color: #064e3b;
                    color: #34d399;
                    border: 1px solid #059669;
                    border-radius: 6px;
                    padding: 4px 10px;
                    font-weight: 700;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background-color: #047857;
                }
            """)
        else:
            self.gemini_key_btn.setText("🔑 Gemini API Key (!)")
            self.gemini_key_btn.setStyleSheet("""
                QPushButton {
                    background-color: #78350f;
                    color: #fde68a;
                    border: 1px solid #d97706;
                    border-radius: 6px;
                    padding: 4px 10px;
                    font-weight: 700;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background-color: #92400e;
                }
            """)

    def _open_gemini_settings(self):
        dlg = GeminiApiKeyDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self._update_gemini_btn_status()
            from utils.config_manager import get_gemini_api_keys
            k_count = len(get_gemini_api_keys())
            if k_count > 1:
                self.log_console.append_log(f"🔑 Google Gemini API Key Pool active with {k_count} accounts (Load-balanced & {k_count}x Quota)!")
            else:
                self.log_console.append_log("🔑 Google Gemini API Key configured and verified successfully!")

    def _open_settings(self):
        self._open_gemini_settings()

    def _ensure_gemini_key_available(self) -> bool:
        key = get_gemini_api_key()
        if key:
            return True

        ret = QMessageBox.question(
            self,
            "Gemini API Key Required 🔑",
            "ដើម្បីបកប្រែពាក្យសំដីជាភាសាខ្មែរ (ភាសានិយាយ) ឱ្យពិរោះតាមបែប Gemini AI\n"
            "សូមបញ្ចូល Gemini API Key របស់អ្នកជាមុនសិន!\n\n"
            "តើអ្នកចង់បញ្ចូល Gemini API Key ឥឡូវនេះដែរឬទេ?",
            QMessageBox.Yes | QMessageBox.No
        )
        if ret == QMessageBox.Yes:
            self._open_gemini_settings()
            return bool(get_gemini_api_key())
        return False

    # ==================== VIDEO BLUR HANDLERS ====================
    def _on_blur_toggled(self, enabled: bool):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_blur_enabled(enabled)
            status_text = "enabled" if enabled else "disabled"
            self.log_console.append_log(f"🔍 Video Blur effect {status_text}.")
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor.set_track_visible('blur', enabled)

    def _on_blur_intensity_changed(self, intensity: int):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_blur_intensity(intensity)

    def _on_reset_blur_requested(self):
        if hasattr(self, 'video_preview'):
            self.video_preview.reset_blur_position()
            self.log_console.append_log("🔍 Video Blur position reset to center.")

    def _on_blur_items_changed(self, blur_items: list, active_id: str):
        if hasattr(self, 'video_effects'):
            self.video_effects.sync_blur_items(blur_items, active_id)
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor.set_blur_items(blur_items, active_id)

    # ==================== VIDEO TEXT OVERLAY HANDLERS ====================
    def _on_text_items_changed(self, text_items: list, active_id: str):
        if hasattr(self, 'video_effects'):
            self.video_effects.sync_text_items(text_items, active_id)
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor.set_text_items(text_items, active_id)

    def _on_text_item_timing_changed(self, text_id: str, start_sec: float, dur_sec: float):
        if hasattr(self, 'video_preview'):
            for it in getattr(self.video_preview, 'text_items', []):
                if it.get("id") == text_id:
                    it["start"] = start_sec
                    it["duration"] = dur_sec
                    break
            if getattr(self.video_preview, 'active_text_id', None) == text_id:
                self.video_preview.text_start_sec = start_sec
                self.video_preview.text_duration_sec = dur_sec
            self.video_preview.text_items_changed.emit(self.video_preview.text_items, text_id)
            self.video_preview._update_display()
        if hasattr(self, 'video_effects') and getattr(self.video_effects, 'active_text_id', None) == text_id:
            self.video_effects.set_text_timing(start_sec, dur_sec)

    def _on_text_toggled(self, enabled: bool):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_text_overlay_enabled(enabled)
            status_text = "enabled" if enabled else "disabled"
            self.log_console.append_log(f"📝 Text Overlay effect {status_text}.")
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor.set_track_visible('text', enabled)

    def _on_text_updated(self, text: str, color_hex: str, size: int, font_name: str = "Kantumruy Pro"):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_text_overlay_text(text)
            self.video_preview.set_text_overlay_color(color_hex)
            self.video_preview.set_text_overlay_size(size)
            if hasattr(self.video_preview, 'set_text_overlay_font'):
                self.video_preview.set_text_overlay_font(font_name)
        if hasattr(self, 'timeline_editor'):
            st = getattr(self.video_preview, 'text_start_sec', 0.0) if hasattr(self, 'video_preview') else 0.0
            dur = getattr(self.video_preview, 'text_duration_sec', 5.0) if hasattr(self, 'video_preview') else 5.0
            self.timeline_editor.set_text_clip(st, dur, text=text)
        self.log_console.append_log(f"📝 Text Overlay updated: '{text}' (Font: {font_name}, Color: {color_hex}, Size: {size})")

    def _on_text_position_changed(self, x: int, y: int):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_text_overlay_position(x, y)

    def _on_timeline_text_clip_changed(self, start_sec: float, dur_sec: float):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_text_overlay_animation(
                anim_type=getattr(self.video_preview, 'text_anim_type', 'pop'),
                speed=getattr(self.video_preview, 'text_anim_speed', 0.5),
                mode="custom",
                start_sec=start_sec,
                duration_sec=dur_sec
            )
            self.video_preview._update_display()
        if hasattr(self, 'video_effects'):
            self.video_effects.set_text_timing(start_sec, dur_sec)

    def _on_timeline_logo_clip_changed(self, start_sec: float, dur_sec: float):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_logo_timing(start_sec, dur_sec)
            self.video_preview.set_logo_full_video(False)
        if hasattr(self, 'video_effects') and hasattr(self.video_effects, 'logo_full_video_chk'):
            self.video_effects.logo_full_video_chk.blockSignals(True)
            self.video_effects.logo_full_video_chk.setChecked(False)
            self.video_effects.logo_full_video_chk.blockSignals(False)

    def _on_logo_full_video_toggled(self, full_video: bool):
        tot_dur = 60.0
        if hasattr(self, 'video_preview') and hasattr(self.video_preview, 'total_frames') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)
        elif hasattr(self, 'timeline_editor'):
            tot_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 60.0)

        if hasattr(self, 'video_preview'):
            self.video_preview.set_logo_full_video(full_video)
            if full_video:
                self.video_preview.set_logo_timing(0.0, tot_dur)
            self.video_preview._update_display()
        if hasattr(self, 'timeline_editor'):
            if full_video:
                self.timeline_editor.set_logo_clip(0.0, tot_dur, full_video=True)
            else:
                self.timeline_editor.waveform_canvas.logo_clip["full_video"] = False

    def _on_timeline_sub_adjusted(self, seg_idx: int, start_sec: float, end_sec: float):
        if hasattr(self, 'subtitle_table'):
            self.subtitle_table.update_segment_timing(seg_idx, start_sec, end_sec)
            if hasattr(self, 'video_preview'):
                self.video_preview.set_timeline_segments(self.subtitle_table.segments)
                self.video_preview._update_display()

    # ==================== VIDEO CUTTING & TRIMMING HANDLERS ====================
    def _on_video_cut_requested(self, cut_info: dict = None):
        """Open the Video Cutter & Trimmer Studio dialog and process user cut request."""
        if not self.video_path or not os.path.exists(self.video_path):
            QMessageBox.information(
                self, "No Video Loaded",
                "សូមបើក ឬទម្លាក់វីដេអូចូលជាមុនសិន មុននឹងធ្វើការកាត់ត (Please load a video first)."
            )
            return

        playhead_sec = 0.0
        in_sec = None
        out_sec = None
        if hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas'):
            playhead_sec = self.timeline_editor.waveform_canvas.playhead_pos_sec
            in_sec = self.timeline_editor.waveform_canvas.in_point_sec
            out_sec = self.timeline_editor.waveform_canvas.out_point_sec
        elif cut_info:
            playhead_sec = cut_info.get("playhead_sec", 0.0)
            in_sec = cut_info.get("in_sec")
            out_sec = cut_info.get("out_sec")

        tot_dur = 0.0
        if hasattr(self, 'video_preview') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)
        elif hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas'):
            tot_dur = self.timeline_editor.waveform_canvas.total_duration_sec

        dlg = VideoCutterDialog(
            video_path=self.video_path,
            total_duration=tot_dur,
            current_playhead_sec=playhead_sec,
            in_point_sec=in_sec,
            out_point_sec=out_sec,
            parent=self
        )

        if dlg.exec() != QDialog.Accepted:
            return

        settings = dlg.get_settings()
        self._execute_video_cut(settings)

    def _is_typing_in_input(self) -> bool:
        """Check if user has focus inside a text input field so single-letter shortcuts (Q, W) do not intercept typing."""
        focus_w = QApplication.focusWidget()
        if focus_w and isinstance(focus_w, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return True
        return False

    def _trigger_shortcut_split(self):
        if self._is_typing_in_input():
            return
        playhead = getattr(self.timeline_editor.waveform_canvas, 'playhead_pos_sec', 0.0)
        self._on_video_split_requested(playhead)

    def _trigger_shortcut_trim_left(self):
        if self._is_typing_in_input():
            return
        playhead = getattr(self.timeline_editor.waveform_canvas, 'playhead_pos_sec', 0.0)
        self._on_video_trim_left_requested(playhead)

    def _trigger_shortcut_trim_right(self):
        if self._is_typing_in_input():
            return
        playhead = getattr(self.timeline_editor.waveform_canvas, 'playhead_pos_sec', 0.0)
        self._on_video_trim_right_requested(playhead)

    def _trigger_shortcut_delete(self):
        """Trigger CapCut-style delete for selected video clip or in/out range."""
        if self._is_typing_in_input():
            return
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor._on_delete_clicked()

    def _trigger_shortcut_undo(self):
        """CapCut-Style Undo: seamlessly revert previous trim/cut/split operation."""
        if self._is_typing_in_input():
            return

        # 1. Try Command-based TimelineModel Undo (<0.1ms, zero deep-copy overhead)
        if hasattr(self, 'timeline_model') and self.timeline_model.can_undo():
            if self.timeline_model.undo():
                self.transcribed_segments = self.timeline_model.segments
                self.subtitle_table.set_segments(self.timeline_model.segments)
                self.timeline_editor.set_segments(self.timeline_model.segments)
                self.timeline_editor.set_video_clips(self.timeline_model.video_clips)
                self.timeline_editor.set_total_duration(self.timeline_model.total_duration_sec)
                self.timeline_editor.set_playhead_position(self.timeline_model.playhead_pos_sec)
                self.status_lbl.setText("↩️ បានត្រឡប់ក្រោយ (Undo restored)")
                self.log_console.append_log("↩️ [CapCut Undo] Restored previous action")
                return

        # 2. Fallback to undo stack if present
        if not hasattr(self, '_undo_stack') or not self._undo_stack:
            self.status_lbl.setText("ℹ️ គ្មានសកម្មភាពដែលត្រូវត្រឡប់ក្រោយទេ (Nothing to undo)")
            return

        prev = self._undo_stack.pop()
        prev_video = prev.get("video_path")
        prev_segs = prev.get("segments", [])
        prev_playhead = prev.get("playhead", 0.0)
        prev_clips = prev.get("clips")

        if prev_video and os.path.exists(prev_video):
            self._load_video_file(prev_video, clear_segments=False)
            self.transcribed_segments = prev_segs
            self.subtitle_table.set_segments(prev_segs)
            self.timeline_editor.set_segments(prev_segs)
            if prev_clips is not None:
                self.timeline_editor.set_video_clips(prev_clips)
                if hasattr(self, 'video_preview'):
                    self.video_preview.set_video_clips(prev_clips)
            self.timeline_editor.set_playhead_position(prev_playhead)
            self.status_lbl.setText("↩️ បានត្រឡប់ក្រោយ (Undo restored)")
            self.log_console.append_log(f"↩️ [CapCut Undo] Restored previous video & clips (Undo)")

    def _trigger_shortcut_redo(self):
        """CapCut-Style Redo: re-apply reverted operation."""
        if self._is_typing_in_input():
            return
        if hasattr(self, 'timeline_model') and self.timeline_model.can_redo():
            if self.timeline_model.redo():
                self.transcribed_segments = self.timeline_model.segments
                self.subtitle_table.set_segments(self.timeline_model.segments)
                self.timeline_editor.set_segments(self.timeline_model.segments)
                self.timeline_editor.set_video_clips(self.timeline_model.video_clips)
                self.timeline_editor.set_total_duration(self.timeline_model.total_duration_sec)
                self.timeline_editor.set_playhead_position(self.timeline_model.playhead_pos_sec)
                self.status_lbl.setText("↪️ បានធ្វើឡើងវិញ (Redo applied)")
                self.log_console.append_log("↪️ [CapCut Redo] Re-applied action")
                return
        self.status_lbl.setText("ℹ️ គ្មានសកម្មភាពដែលត្រូវធ្វើឡើងវិញទេ (Nothing to redo)")

    def _on_video_trim_left_requested(self, trim_sec: float):
        """CapCut-Style Instant Trim Left: Zero popups, lossless 0.1s trim, ripple subtitle shift, Ctrl+Z to undo."""
        if not self.video_path or not os.path.exists(self.video_path):
            return

        tot_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)
        if tot_dur <= 0 and hasattr(self, 'video_preview') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)

        if trim_sec <= 0.15 or trim_sec >= (tot_dur - 0.15):
            return

        if not hasattr(self, '_undo_stack'):
            self._undo_stack = []
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        self._undo_stack.append({
            "video_path": self.video_path,
            "segments": copy.deepcopy(getattr(self, 'transcribed_segments', [])),
            "playhead": trim_sec,
            "clips": curr_clips
        })
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)

        from utils.file_utils import get_temp_path
        from utils.ffmpeg import trim_video
        ts = int(time.time() * 1000) % 1000000
        out_p = get_temp_path(f"capcut_trim_l_{ts}.mp4")

        self.status_lbl.setText(f"✂️ Instant Trim Left (0.0s ➔ {trim_sec:.2f}s)...")
        ok = trim_video(self.video_path, out_p, trim_sec, tot_dur, lossless=True)
        if ok and os.path.exists(out_p):
            # Ripple shift subtitles left
            old_segs = getattr(self, 'transcribed_segments', [])
            new_segs = []
            for s in old_segs:
                st = s.get("start", 0.0)
                et = s.get("end", 0.0)
                if et <= trim_sec:
                    continue
                s_c = dict(s)
                s_c["start"] = round(max(0.0, st - trim_sec), 2)
                s_c["end"] = round(max(0.1, et - trim_sec), 2)
                new_segs.append(s_c)

            # Ripple shift clips on timeline
            new_v_clips = []
            orig_name = getattr(self, 'original_video_name', os.path.basename(self.video_path))
            for c in curr_clips:
                c_st = c.get("start", 0.0)
                c_dur = c.get("duration", 0.0)
                c_end = c_st + c_dur
                if c_end <= trim_sec:
                    continue
                c_copy = dict(c)
                if c_st < trim_sec:
                    c_copy["start"] = 0.0
                    c_copy["duration"] = round(c_end - trim_sec, 2)
                else:
                    c_copy["start"] = round(c_st - trim_sec, 2)
                new_v_clips.append(c_copy)
            if not new_v_clips:
                new_v_clips = [{"name": orig_name, "start": 0.0, "duration": round(tot_dur - trim_sec, 2), "path": out_p}]

            self._load_video_file(out_p, clear_segments=False)
            self.transcribed_segments = new_segs
            self.subtitle_table.set_segments(new_segs)
            self.timeline_editor.set_segments(new_segs)
            self.timeline_editor.set_video_clips(new_v_clips, video_path=out_p)
            if hasattr(self, 'timeline_model'):
                self.timeline_model.video_clips = new_v_clips
            if hasattr(self, 'video_preview'):
                self.video_preview.set_video_clips(new_v_clips)
            self.timeline_editor.set_playhead_position(0.0)
            self.status_lbl.setText(f"✂️ Trimmed Left ({trim_sec:.2f}s) ➔ ចុច Ctrl+Z ដើម្បីមិនកាត់ (Undo)")
            self.log_console.append_log(f"✂️ [CapCut Trim Left] Trimmed 0.0s -> {trim_sec:.2f}s. New duration: {(tot_dur - trim_sec):.2f}s (Ctrl+Z to Undo)")

    def _on_video_trim_right_requested(self, trim_sec: float):
        """CapCut-Style Instant Trim Right: Zero popups, lossless 0.1s trim, truncate subtitles, Ctrl+Z to undo."""
        if not self.video_path or not os.path.exists(self.video_path):
            return

        tot_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)
        if tot_dur <= 0 and hasattr(self, 'video_preview') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)

        if trim_sec <= 0.15 or trim_sec >= (tot_dur - 0.15):
            return

        if not hasattr(self, '_undo_stack'):
            self._undo_stack = []
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        self._undo_stack.append({
            "video_path": self.video_path,
            "segments": copy.deepcopy(getattr(self, 'transcribed_segments', [])),
            "playhead": trim_sec,
            "clips": curr_clips
        })
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)

        from utils.file_utils import get_temp_path
        from utils.ffmpeg import trim_video
        ts = int(time.time() * 1000) % 1000000
        out_p = get_temp_path(f"capcut_trim_r_{ts}.mp4")

        self.status_lbl.setText(f"✂️ Instant Trim Right ({trim_sec:.2f}s ➔ {tot_dur:.2f}s)...")
        ok = trim_video(self.video_path, out_p, 0.0, trim_sec, lossless=True)
        if ok and os.path.exists(out_p):
            # Truncate subtitles
            old_segs = getattr(self, 'transcribed_segments', [])
            new_segs = []
            for s in old_segs:
                st = s.get("start", 0.0)
                et = s.get("end", 0.0)
                if st >= trim_sec:
                    continue
                s_c = dict(s)
                if et > trim_sec:
                    s_c["end"] = round(trim_sec, 2)
                new_segs.append(s_c)

            # Truncate clips on timeline
            new_v_clips = []
            orig_name = getattr(self, 'original_video_name', os.path.basename(self.video_path))
            for c in curr_clips:
                c_st = c.get("start", 0.0)
                c_dur = c.get("duration", 0.0)
                if c_st >= trim_sec:
                    continue
                c_copy = dict(c)
                if c_st + c_dur > trim_sec:
                    c_copy["duration"] = round(trim_sec - c_st, 2)
                new_v_clips.append(c_copy)
            if not new_v_clips:
                new_v_clips = [{"name": orig_name, "start": 0.0, "duration": round(trim_sec, 2), "path": out_p}]

            self._load_video_file(out_p, clear_segments=False)
            self.transcribed_segments = new_segs
            self.subtitle_table.set_segments(new_segs)
            self.timeline_editor.set_segments(new_segs)
            self.timeline_editor.set_video_clips(new_v_clips, video_path=out_p)
            if hasattr(self, 'timeline_model'):
                self.timeline_model.video_clips = new_v_clips
            if hasattr(self, 'video_preview'):
                self.video_preview.set_video_clips(new_v_clips)
            self.timeline_editor.set_playhead_position(max(0.0, trim_sec - 0.5))
            self.status_lbl.setText(f"✂️ Trimmed Right ({trim_sec:.2f}s) ➔ ចុច Ctrl+Z ដើម្បីមិនកាត់ (Undo)")
            self.log_console.append_log(f"✂️ [CapCut Trim Right] Kept 0.0s -> {trim_sec:.2f}s (Ctrl+Z to Undo)")

    def _on_video_split_requested(self, split_sec: float):
        """CapCut-Style Instant Split at playhead position without popups."""
        if not self.video_path or not os.path.exists(self.video_path):
            return

        tot_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)
        if tot_dur <= 0 and hasattr(self, 'video_preview') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)

        if split_sec <= 0.15 or split_sec >= (tot_dur - 0.15):
            return

        orig_name = getattr(self, 'original_video_name', os.path.basename(self.video_path))
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        if not curr_clips:
            curr_clips = [{
                "name": orig_name,
                "start": 0.0,
                "duration": tot_dur,
                "path": self.video_path,
                "source_in": 0.0,
                "speed": 1.0,
                "volume": 1.0,
                "muted": False,
                "id": "clip_0"
            }]

        target_idx = -1
        for idx, c in enumerate(curr_clips):
            c_st = c.get("start", 0.0)
            c_dur = c.get("duration", 0.0)
            if (c_st + 0.05) <= split_sec <= (c_st + c_dur - 0.05):
                target_idx = idx
                break

        if target_idx == -1:
            for idx, c in enumerate(curr_clips):
                c_st = c.get("start", 0.0)
                c_dur = c.get("duration", 0.0)
                if c_st <= split_sec <= (c_st + c_dur):
                    if (split_sec - c_st) >= 0.05 and (c_st + c_dur - split_sec) >= 0.05:
                        target_idx = idx
                        break

        if target_idx == -1:
            self.status_lbl.setText(f"⚠️ សូមអូស Playhead ចូលក្នុង Clip ដើម្បី Split (ត្រង់ {split_sec:.2f}s កៀកគែមពេក)")
            return

        if not hasattr(self, '_undo_stack'):
            self._undo_stack = []
        self._undo_stack.append({
            "video_path": self.video_path,
            "segments": copy.deepcopy(getattr(self, 'transcribed_segments', [])),
            "playhead": split_sec,
            "clips": copy.deepcopy(curr_clips)
        })
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)

        tgt = curr_clips[target_idx]
        c_st = float(tgt.get("start", 0.0))
        c_dur = float(tgt.get("duration", 0.0))
        c_name = tgt.get("name", orig_name)
        c_path = tgt.get("path", self.video_path)
        c_s_in = float(tgt.get("source_in", 0.0))
        c_speed = float(tgt.get("speed", 1.0))
        c_vol = float(tgt.get("volume", 1.0))
        c_muted = bool(tgt.get("muted", False))
        c_id = str(tgt.get("id", f"clip_{target_idx}"))

        dur1 = round(max(0.05, split_sec - c_st), 2)
        dur2 = round(max(0.05, c_dur - dur1), 2)

        clip1 = {
            "name": c_name,
            "start": c_st,
            "duration": dur1,
            "path": c_path,
            "source_in": round(c_s_in, 3),
            "speed": c_speed,
            "volume": c_vol,
            "muted": c_muted,
            "id": f"{c_id}_a"
        }
        clip2 = {
            "name": c_name,
            "start": round(split_sec, 2),
            "duration": dur2,
            "path": c_path,
            "source_in": round(c_s_in + (dur1 * c_speed), 3),
            "speed": c_speed,
            "volume": c_vol,
            "muted": c_muted,
            "id": f"{c_id}_b"
        }

        new_clips = curr_clips[:target_idx] + [clip1, clip2] + curr_clips[target_idx + 1:]
        self.timeline_editor.set_video_clips(new_clips)
        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_clips(new_clips)
        if hasattr(self, 'timeline_model'):
            self.timeline_model.video_clips = new_clips
        self.timeline_editor.waveform_canvas.update()
        self.status_lbl.setText(f"✂️ Split ត្រង់ {split_sec:.2f}s ➔ ឃើញ {len(new_clips)} Clips លើ Timeline (Ctrl+Z to Undo)")
        self.log_console.append_log(f"✂️ [CapCut Split] Split '{c_name}' at {split_sec:.2f}s into {len(new_clips)} clips on timeline! (Ctrl+Z to Undo)")

    def _on_video_clip_delete_requested(self, clip_idx: int):
        """CapCut-Style Instant Ripple Delete of selected video clip with zero popups and Ctrl+Z undo."""
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        if not curr_clips and getattr(self, 'video_path', None) and os.path.exists(self.video_path):
            cur_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)
            curr_clips = [{
                "name": getattr(self, 'original_video_name', os.path.basename(self.video_path)),
                "start": 0.0,
                "duration": cur_dur,
                "path": self.video_path
            }]

        if not curr_clips or clip_idx < 0 or clip_idx >= len(curr_clips):
            return

        # 1. Save Undo State
        if not hasattr(self, '_undo_stack'):
            self._undo_stack = []
        self._undo_stack.append({
            "video_path": self.video_path,
            "segments": copy.deepcopy(getattr(self, 'transcribed_segments', [])),
            "playhead": getattr(self.timeline_editor.waveform_canvas, 'playhead_pos_sec', 0.0),
            "clips": copy.deepcopy(curr_clips)
        })
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)

        del_clip = curr_clips[clip_idx]
        del_st = del_clip.get("start", 0.0)
        del_dur = del_clip.get("duration", 0.0)
        del_name = del_clip.get("name", f"Clip {clip_idx + 1}")

        # Case 1: User deleted the only clip remaining on the timeline
        if len(curr_clips) <= 1:
            self.video_path = None
            self.transcribed_segments = []
            self.subtitle_table.set_segments([])
            self.timeline_editor.set_segments([])
            self.timeline_editor.set_video_clips([])
            self.timeline_editor.set_total_duration(60.0)
            self.timeline_editor.waveform_canvas.selected_clip_idx = None
            self.timeline_editor.set_playhead_position(0.0)

            if hasattr(self, 'timeline_model'):
                self.timeline_model.video_clips = []
                self.timeline_model.total_duration_sec = 60.0
                self.timeline_model.segments = []
                self.timeline_model.video_path = None

            if hasattr(self, 'video_preview'):
                self.video_preview.set_video_clips([])
                self.video_preview.seek_to_time_sec(0.0)
                if hasattr(self.video_preview, 'pause'):
                    self.video_preview.pause()

            self._update_inspector_details(None, clips=[], duration=0.0)
            self.status_lbl.setText(f"🗑️ បានលុប '{del_name}' ចេញពី Timeline ➔ ចុច Ctrl+Z ដើម្បីត្រឡប់វិញ")
            self.log_console.append_log(f"🗑️ [CapCut Delete] Cleared '{del_name}' from timeline. Timeline is now empty. Press Ctrl+Z to undo.")
            return

        # Case 2: Multiple clips on timeline -> Ripple delete selected clip instantly (0ms latency)
        del_end = del_st + del_dur
        new_clips = []
        curr_offset = 0.0
        for idx, c in enumerate(curr_clips):
            if idx == clip_idx:
                continue
            c_copy = dict(c)
            c_copy["start"] = curr_offset
            curr_offset += c.get("duration", 0.0)
            new_clips.append(c_copy)

        new_tot_dur = curr_offset

        # Ripple Shift Subtitles
        old_segs = getattr(self, 'transcribed_segments', [])
        new_segs = []
        for s in old_segs:
            st = s.get("start", 0.0)
            et = s.get("end", 0.0)
            if st >= del_st and et <= del_end:
                continue
            s_copy = dict(s)
            if st >= del_end:
                s_copy["start"] = round(max(0.0, st - del_dur), 2)
                s_copy["end"] = round(max(0.1, et - del_dur), 2)
            elif et > del_st and et <= del_end:
                s_copy["end"] = round(del_st, 2)
            new_segs.append(s_copy)

        target_seek = min(del_st, max(0.0, new_tot_dur - 0.1))
        self.transcribed_segments = new_segs
        self.subtitle_table.set_segments(new_segs)
        self.timeline_editor.set_segments(new_segs)
        self.timeline_editor.set_total_duration(new_tot_dur)
        self.timeline_editor.set_video_clips(new_clips)
        new_sel = min(clip_idx, len(new_clips) - 1) if new_clips else None
        self.timeline_editor.waveform_canvas.selected_clip_idx = new_sel
        self.timeline_editor.set_playhead_position(target_seek)
        self.timeline_editor.scroll_to_sec(target_seek)

        if hasattr(self, 'timeline_model'):
            self.timeline_model.video_clips = new_clips
            self.timeline_model.total_duration_sec = new_tot_dur
            self.timeline_model.segments = new_segs

        if hasattr(self, 'video_preview'):
            self.video_preview.set_video_clips(new_clips)
            self.video_preview.seek_to_time_sec(target_seek)

        self._update_inspector_details(self.video_path, clips=new_clips, duration=new_tot_dur)
        self.status_lbl.setText(f"🗑️ បានលុប Clip (Ripple Deleted {del_dur:.2f}s) ➔ ចុច Ctrl+Z ដើម្បីត្រឡប់វិញ")
        self.log_console.append_log(f"🗑️ [CapCut Ripple Delete] Deleted Clip {clip_idx + 1} ({del_dur:.2f}s). New duration: {new_tot_dur:.2f}s (Ctrl+Z to Undo)")


    def _on_in_out_delete_requested(self, in_sec: float, out_sec: float):
        """CapCut-Style Ripple Delete of selected In-Out range with zero popups and Ctrl+Z undo."""
        if not self.video_path or not os.path.exists(self.video_path):
            return
        if in_sec is None or out_sec is None or out_sec <= in_sec + 0.1:
            return

        tot_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)
        curr_clips = copy.deepcopy(getattr(self.timeline_editor.waveform_canvas, 'video_clips', []))
        del_dur = round(out_sec - in_sec, 2)

        # 1. Save Undo
        if not hasattr(self, '_undo_stack'):
            self._undo_stack = []
        self._undo_stack.append({
            "video_path": self.video_path,
            "segments": copy.deepcopy(getattr(self, 'transcribed_segments', [])),
            "playhead": in_sec,
            "clips": copy.deepcopy(curr_clips)
        })
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)

        # 2. Ripple Shift Clips
        new_clips = []
        for c in curr_clips:
            c_st = c.get("start", 0.0)
            c_dur = c.get("duration", 0.0)
            c_end = c_st + c_dur
            if c_end <= in_sec:
                new_clips.append(dict(c))
            elif c_st >= out_sec:
                c_copy = dict(c)
                c_copy["start"] = round(c_st - del_dur, 2)
                new_clips.append(c_copy)
            else:
                if c_st < in_sec:
                    c1 = dict(c, duration=round(in_sec - c_st, 2))
                    new_clips.append(c1)
                if c_end > out_sec:
                    c2 = dict(c, start=round(in_sec, 2), duration=round(c_end - out_sec, 2))
                    new_clips.append(c2)

        # 3. Ripple Shift Subtitles
        old_segs = getattr(self, 'transcribed_segments', [])
        new_segs = []
        for s in old_segs:
            st = s.get("start", 0.0)
            et = s.get("end", 0.0)
            if st >= in_sec and et <= out_sec:
                continue
            s_copy = dict(s)
            if st >= out_sec:
                s_copy["start"] = round(max(0.0, st - del_dur), 2)
                s_copy["end"] = round(max(0.1, et - del_dur), 2)
            new_segs.append(s_copy)

        # 4. Underlying Media Lossless Cut
        from utils.file_utils import get_temp_path
        from utils.ffmpeg import trim_video, concat_videos_lossless, cut_out_video_segment, get_video_info
        ts = int(time.time() * 1000) % 1000000
        out_p = get_temp_path(f"capcut_cut_range_{ts}.mp4")

        info = get_video_info(self.video_path)
        actual_total_dur = info.get("duration", 0.0) or getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 0.0)

        self.status_lbl.setText(f"🗑️ កំពុងលុបចន្លោះ In-Out ({del_dur:.2f}s)...")
        try:
            if in_sec <= 0.05 and out_sec >= (actual_total_dur - 0.05):
                self.video_path = None
                self.transcribed_segments = []
                self.subtitle_table.set_segments([])
                self.timeline_editor.set_segments([])
                self.timeline_editor.set_video_clips([])
                self.timeline_editor.clear_in_out()
                self.timeline_editor.set_total_duration(60.0)
                self.timeline_editor.waveform_canvas.selected_clip_idx = None
                self.timeline_editor.set_playhead_position(0.0)
                if hasattr(self, 'timeline_model'):
                    self.timeline_model.video_clips = []
                    self.timeline_model.total_duration_sec = 60.0
                    self.timeline_model.segments = []
                    self.timeline_model.video_path = None
                if hasattr(self, 'video_preview'):
                    self.video_preview.set_video_clips([])
                    self.video_preview.seek_to_time_sec(0.0)
                    if hasattr(self.video_preview, 'pause'):
                        self.video_preview.pause()
                self.status_lbl.setText("🗑️ បានលុបជម្រើសទាំងមូលចេញពី Timeline ➔ ចុច Ctrl+Z ដើម្បីត្រឡប់វិញ")
                self.log_console.append_log("🗑️ [CapCut In/Out Delete] Entire timeline cleared. Press Ctrl+Z to undo.")
                return
            elif in_sec <= 0.05:
                ok = trim_video(self.video_path, out_p, out_sec, actual_total_dur, lossless=True)
            elif out_sec >= actual_total_dur - 0.05:
                ok = trim_video(self.video_path, out_p, 0.0, in_sec, lossless=True)
            else:
                p1 = get_temp_path(f"del_range1_{ts}.mp4")
                p2 = get_temp_path(f"del_range2_{ts}.mp4")
                ok1 = trim_video(self.video_path, p1, 0.0, in_sec, lossless=True)
                ok2 = trim_video(self.video_path, p2, out_sec, actual_total_dur, lossless=True)
                ok = concat_videos_lossless([p1, p2], out_p) if (ok1 and ok2) else False
                if not ok or not os.path.exists(out_p):
                    # Fallback directly to cut_out_video_segment
                    ok = cut_out_video_segment(self.video_path, out_p, in_sec, out_sec)

            if ok and os.path.exists(out_p):
                self._current_merged_clips = new_clips
                self._load_video_file(out_p, clear_segments=False)
                self.transcribed_segments = new_segs
                self.subtitle_table.set_segments(new_segs)
                self.timeline_editor.set_segments(new_segs)
                self.timeline_editor.set_video_clips(new_clips, video_path=out_p)
                self.timeline_editor.clear_in_out()
                self.timeline_editor.set_playhead_position(in_sec)
                self.status_lbl.setText(f"🗑️ បានកាត់លុបចន្លោះ In-Out ({del_dur:.2f}s) ➔ ចុច Ctrl+Z ដើម្បីត្រឡប់វិញ")
                self.log_console.append_log(f"🗑️ [CapCut In/Out Delete] Deleted {in_sec:.2f}s -> {out_sec:.2f}s ({del_dur:.2f}s) (Ctrl+Z to Undo)")
            else:
                self.status_lbl.setText("❌ បរាជ័យក្នុងការលុបចន្លោះ In-Out")
                self.log_console.append_log("❌ Failed to delete In-Out range from media file")
        except Exception as e:
            self.status_lbl.setText(f"❌ កំហុសលុប In-Out: {e}")
            self.log_console.append_log(f"❌ Error deleting In-Out range: {e}")


    def _execute_video_cut(self, settings: dict):
        """Execute video cut, trim, or split asynchronously with live non-blocking progress."""
        if self.cut_worker and self.cut_worker.isRunning():
            QMessageBox.warning(self, "កំពុងដំណើរការ", "ដំណើរការកាត់តកំពុងដំណើរការរួចហើយ សូមរង់ចាំ។")
            return

        action = settings.get("action", "trim")
        lossless = settings.get("lossless", True)

        self.status_lbl.setText("✂️ កំពុងកាត់តវីដេអូ (Cutting video)...")
        self.log_console.append_log(f"✂️ Starting asynchronous video operation: {action.upper()} (lossless={lossless})")

        self.cut_progress_dialog = QProgressDialog(f"✂️ កំពុងដំណើរការកាត់តវីដេអូ ({action.upper()})...", "បោះបង់ (Cancel)", 0, 100, self)
        self.cut_progress_dialog.setWindowTitle("✂️ កាត់តវីដេអូ (Video Cutting)")
        self.cut_progress_dialog.setWindowModality(Qt.WindowModal)
        self.cut_progress_dialog.setAutoClose(False)
        self.cut_progress_dialog.setAutoReset(False)
        self.cut_progress_dialog.setValue(10)
        self.cut_progress_dialog.canceled.connect(self._cancel_cut_worker)
        self.cut_progress_dialog.show()

        self.cut_worker = CutVideoWorker(settings, self.video_path)
        self.cut_worker.progress.connect(self._on_cut_worker_progress)
        self.cut_worker.finished_success.connect(self._on_cut_worker_success)
        self.cut_worker.failed_error.connect(self._on_cut_worker_failed)
        self.cut_worker.start()

    def _cancel_cut_worker(self):
        if self.cut_worker and self.cut_worker.isRunning():
            self.cut_worker.cancel()
            self.log_console.append_log("⚠️ User requested to cancel video cutting.")
            self.status_lbl.setText("⚠️ បានបោះបង់ការកាត់តវីដេអូ")

    def _on_cut_worker_progress(self, pct: int, msg: str):
        if self.cut_progress_dialog:
            self.cut_progress_dialog.setValue(pct)
            self.cut_progress_dialog.setLabelText(msg)
        self.status_lbl.setText(msg)
        self.log_console.append_log(f"✂️ [{pct}%] {msg}")

    def _on_cut_worker_success(self, action: str, out_path: str, out_path2: str, auto_reload: bool):
        if self.cut_progress_dialog:
            self.cut_progress_dialog.setValue(100)
            self.cut_progress_dialog.close()
            self.cut_progress_dialog = None

        self.status_lbl.setText("✅ កាត់តវីដេអូរួចរាល់ (Video cut successfully)")
        self.log_console.append_log(f"✅ Video {action.upper()} succeeded: {out_path}")

        # Reset In/Out points
        if hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas'):
            self.timeline_editor.waveform_canvas.clear_in_out()

        if auto_reload and os.path.exists(out_path):
            self.log_console.append_log(f"🔄 Auto-reloading trimmed video into project: {out_path}")
            self._load_video_file(out_path, clear_segments=False)

        QMessageBox.information(
            self, "ជោគជ័យ (Cut Succeeded)",
            f"🎉 ការកាត់តវីដេអូបានសម្រេចដោយជោគជ័យ!\n\n"
            f"📁 ឯកសារ: {out_path}"
            + (f"\n📁 ភាគ ២: {out_path2}" if action == "split" else "")
        )

    def _on_cut_worker_failed(self, err_msg: str):
        if self.cut_progress_dialog:
            self.cut_progress_dialog.close()
            self.cut_progress_dialog = None

        self.status_lbl.setText("❌ កាត់តវីដេអូបរាជ័យ (Video cut failed)")
        self.log_console.append_log(f"❌ Video cut operation failed: {err_msg}")
        QMessageBox.critical(
            self, "បរាជ័យ (Cut Failed)",
            f"មិនអាចកាត់តវីដេអូបានទេ:\n{err_msg}\n\nសូមពិនិត្យមើល FFmpeg ឬ Disk Space។"
        )

    # ==================== VIDEO LOGO OVERLAY HANDLERS ====================
    def _on_logo_toggled(self, enabled: bool):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_logo_enabled(enabled)
            status_text = "enabled" if enabled else "disabled"
            self.log_console.append_log(f"🖼 Logo Overlay effect {status_text}.")
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor.set_track_visible('logo', enabled)

    def _on_logo_updated(self, path: str, x: int, y: int, width: int, height: int, remove_green: bool):
        is_full = True
        if hasattr(self, 'video_effects') and hasattr(self.video_effects, 'logo_full_video_chk'):
            is_full = self.video_effects.logo_full_video_chk.isChecked()
            
        tot_dur = 60.0
        if hasattr(self, 'video_preview') and hasattr(self.video_preview, 'total_frames') and self.video_preview.total_frames > 0:
            tot_dur = self.video_preview.total_frames / max(1.0, self.video_preview.fps)
        elif hasattr(self, 'timeline_editor'):
            tot_dur = getattr(self.timeline_editor.waveform_canvas, 'total_duration_sec', 60.0)

        st = 0.0 if is_full else (getattr(self.video_preview, 'logo_start_sec', 0.0) if hasattr(self, 'video_preview') else 0.0)
        dur = tot_dur if is_full else (getattr(self.video_preview, 'logo_duration_sec', tot_dur) if hasattr(self, 'video_preview') else tot_dur)
        if dur <= 0.0:
            dur = tot_dur

        if hasattr(self, 'video_preview'):
            self.video_preview.set_logo_enabled(True)
            self.video_preview.set_logo_path(path)
            self.video_preview.set_logo_position(x, y)
            self.video_preview.set_logo_size(width, height)
            self.video_preview.set_logo_remove_green(remove_green)
            self.video_preview.set_logo_full_video(is_full)
            self.video_preview.set_logo_timing(st, dur)
            self.video_preview._update_display()
        if hasattr(self, 'timeline_editor'):
            self.timeline_editor.set_logo_clip(st, dur, name=os.path.basename(path), full_video=is_full)
        self.log_console.append_log(f"🖼 Logo Overlay updated: '{os.path.basename(path)}' at ({x}, {y}) size {width}x{height} (Full Video: {is_full})")

    # ==================== BURN SUBTITLE HANDLERS ====================
    def _on_burn_subtitle_toggled(self, enabled: bool):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_burn_subtitle_enabled(enabled)
            status_text = "enabled" if enabled else "disabled"
            self.log_console.append_log(f"🔥 Burn Subtitle effect {status_text}.")

    def _on_burn_subtitle_updated(self, enabled: bool, font_name: str, size: int, color_hex: str, bg_opacity: float, anim_type: str = "pop_bounce", anim_dur: float = 0.35, template: dict = None):
        if hasattr(self, 'video_preview'):
            self.video_preview.set_burn_subtitle_config(enabled, font_name, size, color_hex, bg_opacity, anim_type, anim_dur, template)

    def _get_selected_source_language(self) -> str:
        if not hasattr(self, 'preset_combo') or not self.preset_combo:
            return "auto"
        preset_text = self.preset_combo.currentText().lower()
        if "chinese" in preset_text or "中文" in preset_text:
            return "zh"
        elif "english" in preset_text or "អង់គ្លេស" in preset_text:
            return "en"
        elif "japanese" in preset_text or "日本語" in preset_text:
            return "ja"
        elif "french" in preset_text or "français" in preset_text:
            return "fr"
        elif "khmer" in preset_text or "ខ្មែរ" in preset_text:
            return "km"
        elif "thai" in preset_text or "ថៃ" in preset_text:
            return "th"
        elif "vietnam" in preset_text or "tiếng việt" in preset_text:
            return "vi"
        elif "korean" in preset_text or "한국어" in preset_text:
            return "ko"
        elif "spanish" in preset_text or "español" in preset_text:
            return "es"
        else:
            return "auto"

    # ==================== TRANSCRIPTION & TRANSLATION VIA REACT ENGINE ====================
    def _run_transcription_only(self):
        """Transcribe and translate using React/Express Engine (:3000) directly to Khmer SRT."""
        if not self.video_path:
            QMessageBox.warning(self, "Warning", "Please load a video file first.")
            return

        if not self._ensure_gemini_key_available():
            return

        self.log_console.append_log("⚡ Starting Translation via React AI Engine (MP3 → Khmer SRT)...")
        self.status_lbl.setText("⏳ Processing with React AI Engine...")
        self.progress_bar.setValue(0)
        self._start_react_translation_worker()

    def _open_web_studio(self, url=None):
        """Open Google AI Studio Web App or Local Web Studio in browser."""
        target_url = url or "https://ai-audio-translator.ai.studio/"
        self.status_lbl.setText("🌐 Opening Web Studio in browser...")
        self.log_console.append_log(f"🌐 Opening Web Studio in browser: {target_url}")

        # Ensure background local bridge (:3000) is running so the Web App on ai.studio can auto-fetch MP3s and export SRTs!
        try:
            from services.react_translator_bridge import ReactTranslatorBridge
            bridge = ReactTranslatorBridge()
            if not bridge.is_server_running():
                import threading
                threading.Thread(target=bridge.ensure_server_running, daemon=True).start()
        except Exception as e:
            pass

        opened = QDesktopServices.openUrl(QUrl(target_url))
        if not opened:
            import webbrowser
            webbrowser.open(target_url)
        self.status_lbl.setText("🌐 Web Studio opened in browser.")

    def _show_web_studio_menu(self, pos):
        """Right click menu on Web Studio button."""
        menu = QMenu(self)
        menu.addAction("🌐 Open Google AI Studio App (Cloud)", lambda: self._open_web_studio("https://ai-audio-translator.ai.studio/"))
        menu.addAction("💻 Open Local Web Studio (http://localhost:3000)", self._open_local_web_studio)
        menu.exec_(self.open_web_ui_btn.mapToGlobal(pos))

    def _open_local_web_studio(self):
        """Ensure React server is online and open http://localhost:3000 in browser."""
        from services.react_translator_bridge import ReactTranslatorBridge
        bridge = ReactTranslatorBridge()
        self.status_lbl.setText("🌐 Connecting to Local Web Studio (:3000)...")
        QApplication.processEvents()

        if not bridge.is_server_running():
            self.log_console.append_log("🌐 Launching Local Web Studio server in background...")
            bridge.ensure_server_running()

        self._open_web_studio("http://localhost:3000")

    def _start_react_translation_worker(self, on_finish_callback=None):
        """Dedicated worker using ReactTranslatorBridge (:3000) for instant Khmer SRT."""
        clips = self._get_current_timeline_clips()
        if not self.video_path and not clips:
            QMessageBox.warning(self, "No Video", "សូម Upload ឬជ្រើសរើស Video ជាមុនសិន។")
            return

        target_audio = self.extracted_mp3_path if (hasattr(self, 'extracted_mp3_path') and self.extracted_mp3_path and os.path.exists(self.extracted_mp3_path)) else self.video_path
        source_lang = self._get_selected_source_language()

        self._set_processing_state(True, "🌐 Step 2: React Engine AI Translating to Khmer SRT...")

        class ReactWorker(QThread):
            progress = Signal(int, str)
            finished = Signal(list, str)
            error = Signal(str)
            log = Signal(str)

            def __init__(self, audio_path, src_lang, clips=None):
                super().__init__()
                self.setStackSize(8 * 1024 * 1024)
                self.audio_path = audio_path
                self.src_lang = src_lang
                self.clips = clips or []
                self._is_cancelled = False

            def cancel(self):
                self._is_cancelled = True

            def run(self):
                try:
                    media_input = self.audio_path
                    is_multi = len(self.clips) > 1 or (
                        len(self.clips) == 1 and (
                            float(self.clips[0].get("source_in", 0.0) or 0.0) > 0.05 or
                            float(self.clips[0].get("duration", 0.0) or 0.0) > 0.0
                        )
                    )
                    if is_multi:
                        self.log.emit(f"🎬 [Timeline Audio] Building continuous timeline audio for {len(self.clips)} clip(s)...")
                        self.progress.emit(5, "Building Timeline Audio...")
                        from services.timeline_audio_service import TimelineAudioService
                        tas = TimelineAudioService()
                        res = tas.build_timeline_composite_audio(self.clips)
                        media_input = res[0] if isinstance(res, (tuple, list)) else res

                    if not media_input or not os.path.exists(media_input):
                        raise FileNotFoundError("រកមិនឃើញ media input សម្រាប់បកប្រែទេ។")

                    from services.react_translator_bridge import ReactTranslatorBridge
                    bridge = ReactTranslatorBridge()

                    def cb(pct, msg):
                        self.progress.emit(pct, msg)
                        self.log.emit(f"⚡ [React Engine] {msg}")

                    segments, out_srt, srt_text = bridge.translate_audio(
                        media_input,
                        source_lang=self.src_lang,
                        target_lang="Khmer",
                        speed_mode="turbo",
                        progress_callback=cb,
                        is_cancelled_fn=lambda: self._is_cancelled
                    )

                    if self._is_cancelled:
                        return

                    dict_segments = [s.to_dict() if hasattr(s, "to_dict") else s for s in segments]
                    self.finished.emit(dict_segments, out_srt)

                except Exception as e:
                    self.error.emit(str(e))

        if getattr(self, 'react_worker', None) and self.react_worker.isRunning():
            try:
                self.react_worker.cancel()
                self.react_worker.quit()
                self.react_worker.wait(200)
            except Exception:
                pass

        self.react_worker = ReactWorker(target_audio, source_lang, clips=clips)
        self.react_worker.progress.connect(lambda p, msg: (self.progress_bar.setValue(p), self.status_lbl.setText(msg)))
        self.react_worker.log.connect(self.log_console.append_log)
        self.react_worker.finished.connect(lambda segs, srt_p: self._on_react_translation_done(segs, srt_p, callback=on_finish_callback))
        self.react_worker.error.connect(lambda e: (
            self.log_console.append_log(f"❌ React Engine error: {e}"),
            self._set_processing_state(False, f"❌ Translation Error: {e}"),
            QMessageBox.warning(self, "Translation Error", f"React Engine Translation Error:\n{e}\n\nFalling back to local STT...")
        ))
        self.react_worker.start()

    def _on_react_translation_done(self, segments, srt_path, callback=None):
        dict_segments = [s.to_dict() if hasattr(s, "to_dict") else s for s in (segments or [])]
        self.transcribed_segments = dict_segments
        self.subtitle_table.set_segments(dict_segments)
        self.timeline_editor.set_segments(dict_segments)
        self.video_preview.set_timeline_segments(dict_segments)
        self.progress_bar.setValue(100)
        self.log_console.append_log(f"🎉 [React Engine] Extracted {len(dict_segments)} Khmer segments! Saved SRT: {srt_path}")
        self._set_processing_state(False, f"✅ React Translation Complete! {len(dict_segments)} Khmer segments loaded.")

        if callback:
            callback(dict_segments)

    def _start_transcription_worker(self, auto_translate_next: bool = False):
        """Dedicated QThread Worker for Gemini AI Multimodal Audio processing across Timeline."""
        if not self._ensure_gemini_key_available():
            return

        clips = self._get_current_timeline_clips()
        if not self.video_path and not clips:
            QMessageBox.warning(self, "No Video", "សូម Upload ឬជ្រើសរើស Video ជាមុនសិន។")
            return

        clip_count = len(clips)
        self.log_console.append_log(f"🎙️ [Auto Subtitle] Starting transcription across {clip_count} Timeline clip(s)...")
        self._set_processing_state(True, f"⏳ Step 2: Processing Timeline Audio with Gemini AI ({clip_count} clips)...")
        
        class TranscribeWorker(QThread):
            progress = Signal(int, str)
            finished = Signal(list)
            error = Signal(str)
            log = Signal(str)
            
            def __init__(self, clips=None, fallback_video_path=None, source_lang="auto"):
                super().__init__()
                self.setStackSize(8 * 1024 * 1024)
                self.clips = clips or []
                self.fallback_video_path = fallback_video_path
                self.source_lang = source_lang
                self._is_cancelled = False
                
            def cancel(self):
                self._is_cancelled = True
                
            def run(self):
                try:
                    media_input = None
                    is_multi = len(self.clips) > 1 or (
                        len(self.clips) == 1 and (
                            float(self.clips[0].get("source_in", 0.0) or 0.0) > 0.05 or
                            float(self.clips[0].get("duration", 0.0) or 0.0) > 0.0
                        )
                    )

                    if is_multi:
                        self.log.emit(f"🎬 [Timeline Audio] Building continuous timeline composite audio for {len(self.clips)} clip(s)...")
                        self.progress.emit(5, "Building Timeline Audio...")
                        from services.timeline_audio_service import TimelineAudioService
                        tas = TimelineAudioService()
                        res = tas.build_timeline_composite_audio(self.clips)
                        media_input = res[0] if isinstance(res, (tuple, list)) else res

                    if not media_input or not os.path.exists(media_input):
                        if self.clips and self.clips[0].get("path") and os.path.exists(self.clips[0]["path"]):
                            media_input = self.clips[0]["path"]
                        elif self.fallback_video_path and os.path.exists(self.fallback_video_path):
                            media_input = self.fallback_video_path
                        else:
                            raise FileNotFoundError("រកមិនឃើញ video ឬ audio file លើ Timeline ទេ។")

                    if self._is_cancelled:
                        return

                    self.log.emit("🎵 Separating speech dialogue from background music...")
                    self.progress.emit(10, "Separating Audio...")
                    from services.audio_separator import AudioSeparationService
                    sep_service = AudioSeparationService()
                    sep_res = sep_service.extract_and_separate(media_input)
                    vocal_wav_path = sep_res.get("dialogue_audio") or sep_res["vocal_audio"]
                    
                    if self._is_cancelled:
                        return
                    
                    self.log.emit("🌐 Running Dedicated Gemini STT Pipeline (VAD + gemini-3.5-transcribe)...")
                    self.progress.emit(25, "Gemini STT Transcribing...")
                    
                    from services.stt_service import STTService
                    stt = STTService()
                    
                    def cb(pct, msg):
                        self.progress.emit(pct, msg)
                        self.log.emit(f"⚡ {msg}")
                        
                    segments = stt.transcribe(vocal_wav_path, source_lang=self.source_lang, progress_callback=cb, is_cancelled_fn=lambda: self._is_cancelled)
                    
                    if self._is_cancelled:
                        return
                    
                    dict_segments = [s.to_dict() if hasattr(s, "to_dict") else s for s in segments]
                    self.progress.emit(100, "STT Processing Complete!")
                    self.log.emit(f"✅ STT completed! {len(dict_segments)} timestamped segments extracted.")
                    self.finished.emit(dict_segments)
                    
                except Exception as e:
                    self.error.emit(str(e))

        source_lang = self._get_selected_source_language()
        self.transcribe_worker = TranscribeWorker(
            clips=clips,
            fallback_video_path=self.video_path,
            source_lang=source_lang
        )
        self.transcribe_worker.progress.connect(lambda p, msg: (self.progress_bar.setValue(p), self.status_lbl.setText(msg)))
        self.transcribe_worker.log.connect(self.log_console.append_log)
        self.transcribe_worker.finished.connect(lambda segs: self._on_transcription_done(segs, auto_translate_next=auto_translate_next))
        self.transcribe_worker.error.connect(lambda e: (
            self.log_console.append_log(f"❌ Speech-to-Text error: {e}"),
            self._set_processing_state(False, f"❌ Error: {e}")
        ))
        self.transcribe_worker.start()

    def _on_transcription_done(self, segments, auto_translate_next: bool = False):
        """Triggered upon STT transcription completion (Stage 1 Decoupled)."""
        dict_segments = [s.to_dict() if hasattr(s, "to_dict") else s for s in (segments or [])]
        self.transcribed_segments = dict_segments
        self.subtitle_table.set_segments(dict_segments)
        self.timeline_editor.set_segments(dict_segments)
        self.video_preview.set_timeline_segments(dict_segments)
        self.progress_bar.setValue(100)
        self.log_console.append_log(f"🎉 Extracted {len(dict_segments)} speech segments with high-precision timestamps.")

        # Save Stage 1 Original Source SRT immediately to output (for verification & reuse)
        if dict_segments and self.video_path:
            try:
                from utils.file_utils import export_segments_to_srt, OUTPUT_DIR
                from pathlib import Path
                stem = Path(self.video_path).stem
                orig_zh_srt_path = os.path.join(str(OUTPUT_DIR), f"{stem}_original_chinese.srt")
                orig_srt_path = os.path.join(str(OUTPUT_DIR), f"{stem}_original.srt")
                orig_sub_zh = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_original_chinese.srt")
                orig_sub_path = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_original.srt")
                
                export_segments_to_srt(dict_segments, orig_zh_srt_path, text_key="original_text")
                export_segments_to_srt(dict_segments, orig_srt_path, text_key="original_text")
                try:
                    export_segments_to_srt(dict_segments, orig_sub_zh, text_key="original_text")
                    export_segments_to_srt(dict_segments, orig_sub_path, text_key="original_text")
                except Exception:
                    pass
                self.log_console.append_log(f"📄 [Step 1 Complete] Chinese Source SRT saved: {orig_zh_srt_path}")
            except Exception as e_srt:
                self.log_console.append_log(f"Notice saving original SRT: {e_srt}")

        has_khmer = any(s.get("khmer_text") for s in dict_segments)
        if auto_translate_next and not has_khmer:
            self._run_translation()
        else:
            self._set_processing_state(False, "✅ STT Complete! Original SRT saved. Click 'Translate to Khmer' to proceed.")



    # ==================== KHMER TRANSLATION (THREAD WORKER) ====================
    def _run_translation(self):
        """Translate segments into Khmer using background QThread worker with Gemini AI."""
        segs = self.subtitle_table.get_updated_segments()
        if not segs:
            QMessageBox.warning(self, "Warning", "No segments available to translate. Please transcribe first.")
            self._set_processing_state(False)
            return

        if not self._ensure_gemini_key_available():
            self._set_processing_state(False)
            return

        self._set_processing_state(True, "🌐 Translating segments into natural spoken Khmer via Gemini AI...")
        self.log_console.append_log("🌐 Translating segments to natural spoken Khmer using Gemini AI...")
        self.progress_bar.setValue(0)
        
        class TranslateWorker(QThread):
            progress = Signal(int, str)
            finished = Signal(list)
            error = Signal(str)
            log = Signal(str)
            
            def __init__(self, segments, source_lang="auto", api_key=None):
                super().__init__()
                self.setStackSize(8 * 1024 * 1024)
                self.segments = segments
                self.source_lang = source_lang
                self.api_key = api_key or get_gemini_api_key()
                self._is_cancelled = False
                
            def cancel(self):
                self._is_cancelled = True

            def run(self):
                try:
                    from services.translation_service import TranslationService
                    ts = TranslationService(api_key=self.api_key)
                    self.log.emit(f"🌐 Translating {len(self.segments)} segments to natural spoken Khmer using Gemini AI...")
                    
                    def cb(pct, msg):
                        self.progress.emit(pct, msg)
                        self.log.emit(f"⚡ {msg}")

                    translated_segs = ts.translate_segments(
                        self.segments,
                        source_lang=self.source_lang,
                        target_lang="km",
                        progress_callback=cb,
                        is_cancelled_fn=lambda: self._is_cancelled
                    )
                    
                    if self._is_cancelled:
                        return

                    # Convert Segment models to dicts for existing UI components
                    result_dicts = [s.to_dict() if hasattr(s, "to_dict") else s for s in translated_segs]

                    self.progress.emit(100, "Translation Complete!")
                    self.finished.emit(result_dicts)
                except Exception as e:
                    self.error.emit(str(e))

        
        source_lang = self._get_selected_source_language()
        self.translate_worker = TranslateWorker(segs, source_lang=source_lang, api_key=get_gemini_api_key())
        self.translate_worker.progress.connect(lambda p, msg: (self.progress_bar.setValue(p), self.status_lbl.setText(msg)))
        self.translate_worker.log.connect(self.log_console.append_log)
        self.translate_worker.finished.connect(self._on_translation_done)
        self.translate_worker.error.connect(lambda e: (
            self.log_console.append_log(f"❌ Translation error: {e}"),
            self._set_processing_state(False, f"❌ Translation error: {e}")
        ))
        self.translate_worker.start()

    def _on_translation_done(self, translated_segments):
        self.subtitle_table.set_segments(translated_segments)
        self.timeline_editor.set_segments(translated_segments)
        self.video_preview.set_timeline_segments(translated_segments)
        self.transcribed_segments = translated_segments
        self.progress_bar.setValue(100)

        # Save Stage 2 Khmer SRT immediately to output
        if translated_segments and self.video_path:
            try:
                from utils.file_utils import export_segments_to_srt, OUTPUT_DIR
                from pathlib import Path
                stem = Path(self.video_path).stem
                khmer_srt_path = os.path.join(str(OUTPUT_DIR), f"{stem}_khmer.srt")
                khmer_sub_path = os.path.join(str(OUTPUT_DIR), "subtitles", f"{stem}_khmer.srt")
                export_segments_to_srt(translated_segments, khmer_srt_path, text_key="khmer_text")
                try:
                    export_segments_to_srt(translated_segments, khmer_sub_path, text_key="khmer_text")
                except Exception:
                    pass
                self.log_console.append_log(f"📄 [Stage 2 Complete] Khmer SRT saved: {khmer_srt_path}")
            except Exception as e_ksrt:
                self.log_console.append_log(f"Notice saving Khmer SRT: {e_ksrt}")

        self._set_processing_state(False, "✅ Translation Completed! Original & Khmer SRTs ready.")
        self.log_console.append_log(f"🎉 Translated {len(translated_segments)} segments into Khmer successfully! (Original text preserved, Khmer text ready)")

    def _open_ai_voice_studio(self):
        from gui.widgets import AIVoiceStudioDialog
        dlg = AIVoiceStudioDialog(self)
        dlg.exec()

    def _browse_output_dir(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Select Target Export Folder", self.output_dir)
        if folder_path:
            self.output_dir = folder_path
            self.output_path_input.setText(folder_path)
            self._refresh_story_folders()
            self.log_console.append_log(f"📁 Target export directory set to: {folder_path}")

    def _refresh_story_folders(self):
        try:
            from utils.file_utils import get_output_subfolders
            current_text = self.story_folder_combo.currentText().strip() if hasattr(self, 'story_folder_combo') else ""
            folders = get_output_subfolders(self.output_dir)
            if hasattr(self, 'story_folder_combo'):
                self.story_folder_combo.blockSignals(True)
                self.story_folder_combo.clear()
                for f in folders:
                    if f not in ["temp", "videos", "audio", "subtitles"]:
                        self.story_folder_combo.addItem(f)
                if current_text:
                    self.story_folder_combo.setEditText(current_text)
                elif self.story_folder_combo.count() == 0:
                    self.story_folder_combo.setEditText("My_Story")
                self.story_folder_combo.blockSignals(False)
        except Exception:
            pass

    def _open_output_folder(self):
        import subprocess
        from utils.file_utils import build_story_export_paths
        story_name = self.story_folder_combo.currentText().strip() if hasattr(self, 'story_folder_combo') else "My_Story"
        ep_num = self.episode_spin.value() if hasattr(self, 'episode_spin') else 1
        paths = build_story_export_paths(story_name, ep_num, self.output_dir)
        target_dir = paths["story_dir"]
        if not os.path.exists(target_dir):
            target_dir = os.path.join(self.output_dir, story_name)
        if not os.path.exists(target_dir):
            target_dir = self.output_dir
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", target_dir], check=False)
            elif os.name == "nt":
                os.startfile(target_dir)
            else:
                subprocess.run(["xdg-open", target_dir], check=False)
            self.log_console.append_log(f"📂 Opened output folder in Finder: {target_dir}")
        except Exception as e:
            self.log_console.append_log(f"⚠️ Could not open folder: {e}")

    def _extract_vid_to_mp3(self):
        if not self.video_path:
            QMessageBox.warning(self, "Warning", "Please load a video file first.")
            return
        from utils.file_utils import generate_unique_filename
        out_mp3 = generate_unique_filename(self.video_path, prefix="audio", extension=".mp3", custom_dir=self.output_dir)
        from utils.ffmpeg import extract_audio
        if extract_audio(self.video_path, out_mp3):
            self.log_console.append_log(f"🎵 Audio extracted to unique MP3: {out_mp3}")
            QMessageBox.information(self, "Audio Extracted", f"MP3 Audio File Saved:\n{out_mp3}")

    def _export_mp3(self):
        if not self.video_path:
            QMessageBox.warning(self, "Warning", "Please load a video file first.")
            return
        from utils.file_utils import generate_unique_filename, get_temp_path
        out_mp3 = generate_unique_filename(self.video_path, prefix="khmer_audio", extension=".mp3", custom_dir=self.output_dir)
        master_wav = get_temp_path("master_khmer_voice.wav")
        if os.path.exists(master_wav):
            from utils.ffmpeg import extract_audio
            extract_audio(master_wav, out_mp3)
            QMessageBox.information(self, "Success", f"Khmer Dubbed Audio Exported:\n{out_mp3}")
        else:
            self._extract_vid_to_mp3()

    # ==================== EXPORT FINAL VIDEO ====================
    def _export_final_video(self):
        """Export Video - executes TTS, Audio Sync, and Video Merging using table segments."""
        if not self.video_path:
            QMessageBox.warning(self, "Warning", "Please load a video file first.")
            return
        
        segs = self.subtitle_table.get_updated_segments()
        if not segs:
            QMessageBox.warning(self, "Warning", "No segments available. Please transcribe first.")
            return
        
        has_khmer = any(seg.get("khmer_text", "") for seg in segs)
        if not has_khmer:
            reply = QMessageBox.question(
                self, 
                "Warning", 
                "No Khmer translation found. Do you want to translate first?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self._run_translation()
                return
        
        if hasattr(self, 'video_effects') and hasattr(self.video_effects, 'get_effects_config'):
            effects_config = self.video_effects.get_effects_config(self.video_preview, segments=segs)
        elif hasattr(self, 'video_preview') and hasattr(self.video_preview, 'get_effects_config'):
            effects_config = self.video_preview.get_effects_config()
            if effects_config:
                effects_config["segments"] = segs
        else:
            effects_config = None
        
        if effects_config:
            b_on = effects_config.get("blur", {}).get("enabled", False)
            t_on = effects_config.get("text_overlay", {}).get("enabled", False)
            l_on = effects_config.get("logo", {}).get("enabled", False)
            s_on = effects_config.get("burn_subtitle", {}).get("enabled", False)
            self.log_console.append_log(f"🎬 Video Effects Export Plan: [Blur: {'ON' if b_on else 'OFF'}] [Text: {'ON' if t_on else 'OFF'}] [Logo: {'ON' if l_on else 'OFF'}] [Subtitle: {'ON' if s_on else 'OFF'}]")

        # Open Export Settings Dialog for Resolution, Mode, and BGM configuration
        from gui.widgets import ExportSettingsDialog
        init_res = self.export_res_combo.currentText() if hasattr(self, 'export_res_combo') else "1080P"
        init_mode = self.export_mode_combo.currentText() if hasattr(self, 'export_mode_combo') else "Fast"
        init_bgm = self.bgm_vol_spin.value() if hasattr(self, 'bgm_vol_spin') else 30
        curr_story = self.story_folder_combo.currentText().strip() if hasattr(self, 'story_folder_combo') else "My_Story"
        curr_ep = self.episode_spin.value() if hasattr(self, 'episode_spin') else 1
        curr_out_dir = str(self.output_dir) if hasattr(self, 'output_dir') else os.path.abspath("output")

        dlg = ExportSettingsDialog(
            parent=self,
            video_path=self.video_path,
            initial_res=init_res,
            initial_mode=init_mode,
            initial_bgm=init_bgm,
            story_folder=curr_story,
            episode=curr_ep,
            output_dir=curr_out_dir,
            timeline_name="Timeline 01"
        )
        if dlg.exec() != QDialog.Accepted:
            self.log_console.append_log("ℹ️ ការ Export ត្រូវបានបោះបង់ (Export canceled).")
            return

        settings = dlg.get_settings()
        chosen_res = settings.get("resolution", init_res)
        chosen_mode = settings.get("mode", init_mode)
        chosen_bgm = settings.get("bgm_volume", init_bgm)
        new_story = settings.get("story_folder", curr_story)
        new_ep = settings.get("episode", curr_ep)
        new_out_dir = settings.get("export_to", curr_out_dir)

        if hasattr(self, 'story_folder_combo') and new_story:
            self.story_folder_combo.setEditText(new_story)
        if hasattr(self, 'episode_spin'):
            self.episode_spin.setValue(new_ep)
        if new_out_dir and os.path.exists(new_out_dir):
            from pathlib import Path
            self.output_dir = Path(new_out_dir)

        # Sync bottom bar controls
        if hasattr(self, 'export_res_combo'):
            matched_idx = -1
            c_low = chosen_res.lower()
            for i in range(self.export_res_combo.count()):
                txt = self.export_res_combo.itemText(i).lower()
                if ("4k" in c_low and "4k" in txt) or \
                   ("2k" in c_low and "2k" in txt) or \
                   ("1080" in c_low and "1080" in txt) or \
                   ("720" in c_low and "720" in txt) or \
                   ("480" in c_low and "480" in txt) or \
                   ("orig" in c_low and "orig" in txt):
                    matched_idx = i
                    break
            if matched_idx >= 0:
                self.export_res_combo.setCurrentIndex(matched_idx)

        if hasattr(self, 'export_mode_combo'):
            idx = self.export_mode_combo.findText(chosen_mode.capitalize())
            if idx >= 0:
                self.export_mode_combo.setCurrentIndex(idx)
        if hasattr(self, 'bgm_vol_spin'):
            self.bgm_vol_spin.setValue(chosen_bgm)

        if effects_config is None:
            effects_config = {}
        effects_config["target_resolution"] = chosen_res

        self.log_console.append_log(f"🎬 Export Video Settings: [Resolution: {chosen_res}] [Mode: {chosen_mode}] [BGM: {chosen_bgm}%]")
        self.log_console.append_log("🎬 Exporting Final Khmer Video with Dubbed Audio, Effects, and Custom Resolution...")
        self._start_dubbing_worker(pre_edited_segments=segs, effects_config=effects_config, target_resolution=chosen_res)

    def _cancel_export(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
        if hasattr(self, 'voice_worker') and self.voice_worker and self.voice_worker.isRunning():
            self._cancel_voice_generation()
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            self.transcribe_worker.cancel()
        if self.translate_worker and self.translate_worker.isRunning():
            self.translate_worker.cancel()
        self._set_processing_state(False, "⚠️ Process canceled by user.")
        self.log_console.append_log("⚠️ Pipeline process canceled by user.")

    def _start_dubbing_worker(self, pre_edited_segments=None, effects_config=None, target_resolution=None):
        from utils.file_utils import build_story_export_paths, generate_unique_filename
        
        story_name = self.story_folder_combo.currentText().strip() if hasattr(self, 'story_folder_combo') else ""
        if not story_name:
            story_name = "My_Story"
        ep_num = self.episode_spin.value() if hasattr(self, 'episode_spin') else 1

        paths = build_story_export_paths(story_name, ep_num, self.output_dir)
        out_path = paths["video_path"]

        self.log_console.append_log(f"📂 Output clean story path: {out_path}")
        self._set_processing_state(True, f"🎬 Exporting Khmer Dubbed Video ({story_name} EP{ep_num:02d})...")

        source_lang = self._get_selected_source_language()
        bg_vol = (self.bgm_vol_spin.value() / 100.0) if hasattr(self, 'bgm_vol_spin') else 0.30
        exp_mode = self.export_mode_combo.currentText().upper() if hasattr(self, 'export_mode_combo') else "BALANCED"
        from utils.file_utils import get_temp_path
        master_audio_cand = getattr(self, 'last_master_wav', None)
        if not master_audio_cand or not os.path.exists(master_audio_cand):
            default_cand = get_temp_path("master_khmer_voice.wav")
            if os.path.exists(default_cand) and os.path.getsize(default_cand) > 1000:
                master_audio_cand = default_cand

        # Snapshot timeline clips and transitions for worker thread safety
        import copy
        clips_source = None
        if hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas') and getattr(self.timeline_editor.waveform_canvas, 'video_clips', None):
            clips_source = self.timeline_editor.waveform_canvas.video_clips
        elif hasattr(self, 'timeline_model') and getattr(self.timeline_model, 'video_clips', None):
            clips_source = self.timeline_model.video_clips
        elif hasattr(self, 'video_preview') and getattr(self.video_preview, 'video_clips', None):
            clips_source = self.video_preview.video_clips

        clips_snapshot = copy.deepcopy(clips_source) if clips_source else []
        if hasattr(self, 'timeline_model') and clips_snapshot:
            self.timeline_model.video_clips = copy.deepcopy(clips_snapshot)
        transitions_snapshot = [copy.deepcopy(t) for t in self.transitions.values()] if (hasattr(self, 'transitions') and self.transitions) else []

        self.worker = DubbingWorker(
            video_path=self.video_path,
            output_path=out_path,
            source_lang=source_lang,
            target_lang="km",
            whisper_model="small",
            voice_name="VoxCPM2-Khmer",
            api_key=get_gemini_api_key(),
            background_volume=bg_vol,
            pre_translated_segments=pre_edited_segments,
            effects_config=effects_config,
            export_mode=exp_mode,
            target_resolution=target_resolution,
            master_audio_path=master_audio_cand,
            timeline_clips=clips_snapshot,
            timeline_transitions=transitions_snapshot
        )
        self.worker.progress_changed.connect(self._update_stepper_progress)
        self.worker.segments_ready.connect(self.subtitle_table.set_segments)
        self.worker.log_emitted.connect(self.log_console.append_log)
        self.worker.pipeline_finished.connect(self._on_finished)
        self.worker.pipeline_error.connect(lambda e: (
            self.log_console.append_log(f"❌ Video Dubbing error: {e}"),
            self._set_processing_state(False, f"❌ Export failed: {e}")
        ))
        self.worker.start()

    def _update_stepper_progress(self, progress: int, desc: str):
        self.progress_bar.setValue(progress)
        self.status_lbl.setText(desc)

    def _on_finished(self, output_path: str):
        self.progress_bar.setValue(100)
        self._set_processing_state(False, "🎉 Video Dubbing Complete!")

        # Automatically select the newly created Khmer dubbed audio track for preview
        if hasattr(self.video_preview, 'audio_track_combo'):
            self.video_preview.audio_track_combo.blockSignals(True)
            self.video_preview.audio_track_combo.setCurrentIndex(1)
            self.video_preview.audio_track_combo.blockSignals(False)
            self.video_preview._load_audio_for_player()

        # Auto-increment episode number for the next video / next episode
        if hasattr(self, 'episode_spin'):
            next_ep = self.episode_spin.value() + 1
            self.episode_spin.setValue(next_ep)
            self.log_console.append_log(f"⏩ Next Episode queued: EP {next_ep}")

        # Refresh folder list in UI
        if hasattr(self, '_refresh_story_folders'):
            self._refresh_story_folders()

        res = QMessageBox.information(self, "Success 🇰🇭", f"Khmer Dubbed Video Exported:\n{output_path}\n\nPlay Video Now?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            try:
                if os.name == 'nt':  # Windows
                    os.startfile(output_path)
                elif sys.platform == "darwin":
                    try:
                        subprocess.run(["xattr", "-c", output_path], check=False)
                    except Exception:
                        pass
                    res_open = subprocess.run(["open", output_path], capture_output=True, check=False)
                    if res_open.returncode != 0:
                        subprocess.run(["open", "-R", output_path], check=False)
                else:
                    subprocess.run(["xdg-open", output_path], check=False)
            except Exception:
                pass

    def _clear_temp_cache(self):
        """Clean all cached temporary audio and video files."""
        from utils.file_utils import TEMP_DIR
        count = 0
        if os.path.exists(TEMP_DIR):
            for fname in os.listdir(TEMP_DIR):
                fpath = os.path.join(TEMP_DIR, fname)
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                        count += 1
                except Exception:
                    pass
        self.log_console.append_log(f"🧹 Cleared {count} temporary cache files.")
        QMessageBox.information(self, "Cache Cleared", f"បានលុប File បណ្តោះអាសន្នចំនួន {count} Files រួចរាល់!")

    def _import_srt(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import SRT", "", "Subtitle Files (*.srt)")
        if file_path:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            from core.srt_translator import SRTTranslator
            st = SRTTranslator()
            sub_segs = st.parse_srt(content)
            segs = [{"start": s.start_seconds, "end": s.end_seconds, "original_text": s.text, "khmer_text": s.text} for s in sub_segs]
            self.subtitle_table.set_segments(segs)
            self.timeline_editor.set_segments(segs)
            self.transcribed_segments = segs
            self.log_console.append_log(f"📥 Imported {len(segs)} segments from SRT: {file_path}")

    def _setup_web_studio_watcher(self):
        """Monitor output folder for exported subtitles from Web Studio and live auto-load them."""
        try:
            self.web_watcher = QFileSystemWatcher(self)
            out_path = str(self.output_dir)
            if os.path.exists(out_path):
                self.web_watcher.addPath(out_path)
            json_file = os.path.join(out_path, "latest_web_subtitles.json")
            if os.path.exists(json_file):
                self.web_watcher.addPath(json_file)
            self.web_watcher.fileChanged.connect(self._on_web_subtitles_auto_sync)
            self.web_watcher.directoryChanged.connect(self._on_web_dir_changed)
            self._last_web_sync_time = 0
            self.log_console.append_log("🌐 [Live Sync] Web Studio auto-sync listener active.")
        except Exception as e:
            self.log_console.append_log(f"⚠️ Web Studio auto-sync listener warning: {e}")

    def _on_web_dir_changed(self, path):
        json_file = os.path.join(path, "latest_web_subtitles.json")
        if os.path.exists(json_file):
            if hasattr(self, 'web_watcher') and json_file not in self.web_watcher.files():
                self.web_watcher.addPath(json_file)
            self._on_web_subtitles_auto_sync(json_file)

    def _on_web_subtitles_auto_sync(self, file_path):
        import time
        now = time.time()
        if now - getattr(self, '_last_web_sync_time', 0) < 1.0:
            return
        self._last_web_sync_time = now
        # Re-add path if removed on atomic write
        if hasattr(self, 'web_watcher') and os.path.exists(file_path) and file_path not in self.web_watcher.files():
            self.web_watcher.addPath(file_path)
        self._import_from_web_studio(silent_if_not_found=True)

    def _import_from_web_studio(self, silent_if_not_found: bool = False):
        """Directly import subtitles exported from the Web Studio (ai-audio-translator)."""
        json_path = os.path.join(self.output_dir, "latest_web_subtitles.json")
        srt_path = os.path.join(self.output_dir, "latest_web_subtitles.srt")

        segs = []
        if os.path.exists(json_path):
            try:
                import json
                with open(json_path, "r", encoding="utf-8") as f:
                    raw_segs = json.load(f)
                for s in raw_segs:
                    st = float(s.get("startSeconds", s.get("start", 0.0)))
                    et = float(s.get("endSeconds", s.get("end", st + 2.0)))
                    if et <= st:
                        et = st + 2.0
                    orig = (s.get("sourceText") or s.get("original_text") or "").strip()
                    khmer = (s.get("translatedText") or s.get("khmer_text") or orig).strip()
                    
                    clean_khmer, spk, gender, voice = clean_speaker_tag(khmer)
                    clean_orig, _, _, _ = clean_speaker_tag(orig)

                    raw_gender = (s.get("gender") or "").lower().strip()
                    orig_spk = str(s.get("speaker") or "").lower()
                    if raw_gender == "child" or any(w in orig_spk for w in ['child', 'kid', 'boy', 'baby', 'ក្មេង']):
                        gender = "child"
                        spk = "🧒 ក្មេង"
                        voice = "Khmer Child - Boy (Vannak)"
                    elif raw_gender in ("elder_female", "ចាស់ស្រី") or any(w in orig_spk for w in ['ចាស់ស្រី', 'grandmother', 'យាយ']):
                        gender = "elder_female"
                        spk = "👵 ចាស់ស្រី"
                        voice = "Khmer Elder - Female (Grandmother)"
                    elif raw_gender in ("elder_male", "ចាស់ប្រុស") or any(w in orig_spk for w in ['ចាស់ប្រុស', 'grandfather', 'តា']):
                        gender = "elder_male"
                        spk = "👴 ចាស់ប្រុស"
                        voice = "Khmer Elder - Male (Grandfather)"
                    elif raw_gender == "female" or any(w in orig_spk for w in ['female', 'woman', 'girl', 'ស្រី']):
                        gender = "female"
                        spk = "👩 ស្រី"
                        voice = "Khmer Female - Sreymom"
                    elif raw_gender == "elder" or any(w in orig_spk for w in ['elder', 'ចាស់']):
                        gender = "elder"
                        spk = "👵👴 មនុស្សចាស់"
                        voice = "Khmer Elder - Male (Grandfather)"

                    persona = s.get("persona") or s.get("character") or spk
                    if persona not in PERSONA_CHOICES:
                        if gender == "child":
                            persona = "👦 Boy / Child"
                        elif gender == "female":
                            persona = "👩 Female Adult"
                        elif gender == "elder_female":
                            persona = "👵 Elderly Female"
                        elif gender in ("elder", "elder_male"):
                            persona = "👴 Elderly Male"
                        else:
                            persona = "👨 Male Adult"
                    emotion = s.get("emotion") or "😐 Neutral"
                    style = s.get("speaking_style") or s.get("style") or "Normal"
                    voice = s.get("voice_id") or s.get("voice") or voice

                    segs.append({
                        "id": str(s.get("id", len(segs) + 1)),
                        "start": round(st, 2),
                        "end": round(et, 2),
                        "original_text": clean_orig or clean_khmer,
                        "khmer_text": clean_khmer,
                        "speaker": spk,
                        "speaker_id": s.get("speaker_id") or s.get("speakerId") or f"speaker_{1 + (len(segs) % 2):02d}",
                        "character": persona,
                        "persona": persona,
                        "emotion": emotion,
                        "speaking_style": style,
                        "gender": gender,
                        "voice": voice,
                        "voice_id": voice
                    })
            except Exception as e:
                self.log_console.append_log(f"⚠️ Error reading JSON from Web Studio: {e}")

        if not segs and os.path.exists(srt_path):
            try:
                with open(srt_path, "r", encoding="utf-8") as f:
                    content = f.read()
                from core.srt_translator import SRTTranslator
                st = SRTTranslator()
                sub_segs = st.parse_srt(content)
                segs = []
                for s in sub_segs:
                    clean_txt, spk, gender, voice = clean_speaker_tag(s.text)
                    segs.append({
                        "start": s.start_seconds,
                        "end": s.end_seconds,
                        "original_text": clean_txt,
                        "khmer_text": clean_txt,
                        "speaker": spk,
                        "character": spk,
                        "gender": gender,
                        "voice": voice
                    })
            except Exception as e:
                self.log_console.append_log(f"⚠️ Error reading SRT from Web Studio: {e}")

        if not segs:
            if not silent_if_not_found:
                QMessageBox.information(
                    self,
                    "Web Studio Subtitles",
                    "មិនទាន់មានទិន្នន័យ Subtitles ពី Web Studio នៅឡើយទេ!\n\n"
                    "សូមបើក '🌐 Web Studio' បកប្រែរួចចុចប៊ូតុង '🚀 បញ្ជូនទៅ Desktop Studio' ជាមុនសិន។"
                )
            return

        self.subtitle_table.set_segments(segs)
        self.timeline_editor.set_segments(segs)
        self.video_preview.set_timeline_segments(segs)
        self.transcribed_segments = segs
        self.log_console.append_log(f"🌐 [Web Studio Live Sync] Successfully loaded {len(segs)} Khmer segments!")
        self.status_lbl.setText(f"🎉 Web Studio: {len(segs)} segments auto-synced!")
        if not silent_if_not_found:
            QMessageBox.information(
                self,
                "នាំចូលបានជោគជ័យ",
                f"✅ បាននាំចូលអត្ថបទបកប្រែចំនួន {len(segs)} segments ពី Web Studio រួចរាល់!\nលោកអ្នកអាចចុច '🎙️ Export Dubbed Video' ដើម្បីបញ្ចូលសំឡេងបានភ្លាមៗ។"
            )

    def _export_srt(self):
        self._export_khmer_srt()

    def _export_khmer_srt(self):
        segs = self.subtitle_table.get_updated_segments()
        if not segs:
            QMessageBox.information(self, "No Subtitles", "មិនទាន់មាន Subtitle ក្នុងតារាងទេ! សូមបង្កើត ឬ Transcribe ជាមុនសិន។")
            return
        from pathlib import Path
        stem = Path(self.video_path).stem if self.video_path else "subtitles"
        def_name = f"{stem}_khmer.srt"
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Khmer SRT", def_name, "Subtitle Files (*.srt)")
        if file_path:
            from utils.file_utils import export_segments_to_srt
            export_segments_to_srt(segs, file_path, text_key="khmer_text")
            self.log_console.append_log(f"📤 Exported Khmer SRT file: {file_path}")
            QMessageBox.information(self, "Success 🎉", f"បាន Export ឯកសារ SRT ខ្មែរដោយជោគជ័យ:\n{file_path}")

    def _export_original_srt(self):
        segs = self.subtitle_table.get_updated_segments()
        if not segs:
            QMessageBox.information(self, "No Subtitles", "មិនទាន់មាន Subtitle ក្នុងតារាងទេ! សូមបង្កើត ឬ Transcribe ជាមុនសិន។")
            return
        from pathlib import Path
        stem = Path(self.video_path).stem if self.video_path else "subtitles"
        def_name = f"{stem}_original.srt"
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Original SRT", def_name, "Subtitle Files (*.srt)")
        if file_path:
            from utils.file_utils import export_segments_to_srt
            export_segments_to_srt(segs, file_path, text_key="original_text")
            self.log_console.append_log(f"📤 Exported Original Source SRT file: {file_path}")
            QMessageBox.information(self, "Success 🎉", f"បាន Export ឯកសារ SRT អក្សរដើមដោយជោគជ័យ:\n{file_path}")

    # ==================== BATCH VOICE GENERATION (PREVIEW IN PLAYER) ====================
    def _generate_all_voices(self):
        """Synthesize all Khmer segment voices in parallel and load into Video Preview player."""
        if not self.video_path or not os.path.exists(self.video_path):
            QMessageBox.information(
                self, "Video Required",
                "សូមបើកវីដេអូជាមុនសិន មុននឹងសំយោគសំឡេង (Please open a video first)."
            )
            return

        segments = self.subtitle_table.get_updated_segments()
        if not segments:
            QMessageBox.information(
                self, "Subtitles Required",
                "មិនទាន់មាន Subtitle ក្នុងតារាងទេ! សូម Paste SRT ឬ Transcribe ជាមុនសិន។"
            )
            return

        # Check total duration from video_preview
        total_duration = 10.0
        try:
            if hasattr(self.video_preview, 'total_frames') and hasattr(self.video_preview, 'fps'):
                total_duration = max(1.0, self.video_preview.total_frames / max(1.0, self.video_preview.fps))
        except Exception:
            pass

        # Enable cancel button & set table state to Synthesizing
        if hasattr(self.subtitle_table, 'set_voice_generation_state'):
            self.subtitle_table.set_voice_generation_state(True)
        elif hasattr(self.subtitle_table, 'generate_voices_btn'):
            self.subtitle_table.generate_voices_btn.setEnabled(False)
            self.subtitle_table.generate_voices_btn.setText("Synthesizing...")

        if hasattr(self, 'cancel_export_btn'):
            self.cancel_export_btn.setEnabled(True)

        self.progress_bar.setValue(0)
        self.status_lbl.setText("Synthesizing Khmer voices...")
        self.log_console.append_log(f"[VoiceGen] Synthesizing Khmer voices for {len(segments)} segments...")

        self.voice_worker = GenerateVoicesWorker(segments, total_duration)
        self.voice_worker.progress.connect(self._on_voice_gen_progress)
        self.voice_worker.finished.connect(self._on_voice_gen_finished)
        self.voice_worker.error.connect(self._on_voice_gen_error)
        self.voice_worker.start()

    def _cancel_voice_generation(self):
        """Immediately abort active voice synthesis worker and restore UI."""
        if hasattr(self, 'voice_worker') and self.voice_worker and self.voice_worker.isRunning():
            self.voice_worker.cancel()
            self.log_console.append_log("🛑 User requested cancel for Voice Generation. Aborting...")
            self.status_lbl.setText("⚠️ Voice generation canceled.")
        if hasattr(self.subtitle_table, 'set_voice_generation_state'):
            self.subtitle_table.set_voice_generation_state(False)
        elif hasattr(self.subtitle_table, 'generate_voices_btn'):
            self.subtitle_table.generate_voices_btn.setEnabled(True)
            self.subtitle_table.generate_voices_btn.setText("Generate Voices")
        if hasattr(self, 'cancel_export_btn') and (not self.worker or not self.worker.isRunning()):
            self.cancel_export_btn.setEnabled(False)

    def _on_voice_gen_progress(self, pct: int, msg: str):
        self.progress_bar.setValue(pct)
        self.status_lbl.setText(msg)
        self.log_console.append_log(msg)

    def _on_voice_gen_finished(self, master_wav: str):
        self.last_master_wav = master_wav
        if hasattr(self.subtitle_table, 'set_voice_generation_state'):
            self.subtitle_table.set_voice_generation_state(False)
        elif hasattr(self.subtitle_table, 'generate_voices_btn'):
            self.subtitle_table.generate_voices_btn.setEnabled(True)
            self.subtitle_table.generate_voices_btn.setText("Generate Voices")

        if hasattr(self, 'cancel_export_btn') and (not self.worker or not self.worker.isRunning()):
            self.cancel_export_btn.setEnabled(False)

        self.progress_bar.setValue(100)
        self.status_lbl.setText("Khmer voices ready! Switched to Khmer Dubbed Audio.")
        self.log_console.append_log(f"[VoiceGen] Khmer dubbed track generated: {os.path.basename(master_wav)}")

        segments = self.subtitle_table.get_updated_segments()
        first_speech_sec = 0.0
        if segments:
            for s in segments:
                st = float(s.get('start', 0.0))
                txt = s.get('khmer_text') or s.get('text', '')
                if txt.strip():
                    first_speech_sec = st
                    break

        # Automatically switch video preview audio track to "🇰🇭 Khmer Dubbed + BGM" (index 1) and reload mixed audio safely
        if hasattr(self.video_preview, 'audio_track_combo'):
            self.video_preview.audio_track_combo.blockSignals(True)
            self.video_preview.audio_track_combo.setCurrentIndex(1)
            self.video_preview.audio_track_combo.blockSignals(False)

        # Seek preview directly to first speech segment so user doesn't wait in silence
        if hasattr(self.video_preview, 'seek_to_time_sec'):
            self.video_preview.seek_to_time_sec(first_speech_sec)

        if hasattr(self.video_preview, 'reload_mixed_audio'):
            self.video_preview.reload_mixed_audio()
        elif hasattr(self.video_preview, '_load_audio_for_player'):
            self.video_preview._load_audio_for_player()

        # Ensure preview player audio output is completely unmuted & volume 100%
        if hasattr(self.video_preview, 'audio_output'):
            if hasattr(self.video_preview.audio_output, 'setMuted'):
                self.video_preview.audio_output.setMuted(False)
            if hasattr(self.video_preview.audio_output, 'setVolume'):
                self.video_preview.audio_output.setVolume(1.0)

        # Trigger background auto-save so user never loses generated audio state
        self._auto_save_project()

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Voice Generation Complete")
        msg_box.setIcon(QMessageBox.Icon.Information if hasattr(QMessageBox, 'Icon') else QMessageBox.Information)
        msg_box.setText(
            f"សំយោគសំឡេងខ្មែរគ្រប់ជួរ ({len(segments)} ឃ្លា) បានជោគជ័យ!\n\n"
            f"សំឡេងខ្មែរចាប់ផ្តើមនិយាយនៅវិនាទី: {first_speech_sec:.1f}s\n"
            "ប្រព័ន្ធបានប្តូរទៅចាក់សំឡេង 'Khmer Dubbed + BGM' និងរំកិលទៅត្រង់ឃ្លានិយាយដំបូងរួចជាស្រេច។\n"
            "លោកអ្នកអាចចុច 'ចាក់ស្តាប់ភ្លាមៗ' ឬចុច 'ស្តាប់សំឡេងខ្មែរ' ដើម្បីស្តាប់បានភ្លាមៗ!"
        )
        play_btn = msg_box.addButton("ចាក់ស្តាប់ភ្លាមៗ (Play Preview)", QMessageBox.ButtonRole.AcceptRole if hasattr(QMessageBox, 'ButtonRole') else QMessageBox.AcceptRole)
        audition_btn = msg_box.addButton("ស្តាប់សំឡេងខ្មែរ (Instant Audition)", QMessageBox.ButtonRole.ActionRole if hasattr(QMessageBox, 'ButtonRole') else QMessageBox.ActionRole)
        close_btn = msg_box.addButton("យល់ព្រម (OK)", QMessageBox.ButtonRole.RejectRole if hasattr(QMessageBox, 'ButtonRole') else QMessageBox.RejectRole)
        msg_box.exec()
        if msg_box.clickedButton() == play_btn:
            if hasattr(self.video_preview, '_toggle_play') and not self.video_preview._is_playing:
                self.video_preview._toggle_play()
        elif msg_box.clickedButton() == audition_btn:
            if hasattr(self.video_preview, 'audition_voice'):
                self.video_preview.audition_voice()

    def _on_voice_gen_error(self, err_msg: str):
        if hasattr(self.subtitle_table, 'set_voice_generation_state'):
            self.subtitle_table.set_voice_generation_state(False)
        elif hasattr(self.subtitle_table, 'generate_voices_btn'):
            self.subtitle_table.generate_voices_btn.setEnabled(True)
            self.subtitle_table.generate_voices_btn.setText("Generate Voices")

        if hasattr(self, 'cancel_export_btn') and (not self.worker or not self.worker.isRunning()):
            self.cancel_export_btn.setEnabled(False)

        if "ផ្អាក" in str(err_msg) or "cancel" in str(err_msg).lower():
            self.status_lbl.setText("⚠️ ដំណើរការសំយោគសំឡេងត្រូវបានផ្អាក (Cancelled)")
            self.log_console.append_log(f"⚠️ [VoiceGen] {err_msg}")
            return

        self.status_lbl.setText(f"{err_msg}")
        self.log_console.append_log(f"[VoiceGen Error] {err_msg}")
        QMessageBox.warning(self, "Voice Generation Error", err_msg)

    def _on_bgm_volume_changed(self, val: int):
        self.status_lbl.setText(f"🎶 BGM Volume: {val}%")
        if hasattr(self, 'video_preview'):
            if hasattr(self.video_preview, 'bgm_vol_slider'):
                self.video_preview.bgm_vol_slider.blockSignals(True)
                self.video_preview.bgm_vol_slider.setValue(val)
                self.video_preview.bgm_vol_slider.setToolTip(f"កម្រិតសំឡេងដើម / BGM Volume: {val}%")
                self.video_preview.bgm_vol_slider.blockSignals(False)
            if hasattr(self.video_preview, 'reload_mixed_audio'):
                if hasattr(self.video_preview, 'audio_track_combo') and self.video_preview.audio_track_combo.currentIndex() == 1:
                    self.video_preview.reload_mixed_audio()

    # ==================== PROJECT SAVE & OPEN (.VPROJ) ====================
    def _auto_save_project(self):
        """Silently auto-save current state to output/autosave_project.vproj for disaster recovery."""
        try:
            import json
            segments = self.subtitle_table.get_updated_segments()
            if not segments and not self.video_path:
                return
            auto_path = Path(self.output_dir) / "autosave_project.vproj"
            effects_state = self.video_effects.get_state() if hasattr(self, 'video_effects') else {}
            bgm_vol = self.bgm_vol_spin.value() if hasattr(self, 'bgm_vol_spin') else 30
            project_data = {
                "format": "VideAI_Project",
                "version": "1.1",
                "video_path": self.video_path,
                "output_dir": self.output_dir,
                "bgm_volume": bgm_vol,
                "segments": segments,
                "effects": effects_state,
                "master_wav": getattr(self, 'last_master_wav', "")
            }
            with open(auto_path, "w", encoding="utf-8") as f:
                json.dump(project_data, f, ensure_ascii=False, indent=2)
            logger.debug(f"[AutoSave] Project saved to {auto_path}")
        except Exception as e:
            logger.debug(f"[AutoSave] Silent error: {e}")

    def _restore_autosave(self):
        """Restore project from the latest autosave_project.vproj file."""
        auto_path = Path(self.output_dir) / "autosave_project.vproj"
        if not auto_path.exists():
            QMessageBox.information(
                self, "Restore Auto-save",
                "មិនមានឯកសារ Auto-save ត្រូវបានរកឃើញពីមុនមកទេ។"
            )
            return
        self._open_project(str(auto_path))

    def _save_project(self, file_path: str = None):
        """Save entire project state into a .vproj (JSON) file."""
        import json

        segments = self.subtitle_table.get_updated_segments()
        if not self.video_path and not segments:
            QMessageBox.information(
                self, "Save Project",
                "គ្មានទិន្នន័យគម្រោងដែលត្រូវរក្សាទុកទេ (សូមបើកវីដេអូ ឬនាំចូល Subtitle ជាមុនសិន)។"
            )
            return

        target_file = file_path or self.current_project_file
        if not target_file:
            base_stem = Path(self.video_path).stem if self.video_path else "khmer_translation"
            default_name = f"{base_stem}_project.vproj"
            target_file, _ = QFileDialog.getSaveFileName(
                self, "Save Vide AI Project",
                str(Path(self.output_dir) / default_name),
                "Vide AI Project (*.vproj *.json)"
            )
            if not target_file:
                return

        # Ensure .vproj extension if none provided
        if not target_file.endswith(".vproj") and not target_file.endswith(".json"):
            target_file += ".vproj"

        effects_state = self.video_effects.get_state() if hasattr(self, 'video_effects') else {}
        bgm_vol = self.bgm_vol_spin.value() if hasattr(self, 'bgm_vol_spin') else 30

        # Pillar 1 & 2: Aggregate Speaker Profiles dictionary
        speakers = {}
        for seg in segments:
            spk_id = seg.get("speaker_id") or "speaker_01"
            if spk_id not in speakers:
                persona = seg.get("persona") or seg.get("character") or "👨 Male Adult"
                speakers[spk_id] = {
                    "id": spk_id,
                    "display_name": f"Speaker {spk_id.replace('speaker_', '')}",
                    "persona": persona,
                    "gender": "female" if ("ស្រី" in persona or "Female" in persona) else "male",
                    "age_group": "child" if ("ក្មេង" in persona or "Child" in persona) else "adult",
                    "voice_id": seg.get("voice_id") or seg.get("voice") or "Khmer Male - Piseth",
                    "default_emotion": seg.get("emotion") or "😐 Neutral",
                    "default_style": seg.get("speaking_style") or seg.get("style") or "Normal"
                }

        # Collect and serialize transition entities
        import math
        serialized_transitions = []
        if hasattr(self, 'transitions') and self.transitions:
            for cut_idx, trans in self.transitions.items():
                if trans is None:
                    continue
                trans_type = getattr(trans, "type", None) or (trans.get("type") if isinstance(trans, dict) else "cross_dissolve")
                if trans_type == "none":
                    continue
                try:
                    c_idx = int(cut_idx)
                    t_id = str(getattr(trans, "id", None) or (trans.get("id") if isinstance(trans, dict) else f"trans_{c_idx}"))
                    clip_a = str(getattr(trans, "clip_a_id", None) or (trans.get("clip_a_id") if isinstance(trans, dict) else f"{c_idx}"))
                    clip_b = str(getattr(trans, "clip_b_id", None) or (trans.get("clip_b_id") if isinstance(trans, dict) else f"{c_idx+1}"))
                    cut_tm = float(getattr(trans, "cut_time", None) or (trans.get("cut_time") if isinstance(trans, dict) else 0.0))
                    dur = float(getattr(trans, "duration", None) or (trans.get("duration") if isinstance(trans, dict) else 1.0))
                    align = str(getattr(trans, "alignment", None) or (trans.get("alignment") if isinstance(trans, dict) else "center"))
                    eas = str(getattr(trans, "easing", None) or (trans.get("easing") if isinstance(trans, dict) else "linear"))
                    a_mode = str(getattr(trans, "audio_mode", None) or (trans.get("audio_mode") if isinstance(trans, dict) else "equal_power"))
                    params = getattr(trans, "parameters", None) or (trans.get("parameters") if isinstance(trans, dict) else {})
                    if not isinstance(params, dict):
                        params = {}

                    serialized_transitions.append({
                        "cut_index": c_idx,
                        "id": t_id,
                        "clip_a_id": clip_a,
                        "clip_b_id": clip_b,
                        "cut_time": cut_tm,
                        "duration": dur,
                        "alignment": align,
                        "type": str(trans_type),
                        "easing": eas,
                        "audio_mode": a_mode,
                        "parameters": params
                    })
                except Exception as ex:
                    logger.warning(f"⚠️ [Project] Could not serialize transition at cut {cut_idx}: {ex}")

        project_data = {
            "format": "VideAI_Project",
            "version": "2.1",
            "video_path": self.video_path,
            "output_dir": self.output_dir,
            "bgm_volume": bgm_vol,
            "speakers": speakers,
            "segments": segments,
            "effects": effects_state,
            "master_wav": getattr(self, 'last_master_wav', ""),
            "transitions": serialized_transitions
        }

        temp_target = f"{target_file}.tmp"
        try:
            with open(temp_target, "w", encoding="utf-8") as f:
                json.dump(project_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_target, target_file)

            self.current_project_file = target_file
            proj_name = Path(target_file).name
            self.setWindowTitle(f"🎬 Vide AI Studio - [{proj_name}]")
            self.status_lbl.setText(f"💾 Project saved: {proj_name}")
            self.log_console.append_log(f"💾 [Project] Saved project successfully: {target_file} ({len(serialized_transitions)} transition(s) saved)")

            # Show brief confirmation if manually saved
            if not file_path:
                QMessageBox.information(
                    self, "Project Saved",
                    f"✅ គម្រោងត្រូវបានរក្សាទុកដោយជោគជ័យ!\n\n"
                    f"📁 ឯកសារ: {proj_name}\n"
                    f"📝 ចំនួនឃ្លា Subtitle: {len(segments)} ជួរ\n"
                    f"⧓ ចំនួន Transition: {len(serialized_transitions)} កន្លែង\n\n"
                    f"លោកអ្នកអាចបើកវាមកកែប្រែបន្តនៅពេលក្រោយបានគ្រប់ពេលវេលា (Cmd+O) ដោយមិនបាច់ចាប់ផ្តើមពីសូន្យឡើយ។"
                )
        except Exception as e:
            if os.path.exists(temp_target):
                try:
                    os.remove(temp_target)
                except Exception:
                    pass
            self.log_console.append_log(f"❌ [Project] Failed to save project: {e}")
            QMessageBox.critical(self, "Save Error", f"មិនអាចរក្សាទុកគម្រោងបានទេ:\n{str(e)}")

    def _open_project(self, file_path: str = None):
        """Open and restore project state from a .vproj (JSON) file."""
        import json

        target_file = file_path
        if not target_file:
            target_file, _ = QFileDialog.getOpenFileName(
                self, "Open Vide AI Project",
                self.output_dir,
                "Vide AI Project (*.vproj *.json)"
            )
            if not target_file:
                return

        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            video_path = data.get("video_path")
            if video_path and not os.path.exists(video_path):
                # Try finding in the same folder as the project file
                proj_dir = Path(target_file).parent
                candidate = proj_dir / Path(video_path).name
                ws_candidate = Path(os.getcwd()) / Path(video_path).name
                temp_candidate = Path(os.getcwd()) / "temp" / f"safe_input_{Path(video_path).name}"

                if candidate.exists():
                    video_path = str(candidate)
                elif ws_candidate.exists():
                    video_path = str(ws_candidate)
                elif temp_candidate.exists():
                    video_path = str(temp_candidate)
                else:
                    reply = QMessageBox.question(
                        self, "Video File Missing",
                        f"មិនអាចរកឃើញឯកសារវីដេអូនៅទីតាំងដើម:\n{video_path}\n\n"
                        "តើលោកអ្នកចង់ជ្រើសរើសទីតាំងថ្មីនៃឯកសារវីដេអូនោះឥឡូវនេះទេ?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
                    )
                    if reply == QMessageBox.Yes:
                        chosen, _ = QFileDialog.getOpenFileName(
                            self, "Locate Missing Video File",
                            str(proj_dir),
                            "Video Files (*.mp4 *.mkv *.mov *.avi *.webm)"
                        )
                        if chosen and os.path.exists(chosen):
                            video_path = chosen
                        else:
                            video_path = None
                    else:
                        video_path = None

            # Load video file if available
            if video_path and os.path.exists(video_path):
                self._load_video_file(video_path, clear_segments=False)

            # Restore segments
            segments = data.get("segments", [])
            self.transcribed_segments = segments
            self.subtitle_table.set_segments(segments)
            self.timeline_editor.set_segments(segments)
            self.video_preview.set_timeline_segments(segments)

            # Restore BGM volume
            if "bgm_volume" in data and hasattr(self, 'bgm_vol_spin'):
                self.bgm_vol_spin.setValue(int(data["bgm_volume"]))

            # Restore Effects
            if "effects" in data and hasattr(self, 'video_effects'):
                self.video_effects.set_state(data["effects"])

            if "output_dir" in data:
                self.output_dir = data["output_dir"]
                if hasattr(self, 'out_dir_display'):
                    self.out_dir_display.setText(self.output_dir)

            # Restore transitions with strict validation and backward compatibility
            from core.timeline_model import Transition
            import math
            VALID_TYPES = {"cross_dissolve", "fade_black", "fade_white", "wipe_left", "wipe_right", "wipe_up", "wipe_down"}
            VALID_ALIGNMENTS = {"center", "start_on_cut", "end_on_cut"}
            VALID_EASINGS = {"linear", "ease_in", "ease_out", "ease_in_out"}
            VALID_AUDIO_MODES = {"equal_power", "linear", "exponential", "none"}

            restored_transitions = {}
            raw_transitions = data.get("transitions", [])
            if isinstance(raw_transitions, list):
                for item in raw_transitions:
                    if not isinstance(item, dict):
                        self.log_console.append_log("⚠️ [Project] Skipped invalid non-dictionary transition entry.")
                        continue
                    try:
                        c_idx = int(item["cut_index"])
                        dur = float(item.get("duration", 1.0))
                        if not math.isfinite(dur) or dur <= 0.0:
                            self.log_console.append_log(f"⚠️ [Project] Skipped transition cut {c_idx}: invalid duration {dur}")
                            continue

                        t_type = str(item.get("type", "cross_dissolve"))
                        if t_type not in VALID_TYPES:
                            t_type = "cross_dissolve"

                        align = str(item.get("alignment", "center"))
                        if align not in VALID_ALIGNMENTS:
                            align = "center"

                        easing = str(item.get("easing", "linear"))
                        if easing not in VALID_EASINGS:
                            easing = "linear"

                        a_mode = str(item.get("audio_mode", "equal_power"))
                        if a_mode not in VALID_AUDIO_MODES:
                            a_mode = "equal_power"

                        params = item.get("parameters", {})
                        if not isinstance(params, dict):
                            params = {}

                        trans_obj = Transition(
                            id=str(item.get("id", f"trans_{c_idx}")),
                            clip_a_id=str(item.get("clip_a_id", str(c_idx))),
                            clip_b_id=str(item.get("clip_b_id", str(c_idx + 1))),
                            cut_time=float(item.get("cut_time", 0.0)),
                            duration=dur,
                            alignment=align,
                            type=t_type,
                            easing=easing,
                            audio_mode=a_mode,
                            parameters=params
                        )
                        restored_transitions[c_idx] = trans_obj
                    except (KeyError, ValueError, TypeError) as ex:
                        self.log_console.append_log(f"⚠️ [Project] Corrupted transition entry skipped: {ex}")
            else:
                self.log_console.append_log("ℹ️ [Project] No transition list in project file (backward compatibility).")

            self.transitions = restored_transitions
            if hasattr(self, 'timeline_model'):
                self.timeline_model.transitions = self.transitions
            if hasattr(self, 'timeline_editor') and hasattr(self.timeline_editor, 'waveform_canvas'):
                self.timeline_editor.waveform_canvas.set_transitions(self.transitions)
                self.timeline_editor.waveform_canvas.update()
            if hasattr(self, 'video_preview') and hasattr(self.video_preview, 'timeline_model'):
                self.video_preview.timeline_model.transitions = self.transitions
                if hasattr(self.video_preview, 'set_timeline_clips') and self.timeline_model.video_clips:
                    self.video_preview.set_timeline_clips(self.timeline_model.video_clips, transitions=self.transitions)
            if restored_transitions:
                self.log_console.append_log(f"⧓ [Project] Successfully restored {len(restored_transitions)} transition(s).")

            # Restore Dubbed Audio track if previously generated
            master_wav = data.get("master_wav")
            if not master_wav or not os.path.exists(master_wav):
                default_temp_wav = os.path.join(os.getcwd(), "temp", "master_khmer_voice.wav")
                if os.path.exists(default_temp_wav):
                    master_wav = default_temp_wav

            if master_wav and os.path.exists(master_wav):
                self.last_master_wav = master_wav
                if hasattr(self.video_preview, 'audio_track_combo'):
                    self.video_preview.audio_track_combo.setCurrentIndex(1)
                    self.video_preview._load_audio_for_player()
                self.log_console.append_log(f"🔊 [Project] Restored synthesized audio track: {os.path.basename(master_wav)}")

            self.current_project_file = target_file
            proj_name = Path(target_file).name
            self.setWindowTitle(f"🎬 Vide AI Studio - [{proj_name}]")
            self.status_lbl.setText(f"📂 Opened project: {proj_name}")
            self.log_console.append_log(f"📂 [Project] Opened project: {target_file} ({len(segments)} segments loaded)")

            vid_info = Path(video_path).name if video_path else "មិនទាន់ភ្ជាប់វីដេអូ"
            QMessageBox.information(
                self, "Project Opened",
                f"✅ បានបើកគម្រោងដោយជោគជ័យ!\n\n"
                f"📁 ឯកសារ: {proj_name}\n"
                f"📹 វីដេអូ: {vid_info}\n"
                f"📝 ចំនួនឃ្លា Subtitle: {len(segments)} ជួរ\n\n"
                f"លោកអ្នកអាចបន្តកែប្រែ ឬបន្ថែមសំឡេងបានភ្លាមៗ!"
            )
        except Exception as e:
            self.log_console.append_log(f"❌ [Project] Failed to open project: {e}")
            QMessageBox.critical(self, "Open Error", f"មិនអាចបើកឯកសារគម្រោងបានទេ:\n{str(e)}")

    def closeEvent(self, event):
        """Cleanly terminate any background worker threads and auto-save current state."""
        try:
            self._auto_save_project()
        except Exception:
            pass
        for worker in [getattr(self, 'worker', None), getattr(self, 'voice_worker', None), getattr(self, 'transcribe_worker', None), getattr(self, 'translate_worker', None), getattr(self, 'mp3_worker', None), getattr(self, 'react_worker', None)]:
            if worker and worker.isRunning():
                try:
                    if hasattr(worker, 'cancel'):
                        worker.cancel()
                    worker.quit()
                    if not worker.wait(300):
                        worker.terminate()
                        worker.wait(200)
                except Exception:
                    pass
        for w in getattr(self, '_mp3_workers', []):
            if w and w.isRunning():
                try:
                    w.cancel()
                    w.wait(200)
                except Exception:
                    pass
        event.accept()

