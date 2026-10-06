"""
Media Bin & Library Widget (CapCut-Style Workflow).
Allows importing multiple video files (Clips 01, 02, 03...),
previewing metadata, switching active video in Studio,
combining all clips into a unified timeline sequence, and batch processing.
"""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from qt_compat import (
    Qt, Signal, Slot, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QProgressBar,
    QFileDialog, QMessageBox, QFrame, QMenu, QUrl, QMimeData, QtGui, QStackedLayout,
    QDesktopServices, QApplication, QtCore, QScrollArea, QGridLayout, QSizePolicy
)
from utils.ffmpeg import get_video_info
from utils.logger import logger
from gui.svg_icons import get_svg_icon
import cv2

_THUMBNAIL_CACHE = {}

def extract_video_thumbnail(video_path: str, target_w: int = 76, target_h: int = 46, dur_str: str = "") -> Optional[QtGui.QPixmap]:
    """Extract a fast, high-quality video poster thumbnail with 16:9 / 9:16 letterboxing and duration pill."""
    abs_p = os.path.abspath(video_path)
    if abs_p in _THUMBNAIL_CACHE:
        return _THUMBNAIL_CACHE[abs_p]

    try:
        cap = cv2.VideoCapture(abs_p)
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        # Seek to 0.5s or frame 15 to avoid black opening frames
        target_f = min(int(fps * 0.5), 20)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, target_f))
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None

        h, w, ch = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QtGui.QImage(rgb.data, w, h, ch * w, QtGui.QImage.Format_RGB888)
        pix = QtGui.QPixmap.fromImage(qimg)

        # Scale to thumbnail box smoothly keeping aspect ratio
        scaled_pix = pix.scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        out_pix = QtGui.QPixmap(target_w, target_h)
        out_pix.fill(QtGui.QColor("#090d16"))

        painter = QtGui.QPainter(out_pix)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)

        # Draw centered thumbnail
        ox = (target_w - scaled_pix.width()) // 2
        oy = (target_h - scaled_pix.height()) // 2
        painter.drawPixmap(ox, oy, scaled_pix)

        # Draw subtle border
        painter.setPen(QtGui.QPen(QtGui.QColor("#1e293b"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QtCore.QRect(0, 0, target_w - 1, target_h - 1), 4, 4)

        # Overlaid duration badge pill in bottom-right corner (CapCut style)
        if dur_str:
            badge_font = QtGui.QFont("Menlo", 7, QtGui.QFont.Bold)
            painter.setFont(badge_font)
            fm = QtGui.QFontMetrics(badge_font)
            bw = fm.horizontalAdvance(dur_str) + 6
            bh = fm.height() + 2
            bx = target_w - bw - 3
            by = target_h - bh - 3

            painter.setPen(Qt.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, 200))
            painter.drawRoundedRect(QtCore.QRect(bx, by, bw, bh), 3, 3)

            painter.setPen(QtGui.QColor("#ffffff"))
            painter.drawText(bx + 3, by + fm.ascent() + 1, dur_str)

        painter.end()

        _THUMBNAIL_CACHE[abs_p] = out_pix
        return out_pix
    except Exception:
        return None


class ThumbBox(QFrame):
    """16:9 Thumbnail container with rounded corners and CapCut badges."""
    def __init__(self, pixmap: Optional[QtGui.QPixmap], parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.setFixedHeight(82)
        self.setStyleSheet("border-radius: 6px; background-color: #0b1120;")

    def mousePressEvent(self, event):
        p = self.parent()
        if p and hasattr(p, 'mousePressEvent'):
            p.mousePressEvent(event)
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        p = self.parent()
        if p and hasattr(p, 'mouseDoubleClickEvent'):
            p.mouseDoubleClickEvent(event)
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        
        path = QtGui.QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 6, 6)
        painter.setClipPath(path)

        if self.pixmap and not self.pixmap.isNull():
            scaled = self.pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            ox = (self.width() - scaled.width()) // 2
            oy = (self.height() - scaled.height()) // 2
            painter.drawPixmap(ox, oy, scaled)
        else:
            painter.fillRect(self.rect(), QtGui.QColor("#0f172a"))
            painter.setPen(QtGui.QColor("#475569"))
            painter.setFont(QtGui.QFont("Inter", 10, QtGui.QFont.Bold))
            painter.drawText(self.rect(), Qt.AlignCenter, "🎬 Video")

        # Subtle border
        painter.setPen(QtGui.QPen(QtGui.QColor("#1e293b"), 1))
        painter.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 6, 6)
        painter.end()


