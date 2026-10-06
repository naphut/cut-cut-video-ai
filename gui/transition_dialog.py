"""
Transition Configuration Dialog (Phase 3 Gate 4 GUI)
Allows users to configure, preview, and apply video/audio transitions between timeline clips.
Provides real-time handle calculation and validation diagnostics.
"""
from qt_compat import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QPushButton, QGroupBox, QFormLayout,
    QtCore, QtGui, Qt
)
from core.timeline_model import Transition
from core.transition_engine import evaluate_transition_handles


class TransitionDialog(QDialog):
    """
    CapCut-style Modern Transition Dialog.
    Allows configuring:
      - Transition Type (Cross Dissolve, Fade to Black, Wipe Left/Right/Up/Down)
      - Duration (seconds)
      - Alignment (Center, Start on Cut, End on Cut)
      - Audio Crossfade Mode (Equal-Power, Linear, None)
      - Easing (Linear, Ease-In, Ease-Out, Ease-In-Out)
    Displays real-time handle diagnostics (VALID / CLAMPED / REJECTED).
    """
    def __init__(self, clip_a: dict, clip_b: dict, existing_transition: Transition = None, parent=None):
        super().__init__(parent)
        self.clip_a = clip_a
        self.clip_b = clip_b
        self.existing_transition = existing_transition
        self.applied_transition: Transition = None
        self.delete_requested = False

        self.setWindowTitle("🎬 Transition Studio (ការកំណត់ Transition)")
        self.setFixedSize(480, 460)
        self._init_ui()
        self._update_diagnostics()

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #0b0f19;
                color: #e2e8f0;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            }
            QGroupBox {
                border: 1px solid #1e293b;
                border-radius: 8px;
                margin-top: 12px;
                font-weight: bold;
                color: #94a3b8;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QLabel {
                color: #cbd5e1;
                font-size: 12px;
            }
            QComboBox, QDoubleSpinBox {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #f8fafc;
                padding: 5px 8px;
                font-size: 12px;
            }
            QComboBox:focus, QDoubleSpinBox:focus {
                border-color: #6366f1;
            }
            QPushButton {
                border-radius: 6px;
                padding: 7px 14px;
                font-weight: bold;
                font-size: 12px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Header info
        a_name = self.clip_a.get("name", "Clip A")
        b_name = self.clip_b.get("name", "Clip B")
        header = QLabel(f"⧓ <b>{a_name}</b> ➔ <b>{b_name}</b>")
        header.setStyleSheet("font-size: 14px; color: #a5b4fc;")
        layout.addWidget(header)

        # Settings Group
        form_group = QGroupBox("Transition Settings (ការកំណត់)")
        form_layout = QFormLayout(form_group)
        form_layout.setSpacing(8)

        # Type combo
        self.type_combo = QComboBox(self)
        self.type_combo.addItem("✨ Cross Dissolve (រលាយចូលគ្នា)", "cross_dissolve")
        self.type_combo.addItem("🌑 Fade to Black (ងងឹតខ្មៅ)", "fade_black")
        self.type_combo.addItem("⚪ Fade to White (ភ្លឺស)", "fade_white")
        self.type_combo.addItem("◀ Wipe Left (ជូតទៅឆ្វេង)", "wipe_left")
        self.type_combo.addItem("▶ Wipe Right (ជូតទៅស្តាំ)", "wipe_right")
        self.type_combo.addItem("▲ Wipe Up (ជូតទៅលើ)", "wipe_up")
        self.type_combo.addItem("▼ Wipe Down (ជូតទៅក្រោម)", "wipe_down")
        form_layout.addRow("Transition Type:", self.type_combo)

        # Duration spin
        self.dur_spin = QDoubleSpinBox(self)
        self.dur_spin.setRange(0.1, 5.0)
        self.dur_spin.setSingleStep(0.1)
        self.dur_spin.setValue(1.0)
        self.dur_spin.setSuffix(" s")
        self.dur_spin.valueChanged.connect(self._update_diagnostics)
        form_layout.addRow("Duration (រយៈពេល):", self.dur_spin)

        # Alignment combo
        self.align_combo = QComboBox(self)
        self.align_combo.addItem("Center on Cut (កណ្តាល)", "center")
        self.align_combo.addItem("Start on Cut (ចាប់ផ្តើមត្រង់កាត់)", "start_on_cut")
        self.align_combo.addItem("End on Cut (បញ្ចប់ត្រង់កាត់)", "end_on_cut")
        self.align_combo.currentIndexChanged.connect(self._update_diagnostics)
        form_layout.addRow("Alignment (តម្រឹម):", self.align_combo)

        # Audio Crossfade Mode
        self.audio_combo = QComboBox(self)
        self.audio_combo.addItem("🔊 Equal-Power (សំឡេងរលូន)", "equal_power")
        self.audio_combo.addItem("🔉 Linear Crossfade (លីនេអ៊ែរ)", "linear")
        self.audio_combo.addItem("🔇 Hard Cut / None (គ្មាន)", "none")
        form_layout.addRow("Audio Mode (សំឡេង):", self.audio_combo)

        # Easing combo
        self.easing_combo = QComboBox(self)
        self.easing_combo.addItem("Linear (ថេរ)", "linear")
        self.easing_combo.addItem("Ease In (យឺត -> លឿន)", "ease_in")
        self.easing_combo.addItem("Ease Out (លឿន -> យឺត)", "ease_out")
        self.easing_combo.addItem("Ease In-Out (យឺត -> លឿន -> យឺត)", "ease_in_out")
        form_layout.addRow("Easing Curve:", self.easing_combo)

        layout.addWidget(form_group)

        # Diagnostic Status Box
        diag_group = QGroupBox("Handle Diagnostics (សុពលភាព Handle)")
        diag_layout = QVBoxLayout(diag_group)
        self.diag_label = QLabel("Analyzing media handles...")
        self.diag_label.setWordWrap(True)
        self.diag_label.setStyleSheet("font-size: 11px; padding: 4px;")
        diag_layout.addWidget(self.diag_label)
        layout.addWidget(diag_group)

        # Populate from existing transition if present
        if self.existing_transition:
            # Set type
            idx = self.type_combo.findData(getattr(self.existing_transition, "type", "cross_dissolve"))
            if idx >= 0: self.type_combo.setCurrentIndex(idx)
            self.dur_spin.setValue(float(getattr(self.existing_transition, "duration", 1.0)))
            idx_a = self.align_combo.findData(getattr(self.existing_transition, "alignment", "center"))
            if idx_a >= 0: self.align_combo.setCurrentIndex(idx_a)
            idx_m = self.audio_combo.findData(getattr(self.existing_transition, "audio_mode", "equal_power"))
            if idx_m >= 0: self.audio_combo.setCurrentIndex(idx_m)
            idx_e = self.easing_combo.findData(getattr(self.existing_transition, "easing", "linear"))
            if idx_e >= 0: self.easing_combo.setCurrentIndex(idx_e)

        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        if self.existing_transition:
            self.del_btn = QPushButton("🗑️ Remove (លុប)", self)
            self.del_btn.setStyleSheet("""
                QPushButton {
                    background-color: #450a0a;
                    color: #fca5a5;
                    border: 1px solid #991b1b;
                }
                QPushButton:hover {
                    background-color: #7f1d1d;
                }
            """)
            self.del_btn.clicked.connect(self._on_delete_clicked)
            btn_layout.addWidget(self.del_btn)

        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #f8fafc;
            }
        """)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.apply_btn = QPushButton("✓ Apply Transition", self)
        self.apply_btn.setStyleSheet("""
            QPushButton {
                background-color: #4f46e5;
                color: #ffffff;
                border: 1px solid #6366f1;
            }
            QPushButton:hover {
                background-color: #4338ca;
            }
        """)
        self.apply_btn.clicked.connect(self._on_apply_clicked)
        btn_layout.addWidget(self.apply_btn)

        layout.addLayout(btn_layout)

    def _update_diagnostics(self):
        """Live evaluation of media handles using Gate 1 handle engine."""
        req_dur = self.dur_spin.value()
        align = self.align_combo.currentData() or "center"

        # Clip A details
        s_in_a = float(self.clip_a.get("source_in", 0.0))
        dur_a = float(self.clip_a.get("duration", 3.0))
        spd_a = float(self.clip_a.get("speed", 1.0))
        s_out_a = s_in_a + (dur_a * spd_a)
        med_dur_a = float(self.clip_a.get("media_file_duration", s_out_a + 2.0))

        # Clip B details
        s_in_b = float(self.clip_b.get("source_in", 0.0))
        spd_b = float(self.clip_b.get("speed", 1.0))

        avail = evaluate_transition_handles(
            media_file_duration_A=med_dur_a,
            source_out_A=s_out_a,
            speed_A=spd_a,
            source_in_B=s_in_b,
            speed_B=spd_b,
            requested_duration=req_dur,
            alignment=align
        )

        out_tl = avail.available_out_handle_A / spd_a
        in_tl = avail.available_in_handle_B / spd_b

        if avail.status == "REJECTED":
            self.diag_label.setText(
                f"❌ <b>REJECTED (មិនអាចដាក់បាន):</b> Insufficient handles.\n"
                f"Clip A out: {out_tl:.2f}s, Clip B in: {in_tl:.2f}s. Fallback: Hard cut."
            )
            self.diag_label.setStyleSheet("color: #f87171; font-size: 11px;")
            self.apply_btn.setEnabled(False)
        elif avail.status == "CLAMPED":
            self.diag_label.setText(
                f"⚠️ <b>CLAMPED:</b> Requested {req_dur:.2f}s exceeds available handles.\n"
                f"Effective duration: <b>{avail.allowed_duration:.2f}s</b> (A: {out_tl:.2f}s, B: {in_tl:.2f}s)."
            )
            self.diag_label.setStyleSheet("color: #fbbf24; font-size: 11px;")
            self.apply_btn.setEnabled(True)
        else:
            self.diag_label.setText(
                f"✅ <b>VALID:</b> Full handles available for {req_dur:.2f}s transition.\n"
                f"Clip A out-handle: {out_tl:.2f}s | Clip B in-handle: {in_tl:.2f}s."
            )
            self.diag_label.setStyleSheet("color: #4ade80; font-size: 11px;")
            self.apply_btn.setEnabled(True)

    def _on_apply_clicked(self):
        a_id = str(self.clip_a.get("id", "0"))
        b_id = str(self.clip_b.get("id", "1"))
        cut_time = float(self.clip_a.get("start", 0.0)) + float(self.clip_a.get("duration", 0.0))

        self.applied_transition = Transition(
            id=getattr(self.existing_transition, "id", f"trans_{a_id}_{b_id}"),
            clip_a_id=a_id,
            clip_b_id=b_id,
            cut_time=cut_time,
            duration=self.dur_spin.value(),
            alignment=self.align_combo.currentData(),
            type=self.type_combo.currentData(),
            easing=self.easing_combo.currentData(),
            audio_mode=self.audio_combo.currentData()
        )
        self.accept()

    def _on_delete_clicked(self):
        self.delete_requested = True
        self.accept()
