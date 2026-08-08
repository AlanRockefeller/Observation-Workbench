"""
Settings dialog: cache size, memory limit, prefetch radius, cache directory.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.storage.settings import AppSettings


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(420)
        self._settings = settings
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Cache group
        cache_group = QGroupBox("Disk Image Cache")
        cache_form = QFormLayout(cache_group)

        # Cache dir
        dir_row = QWidget()
        dir_layout = QHBoxLayout(dir_row)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        self._cache_dir_edit = QLineEdit()
        dir_layout.addWidget(self._cache_dir_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse_cache_dir)
        dir_layout.addWidget(browse_btn)
        cache_form.addRow("Cache directory:", dir_row)

        self._max_gb_spin = QDoubleSpinBox()
        self._max_gb_spin.setRange(0.1, 100.0)
        self._max_gb_spin.setSingleStep(0.5)
        self._max_gb_spin.setSuffix(" GB")
        self._max_gb_spin.setDecimals(1)
        cache_form.addRow("Max cache size:", self._max_gb_spin)

        layout.addWidget(cache_group)

        # Memory group
        mem_group = QGroupBox("Memory")
        mem_form = QFormLayout(mem_group)

        self._mem_mb_spin = QSpinBox()
        self._mem_mb_spin.setRange(64, 4096)
        self._mem_mb_spin.setSingleStep(64)
        self._mem_mb_spin.setSuffix(" MB")
        mem_form.addRow("In-memory image cache:", self._mem_mb_spin)

        self._prefetch_spin = QSpinBox()
        self._prefetch_spin.setRange(0, 10)
        self._prefetch_spin.setSuffix(" items")
        mem_form.addRow("Prefetch radius:", self._prefetch_spin)

        layout.addWidget(mem_group)

        # Display group
        display_group = QGroupBox("Display")
        display_form = QFormLayout(display_group)

        self._font_scale_spin = QDoubleSpinBox()
        self._font_scale_spin.setRange(0.5, 3.0)
        self._font_scale_spin.setSingleStep(0.1)
        self._font_scale_spin.setDecimals(1)
        self._font_scale_spin.setSuffix("×")
        self._font_scale_spin.setToolTip(
            "Scales all in-app text and widgets proportionally. "
            "1.0 = system default. Increase on HiDPI / 4K displays (try 1.5–2.0). "
            "Window title bar and decorations are controlled by the OS, not this setting."
        )
        display_form.addRow("UI scale:", self._font_scale_spin)

        self._result_list_scale_spin = QDoubleSpinBox()
        self._result_list_scale_spin.setRange(0.5, 4.0)
        self._result_list_scale_spin.setSingleStep(0.1)
        self._result_list_scale_spin.setDecimals(1)
        self._result_list_scale_spin.setSuffix("×")
        self._result_list_scale_spin.setToolTip(
            "Scales the text size in the left results pane independently of\n"
            "the global UI scale. 1.0 = same as the rest of the app."
        )
        display_form.addRow("Result list text scale:", self._result_list_scale_spin)

        self._scroll_speed_spin = QDoubleSpinBox()
        self._scroll_speed_spin.setRange(0.25, 10.0)
        self._scroll_speed_spin.setSingleStep(0.25)
        self._scroll_speed_spin.setDecimals(2)
        self._scroll_speed_spin.setSuffix("×")
        self._scroll_speed_spin.setToolTip(
            "Multiplies mouse-wheel scrolling inside the app. "
            "1.0 = Qt default. Increase this if WSL/Linux is not using "
            "your Windows mouse-wheel lines setting."
        )
        display_form.addRow("Scroll speed:", self._scroll_speed_spin)

        self._common_names_cb = QCheckBox()
        self._common_names_cb.setToolTip(
            "When checked, show common names alongside scientific names.\n"
            "When unchecked, show scientific names only (default)."
        )
        display_form.addRow("Include common names:", self._common_names_cb)
        layout.addWidget(display_group)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load(self) -> None:
        s = self._settings
        self._cache_dir_edit.setText(str(s.cache_dir))
        self._max_gb_spin.setValue(s.cache_max_gb)
        self._mem_mb_spin.setValue(s.memory_cache_max_mb)
        self._prefetch_spin.setValue(s.prefetch_radius)
        self._font_scale_spin.setValue(s.ui_font_scale)
        self._result_list_scale_spin.setValue(s.result_list_font_scale)
        self._scroll_speed_spin.setValue(s.scroll_speed_multiplier)
        self._common_names_cb.setChecked(s.show_common_names)

    def _save_and_accept(self) -> None:
        s = self._settings
        s.cache_dir = Path(self._cache_dir_edit.text().strip())
        s.cache_max_gb = self._max_gb_spin.value()
        s.memory_cache_max_mb = self._mem_mb_spin.value()
        s.prefetch_radius = self._prefetch_spin.value()
        s.ui_font_scale = self._font_scale_spin.value()
        s.result_list_font_scale = self._result_list_scale_spin.value()
        s.scroll_speed_multiplier = self._scroll_speed_spin.value()
        s.show_common_names = self._common_names_cb.isChecked()
        s.sync()
        self.accept()

    def _browse_cache_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select cache directory", self._cache_dir_edit.text()
        )
        if d:
            self._cache_dir_edit.setText(d)