class MediaCardWidget(QFrame):
    """CapCut-style Video Thumbnail Card with hover '+', duration badge, status badge, and drag-and-drop."""
    clicked = Signal(str)
    double_clicked = Signal(str)
    add_requested = Signal(str)
    delete_requested = Signal(int)

    def __init__(self, item_data: Dict, index: int, parent=None):
        super().__init__(parent)
        self.item_data = item_data
        self.index = index
        self.path = item_data.get("path", "")
        self.name = item_data.get("name", "Clip")
        self._drag_start_pos = None

        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("capcutMediaCard")
        self._init_ui()

    def _init_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(110)
        self.setMaximumWidth(180)
        self.setStyleSheet("""
            QFrame#capcutMediaCard {
                background-color: #101522;
                border: 1px solid #1a2336;
                border-radius: 8px;
            }
            QFrame#capcutMediaCard:hover {
                border-color: #00f2fe;
                background-color: #141c2e;
            }
        """)

        card_lay = QVBoxLayout(self)
        card_lay.setContentsMargins(5, 5, 5, 6)
        card_lay.setSpacing(5)

        # 1. Thumbnail Container (16:9 box)
        dur_sec = self.item_data.get("duration", 0.0)
        mins = int(dur_sec // 60)
        secs = int(dur_sec % 60)
        dur_str = f"{mins:02d}:{secs:02d}" if dur_sec > 0 else "00:00"

        thumb_pix = self.item_data.get("thumbnail")
        if thumb_pix is None:
            thumb_pix = extract_video_thumbnail(self.path, target_w=150, target_h=82, dur_str="")
            self.item_data["thumbnail"] = thumb_pix

        self.thumb_box = ThumbBox(thumb_pix, self)
        t_lay = QVBoxLayout(self.thumb_box)
        t_lay.setContentsMargins(4, 4, 4, 4)

        # Overlay Top Row: Status badge & Duration badge
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)

        status = self.item_data.get("status", "Ready")
        disp_st = "Added" if ("Done" in status or "Complete" in status or status == "Added") else "Ready"
        st_color = "#34d399" if disp_st == "Added" else "#38bdf8"
        st_bg = "#064e3b" if disp_st == "Added" else "rgba(12, 42, 74, 0.9)"
        st_border = "#059669" if disp_st == "Added" else "#0284c7"

        self.st_lbl = QLabel(disp_st, self.thumb_box)
        self.st_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.st_lbl.setStyleSheet(f"""
            color: {st_color};
            background-color: {st_bg};
            border: 1px solid {st_border};
            font-size: 8px;
            font-weight: 800;
            padding: 1px 5px;
            border-radius: 3px;
        """)
        top_row.addWidget(self.st_lbl)
        top_row.addStretch()

        dur_lbl = QLabel(dur_str, self.thumb_box)
        dur_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        dur_lbl.setStyleSheet("""
            color: #ffffff;
            background-color: rgba(0, 0, 0, 0.75);
            font-size: 8px;
            font-weight: 700;
            font-family: Menlo, monospace;
            padding: 1px 5px;
            border-radius: 3px;
        """)
        top_row.addWidget(dur_lbl)
        t_lay.addLayout(top_row)

        t_lay.addStretch()

        # Overlay Bottom Row: Quick Add '+' Button (CapCut Signature)
        bot_row = QHBoxLayout()
        bot_row.setContentsMargins(0, 0, 4, 4)
        bot_row.addStretch()

        self.add_btn = QPushButton(self.thumb_box)
        self.add_btn.setIcon(get_svg_icon("plus", "#ffffff", 12))
        self.add_btn.setIconSize(QtCore.QSize(12, 12))
        self.add_btn.setFixedSize(22, 22)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setToolTip("បញ្ចូលទៅក្នុង Timeline (Add to Sequence)")
        self.add_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00f2fe, stop:1 #0284c7);
                border-radius: 11px;
                border: 1px solid #7dd3fc;
            }
            QPushButton:hover {
                background: #00f2fe;
                border-color: #ffffff;
            }
        """)
        self.add_btn.clicked.connect(self._on_add_clicked)
        bot_row.addWidget(self.add_btn)
        t_lay.addLayout(bot_row)

        card_lay.addWidget(self.thumb_box)

        # 2. Filename Label below thumbnail
        name_lbl = QLabel(self.name, self)
        name_lbl.setStyleSheet("color: #f8fafc; font-size: 11px; font-weight: 700;")
        name_lbl.setWordWrap(False)
        fm = QtGui.QFontMetrics(name_lbl.font())
        elided = fm.elidedText(self.name, Qt.ElideMiddle, 130)
        name_lbl.setText(elided)
        name_lbl.setToolTip(f"{self.name}\nPath: {self.path}\nSize: {self.item_data.get('size_mb', 0):.1f} MB")
        card_lay.addWidget(name_lbl)

    def _on_add_clicked(self):
        self.set_added(True)
        self.add_requested.emit(self.path)

    def set_added(self, is_added: bool = True):
        self.item_data["status"] = "Added" if is_added else "Ready"
        if hasattr(self, 'st_lbl') and self.st_lbl:
            disp_st = "Added" if is_added else "Ready"
            st_color = "#34d399" if is_added else "#38bdf8"
            st_bg = "#064e3b" if is_added else "rgba(12, 42, 74, 0.9)"
            st_border = "#059669" if is_added else "#0284c7"
            self.st_lbl.setText(disp_st)
            self.st_lbl.setStyleSheet(f"""
                color: {st_color};
                background-color: {st_bg};
                border: 1px solid {st_border};
                font-size: 8px;
                font-weight: 800;
                padding: 1px 5px;
                border-radius: 3px;
            """)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.pos()
            self.clicked.emit(self.path)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_add_clicked()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.LeftButton) and self._drag_start_pos is not None:
            distance = (event.pos() - self._drag_start_pos).manhattanLength()
            if distance >= QApplication.startDragDistance():
                drag = QtGui.QDrag(self)
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(self.path)])
                mime.setText(self.path)
                mime.setData("application/x-mediabin-clip", self.path.encode('utf-8'))
                drag.setMimeData(mime)

                # Modern Drag Preview
                pixmap = QtGui.QPixmap(150, 40)
                pixmap.fill(QtGui.QColor(0, 0, 0, 0))
                p = QtGui.QPainter(pixmap)
                p.setRenderHint(QtGui.QPainter.Antialiasing)
                p.setBrush(QtGui.QColor(15, 23, 42, 230))
                p.setPen(QtGui.QPen(QtGui.QColor("#00f2fe"), 1.5))
                p.drawRoundedRect(0, 0, 148, 38, 6, 6)
                p.setPen(QtGui.QColor("#f8fafc"))
                p.setFont(QtGui.QFont("Inter", 9, QtGui.QFont.Bold))
                disp = self.name if len(self.name) <= 16 else self.name[:13] + "..."
                p.drawText(10, 24, f"🎬 {disp}")
                p.end()

                drag.setPixmap(pixmap)
                drag.setHotSpot(QtCore.QPoint(75, 20))
                self._drag_start_pos = None
                drag.exec_(Qt.CopyAction | Qt.MoveAction)
                return
        super().mouseMoveEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0b1120;
                color: #f8fafc;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 18px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #00f2fe;
                color: #000000;
                font-weight: 700;
            }
        """)
        act_add_tl = menu.addAction("➕ បញ្ចូលទៅក្នុង Timeline (Add to Track)")
        act_open = menu.addAction("▶ បើកក្នុង Studio Player (Play Preview)")
        menu.addSeparator()
        act_reveal = menu.addAction("📂 បើកទីតាំង File (Reveal in Finder)")
        menu.addSeparator()
        act_remove = menu.addAction("🗑️ ដកចេញពី Media Bin (Remove)")

        action = menu.exec(event.globalPos())
        if action == act_add_tl:
            self.add_requested.emit(self.path)
        elif action == act_open:
            self.double_clicked.emit(self.path)
        elif action == act_reveal:
            if os.path.exists(self.path):
                subprocess.run(["open", "-R", self.path], check=False)
        elif action == act_remove:
            self.delete_requested.emit(self.index)

    def set_selected(self, selected: bool):
        self._is_selected = selected
        if selected:
            self.setStyleSheet("""
                QFrame#capcutMediaCard {
                    background-color: #14223d;
                    border: 2px solid #00f2fe;
                    border-radius: 8px;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#capcutMediaCard {
                    background-color: #101522;
                    border: 1px solid #1a2336;
                    border-radius: 8px;
                }
                QFrame#capcutMediaCard:hover {
                    border-color: #00f2fe;
                    background-color: #141c2e;
                }
            """)


class MediaBinTableWidget(QTableWidget):
    """Table widget supporting drag & drop of media clips to the timeline."""
    def __init__(self, parent_bin=None, parent=None):
        super().__init__(parent)
        self.parent_bin = parent_bin
        self.setDragEnabled(True)
        self._drag_start_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.LeftButton) and self._drag_start_pos is not None:
            distance = (event.pos() - self._drag_start_pos).manhattanLength()
            if distance >= QApplication.startDragDistance():
                row = self.rowAt(self._drag_start_pos.y())
                if self.parent_bin and hasattr(self.parent_bin, 'items') and 0 <= row < len(self.parent_bin.items):
                    item_data = self.parent_bin.items[row]
                    fpath = item_data.get("path")
                    clip_name = item_data.get("name", "Clip")

                    if fpath and os.path.exists(fpath):
                        drag = QtGui.QDrag(self)
                        mime = QMimeData()
                        mime.setUrls([QUrl.fromLocalFile(fpath)])
                        mime.setText(fpath)
                        mime.setData("application/x-mediabin-clip", fpath.encode('utf-8'))
                        drag.setMimeData(mime)

                        # Create modern drag preview card
                        pixmap = QtGui.QPixmap(160, 36)
                        pixmap.fill(QtGui.QColor(0, 0, 0, 0))
                        painter = QtGui.QPainter(pixmap)
                        painter.setRenderHint(QtGui.QPainter.Antialiasing)
                        painter.setBrush(QtGui.QColor(15, 23, 42, 230))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#00f2fe"), 1.5))
                        painter.drawRoundedRect(0, 0, 158, 34, 6, 6)
                        painter.setPen(QtGui.QColor("#f8fafc"))
                        font = QtGui.QFont("Inter", 9, QtGui.QFont.Bold)
                        painter.setFont(font)
                        disp = clip_name if len(clip_name) <= 18 else clip_name[:15] + "..."
                        painter.drawText(8, 22, f"🎬 {disp}")
                        painter.end()

                        drag.setPixmap(pixmap)
                        drag.setHotSpot(QtCore.QPoint(80, 18))
                        self._drag_start_pos = None
                        drag.exec_(Qt.CopyAction | Qt.MoveAction)
                        return
        super().mouseMoveEvent(event)


class MediaBinWidget(QWidget):
    video_selected = Signal(str)           # Emitted when user selects a video to edit in Studio
    combine_all_requested = Signal(list)   # Emitted when user wants to merge all clips into 1 sequence
    add_to_timeline_requested = Signal(str)# Emitted when user wants to insert clip into timeline
    start_batch_requested = Signal(list)   # Emitted when user clicks 'Start Batch' with list of video paths
    stop_batch_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: List[Dict] = []
        self.is_batch_running = False
        self.view_mode = "grid"
        self._init_ui()

    def _init_ui(self):
        self.setAcceptDrops(True)
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(6, 6, 6, 6)
        main_lay.setSpacing(6)

        # ==================== HEADER TOOLBAR ====================
        header_frame = QFrame(self)
        header_frame.setObjectName("mediaBinHeader")
        header_frame.setStyleSheet("""
            QFrame#mediaBinHeader {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0f172a, stop:1 #090e17);
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 4px;
            }
        """)
        h_vbox = QVBoxLayout(header_frame)
        h_vbox.setContentsMargins(6, 6, 6, 6)
        h_vbox.setSpacing(6)

        # Row 1: Title, Badge, and Primary Import Button (CapCut Cyan)
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        icon_lbl = QLabel("🎬", self)
        icon_lbl.setStyleSheet("font-size: 14px;")
        row1.addWidget(icon_lbl)

        self.title_lbl = QLabel("Media", self)
        self.title_lbl.setStyleSheet("font-weight: 800; font-size: 12px; color: #f8fafc;")
        row1.addWidget(self.title_lbl)

        self.badge_lbl = QLabel("0", self)
        self.badge_lbl.setStyleSheet("""
            QLabel {
                background-color: #0c2a4a;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 8px;
                padding: 1px 6px;
                font-size: 10px;
                font-weight: bold;
            }
        """)
        row1.addWidget(self.badge_lbl)
        row1.addStretch()

        self.add_btn = QPushButton("+ Import", self)
        self.add_btn.setToolTip("ជ្រើសរើសវីដេអូដើម្បីបញ្ចូលក្នុង Media Bin (Import Videos)")
        self.add_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #00f2fe;
                border: 1px solid #0284c7;
                border-radius: 5px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: #0c2a4a;
                color: #ffffff;
                border-color: #00f2fe;
            }
        """)
        self.add_btn.clicked.connect(self._on_add_videos_clicked)
        row1.addWidget(self.add_btn)
        h_vbox.addLayout(row1)

        # Row 2: Secondary Actions (Merge, Dub, View Switch, Clear)
        row2 = QHBoxLayout()
        row2.setSpacing(4)

        self.combine_btn = QPushButton("🔗 Merge", self)
        self.combine_btn.setToolTip("ភ្ជាប់វីដេអូទាំងអស់ក្នុង Media Bin ចូលគ្នាជាវីដេអូតែមួយលើ Timeline (Merge Sequence)")
        self.combine_btn.setStyleSheet("""
            QPushButton {
                background: #4338ca;
                color: #ffffff;
                border: 1px solid #6366f1;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #4f46e5;
            }
        """)
        self.combine_btn.clicked.connect(self._on_combine_all_clicked)
        row2.addWidget(self.combine_btn)

        self.batch_btn = QPushButton("⚡ Dub", self)
        self.batch_btn.setToolTip("ដំណើរការបញ្ចូលសំឡេង AI គ្រប់វីដេអូទាំងអស់តាមលំដាប់ (Batch Processing)")
        self.batch_btn.setStyleSheet("""
            QPushButton {
                background: #059669;
                color: #ffffff;
                border: 1px solid #10b981;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #10b981;
            }
        """)
        self.batch_btn.clicked.connect(self._on_batch_btn_clicked)
        row2.addWidget(self.batch_btn)

        row2.addStretch()

        # View Mode Toggle: Grid vs List (CapCut Signature)
        self.grid_btn = QPushButton(self)
        self.grid_btn.setCheckable(True)
        self.grid_btn.setChecked(True)
        self.grid_btn.setIcon(get_svg_icon("grid", "#00f2fe", 13))
        self.grid_btn.setIconSize(QtCore.QSize(13, 13))
        self.grid_btn.setToolTip("Grid View (ទិដ្ឋភាពក្រឡា CapCut)")
        self.grid_btn.setFixedSize(26, 24)
        self.grid_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155; border-radius: 4px;
            }
            QPushButton:checked {
                background-color: #0c2a4a; border-color: #0284c7;
            }
        """)
        self.grid_btn.clicked.connect(self._set_grid_view)
        row2.addWidget(self.grid_btn)

        self.list_btn = QPushButton(self)
        self.list_btn.setCheckable(True)
        self.list_btn.setChecked(False)
        self.list_btn.setIcon(get_svg_icon("list", "#94a3b8", 13))
        self.list_btn.setIconSize(QtCore.QSize(13, 13))
        self.list_btn.setToolTip("List View (ទិដ្ឋភាពបញ្ជី)")
        self.list_btn.setFixedSize(26, 24)
        self.list_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155; border-radius: 4px;
            }
            QPushButton:checked {
                background-color: #0c2a4a; border-color: #0284c7;
            }
        """)
        self.list_btn.clicked.connect(self._set_list_view)
        row2.addWidget(self.list_btn)

        self.clear_btn = QPushButton(self)
        self.clear_btn.setIcon(get_svg_icon("trash", "#94a3b8", 13))
        self.clear_btn.setIconSize(QtCore.QSize(13, 13))
        self.clear_btn.setToolTip("សម្អាត Media Bin ទាំងអស់ (Clear)")
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #111827;
                border: 1px solid #1f2937;
                border-radius: 4px;
            }
            QPushButton:hover {
                border-color: #ef4444;
                background-color: #1f1418;
            }
        """)
        self.clear_btn.setFixedSize(26, 24)
        self.clear_btn.clicked.connect(self.clear_all)
        row2.addWidget(self.clear_btn)

        h_vbox.addLayout(row2)

        main_lay.addWidget(header_frame)

        # ==================== STACK (Grid vs List vs Empty State) ====================
        content_container = QWidget(self)
        self.stack_lay = QStackedLayout(content_container)
        self.stack_lay.setContentsMargins(0, 0, 0, 0)

        # Page 0: Empty State
        self.empty_widget = QWidget(content_container)
        empty_box = QVBoxLayout(self.empty_widget)
        empty_box.setAlignment(Qt.AlignCenter)
        empty_box.setSpacing(10)

        empty_icon = QLabel("🎬", self.empty_widget)
        empty_icon.setStyleSheet("font-size: 38px; color: #334155;")
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(empty_icon)

        empty_title = QLabel("គ្មានវីដេអូក្នុង Media Bin (No Media Clips)", self.empty_widget)
        empty_title.setStyleSheet("color: #94a3b8; font-size: 13px; font-weight: bold;")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(empty_title)

        empty_desc = QLabel(
            "ទម្លាក់វីដេអូច្រើន (Drag & Drop Clips) នៅទីនេះ\n"
            "ឬចុចប៊ូតុង '+ Import' ខាងលើ ដើម្បីជ្រើសរើសវីដេអូ",
            self.empty_widget
        )
        empty_desc.setStyleSheet("color: #475569; font-size: 11px; line-height: 1.4;")
        empty_desc.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(empty_desc)

        empty_import_btn = QPushButton("📁 ជ្រើសរើសវីដេអូ (Select Files...)", self.empty_widget)
        empty_import_btn.setFixedWidth(190)
        empty_import_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #00f2fe;
                border: 1px dashed #0284c7;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0284c7;
                color: #ffffff;
                border-style: solid;
            }
        """)
        empty_import_btn.clicked.connect(self._on_add_videos_clicked)
        empty_box.addWidget(empty_import_btn, 0, Qt.AlignCenter)

        self.stack_lay.addWidget(self.empty_widget)

        # Page 1: CapCut Gallery Grid View
        self.gallery_scroll = QScrollArea(content_container)
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollBar:vertical {
                background-color: #0b101c;
                width: 8px;
                margin: 0px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background-color: #1e293b;
                min-height: 20px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #00f2fe;
            }
        """)
        self.gallery_widget = QWidget()
        self.gallery_widget.setStyleSheet("background: transparent;")
        self.gallery_layout = QGridLayout(self.gallery_widget)
        self.gallery_layout.setContentsMargins(4, 4, 4, 4)
        self.gallery_layout.setSpacing(8)
        self.gallery_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.gallery_scroll.setWidget(self.gallery_widget)
        self.stack_lay.addWidget(self.gallery_scroll)

        # Page 2: CapCut List Table View
        self.table = MediaBinTableWidget(self, content_container)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["#", "🎬 Media Clips", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 28)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 100)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #070d1a;
                border: 1px solid #161e32;
                border-radius: 8px;
                color: #e2e8f0;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 4px 6px;
                border-bottom: 1px solid #0e172a;
            }
            QTableWidget::item:hover {
                background-color: #0f1c36;
            }
            QTableWidget::item:selected {
                background-color: #1e293b;
                color: #00f2fe;
            }
            QHeaderView::section {
                background-color: #0c1322;
                color: #94a3b8;
                font-weight: bold;
                border: none;
                border-bottom: 1px solid #1e293b;
                padding: 6px 8px;
                font-size: 10px;
                text-transform: uppercase;
            }
        """)
        self.table.itemClicked.connect(self._on_table_item_clicked)
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

        self.stack_lay.addWidget(self.table)
        main_lay.addWidget(content_container, 1)

        # Show empty state by default
        self.stack_lay.setCurrentIndex(0)

    def _set_grid_view(self):
        self.view_mode = "grid"
        self.grid_btn.setChecked(True)
        self.list_btn.setChecked(False)
        self.grid_btn.setIcon(get_svg_icon("grid", "#00f2fe", 13))
        self.list_btn.setIcon(get_svg_icon("list", "#94a3b8", 13))
        self._refresh_table()

    def _set_list_view(self):
        self.view_mode = "list"
        self.grid_btn.setChecked(False)
        self.list_btn.setChecked(True)
        self.grid_btn.setIcon(get_svg_icon("grid", "#94a3b8", 13))
        self.list_btn.setIcon(get_svg_icon("list", "#00f2fe", 13))
        self._refresh_table()

    # ==================== DRAG & DROP SUPPORT ====================
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        valid_exts = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv"}
        added = []
        for url in event.mimeData().urls():
            fpath = url.toLocalFile()
            if os.path.isfile(fpath) and Path(fpath).suffix.lower() in valid_exts:
                added.append(fpath)
            elif os.path.isdir(fpath):
                for r, _, files in os.walk(fpath):
                    for fn in files:
                        p = os.path.join(r, fn)
                        if Path(p).suffix.lower() in valid_exts:
                            added.append(p)
        if added:
            self.add_videos(added, auto_load=False)
            event.acceptProposedAction()

    # ==================== ITEM MANAGEMENT ====================
    def add_videos(self, paths: List[str], auto_load: bool = False):
        """Add list of video paths to Media Bin with metadata and CapCut thumbnail."""
        from utils.file_utils import ensure_accessible_video_file, check_file_access, get_user_friendly_file_access_message
        existing_paths = {it["path"] for it in self.items}
        newly_added = []
        for p in paths:
            if not p:
                continue
            acc = check_file_access(p)
            if not acc.is_accessible:
                title, msg = get_user_friendly_file_access_message(acc)
                QMessageBox.warning(self, title, msg)
                continue

            abs_p = ensure_accessible_video_file(p)
            if abs_p not in existing_paths and os.path.exists(abs_p):
                name = os.path.basename(p)  # Display original friendly filename
                size_mb = os.path.getsize(abs_p) / (1024 * 1024)
                
                # Fast metadata extraction
                dur = 0.0
                width, height = 0, 0
                fps = 30.0
                try:
                    cap = cv2.VideoCapture(abs_p)
                    if cap.isOpened():
                        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                        dur = (frames / fps) if fps > 0 else 0.0
                        cap.release()
                except Exception:
                    pass

                if dur <= 0.0:
                    try:
                        from utils.ffmpeg import get_video_info
                        vinfo = get_video_info(abs_p)
                        dur = float(vinfo.get("duration", 0.0) or 0.0)
                        if width <= 0:
                            width = int(vinfo.get("width", 0) or 0)
                        if height <= 0:
                            height = int(vinfo.get("height", 0) or 0)
                    except Exception:
                        pass

                mins = int(dur // 60)
                secs = int(dur % 60)
                dur_str = f"{mins:02d}:{secs:02d}" if dur > 0 else "00:00"
                thumb_pix = extract_video_thumbnail(abs_p, target_w=76, target_h=46, dur_str=dur_str)

                self.items.append({
                    "path": abs_p,
                    "name": name,
                    "size_mb": size_mb,
                    "duration": dur,
                    "width": width,
                    "height": height,
                    "fps": round(fps, 1),
                    "status": "Ready",
                    "progress": 0,
                    "thumbnail": thumb_pix
                })
                newly_added.append(abs_p)
        self._refresh_table()

        # Instant Preview: Automatically load the first imported clip into the Studio Player & Timeline
        if auto_load and newly_added:
            self.video_selected.emit(newly_added[0])

    def _refresh_table(self):
        n = len(self.items)
        self.badge_lbl.setText(str(n))
        self.combine_btn.setEnabled(n > 0)
        self.batch_btn.setEnabled(n > 0)

        if n == 0:
            self.stack_lay.setCurrentIndex(0)
            return

        # 1. Update CapCut Gallery Grid View
        while self.gallery_layout.count():
            item = self.gallery_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for idx, it in enumerate(self.items):
            card = MediaCardWidget(it, idx, self)
            card.clicked.connect(self._on_card_clicked)
            card.double_clicked.connect(self._on_card_add_to_timeline)
            card.add_requested.connect(self._on_card_add_to_timeline)
            card.delete_requested.connect(self._remove_item)
            row = idx // 2
            col = idx % 2
            self.gallery_layout.addWidget(card, row, col)

        self.gallery_layout.setRowStretch(len(self.items) // 2 + 1, 1)

        # 2. Update Table View (List Mode)
        self.table.setRowCount(n)

        for row, it in enumerate(self.items):
            # 0. Index Badge
            idx_item = QTableWidgetItem(f"{row+1:02d}")
            idx_item.setTextAlignment(Qt.AlignCenter)
            idx_item.setForeground(QtGui.QColor("#64748b"))
            idx_item.setFont(QtGui.QFont("Menlo", 10, QtGui.QFont.Bold))
            self.table.setItem(row, 0, idx_item)

            # 1. CapCut Media Info Card Widget with Video Thumbnail & Analytics
            card_widget = QWidget()
            card_widget.setCursor(Qt.PointingHandCursor)
            card_widget.mousePressEvent = lambda _, p=it["path"]: self.video_selected.emit(p)
            card_lay = QHBoxLayout(card_widget)
            card_lay.setContentsMargins(4, 4, 4, 4)
            card_lay.setSpacing(10)
            card_lay.setAlignment(Qt.AlignVCenter)

            # Thumbnail Poster
            thumb_lbl = QLabel(card_widget)
            thumb_lbl.setFixedSize(76, 46)
            thumb_lbl.setCursor(Qt.PointingHandCursor)
            thumb_lbl.setStyleSheet("""
                QLabel {
                    background-color: #0b1120;
                    border: 1px solid #1e293b;
                    border-radius: 5px;
                }
            """)
            thumb_lbl.setAlignment(Qt.AlignCenter)
            
            dur_sec = it.get("duration", 0.0)
            mins = int(dur_sec // 60)
            secs = int(dur_sec % 60)
            dur_str = f"{mins:02d}:{secs:02d}" if dur_sec > 0 else "00:00"

            thumb_pix = it.get("thumbnail")
            if thumb_pix is None:
                thumb_pix = extract_video_thumbnail(it["path"], target_w=76, target_h=46, dur_str=dur_str)
                it["thumbnail"] = thumb_pix

            if thumb_pix and not thumb_pix.isNull():
                thumb_lbl.setPixmap(thumb_pix)
            else:
                thumb_lbl.setText("🎬\nVideo")
                thumb_lbl.setStyleSheet("color: #64748b; font-size: 10px; font-weight: bold; background: #0f172a; border-radius: 5px;")
            
            # Double-click or single-click thumbnail loads video
            thumb_lbl.mousePressEvent = lambda _, p=it["path"]: self.video_selected.emit(p)
            card_lay.addWidget(thumb_lbl)

            # Analytics Info Box
            info_box = QVBoxLayout()
            info_box.setContentsMargins(0, 0, 0, 0)
            info_box.setSpacing(4)
            info_box.setAlignment(Qt.AlignVCenter)

            name_lbl = QLabel(it["name"], card_widget)
            name_lbl.setStyleSheet("font-weight: 700; color: #f8fafc; font-size: 11px;")
            name_lbl.setToolTip(it["path"])
            info_box.addWidget(name_lbl)

            # Analytics & Status Badges Row
            pills_lay = QHBoxLayout()
            pills_lay.setContentsMargins(0, 0, 0, 0)
            pills_lay.setSpacing(6)

            # Status Pill inside card
            status = it.get("status", "Ready")
            disp_status = status
            st_color = "#38bdf8"
            st_bg = "#0c2a4a"
            st_border = "#0284c7"
            if "Error" in status or "Fail" in status:
                disp_status = "❌ Error"
                st_color = "#f87171"
                st_bg = "#450a0a"
                st_border = "#dc2626"
            elif "Complete" in status or "Done" in status:
                disp_status = "✓ Done"
                st_color = "#34d399"
                st_bg = "#064e3b"
                st_border = "#059669"
            elif "..." in status or "%" in status:
                disp_status = "⏳ Proc"
                st_color = "#a5b4fc"
                st_bg = "#1e1b4b"
                st_border = "#6366f1"
            else:
                disp_status = "● Ready"

            st_badge = QLabel(disp_status, card_widget)
            st_badge.setToolTip(f"Status: {status}")
            st_badge.setStyleSheet(f"""
                color: {st_color}; font-size: 9px; font-weight: 700;
                background: {st_bg}; padding: 2px 6px; border-radius: 4px;
                border: 1px solid {st_border};
            """)
            pills_lay.addWidget(st_badge)

            w = it.get("width", 0)
            h = it.get("height", 0)
            res_txt = f"{w}×{h}" if (w and h) else "HD"
            aspect_txt = ""
            if w > 0 and h > 0:
                if w < h:
                    aspect_txt = "9:16"
                elif w > h:
                    aspect_txt = "16:9"
            res_full = f"{res_txt} ({aspect_txt})" if aspect_txt else res_txt
            res_pill = QLabel(f"📐 {res_txt}", card_widget)
            res_pill.setToolTip(f"Resolution: {res_full}")
            res_pill.setStyleSheet("color: #38bdf8; font-size: 9.5px; font-weight: 600; background: #0c2a4a; padding: 2px 5px; border-radius: 4px; border: 1px solid #0284c7;")
            pills_lay.addWidget(res_pill)

            # FPS badge
            fps_val = it.get("fps", 30.0)
            fps_pill = QLabel(f"🎞 {fps_val:.0f} FPS", card_widget)
            fps_pill.setStyleSheet("color: #c084fc; font-size: 9px; font-weight: 600; background: #2e1065; padding: 2px 6px; border-radius: 4px; border: 1px solid #7e22ce;")
            pills_lay.addWidget(fps_pill)

            # Size badge
            size_pill = QLabel(f"💾 {it['size_mb']:.1f} MB", card_widget)
            size_pill.setStyleSheet("color: #cbd5e1; font-size: 9px; font-weight: 600; background: #1e293b; padding: 2px 6px; border-radius: 4px; border: 1px solid #334155;")
            pills_lay.addWidget(size_pill)

            pills_lay.addStretch()
            info_box.addLayout(pills_lay)

            card_lay.addLayout(info_box)
            self.table.setCellWidget(row, 1, card_widget)

            # 2. Action Buttons Widget (CapCut Compact Icon Buttons: ➕, ▶, 🗑)
            btn_frame = QWidget()
            b_lay = QHBoxLayout(btn_frame)
            b_lay.setContentsMargins(2, 2, 2, 2)
            b_lay.setSpacing(4)
            b_lay.setAlignment(Qt.AlignCenter)

            # Insert to Timeline Button
            add_tl_btn = QPushButton(btn_frame)
            add_tl_btn.setIcon(get_svg_icon("plus", "#c4b5fd", 13))
            add_tl_btn.setIconSize(QtCore.QSize(13, 13))
            add_tl_btn.setFixedSize(26, 26)
            add_tl_btn.setCursor(Qt.PointingHandCursor)
            add_tl_btn.setToolTip("បញ្ចូលទៅក្នុង Timeline (Add to Sequence)")
            add_tl_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1e1b4b;
                    border: 1px solid #6366f1; border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #4f46e5; border-color: #818cf8;
                }
            """)
            add_tl_btn.clicked.connect(lambda _, p=it["path"]: self.add_to_timeline_requested.emit(p))
            b_lay.addWidget(add_tl_btn)

            # Open Button
            load_btn = QPushButton(btn_frame)
            load_btn.setIcon(get_svg_icon("play", "#38bdf8", 12))
            load_btn.setIconSize(QtCore.QSize(12, 12))
            load_btn.setFixedSize(26, 26)
            load_btn.setCursor(Qt.PointingHandCursor)
            load_btn.setToolTip("បើកកាត់តក្នុង Studio Player (Open in Player)")
            load_btn.setStyleSheet("""
                QPushButton {
                    background-color: #0c2a4a;
                    border: 1px solid #0284c7; border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #0284c7; border-color: #38bdf8;
                }
            """)
            load_btn.clicked.connect(lambda _, p=it["path"]: self.video_selected.emit(p))
            b_lay.addWidget(load_btn)

            # Delete Button
            del_btn = QPushButton(btn_frame)
            del_btn.setIcon(get_svg_icon("trash", "#f87171", 13))
            del_btn.setIconSize(QtCore.QSize(13, 13))
            del_btn.setFixedSize(26, 26)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip("ដកចេញពី Media Bin (Remove)")
            del_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1e293b;
                    border: 1px solid #334155; border-radius: 4px;
                }
                QPushButton:hover {
                    border-color: #ef4444; background-color: #2a151b;
                }
            """)
            del_btn.clicked.connect(lambda _, r=row: self._remove_item(r))
            b_lay.addWidget(del_btn)

            self.table.setCellWidget(row, 2, btn_frame)
            self.table.setRowHeight(row, 54)

        # Show active view mode (Page 1: Grid, Page 2: List)
        if self.view_mode == "grid":
            self.stack_lay.setCurrentIndex(1)
        else:
            self.stack_lay.setCurrentIndex(2)

    def _on_card_clicked(self, path: str):
        for i in range(self.gallery_layout.count()):
            it = self.gallery_layout.itemAt(i)
            if it and it.widget() and isinstance(it.widget(), MediaCardWidget):
                it.widget().set_selected(it.widget().path == path)
        self.video_selected.emit(path)

    def _on_card_add_to_timeline(self, path: str):
        """Immediately insert the selected clip into the timeline sequence (CapCut-grade)."""
        self.add_to_timeline_requested.emit(path)

    def sync_timeline_clips(self, clips: list):
        """Sync 'Added' status badges with current clips present on timeline sequence."""
        active_paths = set()
        for c in (clips or []):
            cp = c.get("path")
            if cp:
                active_paths.add(os.path.abspath(cp))

        for it in self.items:
            p = os.path.abspath(it.get("path", ""))
            it["status"] = "Added" if p in active_paths else "Ready"

        for i in range(self.gallery_layout.count()):
            w = self.gallery_layout.itemAt(i)
            if w and w.widget() and isinstance(w.widget(), MediaCardWidget):
                card = w.widget()
                cp = os.path.abspath(card.path)
                card.set_added(cp in active_paths)

    def _remove_item(self, row_idx: int):
        if 0 <= row_idx < len(self.items):
            self.items.pop(row_idx)
            self._refresh_table()

    def clear_all(self):
        if self.is_batch_running:
            QMessageBox.warning(self, "កំពុងដំណើរការ", "សូមរង់ចាំដំណើរការ Batch ឬចុច Cancel ជាមុនសិន។")
            return
        self.items.clear()
        self._refresh_table()

    def update_item_status(self, video_path: str, status: str, progress: int = 0):
        for it in self.items:
            if it["path"] == video_path:
                it["status"] = status
                it["progress"] = progress
                break
        self._refresh_table()

    def _on_add_videos_clicked(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Video Files for Media Bin",
            "",
            "Video Files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All Files (*)"
        )
        if files:
            self.add_videos(files, auto_load=False)
            if hasattr(self, 'video_selected') and files:
                self.video_selected.emit(files[0])

    def _on_combine_all_clicked(self):
        if not self.items:
            QMessageBox.information(self, "Media Bin Empty", "គ្មានវីដេអូដើម្បីភ្ជាប់ទេ។ សូមបញ្ចូលវីដេអូចូលទៅក្នុង Media Bin ជាមុនសិន។")
            return
        paths = [it["path"] for it in self.items]
        self.combine_all_requested.emit(paths)

    def _on_table_item_clicked(self, item):
        if not item:
            return
        row = item.row()
        if 0 <= row < len(self.items):
            self.video_selected.emit(self.items[row]["path"])

    def _on_table_cell_clicked(self, row: int, col: int):
        if 0 <= row < len(self.items) and col != 2:
            self.video_selected.emit(self.items[row]["path"])

    def _on_item_double_clicked(self, item):
        row = item.row()
        if 0 <= row < len(self.items):
            self.video_selected.emit(self.items[row]["path"])

    def _show_context_menu(self, pos):
        item = self.table.itemAt(pos)
        row = item.row() if item else self.table.currentRow()
        if row < 0 or row >= len(self.items):
            return

        it = self.items[row]
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0b1120;
                color: #f8fafc;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 18px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #2563eb;
                color: #ffffff;
            }
        """)

        act_open = menu.addAction("▶ បើកក្នុង Studio Player")
        act_add_tl = menu.addAction("➕ បញ្ចូលទៅក្នុង Timeline")
        menu.addSeparator()
        act_combine = menu.addAction("🔗 ភ្ជាប់វីដេអូទាំងអស់ (Combine All Sequences)")
        act_reveal = menu.addAction("📂 បើកទីតាំង File (Reveal in Finder)")
        menu.addSeparator()
        act_remove = menu.addAction("🗑️ ដកចេញពី Media Bin")

        action = menu.exec(self.table.mapToGlobal(pos))
        if action == act_open:
            self.video_selected.emit(it["path"])
        elif action == act_add_tl:
            self.add_to_timeline_requested.emit(it["path"])
        elif action == act_combine:
            self._on_combine_all_clicked()
        elif action == act_reveal:
            fpath = it["path"]
            if os.path.exists(fpath):
                subprocess.run(["open", "-R", fpath], check=False)
        elif action == act_remove:
            self._remove_item(row)

    def _on_batch_btn_clicked(self):
        if self.is_batch_running:
            self.stop_batch_requested.emit()
            return

        if not self.items:
            QMessageBox.information(self, "Media Bin Empty", "សូមបន្ថែម File វីដេអូចូលទៅក្នុង Media Bin ជាមុនសិន។")
            return

        self.start_batch_requested.emit([it["path"] for it in self.items if "Done" not in it.get("status", "")])

    def set_batch_running(self, running: bool):
        self.is_batch_running = running
        if running:
            self.batch_btn.setText("⏹ Stop Batch")
            self.batch_btn.setStyleSheet("""
                QPushButton {
                    background: #991b1b; color: #ffffff;
                    border: 1px solid #ef4444; border-radius: 6px;
                    padding: 4px 12px; font-size: 11px; font-weight: 800;
                }
            """)
        else:
            self.batch_btn.setText("⚡ Batch Dub")
            self.batch_btn.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #10b981);
                    color: #ffffff; border: 1px solid #34d399; border-radius: 6px;
                    padding: 4px 10px; font-size: 11px; font-weight: 700;
                }
            """)
