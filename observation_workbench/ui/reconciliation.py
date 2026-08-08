"""Reconciliation dashboard with reviewed Gate 1B and Gate 1C writes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QBuffer,
    QByteArray,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QImage,
    QImageReader,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QApplication,
    QLabel,
    QInputDialog,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.reconciliation.coordinator import ReconciliationCoordinator
from observation_workbench.reconciliation.actions import (
    simulate_link_repair_final_state,
)
from observation_workbench.reconciliation.consolidation import select_canonical
from observation_workbench.reconciliation.deletion import (
    DonorDeletionPreview,
)
from observation_workbench.reconciliation.db import REPAIRABLE_LINK_ISSUE_TYPES
from observation_workbench.reconciliation.mo_client import (
    ReconciliationCancelled,
    mo_login_of,
)
from observation_workbench.reconciliation.mo_parsing import (
    MO_LARGEST_FETCHABLE_SIZE,
    mo_image_page_url,
    mo_image_url,
    mo_observation_photo_count,
    mo_original_is_public,
)
from observation_workbench.reconciliation.photo_license import (
    normalized_pixel_fingerprint,
    pixel_fingerprint_distance,
)
from observation_workbench.reconciliation.types import (
    CoordinateActionOption,
    CoordinateComparisonPreview,
    CoordinateRecordSnapshot,
    ConsolidationMemberSnapshot,
    ConsolidationPreview,
    ITSActionOption,
    ITSComparisonPreview,
    ITSRecordSnapshot,
    LinkRepairOption,
    LinkRepairPreview,
    MediaIdentity,
    NameProposalCandidate,
    NameProposalPreview,
    ObservationCreationItem,
    ObservationCreationPreview,
    PhotoActionOption,
    PhotoComparisonPreview,
    PhotoIdentityPreview,
    PhotoRecordSnapshot,
    ReconciliationProfile,
    RemoteSite,
)

PAGE_SIZE = 250
CATEGORIES = (
    ("link_issues", "Link issues"),
    ("link_actions", "Sync action journal"),
    ("candidate_pairs", "Candidate pairs"),
    ("confirmed_links", "Confirmed links"),
    ("confirmed_conflicts", "Reciprocal links with metadata conflicts"),
    ("unpaired", "Unpaired / possibly missing"),
    ("same_site_duplicates", "Same-site duplicate candidates"),
    ("consolidation_history", "Consolidation history"),
    ("changed_deleted", "Changed / deleted records"),
    ("rejected_excluded", "Rejected / excluded pairs"),
    ("ignored_resolved", "Ignored/resolved issues"),
)

# Sort a category is switched INTO with, as (dashboard_rows column, descending).
# Candidate review is the one list a person works top-down, so it opens on the
# reconciliation score. Every other category projects NULL AS score, where
# "ORDER BY score DESC" collapses to row_key order and silently stops being
# newest-first — so each one must name a column it actually has.
DEFAULT_CATEGORY_SORT = ("updated_at", True)
CATEGORY_SORT = {"candidate_pairs": ("score", True)}

# Every worker part the coordinator emits scan progress for, in the order the
# scan reaches them, paired with wording a user can act on. A scan publishes no
# partial results — the dashboard only reloads once persistence finishes — so
# this table IS the entire signal that the run is alive and where it is.
#
# Parts that share a step number run CONCURRENTLY in separate pools (the two
# inventory reads, the two context fetches, the two pair hydrations), which is
# why the step number is not simply the row index: reporting "step 2 of 10"
# then "step 1 of 10" as the other pool reports in would read as going
# backwards.
SCAN_PHASES: tuple[tuple[str, int, str], ...] = (
    ("mo", 1, "Reading Mushroom Observer inventory"),
    ("inat", 1, "Reading iNaturalist inventory"),
    ("prepared", 2, "Comparing against stored records"),
    ("context_inat", 3, "Fetching linked iNaturalist records"),
    ("context_mo", 3, "Fetching linked Mushroom Observer records"),
    ("validation_input", 4, "Selecting reciprocally linked pairs"),
    ("validation_inat", 5, "Validating iNaturalist pair details"),
    ("validation_mo", 5, "Validating Mushroom Observer pair details"),
    ("plan", 6, "Matching pairs and planning updates"),
    ("persist", 7, "Saving results"),
)

# Sub-stages a phase may report (arriving as "mo:external_links"). The Mushroom
# Observer inventory phase is the reason these exist: for a large account it is
# ten-plus minutes of sequential requests across four distinct kinds of work,
# and naming only the phase left the display frozen for all of it.
SCAN_STAGE_LABELS = {
    "observations": "listing observations",
    "names": "resolving taxon names",
    "external_sites": "finding the iNaturalist site definition",
    "external_links": "reading iNaturalist links",
    "recover": "re-checking records missing from this scan",
    "deleted": "reading the deleted-observation feed",
}

# How much of a phase each of its stages accounts for, in the order the phase
# runs them. Needed because a phase's stages each report their own 0..N count,
# so a raw fraction RESTARTS at every stage boundary and a bar driven from it
# would freeze (it must never move backwards). Folding the stages into one
# cumulative 0..1 fraction is what lets the bar keep moving through the long
# Mushroom Observer stages.
#
# The weights are rough measurements of a large account, not guesses to
# re-derive: MO's link read dominates because it is one request per 100
# observations at >=5s spacing, while listing observations is a single page.
SCAN_STAGE_WEIGHTS: dict[str, tuple[tuple[str, float], ...]] = {
    "mo": (
        ("observations", 0.10),
        ("names", 0.35),
        ("external_sites", 0.02),
        ("recover", 0.08),
        ("external_links", 0.45),
    ),
    "inat": (("observations", 0.95), ("deleted", 0.05)),
}
SCAN_PHASE_LABELS = {part: label for part, _step, label in SCAN_PHASES}
SCAN_PHASE_STEPS = {part: step for part, step, _label in SCAN_PHASES}
SCAN_STEP_COUNT = max(step for _part, step, _label in SCAN_PHASES)
# Parts sharing each step, so a step's progress can be scored as its SLOWEST
# member: two concurrent pools both have to finish before the step is done.
SCAN_STEP_PARTS: dict[int, tuple[str, ...]] = {
    step: tuple(p for p, s, _ in SCAN_PHASES if s == step)
    for _part, step, _label in SCAN_PHASES
}


def _stage_fraction(base: str, stage: str, within: float) -> float:
    """Fold one stage's own 0..1 progress into its phase's overall 0..1."""
    weights = SCAN_STAGE_WEIGHTS.get(base)
    if not weights or not stage:
        return within
    consumed = 0.0
    for name, weight in weights:
        if name == stage:
            return min(1.0, consumed + weight * within)
        consumed += weight
    # An unrecognised stage must not rewind the phase, so hold where we are.
    return consumed


class LinkRepairPreviewDialog(QDialog):
    """Memory-only current/final-state preview with unchecked action choices."""

    def __init__(self, preview: LinkRepairPreview, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("Preview reciprocal-link repairs")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Exact records: MO {preview.mo_observation_id} ↔ iNaturalist "
            f"{preview.inat_observation_id}\n"
            "Nothing is selected by default. Every checked row is journaled and "
            "preflighted again immediately before its write."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        current = QPlainTextEdit()
        current.setReadOnly(True)
        current.setMaximumHeight(190)
        current.setPlainText(self._current_state_text(preview))
        layout.addWidget(current)
        self.table = QTableWidget(len(preview.options), 5)
        self.table.setHorizontalHeaderLabels(
            ("Run", "Site", "Action", "Current", "Proposed final")
        )
        self._checks: list[tuple[QCheckBox, LinkRepairOption]] = []
        for row_index, option in enumerate(preview.options):
            check = QCheckBox()
            check.setEnabled(option.enabled)
            check.setToolTip(option.disabled_reason)
            check.stateChanged.connect(self._update_aggregate)
            self.table.setCellWidget(row_index, 0, check)
            self.table.setItem(row_index, 1, QTableWidgetItem(option.site.value))
            label = option.description + (
                " (destructive)" if option.destructive else ""
            )
            if not option.enabled and option.disabled_reason:
                label += f" — blocked: {option.disabled_reason}"
            self.table.setItem(row_index, 2, QTableWidgetItem(label))
            self.table.setItem(
                row_index,
                3,
                QTableWidgetItem(
                    str(option.current_target_id or "missing / malformed")
                ),
            )
            self.table.setItem(
                row_index,
                4,
                QTableWidgetItem(
                    "row removed"
                    if option.desired_target_id is None
                    else f"canonical target {option.desired_target_id}"
                ),
            )
            self._checks.append((check, option))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)
        self.aggregate = QLabel(
            "Aggregate proposed final state: select actions to preview it."
        )
        self.aggregate.setWordWrap(True)
        layout.addWidget(self.aggregate)
        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Confirm selected actions…"
        )
        self.buttons.accepted.connect(self._confirm)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def selected_options(self) -> list[LinkRepairOption]:
        return [
            option
            for check, option in self._checks
            if check.isChecked() and option.enabled
        ]

    def _confirm(self) -> None:
        selected = self.selected_options()
        if not selected:
            QMessageBox.warning(
                self, "No actions selected", "Select at least one enabled action."
            )
            return
        exact_rows = [
            (item.site.value, item.remote_row_uuid or item.remote_row_id)
            for item in selected
            if item.remote_row_uuid or item.remote_row_id
        ]
        if len(exact_rows) != len(set(exact_rows)):
            QMessageBox.warning(
                self,
                "Choose one outcome per row",
                "Repair and removal are alternatives for an exact remote row. Select only one.",
            )
            return
        repaired_sites = [
            item.site.value
            for item in selected
            if item.action_type.value.endswith("_repair")
        ]
        if len(repaired_sites) != len(set(repaired_sites)):
            QMessageBox.warning(
                self,
                "One repaired survivor per site",
                "Repair at most one row on each site; explicitly remove other conflicting rows instead.",
            )
            return
        try:
            final_state = simulate_link_repair_final_state(self.preview, selected)
        except Exception as exc:
            QMessageBox.warning(self, "Unsafe aggregate final state", str(exc))
            return
        destructive = [item for item in selected if item.destructive]
        message = (
            f"Journal and execute {len(selected)} selected reciprocal-link action(s)?\n\n"
            "Each action will receive a fresh preflight and post-write verification.\n\n"
            + self._format_aggregate(final_state)
        )
        if destructive:
            message += f"\n\n{len(destructive)} selected action(s) repair or remove an existing remote row."
        if (
            QMessageBox.question(
                self,
                "Explicit remote-write confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()

    def _update_aggregate(self) -> None:
        selected = self.selected_options()
        if not selected:
            self.aggregate.setText(
                "Aggregate proposed final state: select actions to preview it."
            )
            return
        try:
            state = simulate_link_repair_final_state(
                self.preview, selected, enforce=False
            )
            text = self._format_aggregate(state)
            if self.preview.review_intent == "reciprocal":
                try:
                    simulate_link_repair_final_state(self.preview, selected)
                except Exception as exc:
                    text += f"\nSelection is not executable: {exc}"
            self.aggregate.setText(text)
        except Exception as exc:
            self.aggregate.setText(f"Selection is not executable: {exc}")

    @staticmethod
    def _format_aggregate(
        state: dict[RemoteSite, tuple[tuple[str, Optional[int]], ...]],
    ) -> str:
        def side(site: RemoteSite) -> str:
            values = state.get(site, ())
            if not values:
                return "none"
            return ", ".join(
                f"{parse_state} → {target or 'unknown'}"
                for parse_state, target in values
            )

        return (
            "Aggregate proposed final state:\n"
            f"• iNaturalist field values: {side(RemoteSite.INAT)}\n"
            f"• Mushroom Observer external links: {side(RemoteSite.MO)}"
        )

    @staticmethod
    def _current_state_text(preview: LinkRepairPreview) -> str:
        lines = ["Current iNaturalist Mushroom Observer URL values"]
        lines.extend(
            f"• row {row.row_uuid or row.row_id}: {row.display_value or '[empty]'} "
            f"({row.parse_state})"
            for row in preview.inat_rows
        )
        if not preview.inat_rows:
            lines.append("• none")
        lines.append("\nCurrent Mushroom Observer iNaturalist external links")
        lines.extend(
            f"• row {row.row_id}: {row.display_value or '[empty]'} ({row.parse_state})"
            for row in preview.mo_rows
        )
        if not preview.mo_rows:
            lines.append("• none")
        return "\n".join(lines)


class ITSComparisonDialog(QDialog):
    """Memory-only ITS comparison with exactly one selectable reviewed write."""

    def __init__(self, preview: ITSComparisonPreview, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("Compare ITS data")
        self.resize(980, 720)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Confirmed pair: MO {preview.mo_observation_id} ↔ iNaturalist "
            f"{preview.inat_observation_id}\nComparison states: "
            + ", ".join(value.replace("_", " ") for value in preview.states)
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        values = QPlainTextEdit()
        values.setReadOnly(True)
        values.setPlainText(self._comparison_text(preview))
        layout.addWidget(values, 1)

        self.table = QTableWidget(len(preview.options), 5)
        self.table.setHorizontalHeaderLabels(
            ("Run", "Destination", "Action", "Evidence", "Availability")
        )
        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._choices: list[tuple[QRadioButton, ITSActionOption]] = []
        for row_index, option in enumerate(preview.options):
            choice = QRadioButton()
            choice.setEnabled(option.enabled)
            choice.setToolTip(option.disabled_reason)
            self._buttons.addButton(choice)
            self.table.setCellWidget(row_index, 0, choice)
            self.table.setItem(
                row_index, 1, QTableWidgetItem(option.destination_site.value)
            )
            label = option.description + (
                " (destructive)" if option.destructive else ""
            )
            self.table.setItem(row_index, 2, QTableWidgetItem(label))
            if option.sequence_fingerprint:
                evidence = f"sequence fingerprint {option.sequence_fingerprint[:12]}…"
            elif option.normalized_accession:
                evidence = (
                    f"accession {option.archive or '?'}:{option.normalized_accession}"
                )
            else:
                # A removal/replacement of a non-empty invalid value: the exact
                # remote text is spelled out in the Action column and the panel.
                evidence = "invalid remote value (see Action and comparison panel)"
            self.table.setItem(row_index, 3, QTableWidgetItem(evidence))
            self.table.setItem(
                row_index,
                4,
                QTableWidgetItem("ready" if option.enabled else option.disabled_reason),
            )
            self._choices.append((choice, option))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)
        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Confirm one ITS action…"
        )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_option(self) -> Optional[ITSActionOption]:
        return next(
            (option for button, option in self._choices if button.isChecked()), None
        )

    def _confirm(self) -> None:
        option = self.selected_option()
        if option is None:
            QMessageBox.warning(
                self, "No action selected", "Select one enabled ITS action."
            )
            return
        message = (
            "Journal and execute this one individually reviewed ITS write?\n\n"
            f"{option.description}\n\n"
            "The source and destination will be reread before submission and the normalized "
            "destination state will be verified afterward."
        )
        if option.destructive:
            message += (
                "\n\nThis changes or removes a non-empty remote value. Confirm that this exact "
                "destination row is incorrect and the displayed source is authoritative."
            )
        if (
            QMessageBox.question(
                self,
                "Explicit ITS write confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()

    @classmethod
    def _comparison_text(cls, preview: ITSComparisonPreview) -> str:
        sections = ["iNaturalist ITS sequence and accession field values"]
        sections.extend(cls._record_text(item) for item in preview.inat_records)
        if not preview.inat_records:
            sections.append("• none")
        sections.append("\nMushroom Observer ITS sequence records")
        sections.extend(cls._record_text(item) for item in preview.mo_records)
        if not preview.mo_records:
            sections.append("• none")
        return "\n".join(sections)

    @staticmethod
    def _record_text(record: ITSRecordSnapshot) -> str:
        identity = record.remote_uuid or record.remote_id
        metadata = f"{record.label or 'ITS'}; state={record.validation_state}"
        public = "; ".join(f"{key}={value}" for key, value in record.public_metadata)
        if public:
            metadata += "; " + public
        if record.sequence_fingerprint:
            orientation = record.normalized_sequence
            return (
                f"• row {identity}: {metadata}; normalized length={len(orientation)}; "
                f"fingerprint={record.sequence_fingerprint[:16]}…\n"
                f"  Raw sequence (memory only): {record.raw_sequence}"
            )
        if record.normalized_accession and record.validation_state == "valid":
            return (
                f"• row {identity}: {metadata}; accession={record.normalized_accession}; "
                f"archive={record.archive or 'not specified'}"
            )
        # Any remaining nonempty value is invalid. Always show the exact remote
        # text (memory only) so a destructive removal/replacement is never blind.
        if record.raw_value or record.raw_sequence:
            return (
                f"• row {identity}: {metadata}; no valid normalized ITS value\n"
                f"  Exact remote value (memory only): {record.raw_value or record.raw_sequence}"
            )
        return f"• row {identity}: {metadata}; empty value"


class CoordinateComparisonDialog(QDialog):
    """Coordinate comparison (Mushroom Observer → iNaturalist).

    The preview snapshots carry only privacy state, availability, and accuracy —
    the raw latitude/longitude are stripped before the preview leaves the worker,
    and no coordinate-derived fingerprint is carried. Raw coordinates therefore
    remain in memory in the service only, and are never rendered, persisted,
    logged, copied to the clipboard, or included in a map URL by this dialog.
    """

    def __init__(self, preview: CoordinateComparisonPreview, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("Compare coordinates")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Confirmed pair: MO {preview.mo_observation_id} ↔ iNaturalist "
            f"{preview.inat_observation_id}\n"
            "Source is Mushroom Observer; the only destination is iNaturalist."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        states = QPlainTextEdit()
        states.setReadOnly(True)
        states.setPlainText(
            "Mushroom Observer source coordinate\n"
            + self._snapshot_text(preview.source)
            + "\n\niNaturalist destination coordinate\n"
            + self._snapshot_text(preview.destination)
        )
        layout.addWidget(states, 1)

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._choices: list[tuple[QRadioButton, CoordinateActionOption]] = []
        self.table = QTableWidget(len(preview.options), 4)
        self.table.setHorizontalHeaderLabels(
            ("Run", "Action", "Effect", "Availability")
        )
        for row_index, option in enumerate(preview.options):
            choice = QRadioButton()
            choice.setEnabled(option.enabled)
            choice.setToolTip(option.disabled_reason)
            self._buttons.addButton(choice)
            self.table.setCellWidget(row_index, 0, choice)
            self.table.setItem(row_index, 1, QTableWidgetItem(option.description))
            effect = "replaces existing" if option.replaces_data else "sets empty"
            if option.sends_nonpublic_source:
                effect += "; discloses a private/obscured source point"
            if option.broadens_visibility:
                effect += "; broadens destination visibility"
            if option.large_discrepancy:
                effect += "; large discrepancy"
            self.table.setItem(row_index, 2, QTableWidgetItem(effect))
            self.table.setItem(
                row_index,
                3,
                QTableWidgetItem("ready" if option.enabled else option.disabled_reason),
            )
            self._choices.append((choice, option))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)

        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Confirm one coordinate copy…"
        )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_option(self) -> Optional[CoordinateActionOption]:
        return next(
            (option for button, option in self._choices if button.isChecked()), None
        )

    def _confirm(self) -> None:
        option = self.selected_option()
        if option is None:
            QMessageBox.warning(
                self, "No action selected", "Select one enabled coordinate action."
            )
            return
        message = (
            "Journal and execute this one reviewed coordinate copy?\n\n"
            f"{option.description}\n\n"
            "The source coordinate is reread before submission and the destination is verified afterward."
        )
        if option.sends_nonpublic_source:
            message += (
                "\n\nThe source coordinate is private or obscured. This sends the exact source coordinate "
                "to iNaturalist; its public visibility there will remain private/obscured according to the "
                "matching geoprivacy setting. Confirm you intend to send the point to iNaturalist."
            )
        if option.broadens_visibility:
            message += (
                "\n\nThis makes the EXISTING iNaturalist coordinate more publicly visible: the proposed "
                f"geoprivacy ({option.proposed_privacy_state}) is less restrictive than the current "
                "destination setting. Confirm you intend to broaden its visibility."
            )
        if option.replaces_data:
            message += (
                "\n\nThis REPLACES an existing iNaturalist coordinate. Confirm the current destination "
                "coordinate is wrong and the Mushroom Observer source is authoritative."
            )
        if (
            QMessageBox.question(
                self,
                "Explicit coordinate write confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()

    @staticmethod
    def _snapshot_text(snapshot: CoordinateRecordSnapshot) -> str:
        if snapshot.exact_point_unreadable:
            return (
                f"• existing coordinate whose exact point is unreadable here (privacy: "
                f"{snapshot.privacy_state}); automated replacement is blocked"
            )
        if not snapshot.coordinates_available:
            return f"• no coordinate present (privacy: {snapshot.privacy_state})"
        accuracy = (
            f"{snapshot.accuracy_m:.0f} m"
            if snapshot.accuracy_m is not None
            else "unspecified"
        )
        return (
            f"• available; privacy: {snapshot.privacy_state}; accuracy: {accuracy}\n"
            "  (raw coordinates are intentionally not shown or linked to an external map)"
        )


_PHOTO_THUMBNAIL_SIZE = 88
_PHOTO_PREVIEW_SIZE = 340


class _ClickableImageLabel(QLabel):
    """A thumbnail label that reports plain and double clicks."""

    clicked = Signal()
    doubleClicked = Signal()

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class _PhotoThumbnailSignals(QObject):
    done = Signal(str, object, bool)  # key, QImage or None, byte_fingerprint_mismatch


class _PhotoThumbnailWorker(QRunnable):
    """Downloads one image rendition off the UI thread.

    ``QRunnable`` is not a ``QObject``: the owning dialog must keep
    ``self.signals`` alive in a set until the ``done`` signal is handled, or
    Python may garbage-collect the worker (and its signals) before the queued
    delivery reaches the main thread.

    Round-5 smaller issue: when ``expected_byte_fingerprint`` is supplied
    (Gate 2A's creation dialog only — the service already downloaded and
    pinned this exact fingerprint during ``prepare_preview``), the raw
    bytes this SEPARATE thumbnail download fetches are hashed here, off the
    UI thread, and compared before the bytes themselves are discarded — the
    full image bytes are never retained beyond this method, only the
    resulting ``QImage`` and a boolean mismatch flag cross back to the
    dialog. This closes the gap between "what the service pinned as
    reviewed" and "what the dialog actually shows the user": without it, a
    changed image would still be caught before upload (item mint re-checks
    independently), but the dialog could show the user bytes that were
    never proven to be the ones actually pinned.
    """

    def __init__(
        self,
        key: str,
        url: str,
        download_image,
        is_cancelled,
        expected_byte_fingerprint: str = "",
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._key = key
        self._url = url
        self._download_image = download_image
        self._is_cancelled = is_cancelled
        self._expected_byte_fingerprint = expected_byte_fingerprint
        self.signals = _PhotoThumbnailSignals()

    def run(self) -> None:
        image: Optional[QImage] = None
        mismatch = False
        if not self._is_cancelled():
            try:
                data = self._download_image(self._url)
            except Exception:
                data = None
            if data:
                if self._expected_byte_fingerprint:
                    from observation_workbench.reconciliation.photo_license import (
                        photo_byte_fingerprint,
                    )

                    mismatch = (
                        photo_byte_fingerprint(data) != self._expected_byte_fingerprint
                    )
                candidate = QImage()
                if candidate.loadFromData(data):
                    image = candidate
        try:
            self.signals.done.emit(self._key, image, mismatch)
        except RuntimeError:
            pass  # The dialog's C++ side is already gone; nothing left to update.


PHOTO_CLOSE_DISTANCE = 8
PHOTO_PREFETCH_ROWS = 4
# How long a just-made confirm/reject decision stays reversible in one click.
PAIR_UNDO_SECONDS = 30
# dHash width, from normalized_pixel_fingerprint's 9x8 downscale.
_PHOTO_FINGERPRINT_BITS = 64
# Must stay <= the Photo preview column width set in _category_changed.
_PHOTO_MOSAIC_WIDTH = 186


@dataclass
class _PhotoPrefetchToken:
    cancelled: bool = False


@dataclass(frozen=True)
class _HashedIdentityPhoto:
    photo: PhotoRecordSnapshot
    fingerprint: str
    image: QImage


@dataclass(frozen=True)
class _CandidatePhotoAnalysis:
    pair_id: int
    mo_observation_id: int
    inat_observation_id: int
    mo_photos: tuple[_HashedIdentityPhoto, ...]
    inat_photos: tuple[_HashedIdentityPhoto, ...]
    # MO index, iNaturalist index, Hamming distance. Pairs are unique on both
    # sides and sorted from closest to least similar.
    matches: tuple[tuple[int, int, int], ...]
    mo_total: int
    inat_total: int
    failures: tuple[str, ...] = ()

    @property
    def close_matches(self) -> tuple[tuple[int, int, int], ...]:
        return tuple(
            match for match in self.matches if match[2] <= PHOTO_CLOSE_DISTANCE
        )

    @property
    def denominator(self) -> int:
        return max(self.mo_total, self.inat_total)

    @property
    def ready_for_quick_review(self) -> bool:
        return (
            not self.failures
            and self.mo_total > 0
            and self.inat_total > 0
            and len(self.mo_photos) == self.mo_total
            and len(self.inat_photos) == self.inat_total
        )


class _CandidatePhotoAnalysisSignals(QObject):
    finished = Signal(int, int, object)  # generation, pair_id, analysis/error


# Emitted instead of an analysis when the worker stood down for a foreground
# operation. The row is put back in the queue rather than left "loading…".
_PHOTO_ANALYSIS_DEFERRED = {"deferred": True}
# Emitted instead of an analysis when the batch was cancelled. It carries no
# result and the window does nothing with it -- its only job is to make every
# worker report EXACTLY ONCE, which is what releases the window's reference to
# that worker's signals object. A worker that returns silently strands it.
_PHOTO_ANALYSIS_CANCELLED = {"cancelled": True}


class _CandidatePhotoAnalysisWorker(QRunnable):
    """Read and hash one candidate's complete photo sets off the GUI thread.

    This is the one photo path that is not user-initiated, so it must be the
    one that yields. It goes straight to the photo service rather than through
    :class:`ReconciliationCoordinator`, and so is not covered by the
    ``_scan is not None or _action_running`` guard every explicit action
    respects; without ``_stand_down`` a batch launched a moment before the user
    starts a scan would keep competing for ``MOClient``'s request lock and the
    shared iNaturalist rate limiter, slowing the operation the user is actually
    waiting on. It therefore re-checks between every remote read and abandons
    its work rather than finishing it.
    """

    def __init__(
        self,
        coordinator: ReconciliationCoordinator,
        profile_id: int,
        pair_id: int,
        generation: int,
        token: _PhotoPrefetchToken,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.coordinator = coordinator
        self.profile_id = profile_id
        self.pair_id = pair_id
        self.generation = generation
        self.token = token
        self.signals = _CandidatePhotoAnalysisSignals()

    def _stand_down(self) -> bool:
        return self.coordinator.foreground_operation_running

    def _stop(self) -> bool:
        return self.token.cancelled or self._stand_down()

    def run(self) -> None:
        try:
            if self.token.cancelled:
                # Queued before the batch was cancelled and started anyway --
                # the queue is deliberately not cleared; see
                # _cancel_candidate_photo_prefetch. Do no work, but still
                # report.
                result: object = _PHOTO_ANALYSIS_CANCELLED
            else:
                result = self._analyze()
        except Exception as exc:
            # A stand-down surfaces here as the service's own cancellation, and
            # must not be reported to the operator as a failed analysis.
            result = (
                _PHOTO_ANALYSIS_CANCELLED
                if self.token.cancelled
                else (
                    _PHOTO_ANALYSIS_DEFERRED
                    if self._stand_down()
                    else {"error": str(exc)}
                )
            )
        finally:
            self.coordinator.db.close_thread_connection()
        # ALWAYS emit, even when the result is worthless. This signal is the
        # only thing that releases the window's reference to self.signals, so a
        # worker that returns silently leaks that object -- and the connection
        # to a bound method of the window that it holds -- for the window's
        # whole lifetime. The window discards stale generations on arrival.
        if result is None or self.token.cancelled:
            result = _PHOTO_ANALYSIS_CANCELLED
        try:
            self.signals.finished.emit(self.generation, self.pair_id, result)
        except RuntimeError:
            pass

    def _analyze(self) -> object:
        """Return the analysis, the deferral marker, or None to stay silent."""
        if self._stand_down():
            return _PHOTO_ANALYSIS_DEFERRED
        preview = self.coordinator.photos.prepare_identity_preview(
            self.profile_id,
            self.pair_id,
            cancelled=self._stop,
        )
        if self.token.cancelled:
            return None
        if self._stand_down():
            return _PHOTO_ANALYSIS_DEFERRED
        mo_photos, mo_failures = self._read_photos(preview.mo_photos)
        inat_photos, inat_failures = self._read_photos(preview.inat_photos)
        if self.token.cancelled:
            return None
        # Checked AFTER the reads too: standing down part-way through a photo
        # set would otherwise be reported as "download failed" for every photo
        # the loop never reached.
        if self._stand_down():
            return _PHOTO_ANALYSIS_DEFERRED
        return _CandidatePhotoAnalysis(
            pair_id=self.pair_id,
            mo_observation_id=preview.mo_observation_id,
            inat_observation_id=preview.inat_observation_id,
            mo_photos=mo_photos,
            inat_photos=inat_photos,
            matches=_unique_photo_matches(mo_photos, inat_photos),
            mo_total=len(preview.mo_photos),
            inat_total=len(preview.inat_photos),
            # The service's warnings count as failures here. They report that a
            # site returned FEWER photos than it says the observation has, and
            # quick confirmation must never be offered on a set already known
            # to be short -- the missing photo is the one that would have
            # disproved the pairing.
            failures=tuple((*preview.warnings, *mo_failures, *inat_failures)),
        )

    def _read_photos(
        self,
        photos: tuple[PhotoRecordSnapshot, ...],
    ) -> tuple[tuple[_HashedIdentityPhoto, ...], tuple[str, ...]]:
        values: list[_HashedIdentityPhoto] = []
        failures: list[str] = []
        for photo in photos:
            if self._stop():
                break
            url = _identity_photo_url(photo, "large")
            if not url:
                failures.append(
                    f"{photo.site.value.upper()} photo {photo.photo_id}: no image URL"
                )
                continue
            try:
                # MO images go through MOClient so they are serialized and
                # spaced: this path is automatic and reads both complete photo
                # sets per pair, and INatClient.download_image is documented as
                # unthrottled on the grounds that it talks to iNaturalist's
                # CDN — which is true for iNaturalist photos and only those.
                data = (
                    self.coordinator.mo_client.download_image(url, self._stop)
                    if photo.site == RemoteSite.MO
                    else self.coordinator.inat_client.download_image(url)
                )
            except ReconciliationCancelled:
                # A cancellation or stand-down, not a bad photo. Abandon the
                # set rather than recording every remaining photo as failed.
                break
            except Exception:
                failures.append(
                    f"{photo.site.value.upper()} photo {photo.photo_id}: download failed"
                )
                continue
            fingerprint = normalized_pixel_fingerprint(data)
            image = _decode_oriented_image(data)
            if not fingerprint or image is None:
                failures.append(
                    f"{photo.site.value.upper()} photo {photo.photo_id}: decode failed"
                )
                continue
            values.append(_HashedIdentityPhoto(photo, fingerprint, image))
        return tuple(values), tuple(failures)


def _decode_oriented_image(data: bytes) -> Optional[QImage]:
    payload = QByteArray(data)
    buffer = QBuffer(payload)
    if not buffer.open(QBuffer.OpenModeFlag.ReadOnly):
        return None
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    image = reader.read()
    buffer.close()
    return None if image.isNull() else image


# This UI asks for photos by iNaturalist's size vocabulary; map those names
# onto Mushroom Observer's own ladder. Reading a modest rendition matters here
# because candidate review downloads BOTH complete photo sets automatically for
# every prefetched row, and an 88px mosaic tile and a 9x8 dHash need nothing
# larger. Nothing here ever asks MO for ``orig``: the transfer path is the only
# caller that wants full fidelity, and the one place a person is offered the
# true original is _full_size_photo_page_url, which hands MO's own endpoint to
# the browser instead of downloading anything.
_MO_SIZE_FOR = {
    "square": "thumb",
    "thumb": "thumb",
    "small": "320",
    "medium": "640",
    "large": "960",
    "original": MO_LARGEST_FETCHABLE_SIZE,
}


def _identity_photo_url(photo: PhotoRecordSnapshot, size: str) -> str:
    url = photo.source_url.strip()
    if not url:
        return url
    if photo.site == RemoteSite.MO:
        wanted = _MO_SIZE_FOR.get(size.casefold())
        return mo_image_url(url, wanted) if wanted else url
    if photo.site != RemoteSite.INAT:
        return url
    swapped, count = re.subn(
        r"/(square|thumb|small|medium|large|original)" r"\.(jpe?g|png|gif|webp)",
        rf"/{size}.\2",
        url,
        flags=re.IGNORECASE,
    )
    return swapped if count else url


def _full_size_photo_page_url(photo: PhotoRecordSnapshot) -> str:
    """Where to send the operator's BROWSER to see a photo at full size.

    Mushroom Observer splits in two here. A recent image's original is served
    straight off the image server, so link the file itself -- rewritten from
    whatever rendition the snapshot holds, keeping its version token so the
    browser is not handed a stale cached copy. An archived image's original
    exists only behind MO's signed-in, quota-checked retrieval, so link MO's
    page and let its own flow do the work (see mo_image_page_url).
    """
    if photo.site != RemoteSite.MO:
        return _identity_photo_url(photo, "original")
    if mo_original_is_public(photo.photo_id):
        return mo_image_url(photo.source_url, "orig")
    return mo_image_page_url(photo.photo_id)


def _unique_photo_matches(
    mo_photos: tuple[_HashedIdentityPhoto, ...],
    inat_photos: tuple[_HashedIdentityPhoto, ...],
) -> tuple[tuple[int, int, int], ...]:
    """Greedily choose non-overlapping closest dHash pairs.

    Photo sets are normally small and the same photograph is sharply closer
    than every unrelated photograph. Sorting every possible edge first gives
    a deterministic one-to-one assignment while preventing one especially
    generic image from claiming several counterparts.
    """
    candidates: list[tuple[int, int, int]] = []
    for mo_index, mo_photo in enumerate(mo_photos):
        for inat_index, inat_photo in enumerate(inat_photos):
            distance = pixel_fingerprint_distance(
                mo_photo.fingerprint, inat_photo.fingerprint
            )
            if distance is not None:
                candidates.append((distance, mo_index, inat_index))
    used_mo: set[int] = set()
    used_inat: set[int] = set()
    matches: list[tuple[int, int, int]] = []
    for distance, mo_index, inat_index in sorted(candidates):
        if mo_index in used_mo or inat_index in used_inat:
            continue
        used_mo.add(mo_index)
        used_inat.add(inat_index)
        matches.append((mo_index, inat_index, distance))
    return tuple(matches)


def _photo_similarity_score(distance: int) -> int:
    """Express a 64-bit dHash Hamming distance as a 0..100 similarity.

    This is the number the candidate list and its tooltip both show, so it is
    defined in exactly one place: 100 minus the percentage of differing bits.
    It inherits every caveat of :func:`normalized_pixel_fingerprint` -- it is a
    similarity signal, never proof that two photographs are the same one.
    """
    bounded = min(max(int(distance), 0), _PHOTO_FINGERPRINT_BITS)
    return round(100 - bounded * 100 / _PHOTO_FINGERPRINT_BITS)


def _photo_analysis_mosaic(analysis: _CandidatePhotoAnalysis) -> QImage:
    """Render one candidate's two photo sets as a small paired mosaic.

    Matched photographs are drawn in the SAME COLUMN -- Mushroom Observer on
    the top row, iNaturalist below -- so the operator judges the pairing by
    looking down a column rather than by reading a number. Each matched column
    is underlined in green when the pair is within ``PHOTO_CLOSE_DISTANCE`` and
    amber when it is only the best available match. Unmatched photos follow
    their own row's matches, with no counterpart above or below them, which is
    what makes a photo-count mismatch visible at a glance.
    """
    cell, gap = 33, 2
    width, height = _PHOTO_MOSAIC_WIDTH, 2 * cell + 3 * gap
    columns = max(1, (width - gap) // (cell + gap))
    mosaic = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    mosaic.fill(Qt.GlobalColor.transparent)
    matched_mo = {mo_index for mo_index, _inat, _distance in analysis.matches}
    matched_inat = {inat_index for _mo, inat_index, _distance in analysis.matches}
    # Column plan: matched pairs first, then each side's leftovers.
    plan: list[tuple[Optional[int], Optional[int], Optional[int]]] = [
        (mo_index, inat_index, distance)
        for mo_index, inat_index, distance in analysis.matches
    ]
    plan.extend(
        (index, None, None)
        for index in range(len(analysis.mo_photos))
        if index not in matched_mo
    )
    plan.extend(
        (None, index, None)
        for index in range(len(analysis.inat_photos))
        if index not in matched_inat
    )
    painter = QPainter(mosaic)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    try:
        for column, (mo_index, inat_index, distance) in enumerate(plan[:columns]):
            left = gap + column * (cell + gap)
            for row, (photos, index) in enumerate(
                ((analysis.mo_photos, mo_index), (analysis.inat_photos, inat_index))
            ):
                top = gap + row * (cell + gap)
                box = QRect(left, top, cell, cell)
                if index is None:
                    # An absent counterpart is drawn, not skipped: an empty
                    # slot under a photo IS the "no match on the other site"
                    # signal, and a blank gap would read as a rendering fault.
                    painter.fillRect(box, QColor("#f0f0f0"))
                    painter.setPen(QColor("#c8c8c8"))
                    painter.drawRect(box.adjusted(0, 0, -1, -1))
                    continue
                painter.drawImage(box, _fitted_thumbnail(photos[index].image, cell))
            if distance is not None:
                painter.fillRect(
                    QRect(left, height - gap, cell, gap),
                    (
                        QColor("#167236")
                        if distance <= PHOTO_CLOSE_DISTANCE
                        else QColor("#b9770e")
                    ),
                )
        hidden = len(plan) - columns
        if hidden > 0:
            painter.setPen(QColor("#444444"))
            painter.drawText(
                QRect(width - 34, height - 16, 32, 14),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                f"+{hidden}",
            )
    finally:
        painter.end()
    return mosaic


def _fitted_thumbnail(image: QImage, size: int) -> QImage:
    """Centre-crop to a square, then scale, so cells are not letterboxed."""
    side = min(image.width(), image.height())
    if side <= 0:
        return QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    square = image.copy(
        QRect(
            (image.width() - side) // 2,
            (image.height() - side) // 2,
            side,
            side,
        )
    )
    return square.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class PhotoIdentityReviewDialog(QDialog):
    """Read-only, pre-confirmation comparison of both complete photo sets."""

    def __init__(
        self,
        preview: PhotoIdentityPreview,
        download_image,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self.decision = ""
        self._download_image = download_image
        self._pool = QThreadPool.globalInstance()
        self._live_thumbnail_signals: set[_PhotoThumbnailSignals] = set()
        self._thumbnail_labels: dict[str, _ClickableImageLabel] = {}
        self._images: dict[str, QImage] = {}
        self._captions: dict[str, str] = {}
        self._full_image_urls: dict[str, str] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._preview_key: Optional[str] = None
        self._closed = False
        self._decision_buttons: list[QPushButton] = []

        self.setWindowTitle("Compare photos for pair identity")
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Read-only identity review: MO {preview.mo_observation_id} ↔ "
            f"iNaturalist {preview.inat_observation_id}\n"
            "No journal entry or remote change can be made from this comparison. "
            "Confirm only if both observations depict the same specimen."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        open_row = QHBoxLayout()
        open_mo = QPushButton("Open Mushroom Observer observation")
        open_inat = QPushButton("Open iNaturalist observation")
        open_mo.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(
                    f"https://mushroomobserver.org/obs/" f"{preview.mo_observation_id}"
                )
            )
        )
        open_inat.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(
                    f"https://www.inaturalist.org/observations/"
                    f"{preview.inat_observation_id}"
                )
            )
        )
        open_row.addWidget(open_mo)
        open_row.addWidget(open_inat)
        open_row.addStretch(1)
        layout.addLayout(open_row)

        sets = QHBoxLayout()
        sets.addWidget(
            self._photo_set(
                "Mushroom Observer",
                preview.mo_photos,
                "mo",
            ),
            1,
        )
        sets.addWidget(
            self._photo_set(
                "iNaturalist",
                preview.inat_photos,
                "inat",
            ),
            1,
        )
        layout.addLayout(sets, 2)

        layout.addWidget(QLabel("Selected image"))
        self._preview_label = QLabel(
            "Click any thumbnail to inspect it at a larger size."
        )
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumHeight(_PHOTO_PREVIEW_SIZE)
        self._preview_label.setFrameShape(QFrame.Shape.Box)
        layout.addWidget(self._preview_label, 2)
        self._preview_caption = QLabel("")
        self._preview_caption.setWordWrap(True)
        layout.addWidget(self._preview_caption)
        self._open_image = QPushButton("Open selected full-size image")
        self._open_image.setEnabled(False)
        self._open_image.clicked.connect(self._open_selected_image)
        layout.addWidget(self._open_image)

        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)

        controls = QHBoxLayout()
        if preview.review_state == "candidate":
            confirm = QPushButton("Confirm pair")
            reject = QPushButton("Reject candidate")
            confirm.clicked.connect(lambda: self._finish("confirmed"))
            reject.clicked.connect(lambda: self._finish("rejected"))
            self._decision_buttons.extend((confirm, reject))
            controls.addWidget(confirm)
            controls.addWidget(reject)
        controls.addStretch(1)
        close = QPushButton("Close without decision")
        close.clicked.connect(self.reject)
        controls.addWidget(close)
        layout.addLayout(controls)
        self._update_decision_buttons()

    def _photo_set(
        self,
        label: str,
        photos: tuple[PhotoRecordSnapshot, ...],
        prefix: str,
    ) -> QWidget:
        group = QWidget()
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(QLabel(f"{label} — {len(photos)} photo(s)"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        row = QHBoxLayout(holder)
        if not photos:
            row.addWidget(QLabel("No photos returned"))
        for ordinal, photo in enumerate(photos, 1):
            key = f"{prefix}:{photo.photo_id}"
            caption = (
                f"{label} photo {ordinal} of {len(photos)}\n" f"ID {photo.photo_id}"
            )
            self._captions[key] = caption
            full_url = _full_size_photo_page_url(photo)
            if full_url:
                self._full_image_urls[key] = full_url
            thumb = _ClickableImageLabel("loading…")
            thumb.setFixedSize(_PHOTO_THUMBNAIL_SIZE, _PHOTO_THUMBNAIL_SIZE)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setFrameShape(QFrame.Shape.Box)
            thumb.clicked.connect(lambda _k=key: self._show_preview(_k))
            thumb.doubleClicked.connect(lambda _k=key: self._show_preview(_k))
            self._thumbnail_labels[key] = thumb
            item = QVBoxLayout()
            item.addWidget(thumb)
            text = QLabel(f"{ordinal}")
            text.setAlignment(Qt.AlignmentFlag.AlignCenter)
            item.addWidget(text)
            wrapper = QWidget()
            wrapper.setLayout(item)
            row.addWidget(wrapper)
            if photo.source_url:
                self._request_thumbnail(key, self._photo_url(photo, "large"))
            else:
                self._failed.add(key)
                thumb.setText("no image URL")
        row.addStretch(1)
        scroll.setWidget(holder)
        group_layout.addWidget(scroll)
        return group

    def _request_thumbnail(self, key: str, url: str) -> None:
        self._pending.add(key)
        worker = _PhotoThumbnailWorker(
            key,
            url,
            self._download_image,
            lambda: self._closed,
        )
        signals = worker.signals
        self._live_thumbnail_signals.add(signals)
        signals.done.connect(
            lambda k, image, _mismatch, owned=signals: self._thumbnail_loaded(
                owned, k, image
            )
        )
        self._pool.start(worker)

    def _thumbnail_loaded(
        self,
        signals: _PhotoThumbnailSignals,
        key: str,
        image: Optional[QImage],
    ) -> None:
        self._live_thumbnail_signals.discard(signals)
        self._pending.discard(key)
        if self._closed:
            return
        label = self._thumbnail_labels.get(key)
        if image is None or image.isNull():
            self._failed.add(key)
            if label is not None:
                label.setText("unavailable")
                label.setToolTip(
                    "This image could not be displayed, so the photo sets are "
                    "not complete enough to decide here."
                )
        else:
            self._images[key] = image
            if label is not None:
                label.setPixmap(
                    QPixmap.fromImage(image).scaled(
                        _PHOTO_THUMBNAIL_SIZE,
                        _PHOTO_THUMBNAIL_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        if self._preview_key == key:
            self._render_preview(key)
        self._update_decision_buttons()

    def _show_preview(self, key: str) -> None:
        self._preview_key = key
        self._open_image.setEnabled(key in self._full_image_urls)
        self._render_preview(key)

    def _open_selected_image(self) -> None:
        if self._preview_key:
            url = self._full_image_urls.get(self._preview_key, "")
            if url:
                QDesktopServices.openUrl(QUrl(url))

    @staticmethod
    def _photo_url(photo: PhotoRecordSnapshot, size: str) -> str:
        return _identity_photo_url(photo, size)

    def _render_preview(self, key: str) -> None:
        self._preview_caption.setText(self._captions.get(key, key))
        image = self._images.get(key)
        if image is None:
            self._preview_label.setText(
                "Image unavailable" if key in self._failed else "Loading…"
            )
            self._preview_label.setPixmap(QPixmap())
            return
        self._preview_label.setText("")
        self._preview_label.setPixmap(
            QPixmap.fromImage(image).scaled(
                _PHOTO_PREVIEW_SIZE,
                _PHOTO_PREVIEW_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _update_decision_buttons(self) -> None:
        ready = (
            not self._pending
            and not self._failed
            and bool(self.preview.mo_photos)
            and bool(self.preview.inat_photos)
        )
        reason = (
            ""
            if ready
            else "Wait until every photo from both observations is displayed. "
            "Use the site buttons if any image remains unavailable."
        )
        for button in self._decision_buttons:
            button.setEnabled(ready)
            button.setToolTip(reason)

    def _finish(self, decision: str) -> None:
        self.decision = decision
        self.accept()

    def closeEvent(self, event) -> None:
        self._closed = True
        super().closeEvent(event)


class PhotoComparisonDialog(QDialog):
    """Photo synchronization preview (Mushroom Observer → iNaturalist).

    Similarity to an existing destination photo can only ever DISABLE an
    option here — it is never used to preselect or auto-check a transfer. The
    operator must explicitly choose the one photo to send, and must be able
    to actually SEE it first: every source option shows a real thumbnail
    (never just an ID), destination photos are shown as a comparable strip,
    and any thumbnail enlarges into a shared preview pane on click or
    double-click.

    "Must be able to SEE it" is enforced, not merely intended: a source
    option starts UNSELECTABLE and only becomes selectable once
    ``_thumbnail_loaded`` has actually rendered a valid image whose bytes
    hash to the ``byte_fingerprint`` the preview pinned. A download failure,
    a decode failure, or a source image that changed since the preview all
    leave the option permanently disabled. ``selected_option`` re-checks
    ``_visually_verified_photo_keys`` independently of radio-button state,
    so a race between "radio got enabled" and "user clicked OK" cannot
    smuggle an unreviewed photo into an upload this app can never undo.
    """

    def __init__(
        self, preview: PhotoComparisonPreview, download_image, parent=None
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self._download_image = download_image
        self._pool = QThreadPool.globalInstance()
        self._live_thumbnail_signals: set[_PhotoThumbnailSignals] = set()
        self._thumbnail_labels: dict[str, _ClickableImageLabel] = {}
        self._images: dict[str, QImage] = {}
        self._captions: dict[str, str] = {}
        self._preview_key: Optional[str] = None
        self._closed = False
        # Single source of truth for "the operator has actually seen the
        # pinned bytes of this source photo". Only ``_thumbnail_loaded``
        # populates it, and only when a decoded image and a matching
        # fingerprint line up for a key this dialog itself created.
        self._visually_verified_photo_keys: set[str] = set()
        self._choice_by_key: dict[str, QRadioButton] = {}
        self._option_by_key: dict[str, PhotoActionOption] = {}
        self._availability_by_key: dict[str, QTableWidgetItem] = {}

        self.setWindowTitle("Preview photo synchronization")
        self.resize(960, 700)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Confirmed pair: MO {preview.mo_observation_id} ↔ iNaturalist "
            f"{preview.inat_observation_id}\n"
            "Source is Mushroom Observer; the only destination is iNaturalist. "
            "This is a synchronization preview, not the pair-identity review. "
            "At most one photo is journaled and uploaded per confirmation."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        body = QHBoxLayout()
        layout.addLayout(body, 1)
        left = QVBoxLayout()
        body.addLayout(left, 3)

        source_by_id = {p.photo_id: p for p in preview.source_photos}

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._choices: list[tuple[QRadioButton, PhotoActionOption]] = []

        left.addWidget(QLabel("Mushroom Observer source images (pick one to send)"))
        self.table = QTableWidget(len(preview.options), 4)
        self.table.setHorizontalHeaderLabels(
            ("Send", "Image", "Destination license", "Availability")
        )
        self.table.verticalHeader().setDefaultSectionSize(_PHOTO_THUMBNAIL_SIZE + 12)
        for row_index, option in enumerate(preview.options):
            choice = QRadioButton()
            # Always starts unselectable: even an option the preview enabled
            # stays disabled until its pinned image has actually rendered.
            choice.setEnabled(False)
            choice.setToolTip(option.disabled_reason)
            choice.toggled.connect(self._update_confirm_enabled)
            self._buttons.addButton(choice)
            self.table.setCellWidget(row_index, 0, choice)

            key = f"source:{option.source_photo_id}"
            caption = (
                f"MO {option.source_photo_id}\n{option.description}\n"
                f"license={option.source_license_label or 'unknown'}; "
                f"holder={option.source_copyright_holder or 'unknown'}"
            )
            self._captions[key] = caption
            thumb = self._make_thumbnail_label()
            self._thumbnail_labels[key] = thumb
            thumb.clicked.connect(
                lambda _c=choice: _c.setChecked(True) if _c.isEnabled() else None
            )
            thumb.clicked.connect(lambda _k=key: self._show_preview(_k))
            thumb.doubleClicked.connect(lambda _k=key: self._show_preview(_k))

            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(2, 2, 2, 2)
            text = QLabel(caption)
            text.setWordWrap(True)
            cell_layout.addWidget(thumb)
            cell_layout.addWidget(text, 1)
            self.table.setCellWidget(row_index, 1, cell)

            self.table.setItem(
                row_index, 2, QTableWidgetItem(option.destination_license_note)
            )
            availability = QTableWidgetItem(
                "loading image…" if option.enabled else option.disabled_reason
            )
            self.table.setItem(row_index, 3, availability)
            self._choices.append((choice, option))
            self._choice_by_key[key] = choice
            self._option_by_key[key] = option
            self._availability_by_key[key] = availability

            snapshot = source_by_id.get(option.source_photo_id)
            if option.enabled and snapshot is not None and snapshot.source_url:
                # The preview already downloaded and pinned these exact bytes
                # as ``byte_fingerprint``; this is a SEPARATE fetch, so hash
                # what is about to be displayed and compare before the option
                # is ever offered (see _PhotoThumbnailWorker's docstring).
                self._request_thumbnail(
                    key,
                    snapshot.source_url,
                    option.byte_fingerprint,
                )
            elif option.enabled:
                self._disable_source_option(
                    key,
                    "No image is available to review; this photo cannot be selected.",
                )
        self.table.resizeColumnsToContents()
        left.addWidget(self.table, 1)

        if not any(option.enabled for option in preview.options):
            none_ready = QLabel(
                "No photo in this pair currently qualifies for transfer. See the Availability "
                "column above for each image's reason."
            )
            none_ready.setWordWrap(True)
            left.addWidget(none_ready)

        left.addWidget(
            QLabel("iNaturalist destination images (already on the observation)")
        )
        dest_scroll = QScrollArea()
        dest_scroll.setWidgetResizable(True)
        dest_scroll.setFixedHeight(_PHOTO_THUMBNAIL_SIZE + 56)
        dest_holder = QWidget()
        dest_row = QHBoxLayout(dest_holder)
        if preview.destination_photos:
            for photo in preview.destination_photos:
                key = f"dest:{photo.photo_id}"
                self._captions[key] = (
                    f"iNat {photo.photo_id}\nlicense={photo.license_label or 'unknown'}; "
                    f"holder={photo.copyright_holder or 'unknown'}"
                )
                thumb = self._make_thumbnail_label()
                self._thumbnail_labels[key] = thumb
                thumb.clicked.connect(lambda _k=key: self._show_preview(_k))
                thumb.doubleClicked.connect(lambda _k=key: self._show_preview(_k))
                if photo.source_url:
                    self._request_thumbnail(key, photo.source_url)
                item = QVBoxLayout()
                item_caption = QLabel(f"iNat {photo.photo_id}")
                item_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
                item.addWidget(thumb)
                item.addWidget(item_caption)
                wrapper = QWidget()
                wrapper.setLayout(item)
                dest_row.addWidget(wrapper)
        else:
            dest_row.addWidget(QLabel("none"))
        dest_row.addStretch(1)
        dest_scroll.setWidget(dest_holder)
        left.addWidget(dest_scroll)

        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            left.addWidget(warning)

        right = QVBoxLayout()
        body.addLayout(right, 2)
        right.addWidget(
            QLabel("Selected image (click, or double-click, any thumbnail to preview)")
        )
        self._preview_label = QLabel("No image selected")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(_PHOTO_PREVIEW_SIZE, _PHOTO_PREVIEW_SIZE)
        self._preview_label.setFrameShape(QFrame.Shape.Box)
        right.addWidget(self._preview_label, 1)
        self._preview_caption = QLabel("")
        self._preview_caption.setWordWrap(True)
        right.addWidget(self._preview_caption)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText("Confirm one photo transfer…")
        self._ok_button.setEnabled(False)
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_option(self) -> Optional[PhotoActionOption]:
        """The one chosen option, but only once its pinned image has actually
        been displayed. Never trusts radio-button state alone: this is the
        last line of defence against enabling and confirming in the same
        instant an unverified thumbnail callback lands."""
        for button, option in self._choices:
            if not button.isChecked():
                continue
            key = f"source:{option.source_photo_id}"
            if not option.enabled or key not in self._visually_verified_photo_keys:
                return None
            return option
        return None

    def closeEvent(self, event) -> None:
        self._closed = True
        super().closeEvent(event)

    def _make_thumbnail_label(self) -> _ClickableImageLabel:
        label = _ClickableImageLabel("…")
        label.setFixedSize(_PHOTO_THUMBNAIL_SIZE, _PHOTO_THUMBNAIL_SIZE)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFrameShape(QFrame.Shape.Box)
        return label

    def _request_thumbnail(
        self, key: str, url: str, expected_byte_fingerprint: str = ""
    ) -> None:
        worker = _PhotoThumbnailWorker(
            key,
            url,
            self._download_image,
            lambda: self._closed,
            expected_byte_fingerprint,
        )
        signals = worker.signals
        self._live_thumbnail_signals.add(signals)
        signals.done.connect(
            lambda k, image, mismatch, s=signals: self._thumbnail_loaded(
                s, k, image, mismatch
            )
        )
        self._pool.start(worker)

    def _disable_source_option(
        self,
        key: str,
        reason: str,
        *,
        blank_thumbnail: bool = True,
    ) -> None:
        """Make a source option permanently unselectable, and unselect it if it
        somehow already was. ``blank_thumbnail=False`` keeps a real (merely
        stale) rendered image on screen instead of placeholder text."""
        self._visually_verified_photo_keys.discard(key)
        choice = self._choice_by_key.get(key)
        if choice is not None:
            self._buttons.setExclusive(False)
            choice.setChecked(False)
            self._buttons.setExclusive(True)
            choice.setEnabled(False)
            choice.setToolTip(reason)
        item = self._availability_by_key.get(key)
        if item is not None:
            item.setText(reason)
        label = self._thumbnail_labels.get(key)
        if label is not None:
            label.setEnabled(False)
            if blank_thumbnail:
                label.setText("no image")
            label.setToolTip(reason)
        self._update_confirm_enabled()

    def _enable_source_option(self, key: str) -> None:
        """Counterpart to ``_disable_source_option``: the pinned bytes rendered
        and matched, so this option may finally be selected."""
        option = self._option_by_key.get(key)
        choice = self._choice_by_key.get(key)
        if option is None or choice is None or not option.enabled:
            return
        self._visually_verified_photo_keys.add(key)
        choice.setEnabled(True)
        choice.setToolTip("")
        item = self._availability_by_key.get(key)
        if item is not None:
            item.setText("ready")
        self._update_confirm_enabled()

    def _thumbnail_loaded(
        self,
        signals: _PhotoThumbnailSignals,
        key: str,
        image: Optional[QImage],
        mismatch: bool = False,
    ) -> None:
        self._live_thumbnail_signals.discard(signals)
        if self._closed:
            return
        label = self._thumbnail_labels.get(key)
        is_source_option = key in self._choice_by_key
        if image is None or not isinstance(image, QImage) or image.isNull():
            # A failed download, empty body, or decode failure must never leave
            # a selectable option behind a "no image" placeholder.
            if is_source_option:
                self._disable_source_option(
                    key,
                    "Image could not be displayed and reviewed; this photo cannot be selected.",
                )
            elif label is not None:
                label.setText("no image")
            if self._preview_key == key:
                self._render_preview(key)
            return
        self._images[key] = image
        if label is not None:
            label.setPixmap(
                QPixmap.fromImage(image).scaled(
                    _PHOTO_THUMBNAIL_SIZE,
                    _PHOTO_THUMBNAIL_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        if is_source_option:
            if mismatch:
                # The source image changed between the preview that pinned
                # byte_fingerprint and this display. The write would refuse
                # later anyway; never offer content already known to be stale.
                self._disable_source_option(
                    key,
                    "This image changed since it was reviewed and cannot be selected.",
                    blank_thumbnail=False,
                )
            else:
                self._enable_source_option(key)
        if self._preview_key == key:
            self._render_preview(key)

    def _show_preview(self, key: str) -> None:
        self._preview_key = key
        self._render_preview(key)

    def _render_preview(self, key: str) -> None:
        self._preview_caption.setText(self._captions.get(key, key))
        image = self._images.get(key)
        if image is None:
            self._preview_label.setText("Loading…")
            self._preview_label.setPixmap(QPixmap())
            return
        self._preview_label.setText("")
        self._preview_label.setPixmap(
            QPixmap.fromImage(image).scaled(
                _PHOTO_PREVIEW_SIZE,
                _PHOTO_PREVIEW_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _update_confirm_enabled(self, *_args) -> None:
        # Reachable from _disable_source_option while __init__ is still
        # building the option rows, before the button box exists.
        button = getattr(self, "_ok_button", None)
        if button is None:
            return
        option = self.selected_option()
        button.setEnabled(option is not None and option.enabled)

    def _confirm(self) -> None:
        option = self.selected_option()
        if option is None or not option.enabled:
            QMessageBox.warning(
                self,
                "No reviewed photo selected",
                "Select one photo whose image has actually been displayed here. An image that "
                "could not be loaded, or that changed since it was reviewed, cannot be sent.",
            )
            return
        message = (
            "Journal and upload this one reviewed photo to iNaturalist?\n\n"
            f"{option.description}\n\n"
            f"{option.destination_license_note}\n\n"
            "The source image is reread and refingerprinted before upload; the destination is "
            "verified afterward. iNaturalist accepts no license parameter on upload, and this app "
            "never deletes a photo — a duplicate upload cannot be undone."
        )
        if (
            QMessageBox.question(
                self,
                "Explicit photo upload confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()


class ObservationCreationPreviewDialog(QDialog):
    """Gate 2A: the complete reviewed missing-observation creation proposal.

    Every field that will be created is shown explicitly; anything the
    direction-specific policy could not transfer is listed separately under
    "Cannot be transferred," never silently dropped. Every photo/identifier/
    sequence item is its own checkbox — nothing preselected, no bundle
    checkbox — mirroring every other dialog's "never a batch" convention.
    This is the most explicit confirmation in the app: it is the first
    action that creates a brand-new remote identity, which this app cannot
    delete afterward.
    """

    def __init__(
        self, preview: ObservationCreationPreview, download_image, parent=None
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self._download_image = download_image
        self._pool = QThreadPool.globalInstance()
        self._live_thumbnail_signals: set[_PhotoThumbnailSignals] = set()
        self._thumbnail_labels: dict[str, _ClickableImageLabel] = {}
        self._check_by_key: dict[str, QCheckBox] = {}
        self._item_by_key: dict[str, ObservationCreationItem] = {}
        self._images: dict[str, QImage] = {}
        self._captions: dict[str, str] = {}
        self._preview_key: Optional[str] = None
        self._closed = False
        # Section 1 (release blocker): a photo checkbox must never be
        # selectable until the dialog has actually SHOWN a verified image
        # for it. This set is the single source of truth for that -- it is
        # populated only inside ``_thumbnail_loaded`` when a valid decoded
        # QImage, a matching pinned byte fingerprint (when one was
        # supplied), and the correct/current item all line up. ``_confirm``/
        # ``selected_items`` re-check this set independently of checkbox
        # enabled/checked state, so a race between "checkbox got enabled"
        # and "user clicked OK" can never smuggle an unverified photo through.
        self._visually_verified_photo_keys: set[str] = set()
        self.setWindowTitle("Create missing observation")
        self.resize(900, 700)
        layout = QVBoxLayout(self)

        header = QLabel(
            f"<b>Destination:</b> {preview.destination_site.value} account "
            f"<b>{preview.destination_account_login}</b><br>"
            f"Source: {preview.source_site.value} observation {preview.source_observation_id}"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        fields = QPlainTextEdit()
        fields.setReadOnly(True)
        fields.setMaximumHeight(170)
        lines = [
            f"Observed date: {preview.observed_on_string or '(none)'}",
            f"Source taxon/name (original string): {preview.taxon_name or '(none)'}",
            # Section 10/11: the user confirms the ACTUAL resolved destination
            # taxon, never just the raw source string above.
            f"Resolved destination taxon: {preview.resolved_taxon_name or '(none — see Cannot be transferred, below)'}"
            f"{f' (iNaturalist taxon id {preview.taxon_id})' if preview.taxon_id is not None else ''}",
            f"Public locality: {preview.place_guess or '(none)'}",
            f"Description/notes: {preview.description or '(none)'}",
            f"Attribution (original observer): {preview.source_attribution_name or '(unknown)'}",
        ]
        if preview.latitude is not None and preview.longitude is not None:
            lines.append(
                f"Coordinates: available (accuracy "
                f"{preview.positional_accuracy if preview.positional_accuracy is not None else 'unknown'} m; "
                f"privacy: {preview.geoprivacy or 'open'})"
            )
        else:
            lines.append("Coordinates: not included")
        fields.setPlainText("\n".join(lines))
        layout.addWidget(fields)

        if preview.marker_in_public_notes:
            marker_note = QLabel(
                "A short recovery marker will be embedded in the new Mushroom Observer "
                "observation's PUBLIC Notes field, because Mushroom Observer's create response "
                "cannot otherwise be matched back to the new observation id. It is visible to "
                "anyone who views the observation."
            )
            marker_note.setWordWrap(True)
            layout.addWidget(marker_note)

        if preview.approved_field_gaps:
            from observation_workbench.reconciliation.observation_creation import (
                gap_display_text,
            )

            gaps = QLabel(
                "Cannot be transferred:\n"
                + "\n".join(
                    f"• {gap_display_text(gap)}" for gap in preview.approved_field_gaps
                )
            )
            gaps.setWordWrap(True)
            layout.addWidget(gaps)

        body = QHBoxLayout()
        layout.addLayout(body, 1)
        left = QVBoxLayout()
        body.addLayout(left, 3)

        left.addWidget(
            QLabel(
                "Select identifiers and photos to include (nothing is preselected). Click a photo "
                "thumbnail to preview it — clicking never selects it."
            )
        )
        self._checks: list[tuple[QCheckBox, ObservationCreationItem, Optional[str]]] = (
            []
        )
        items_list = QListWidget()
        for index, item in enumerate(preview.items):
            box = QCheckBox(
                item.description
                if item.enabled
                else f"{item.description}  [disabled: {item.disabled_reason}]"
            )
            box.setChecked(False)
            item_key: Optional[str] = None
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(4, 2, 4, 2)
            if item.item_type == "photo":
                key = f"item:{index}:{item.source_identity}"
                item_key = key
                caption = (
                    f"{item.source_site.value} photo {item.source_identity}\n"
                    f"license={item.license_label or 'unknown'}; "
                    f"holder={item.copyright_holder or 'unknown'}"
                )
                self._captions[key] = caption
                thumb = self._make_thumbnail_label()
                self._thumbnail_labels[key] = thumb
                if not item.enabled:
                    thumb.setEnabled(False)
                # Clicking a photo NEVER toggles the checkbox -- it only ever
                # opens the larger preview pane (section 7: "clicking a photo
                # must never silently approve it").
                thumb.clicked.connect(lambda _k=key: self._show_preview(_k))
                thumb.doubleClicked.connect(lambda _k=key: self._show_preview(_k))
                self._check_by_key[key] = box
                self._item_by_key[key] = item
                if item.enabled and item.source_url:
                    # Section 1 (release blocker): the checkbox starts
                    # DISABLED, with a visible "loading image..." reason --
                    # never selectable until ``_thumbnail_loaded`` proves the
                    # pinned bytes actually rendered. This closes the race
                    # where a user could check+confirm before the async
                    # thumbnail worker ever reports back.
                    box.setEnabled(False)
                    box.setText(f"{box.text()}  [loading image…]")
                    self._request_thumbnail(
                        key, item.source_url, item.reviewed_byte_fingerprint
                    )
                else:
                    # No URL to review from -- there is no path by which this
                    # photo can ever be visually verified, so it must never
                    # become selectable.
                    box.setEnabled(False)
                    if item.enabled and box.text().find("[disabled:") < 0:
                        box.setText(
                            f"{box.text()}  [disabled: no image available to review]"
                        )
                row_layout.addWidget(thumb)
                license_label = QLabel(
                    f"license: {item.license_label or 'unknown'}\nholder: {item.copyright_holder or 'unknown'}"
                )
                license_label.setWordWrap(True)
                row_layout.addWidget(license_label)
            else:
                box.setEnabled(item.enabled)
            row_layout.addWidget(box, 1)
            from PySide6.QtWidgets import QListWidgetItem

            list_item = QListWidgetItem()
            # Section 1/3 (release blocker): without an explicit size hint a
            # QListWidgetItem carrying an item widget is sized from its own
            # (empty) data, so the row collapses to a single line of text and
            # clips the 88px thumbnail down to a ~14px sliver. The checkbox
            # would then become "visually verified" for an image the operator
            # cannot actually see. Size the row from the widget itself.
            list_item.setSizeHint(row_widget.sizeHint())
            items_list.addItem(list_item)
            items_list.setItemWidget(list_item, row_widget)
            self._checks.append((box, item, item_key))
        left.addWidget(items_list, 1)

        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            left.addWidget(warning)

        right = QVBoxLayout()
        body.addLayout(right, 2)
        right.addWidget(
            QLabel("Selected photo (click, or double-click, a thumbnail to preview)")
        )
        self._preview_label = QLabel("No photo selected")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(_PHOTO_PREVIEW_SIZE, _PHOTO_PREVIEW_SIZE)
        self._preview_label.setFrameShape(QFrame.Shape.Box)
        right.addWidget(self._preview_label, 1)
        self._preview_caption = QLabel("")
        self._preview_caption.setWordWrap(True)
        right.addWidget(self._preview_caption)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Create observation…"
        )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_items(self) -> list[ObservationCreationItem]:
        # Section 1 (release blocker): never trust checkbox isChecked()/
        # isEnabled() state alone for photo items -- independently require
        # that the key was actually visually verified. This is the last
        # line of defense against the checkbox-enabled-before-thumbnail-
        # loaded race, regardless of any UI-thread timing.
        result = []
        for box, item, key in self._checks:
            if not box.isChecked():
                continue
            if (
                item.item_type == "photo"
                and key not in self._visually_verified_photo_keys
            ):
                continue
            result.append(item)
        return result

    def closeEvent(self, event) -> None:
        self._closed = True
        super().closeEvent(event)

    def _make_thumbnail_label(self) -> _ClickableImageLabel:
        label = _ClickableImageLabel("…")
        label.setFixedSize(_PHOTO_THUMBNAIL_SIZE, _PHOTO_THUMBNAIL_SIZE)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFrameShape(QFrame.Shape.Box)
        return label

    def _request_thumbnail(
        self, key: str, url: str, expected_byte_fingerprint: str = ""
    ) -> None:
        worker = _PhotoThumbnailWorker(
            key,
            url,
            self._download_image,
            lambda: self._closed,
            expected_byte_fingerprint,
        )
        signals = worker.signals
        self._live_thumbnail_signals.add(signals)
        signals.done.connect(
            lambda k, image, mismatch, s=signals: self._thumbnail_loaded(
                s, k, image, mismatch
            )
        )
        self._pool.start(worker)

    def _disable_item(
        self, key: str, reason: str, *, blank_thumbnail: bool = True
    ) -> None:
        """Section 3: the user must actually SEE the pinned image before a
        photo is selectable. Disable AND uncheck the checkbox (never leave
        it enabled with a broken/blank thumbnail) so the confirmation
        button — which re-reads live checkbox state in ``_confirm`` — can
        never include this item in ``selected_items()``.

        ``blank_thumbnail=False`` is used for the "content changed since
        review" case, where a real (just stale) thumbnail already rendered
        successfully — the pixmap stays visible (dimmed via ``setEnabled``)
        instead of being replaced with placeholder text."""
        self._visually_verified_photo_keys.discard(key)
        box = self._check_by_key.get(key)
        if box is not None and box.text().find("[disabled:") < 0:
            text = box.text()
            loading_marker = "  [loading image…]"
            if text.endswith(loading_marker):
                text = text[: -len(loading_marker)]
            box.setChecked(False)
            box.setEnabled(False)
            box.setText(f"{text}  [disabled: {reason}]")
        label = self._thumbnail_labels.get(key)
        if label is not None:
            label.setEnabled(False)
            if blank_thumbnail:
                label.setText("unavailable")
            label.setToolTip(reason)

    def _thumbnail_loaded(
        self,
        signals: _PhotoThumbnailSignals,
        key: str,
        image: Optional[QImage],
        mismatch: bool = False,
    ) -> None:
        self._live_thumbnail_signals.discard(signals)
        if self._closed:
            return
        # Section 1/3 (safety-critical): a stale or malformed callback must
        # never be able to enable a checkbox. The key must still resolve to
        # a known checkbox/item pair that this dialog itself created --
        # anything else is treated as "no update", never as a green light.
        box = self._check_by_key.get(key)
        item = self._item_by_key.get(key)
        if box is None or item is None or not isinstance(key, str):
            return
        # Section 3 (safety-critical): ANY failure to actually display the
        # pinned image -- download failure, empty bytes, decode failure, a
        # null/invalid QImage, or a malformed callback payload (defensive:
        # Qt's typed signal already constrains this, but never trust it
        # blindly) -- must disable and uncheck the item, never merely show
        # placeholder text while leaving the checkbox selectable.
        if image is None or not isinstance(image, QImage) or image.isNull():
            self._disable_item(
                key,
                "Image could not be displayed and reviewed; this photo cannot be selected.",
            )
            if self._preview_key == key:
                self._render_preview(key)
            return
        self._images[key] = image
        label = self._thumbnail_labels.get(key)
        if label is not None:
            label.setPixmap(
                QPixmap.fromImage(image).scaled(
                    _PHOTO_THUMBNAIL_SIZE,
                    _PHOTO_THUMBNAIL_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        # Round-5 smaller issue: the dialog's own thumbnail download is a
        # SEPARATE fetch from the one prepare_preview already pinned as
        # reviewed_byte_fingerprint -- nothing previously proved the two
        # were the same bytes. A mismatch here means the image changed
        # between preview and the moment the dialog is actually showing it
        # to the user; disable and uncheck the item rather than let it be
        # selected on a display the app cannot vouch for (a later mint-time
        # re-check would still catch it before any upload, but the user
        # should never be offered a checkbox for content already known to
        # be stale).
        if mismatch:
            self._disable_item(
                key,
                "This image changed since it was reviewed and cannot be selected.",
                blank_thumbnail=False,
            )
        else:
            # All three conditions hold: a valid decoded QImage, a matching
            # pinned byte fingerprint (or none was required), and a callback
            # that resolved to this dialog's own current key/item. Only now
            # may the checkbox become selectable.
            self._enable_item(key)
        if self._preview_key == key:
            self._render_preview(key)

    def _enable_item(self, key: str) -> None:
        """Counterpart to ``_disable_item``: marks ``key`` as visually
        verified and, only then, makes its checkbox selectable."""
        self._visually_verified_photo_keys.add(key)
        box = self._check_by_key.get(key)
        item = self._item_by_key.get(key)
        if box is None or item is None or not item.enabled:
            return
        text = box.text()
        loading_marker = "  [loading image…]"
        if text.endswith(loading_marker):
            text = text[: -len(loading_marker)]
        box.setText(text)
        box.setEnabled(True)

    def _show_preview(self, key: str) -> None:
        self._preview_key = key
        self._render_preview(key)

    def _render_preview(self, key: str) -> None:
        self._preview_caption.setText(self._captions.get(key, key))
        image = self._images.get(key)
        if image is None:
            self._preview_label.setText("Loading…")
            self._preview_label.setPixmap(QPixmap())
            return
        self._preview_label.setText("")
        self._preview_label.setPixmap(
            QPixmap.fromImage(image).scaled(
                _PHOTO_PREVIEW_SIZE,
                _PHOTO_PREVIEW_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _confirm(self) -> None:
        if not self.preview.observed_on_string and not self.preview.taxon_name:
            QMessageBox.warning(
                self,
                "Missing required fields",
                "Neither an observed date nor a taxon/name is available to create with.",
            )
            return
        message = (
            f"Create a NEW observation on {self.preview.destination_site.value.upper()} under the account "
            f"{self.preview.destination_account_login!r}?\n\n"
            f"Source string: {self.preview.taxon_name or '(none)'}\n"
            f"Resolved destination taxon: {self.preview.resolved_taxon_name or '(none)'}"
            f"{f' (iNaturalist taxon id {self.preview.taxon_id})' if self.preview.taxon_id is not None else ''}\n"
            f"Date: {self.preview.observed_on_string or '(none)'}\n"
            f"{len(self.selected_items())} identifier/photo item(s) selected.\n\n"
            "This app CANNOT delete a created observation afterward. Creating it is not reversible "
            "through this application."
        )
        if (
            QMessageBox.question(
                self,
                "Explicit observation creation confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()


class DuplicateSetDialog(QDialog):
    """Explicit member entry. This dialog never chooses a canonical record."""

    def __init__(
        self,
        *,
        mo_ids: tuple[int, ...] = (),
        inat_ids: tuple[int, ...] = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Identify duplicate observation set")
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Enter every observation that represents the same physical specimen. "
            "This step identifies the set only; no canonical record is preselected."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.mo_ids = QLineEdit(", ".join(str(value) for value in mo_ids))
        self.inat_ids = QLineEdit(", ".join(str(value) for value in inat_ids))
        self.mo_ids.setPlaceholderText("e.g. 12345, 12346")
        self.inat_ids.setPlaceholderText("e.g. 98765")
        form.addRow("Mushroom Observer IDs", self.mo_ids)
        form.addRow("iNaturalist IDs", self.inat_ids)
        layout.addLayout(form)
        boundary = QLabel(
            "Donor observations will remain online. Phase 2B never deletes, hides, "
            "withdraws, or automatically edits a donor."
        )
        boundary.setWordWrap(True)
        layout.addWidget(boundary)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Read selected records…"
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _parse_ids(text: str) -> tuple[int, ...]:
        values: list[int] = []
        for part in re.split(r"[\s,;]+", text.strip()):
            if not part:
                continue
            value = int(part)
            if value <= 0:
                raise ValueError("Observation IDs must be positive")
            values.append(value)
        if len(values) != len(set(values)):
            raise ValueError("Each observation may appear only once")
        return tuple(values)

    def candidates(self) -> list[tuple[str, int]]:
        return [
            *(("mo", value) for value in self._parse_ids(self.mo_ids.text())),
            *(("inat", value) for value in self._parse_ids(self.inat_ids.text())),
        ]

    def _accept(self) -> None:
        try:
            candidates = self.candidates()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid observation IDs", str(exc))
            return
        counts = {
            site: sum(1 for candidate_site, _ in candidates if candidate_site == site)
            for site in ("mo", "inat")
        }
        if len(candidates) < 2 or max(counts.values(), default=0) < 2:
            QMessageBox.warning(
                self,
                "Duplicate set required",
                "Select at least two observations, with a canonical and donor on "
                "at least one site.",
            )
            return
        self.accept()


class ConsolidationPreviewDialog(QDialog):
    """Explicit canonical selection and link-only Phase 2B review."""

    def __init__(
        self, preview: ConsolidationPreview, download_image, parent=None
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self.approved_preview: Optional[ConsolidationPreview] = None
        self._download_image = download_image
        self._pool = QThreadPool.globalInstance()
        self._live_thumbnail_signals: set[_PhotoThumbnailSignals] = set()
        self._thumbnail_labels: dict[str, _ClickableImageLabel] = {}
        self._images: dict[str, QImage] = {}
        self._captions: dict[str, str] = {}
        self._preview_key: Optional[str] = None
        self._closed = False

        self.setWindowTitle("Consolidate duplicate observations")
        self.resize(1180, 820)
        layout = QVBoxLayout(self)
        retained = QLabel(
            "DONOR OBSERVATIONS WILL REMAIN ONLINE. This phase does not delete or "
            "hide them. "
            + (
                "This adds newly reviewed donors to an existing stable consolidation; "
                "canonical choices are fixed."
                if preview.is_extension
                else "Choose one canonical observation on every participating site; "
                "nothing is preselected."
            )
        )
        retained.setWordWrap(True)
        layout.addWidget(retained)

        if not preview.eligibility.eligible:
            blocked = QLabel(
                "Consolidation is blocked:\n"
                + "\n".join(
                    f"• {reason}" for reason in preview.eligibility.blocking_reasons
                )
            )
            blocked.setWordWrap(True)
            layout.addWidget(blocked)
        if preview.evidence_edges:
            identity = [
                (
                    f"{edge.left_site.value} #{edge.left_observation_id} ↔ "
                    f"{edge.right_site.value} #{edge.right_observation_id}: "
                    f"{edge.display_summary}"
                )
                for edge in preview.evidence_edges
                if edge.evidence_strength == "strong"
            ]
            supporting = [
                (
                    f"{edge.left_site.value} #{edge.left_observation_id} ↔ "
                    f"{edge.right_site.value} #{edge.right_observation_id}: "
                    f"{edge.display_summary}"
                )
                for edge in preview.evidence_edges
                if edge.evidence_strength == "corroborating"
            ]
            evidence = QLabel(
                "Identity evidence (strong):\n"
                + ("\n".join(f"• {item}" for item in identity) or "• None")
                + "\n\nSupporting evidence (corroborating only; never sufficient alone):\n"
                + ("\n".join(f"• {item}" for item in supporting) or "• None")
            )
            evidence.setWordWrap(True)
            layout.addWidget(evidence)
        if preview.eligibility.evidence_unavailable:
            unavailable = QLabel(
                "Unavailable or insufficient supporting evidence (not treated "
                "as a conflict):\n"
                + "\n".join(
                    f"• {item}" for item in preview.eligibility.evidence_unavailable
                )
            )
            unavailable.setWordWrap(True)
            layout.addWidget(unavailable)
        if preview.warnings:
            warnings = QLabel(
                "Attempt notes:\n" + "\n".join(f"• {item}" for item in preview.warnings)
            )
            warnings.setWordWrap(True)
            layout.addWidget(warnings)
        if preview.previous_members:
            previous = QLabel(
                "Previously superseded donors (read-only; unchanged by this attempt):\n"
                + "\n".join(
                    f"• {member.site.value} #{member.observation_id} — added by "
                    f"attempt #{member.added_by_attempt_id or '?'}; "
                    f"superseded by attempt #{member.superseded_by_attempt_id or '?'} "
                    f"at {member.superseded_at or 'unknown time'}"
                    for member in preview.previous_members
                )
            )
            previous.setWordWrap(True)
            layout.addWidget(previous)

        layout.addWidget(
            QLabel(
                "1. Canonical records (fixed)"
                if preview.is_extension
                else "1. Canonical records (choose explicitly) / 2. Donors retained remotely"
            )
        )
        self.table = QTableWidget(len(preview.members), 9)
        self.table.setHorizontalHeaderLabels(
            (
                "Canonical",
                "Site / ID",
                "Owner / account",
                "Taxon",
                "Date",
                "Locality / coordinates / privacy",
                "Identifiers / sequence status",
                "Reciprocal links / current pair",
                "Photos / updated",
            )
        )
        self._groups = {
            RemoteSite.MO: QButtonGroup(self),
            RemoteSite.INAT: QButtonGroup(self),
        }
        self._choices: dict[tuple[RemoteSite, int], QRadioButton] = {}
        for group in self._groups.values():
            group.setExclusive(True)
        for row_index, member in enumerate(preview.members):
            choice = QRadioButton()
            is_fixed = preview.is_extension and (
                (
                    member.site is RemoteSite.MO
                    and member.observation_id == preview.canonical_mo_observation_id
                )
                or (
                    member.site is RemoteSite.INAT
                    and member.observation_id == preview.canonical_inat_observation_id
                )
            )
            choice.setChecked(is_fixed)
            choice.setEnabled(not preview.is_extension)
            choice.setToolTip(
                "Canonical choices are fixed for an extension."
                if preview.is_extension
                else "No canonical is selected by default."
            )
            self._groups[member.site].addButton(choice)
            self._choices[(member.site, member.observation_id)] = choice
            self.table.setCellWidget(row_index, 0, choice)
            self.table.setItem(
                row_index,
                1,
                QTableWidgetItem(f"{member.site.value} #{member.observation_id}"),
            )
            self.table.setItem(
                row_index,
                2,
                QTableWidgetItem(
                    f"{member.owner_login or '?'} / owner {member.owner_id or '?'}; "
                    f"profile account {member.account_id or '?'}"
                ),
            )
            self.table.setItem(
                row_index,
                3,
                QTableWidgetItem(
                    f"{member.taxon_name or '?'} ({member.taxon_rank or 'rank unknown'}; "
                    f"id {member.taxon_id or '?'})"
                ),
            )
            self.table.setItem(
                row_index,
                4,
                QTableWidgetItem(member.observed_on_string or "unknown"),
            )
            coordinate = (
                f"{member.latitude:.5f}, {member.longitude:.5f}"
                if member.latitude is not None and member.longitude is not None
                else "coordinates unavailable"
            )
            self.table.setItem(
                row_index,
                5,
                QTableWidgetItem(
                    f"{member.locality or '?'}; {coordinate}; "
                    f"privacy={member.geoprivacy or 'unspecified'}"
                ),
            )
            identifiers = [
                *(f"voucher {value}" for value in member.voucher_identifiers),
                *(f"collection {value}" for value in member.collection_identifiers),
                *(
                    f"accession {value} (display only; not identity evidence)"
                    for value in member.accessions
                ),
                *member.sequence_summaries,
            ]
            self.table.setItem(
                row_index,
                6,
                QTableWidgetItem("; ".join(identifiers) or "none / unavailable"),
            )
            pair_text = (
                f"; confirmed pair {member.current_pair_partner_site.value} "
                f"#{member.current_pair_partner_id}"
                if member.current_pair_partner_site and member.current_pair_partner_id
                else "; no current confirmed pair"
            )
            self.table.setItem(
                row_index,
                7,
                QTableWidgetItem(member.reciprocal_link_state + pair_text),
            )
            self.table.setItem(
                row_index,
                8,
                QTableWidgetItem(
                    f"{len(member.photos)} photo(s); updated "
                    f"{member.remote_updated_at or 'unknown'}"
                ),
            )
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 2)
        self._path_label = QLabel()
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)
        descriptions = QPlainTextEdit()
        descriptions.setReadOnly(True)
        descriptions.setMaximumHeight(120)
        descriptions.setPlainText(
            "\n\n".join(
                f"{member.site.value} #{member.observation_id} description/notes:\n"
                f"{member.description or '[none]'}"
                for member in preview.members
            )
        )
        layout.addWidget(descriptions)

        body = QHBoxLayout()
        photo_side = QVBoxLayout()
        photo_side.addWidget(
            QLabel("Photo thumbnails (view-only; transfer is disabled)")
        )
        photo_scroll = QScrollArea()
        photo_scroll.setWidgetResizable(True)
        photo_holder = QWidget()
        photo_row = QHBoxLayout(photo_holder)
        photo_count = 0
        for member in preview.members:
            for photo in member.photos:
                photo_count += 1
                key = f"{member.site.value}:{member.observation_id}:{photo.photo_id}"
                caption = (
                    f"{member.site.value} #{member.observation_id}, photo {photo.photo_id}\n"
                    f"license={photo.license_label or 'unknown'}; "
                    f"holder={photo.copyright_holder or 'unknown'}"
                )
                self._captions[key] = caption
                thumb = _ClickableImageLabel("loading…")
                thumb.setFixedSize(_PHOTO_THUMBNAIL_SIZE, _PHOTO_THUMBNAIL_SIZE)
                thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                thumb.setFrameShape(QFrame.Shape.Box)
                self._thumbnail_labels[key] = thumb
                thumb.clicked.connect(lambda _k=key: self._show_photo(_k))
                thumb.doubleClicked.connect(lambda _k=key: self._show_photo(_k))
                if photo.source_url:
                    self._request_photo(key, photo.source_url)
                wrapper = QWidget()
                wrapper_layout = QVBoxLayout(wrapper)
                wrapper_layout.addWidget(thumb)
                wrapper_layout.addWidget(
                    QLabel(
                        f"{member.site.value} {member.observation_id}/{photo.photo_id}"
                    )
                )
                photo_row.addWidget(wrapper)
        if not photo_count:
            photo_row.addWidget(QLabel("No photos returned."))
        photo_row.addStretch(1)
        photo_scroll.setWidget(photo_holder)
        photo_side.addWidget(photo_scroll)
        body.addLayout(photo_side, 3)
        preview_side = QVBoxLayout()
        self._photo_preview = QLabel("Click a thumbnail for a larger preview")
        self._photo_preview.setMinimumSize(280, 220)
        self._photo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._photo_preview.setFrameShape(QFrame.Shape.Box)
        self._photo_caption = QLabel("")
        self._photo_caption.setWordWrap(True)
        preview_side.addWidget(self._photo_preview)
        preview_side.addWidget(self._photo_caption)
        body.addLayout(preview_side, 2)
        layout.addLayout(body, 1)

        layout.addWidget(QLabel("3. Items that will be copied"))
        copied = QLabel(
            "No optional donor data item is enabled in this release. The only possible "
            "remote writes are additive canonical reciprocal links."
        )
        copied.setWordWrap(True)
        layout.addWidget(copied)

        layout.addWidget(QLabel("4. Conflicts requiring a decision"))
        conflicts = QLabel(
            "\n".join(f"• {item.description}" for item in preview.conflicts)
            or "• No display-level conflicts found."
        )
        conflicts.setWordWrap(True)
        layout.addWidget(conflicts)

        layout.addWidget(QLabel("5. Unsupported fields that will not be transferred"))
        self.unsupported = QTableWidget(len(preview.unsupported_items), 3)
        self.unsupported.setHorizontalHeaderLabels(("Select", "Item type", "Reason"))
        for row_index, item in enumerate(preview.unsupported_items):
            disabled = QCheckBox()
            disabled.setEnabled(False)
            disabled.setToolTip(item.disabled_reason)
            self.unsupported.setCellWidget(row_index, 0, disabled)
            self.unsupported.setItem(row_index, 1, QTableWidgetItem(item.description))
            self.unsupported.setItem(
                row_index, 2, QTableWidgetItem(item.disabled_reason)
            )
        self.unsupported.resizeColumnsToContents()
        layout.addWidget(self.unsupported)

        local = QLabel(
            "6. Local changes after verification: when both sites participate, the chosen "
            "cross-site pair is confirmed; every donor is marked locally superseded and "
            "leaves normal candidate queues.\n"
            "7. Explicitly deferred deletion work: donor links may remain and any donor "
            "deletion requires a separately reviewed Phase 2C."
        )
        local.setWordWrap(True)
        layout.addWidget(local)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("Review final consolidation confirmation…")
        self._ok.setEnabled(False)
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for button in self._choices.values():
            button.toggled.connect(self._refresh_evidence_paths)
        self._refresh_evidence_paths()

    def closeEvent(self, event) -> None:
        self._closed = True
        super().closeEvent(event)

    def _request_photo(self, key: str, url: str) -> None:
        worker = _PhotoThumbnailWorker(
            key, url, self._download_image, lambda: self._closed
        )
        signals = worker.signals
        self._live_thumbnail_signals.add(signals)
        signals.done.connect(
            lambda k, image, mismatch, s=signals: self._photo_loaded(s, k, image)
        )
        self._pool.start(worker)

    def _photo_loaded(
        self,
        signals: _PhotoThumbnailSignals,
        key: str,
        image: Optional[QImage],
    ) -> None:
        self._live_thumbnail_signals.discard(signals)
        if self._closed:
            return
        label = self._thumbnail_labels.get(key)
        if image is None or image.isNull():
            if label is not None:
                label.setText("unavailable")
            return
        self._images[key] = image
        if label is not None:
            label.setPixmap(
                QPixmap.fromImage(image).scaled(
                    _PHOTO_THUMBNAIL_SIZE,
                    _PHOTO_THUMBNAIL_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        if self._preview_key == key:
            self._show_photo(key)

    def _show_photo(self, key: str) -> None:
        self._preview_key = key
        self._photo_caption.setText(self._captions.get(key, key))
        image = self._images.get(key)
        if image is None:
            self._photo_preview.setText("Loading or unavailable")
            self._photo_preview.setPixmap(QPixmap())
            return
        self._photo_preview.setText("")
        self._photo_preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                _PHOTO_PREVIEW_SIZE,
                _PHOTO_PREVIEW_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _selected_id(self, site: RemoteSite) -> Optional[int]:
        return next(
            (
                observation_id
                for (member_site, observation_id), button in self._choices.items()
                if member_site is site and button.isChecked()
            ),
            None,
        )

    def _refresh_evidence_paths(self) -> None:
        try:
            selected = select_canonical(
                self.preview,
                self._selected_id(RemoteSite.MO),
                self._selected_id(RemoteSite.INAT),
            )
        except Exception as exc:
            self._path_label.setText(f"Donor evidence paths: {exc}")
            self._ok.setEnabled(False)
            return
        if not selected.eligibility.eligible:
            valid_paths = "\n".join(
                f"• {path.donor_site.value} #{path.donor_observation_id}\n  "
                + "\n  ".join(path.steps)
                + f"\n  Required strong anchor: {path.strong_anchor_step}"
                for path in selected.donor_evidence_paths
            )
            self._path_label.setText(
                "Donor evidence paths are incomplete:\n"
                + "\n".join(
                    f"• {reason}" for reason in selected.eligibility.blocking_reasons
                )
                + (
                    "\n\nValid strong-anchored paths in this blocked set:\n"
                    + valid_paths
                    if valid_paths
                    else ""
                )
            )
            self._ok.setEnabled(False)
            return
        self._path_label.setText(
            "Reviewed donor evidence paths (each names its required strong anchor):\n"
            + "\n".join(
                f"• {path.donor_site.value} #{path.donor_observation_id}\n  "
                + "\n  ".join(path.steps)
                + f"\n  Required strong anchor: {path.strong_anchor_step}"
                for path in selected.donor_evidence_paths
            )
        )
        self._ok.setEnabled(
            len(selected.donor_evidence_paths) == len(selected.donor_members)
        )

    def _confirm(self) -> None:
        try:
            selected = select_canonical(
                self.preview,
                self._selected_id(RemoteSite.MO),
                self._selected_id(RemoteSite.INAT),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Canonical selection required", str(exc))
            return
        if not selected.eligibility.eligible:
            QMessageBox.warning(
                self,
                "Evidence path required",
                "\n".join(selected.eligibility.blocking_reasons),
            )
            return
        donors = (
            ", ".join(
                f"{member.site.value} #{member.observation_id}"
                for member in selected.donor_members
            )
            or "none"
        )
        accounts = ", ".join(
            f"{member.site.value} {member.owner_login} (id {member.account_id})"
            for member in selected.canonical_members
        )
        canonical_taxa = ", ".join(
            f"{member.site.value} {member.taxon_name or 'unknown'} "
            f"({member.taxon_rank or 'rank unknown'}; id {member.taxon_id or 'unknown'})"
            for member in selected.canonical_members
        )
        write_types: list[str] = []
        if (
            selected.canonical_mo_observation_id is not None
            and selected.canonical_inat_observation_id is not None
        ):
            mo_member = next(
                member
                for member in selected.canonical_members
                if member.site is RemoteSite.MO
            )
            inat_member = next(
                member
                for member in selected.canonical_members
                if member.site is RemoteSite.INAT
            )
            if not any(
                row.target_observation_id == selected.canonical_inat_observation_id
                for row in mo_member.reciprocal_links
            ):
                write_types.append("MO reciprocal-link addition")
            if not any(
                row.target_observation_id == selected.canonical_mo_observation_id
                for row in inat_member.reciprocal_links
            ):
                write_types.append("iNaturalist reciprocal-link addition")
        message = (
            f"Canonical MO: {selected.canonical_mo_observation_id or 'not participating'}\n"
            f"Canonical iNaturalist: {selected.canonical_inat_observation_id or 'not participating'}\n"
            f"Canonical taxon choice(s): {canonical_taxa}\n"
            f"Donors retained online: {donors}\n"
            f"Destination accounts: {accounts}\n"
            f"Selected remote write actions: {len(write_types)}"
            + (
                f" ({'; '.join(write_types)})"
                if write_types
                else " (verification only)"
            )
            + "\n\nDonors remain remotely unchanged. They may retain old reciprocal links. "
            "Deletion, hiding, withdrawal, or cleanup requires a later Phase 2C review.\n\n"
            "Journal and execute this immutable consolidation attempt?"
        )
        if (
            QMessageBox.question(
                self,
                "Final non-destructive consolidation confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.approved_preview = selected
            self.accept()


class DonorDeletionPreviewDialog(QDialog):
    """Lossless-only readiness review; blocked rows are non-interactive."""

    def __init__(self, preview: DonorDeletionPreview, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.approved_member_ids: tuple[int, ...] = ()
        self._checks: dict[int, QCheckBox] = {}
        self.setWindowTitle("Review donor deletion")
        self.resize(1180, 760)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "DELETION IS PERMANENT. Donor observations and their remote history "
            "may not be recoverable.\n\n"
            "This phase deletes only donors whose supported content is fully "
            "preserved. It does not merge or discard unique data."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        canonical = QLabel(
            f"Canonical MO: {preview.canonical_mo_observation_id or 'not participating'}\n"
            "Canonical iNaturalist: "
            f"{preview.canonical_inat_observation_id or 'not participating'}\n"
            f"Finalized Phase 2B baseline: attempt "
            f"#{preview.base_finalized_attempt_id}"
        )
        canonical.setWordWrap(True)
        layout.addWidget(canonical)
        if preview.warnings:
            notice = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            notice.setWordWrap(True)
            layout.addWidget(notice)

        self.table = QTableWidget(len(preview.donors), 9)
        self.table.setHorizontalHeaderLabels(
            (
                "Delete",
                "Donor",
                "Owner / stable identity",
                "Phase 2B provenance",
                "Updated",
                "Content inventory",
                "Parity",
                "Third-party / dependencies",
                "Eligibility",
            )
        )
        for row_index, donor in enumerate(preview.donors):
            check = QCheckBox()
            check.setChecked(False)
            check.setEnabled(donor.eligible)
            check.setToolTip(
                "Nothing starts selected."
                if donor.eligible
                else "\n".join(donor.blocking_reasons)
            )
            self._checks[donor.stable_member_id] = check
            self.table.setCellWidget(row_index, 0, check)
            self.table.setItem(
                row_index,
                1,
                QTableWidgetItem(f"{donor.site.value.upper()} #{donor.observation_id}"),
            )
            self.table.setItem(
                row_index,
                2,
                QTableWidgetItem(
                    f"{donor.owner_account}; UUID/stable id="
                    f"{donor.remote_uuid or '[none]'}"
                ),
            )
            self.table.setItem(
                row_index,
                3,
                QTableWidgetItem(
                    f"admitted by #{donor.admitting_attempt_id}; "
                    f"{donor.evidence_path}"
                ),
            )
            self.table.setItem(
                row_index,
                4,
                QTableWidgetItem(donor.remote_updated_at or "unavailable"),
            )
            self.table.setItem(
                row_index,
                5,
                QTableWidgetItem(
                    "\n".join(
                        f"{item.content_type}: {item.safe_summary} "
                        f"[{item.identity}]"
                        for item in donor.content_inventory
                    )
                    or "No supported content returned"
                ),
            )
            self.table.setItem(
                row_index,
                6,
                QTableWidgetItem(
                    "\n".join(
                        f"{'preserved' if item.preserved else 'BLOCKED'} — "
                        f"{item.content_type}/{item.source_identity}: "
                        f"{item.match_method or item.blocking_reason}"
                        for item in donor.parity_items
                    )
                    or "No parity rows"
                ),
            )
            activity = [
                *(item.safe_summary for item in donor.third_party),
                *(item.safe_summary for item in donor.dependencies),
            ]
            self.table.setItem(
                row_index,
                7,
                QTableWidgetItem("\n".join(activity) or "None detected"),
            )
            self.table.setItem(
                row_index,
                8,
                QTableWidgetItem(
                    donor.status
                    + (
                        "\n" + "\n".join(donor.blocking_reasons[1:])
                        if len(donor.blocking_reasons) > 1
                        else ""
                    )
                ),
            )
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Review destructive confirmation…")
        ok.setEnabled(False)
        for check in self._checks.values():
            check.toggled.connect(
                lambda _checked, button=ok: button.setEnabled(
                    bool(self.selected_member_ids())
                )
            )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_member_ids(self) -> tuple[int, ...]:
        eligible = {
            item.stable_member_id for item in self.preview.donors if item.eligible
        }
        return tuple(
            item.stable_member_id
            for item in self.preview.donors
            if (
                item.stable_member_id in eligible
                and self._checks[item.stable_member_id].isEnabled()
                and self._checks[item.stable_member_id].isChecked()
            )
        )

    def _confirm(self) -> None:
        selected = self.selected_member_ids()
        if not selected:
            return
        lookup = {item.stable_member_id: item for item in self.preview.donors}
        chosen = [lookup[value] for value in selected]
        donor_labels = ", ".join(
            f"{item.site.value.upper()} #{item.observation_id}" for item in chosen
        )
        photos = sum(
            1
            for item in chosen
            for content in item.content_inventory
            if content.content_type == "photo"
        )
        objects = sum(len(item.content_inventory) for item in chosen)
        first_message = (
            f"Remote observations to delete: {len(chosen)}\n"
            f"Donors: {donor_labels}\n"
            f"Canonical MO: "
            f"{self.preview.canonical_mo_observation_id or 'not participating'}\n"
            f"Canonical iNaturalist: "
            f"{self.preview.canonical_inat_observation_id or 'not participating'}\n"
            f"Photos affected: {photos}; reviewed content objects: {objects}\n"
            "Lossless parity passed for every selected donor.\n"
            "No third-party contribution was detected.\n\n"
            "Deletion is permanent and no rollback recreation will be attempted.\n"
            "Continue to typed confirmation?"
        )
        if (
            QMessageBox.question(
                self,
                "Permanent donor deletion",
                first_message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        phrase = self.preview.typed_phrase(selected)
        typed, accepted = QInputDialog.getText(
            self,
            "Typed permanent-deletion confirmation",
            f"Type exactly:\n{phrase}",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not accepted:
            return
        if typed != phrase:
            QMessageBox.warning(
                self,
                "Confirmation rejected",
                "The typed phrase did not exactly match this reviewed plan.",
            )
            return
        # Recollect after typing. Any selection change invalidates the phrase
        # and requires both confirmation stages again.
        if self.selected_member_ids() != selected:
            QMessageBox.warning(
                self,
                "Selection changed",
                "The donor selection changed; restart confirmation.",
            )
            return
        self.approved_member_ids = selected
        self.accept()


class NameProposalDialog(QDialog):
    """Reviewed name proposal in either direction, respecting each site's model."""

    def __init__(self, preview: NameProposalPreview, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("Propose name")
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Confirmed pair: MO {preview.mo_observation_id} ↔ iNaturalist "
            f"{preview.inat_observation_id}\n"
            f"iNaturalist current name: {preview.inat_current_name or '—'}\n"
            f"Mushroom Observer current name: {preview.mo_current_name or '—'}"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._choices: list[tuple[QRadioButton, NameProposalCandidate]] = []
        self.table = QTableWidget(len(preview.candidates), 5)
        self.table.setHorizontalHeaderLabels(
            ("Run", "Target", "Proposed", "Rank / synonyms", "Availability")
        )
        for row_index, candidate in enumerate(preview.candidates):
            choice = QRadioButton()
            choice.setEnabled(candidate.enabled)
            choice.setToolTip(candidate.disabled_reason)
            self._buttons.addButton(choice)
            self.table.setCellWidget(row_index, 0, choice)
            target = (
                "iNaturalist identification"
                if candidate.target_site is RemoteSite.INAT
                else "Mushroom Observer proposal"
            )
            self.table.setItem(row_index, 1, QTableWidgetItem(target))
            self.table.setItem(
                row_index,
                2,
                QTableWidgetItem(
                    f"{candidate.source_name} → {candidate.proposed_name}"
                ),
            )
            detail = candidate.proposed_rank or "?"
            if candidate.synonyms:
                detail += "; synonyms: " + ", ".join(candidate.synonyms[:4])
            if not candidate.kingdom_compatible:
                detail += "; incompatible kingdom"
            self.table.setItem(row_index, 3, QTableWidgetItem(detail))
            availability = "ready" if candidate.enabled else candidate.disabled_reason
            self.table.setItem(row_index, 4, QTableWidgetItem(availability))
            self._choices.append((choice, candidate))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)

        note = QLabel(
            "iNaturalist proposals are delegated to the Identify subsystem (queued, paused by default). "
            "Mushroom Observer proposals are tracked as pending; per-observation submission is not yet wired."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        if preview.warnings:
            warning = QLabel("\n".join(f"• {item}" for item in preview.warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Confirm one proposal…"
        )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_candidate(self) -> Optional[NameProposalCandidate]:
        return next(
            (candidate for button, candidate in self._choices if button.isChecked()),
            None,
        )

    def _confirm(self) -> None:
        candidate = self.selected_candidate()
        if candidate is None:
            QMessageBox.warning(
                self, "No candidate selected", "Select one enabled name proposal."
            )
            return
        if candidate.string_similarity_only:
            QMessageBox.warning(
                self,
                "Unresolved name",
                "This candidate could not be resolved to an exact taxon and cannot be proposed.",
            )
            return
        if candidate.target_site is RemoteSite.INAT:
            message = (
                "Queue an iNaturalist identification for this observation via the Identify subsystem?\n\n"
                f"{candidate.source_name} → {candidate.proposed_name}\n\n"
                "It is enqueued paused; review and submit it in the Identify window."
            )
        else:
            message = (
                "Record a Mushroom Observer name proposal?\n\n"
                f"{candidate.source_name} → {candidate.proposed_name}\n\n"
                "It is tracked as pending and does NOT immediately change the consensus name. "
                "Per-observation submission is not yet available."
            )
        if (
            QMessageBox.question(
                self,
                "Explicit name proposal confirmation",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()


class ReconciliationSetupDialog(QDialog):
    """Resolve exact account identities before creating or selecting a profile."""

    def __init__(
        self,
        coordinator: ReconciliationCoordinator,
        authenticated_login: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Reconciliation profile")
        self.setMinimumWidth(560)
        self.coordinator = coordinator
        self.profile: Optional[ReconciliationProfile] = None
        self._inat_user: Optional[dict[str, Any]] = None
        self._mo_user: Optional[dict[str, Any]] = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("Create a new profile…", None)
        for profile in coordinator.profiles():
            self.profile_combo.addItem(
                f"{profile.inat_login} ↔ {profile.mo_login}  (iNat {profile.inat_user_id}, MO {profile.mo_user_id})",
                profile.profile_id,
            )
        self.inat_login = QLineEdit(authenticated_login)
        # The iNaturalist login comes free from the authenticated session; the
        # MO username has no such source, so carry the last one forward.
        self.mo_login = QLineEdit(coordinator.settings.reconciliation_last_mo_login)
        self.inat_status = QLabel("Not resolved")
        self.mo_status = QLabel("Not resolved")
        form.addRow("Existing profile", self.profile_combo)
        form.addRow("iNaturalist login", self.inat_login)
        form.addRow("Mushroom Observer username", self.mo_login)
        form.addRow("iNaturalist account", self.inat_status)
        form.addRow("MO account", self.mo_status)
        layout.addLayout(form)
        self.availability = QLabel()
        self.availability.setWordWrap(True)
        layout.addWidget(self.availability)
        button_row = QHBoxLayout()
        self.resolve_button = QPushButton("Resolve exact accounts")
        self.resolve_button.clicked.connect(self._resolve)
        button_row.addWidget(self.resolve_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setEnabled(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        coordinator.accounts_resolved.connect(self._resolved)
        coordinator.account_resolution_failed.connect(self._failed)
        self._connected = True
        self._profile_changed()

    def _disconnect_coordinator(self) -> None:
        """Drop this dialog's coordinator subscriptions exactly once.

        Both exits must do this. A dialog that only disconnected on reject
        stayed subscribed for the lifetime of its parent window, so every
        later "Resolve exact accounts" also drove the labels of dismissed
        dialogs, one more listener per accepted setup. Idempotent, because
        reject() is reachable from the button box, Escape, and closeEvent.
        """
        if not self._connected:
            return
        self._connected = False
        self.coordinator.accounts_resolved.disconnect(self._resolved)
        self.coordinator.account_resolution_failed.disconnect(self._failed)

    def _profile_changed(self) -> None:
        profile_id = self.profile_combo.currentData()
        existing = profile_id is not None
        self.inat_login.setEnabled(not existing)
        self.mo_login.setEnabled(not existing)
        self.resolve_button.setEnabled(not existing)
        self.ok_button.setEnabled(existing)
        if existing:
            profile = self.coordinator.db.profile(int(profile_id))
            self.inat_status.setText(
                f"Exact: {profile.inat_login} (numeric ID {profile.inat_user_id})"
            )
            self.mo_status.setText(
                f"Exact: {profile.mo_login} (numeric ID {profile.mo_user_id})"
            )
            self.availability.setText(self._availability_text(profile))
        else:
            self.inat_status.setText("Not resolved")
            self.mo_status.setText("Not resolved")
            self.availability.clear()

    def _resolve(self) -> None:
        if not self.inat_login.text().strip() or not self.mo_login.text().strip():
            QMessageBox.warning(self, "Accounts required", "Enter both account names.")
            return
        self.resolve_button.setEnabled(False)
        self.ok_button.setEnabled(False)
        self.inat_status.setText("Resolving…")
        self.mo_status.setText("Resolving…")
        # Remember what was TYPED, before knowing whether it resolves. A name
        # that failed is exactly the one worth handing back next time, since
        # the usual reason to reopen this dialog is to correct a typo in it.
        self.coordinator.settings.reconciliation_last_mo_login = self.mo_login.text()
        self.coordinator.resolve_accounts(self.inat_login.text(), self.mo_login.text())

    def _resolved(self, inat_user: object, mo_user: object) -> None:
        self._inat_user = dict(inat_user)  # type: ignore[arg-type]
        self._mo_user = dict(mo_user)  # type: ignore[arg-type]
        inat_name = str(self._inat_user.get("login") or "")
        mo_name = mo_login_of(self._mo_user)
        self.inat_status.setText(
            f"Exact: {inat_name} (numeric ID {self._inat_user.get('id')})"
        )
        self.mo_status.setText(
            f"Exact: {mo_name} (numeric ID {self._mo_user.get('id')})"
        )
        # Upgrade the remembered name to MO's canonical spelling now that one
        # exists, so a prefill never reintroduces the user's casing variant.
        if mo_name:
            self.coordinator.settings.reconciliation_last_mo_login = mo_name
        self.resolve_button.setEnabled(True)
        self.ok_button.setEnabled(True)
        auth = self.coordinator.auth_provider()
        if not auth.is_authenticated or auth.login.casefold() != inat_name.casefold():
            self.availability.setText(
                "Public scanning is available. The deleted feed, private coordinates, and some reciprocal-link "
                "validation are unavailable because authentication does not match this iNaturalist account."
            )
        else:
            self.availability.setText(
                "Authentication matches; authorized read-only validation is available."
            )

    def _failed(self, message: str) -> None:
        self.inat_status.setText("Resolution failed")
        self.mo_status.setText("Resolution failed")
        self.resolve_button.setEnabled(True)
        QMessageBox.warning(self, "Account resolution failed", message)

    def _availability_text(self, profile: ReconciliationProfile) -> str:
        auth = self.coordinator.auth_provider()
        if (
            auth.is_authenticated
            and auth.login.casefold() == profile.inat_login.casefold()
        ):
            return (
                "Authentication matches; authorized read-only validation is available."
            )
        return (
            "Public scanning is available. Authentication does not match, so the deleted feed, private "
            "coordinates, and some reciprocal-link validation are unavailable."
        )

    def _accept(self) -> None:
        profile_id = self.profile_combo.currentData()
        if profile_id is not None:
            self.profile = self.coordinator.db.profile(int(profile_id))
        elif self._inat_user and self._mo_user:
            self.profile = self.coordinator.save_profile(self._inat_user, self._mo_user)
        if self.profile is None:
            return
        self._disconnect_coordinator()
        self.accept()

    def reject(self) -> None:
        self._disconnect_coordinator()
        super().reject()


class ReconciliationTableModel(QAbstractTableModel):
    HEADERS = (
        "Type",
        "Site",
        "Record",
        "Other record",
        "Reconciliation score",
        "State",
        "Summary",
        "Updated",
        "Photo preview",
        "Photo similarity",
        "Confirm",
        "Reject",
    )
    KEYS = (
        "kind",
        "site",
        "remote_id",
        "other_id",
        "score",
        "state",
        "title",
        "updated_at",
        "_photo_preview",
        "_photo_summary",
        "_confirm",
        "_reject",
    )
    PHOTO_PREVIEW_COLUMN = 8
    PHOTO_SUMMARY_COLUMN = 9
    CONFIRM_COLUMN = 10
    REJECT_COLUMN = 11

    def __init__(self, coordinator: ReconciliationCoordinator, parent=None) -> None:
        super().__init__(parent)
        self.coordinator = coordinator
        self.profile_id = 0
        self.category = "link_issues"
        self.rows: list[dict[str, Any]] = []
        # None means "not known yet": paging stops when a page comes back
        # short, so the row count never has to be asked for up front. An
        # unqueried model starts at 0 so it fetches nothing.
        self.total: Optional[int] = 0
        self.sort_column = "updated_at"
        self.descending = True

    def set_query(self, profile_id: int, category: str) -> None:
        """Point the model at a category WITHOUT counting it first.

        The count used to be taken here, synchronously, on the GUI thread --
        on every category switch, every reload, and every header sort. That is
        the same whole-category COUNT that _DashboardCountWorker was moved off
        the GUI thread for, so leaving it here meant _reload() still froze the
        UI for it, and paid for it twice: once inline and once in the worker.
        The sidebar counts come from that worker; the view only ever needed to
        know whether MORE rows exist, which canFetchMore now infers.
        """
        self.beginResetModel()
        self.profile_id, self.category = profile_id, category
        self.rows = []
        self.total = None
        self.endResetModel()
        self.fetchMore()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = index.column()
        key = self.KEYS[column]
        candidate = (
            self.category == "candidate_pairs"
            and row.get("kind") == "pair"
            and row.get("state") == "candidate"
        )
        if role == Qt.ItemDataRole.DisplayRole:
            if column == self.PHOTO_PREVIEW_COLUMN:
                return ""
            if column == self.PHOTO_SUMMARY_COLUMN:
                return row.get("_photo_summary", "waiting…") if candidate else ""
            if column == self.CONFIRM_COLUMN:
                return "✓" if candidate and row.get("_photo_ready") else ""
            if column == self.REJECT_COLUMN:
                return "✕" if candidate and row.get("_photo_ready") else ""
            value = row.get(key)
            return "" if value is None else str(value)
        if (
            role == Qt.ItemDataRole.DecorationRole
            and column == self.PHOTO_PREVIEW_COLUMN
        ):
            return row.get("_photo_preview")
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {
            self.PHOTO_SUMMARY_COLUMN,
            self.CONFIRM_COLUMN,
            self.REJECT_COLUMN,
        }:
            return int(Qt.AlignmentFlag.AlignCenter)
        if (
            role == Qt.ItemDataRole.BackgroundRole
            and candidate
            and row.get("_photo_ready")
        ):
            if column == self.CONFIRM_COLUMN:
                return QColor("#d9f2df")
            if column == self.REJECT_COLUMN:
                return QColor("#f7d7d7")
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and candidate
            and row.get("_photo_ready")
        ):
            if column == self.CONFIRM_COLUMN:
                return QColor("#167236")
            if column == self.REJECT_COLUMN:
                return QColor("#a51d24")
        if role == Qt.ItemDataRole.ToolTipRole:
            if column in {self.PHOTO_PREVIEW_COLUMN, self.PHOTO_SUMMARY_COLUMN}:
                return row.get(
                    "_photo_tooltip",
                    "Photo analysis is lazy-loaded for the current row and the next few candidates.",
                )
            if column == self.CONFIRM_COLUMN:
                return (
                    "Confirm this observation pair locally and advance (C)."
                    if row.get("_photo_ready")
                    else "Quick confirmation is available after every photo loads."
                )
            if column == self.REJECT_COLUMN:
                return (
                    "Reject this candidate locally and advance (X)."
                    if row.get("_photo_ready")
                    else "Quick rejection is available after every photo loads."
                )
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role=Qt.ItemDataRole.DisplayRole,
    ):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        if parent.isValid():
            return False
        return self.total is None or len(self.rows) < self.total

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or not self.canFetchMore(parent):
            return
        incoming = self.coordinator.db.dashboard_rows(
            self.profile_id,
            self.category,
            len(self.rows),
            PAGE_SIZE,
            sort_column=self.sort_column,
            descending=self.descending,
        )
        if not incoming:
            self.total = len(self.rows)
            return
        first = len(self.rows)
        self.beginInsertRows(QModelIndex(), first, first + len(incoming) - 1)
        self.rows.extend(incoming)
        self.endInsertRows()
        if len(incoming) < PAGE_SIZE:
            # PAGE_SIZE is the LIMIT that was asked for, so a short page is the
            # last one. Settling the total here rather than on the next empty
            # page saves a whole extra query per category.
            self.total = len(self.rows)

    def sort(
        self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
    ) -> None:
        if 0 <= column < self.PHOTO_PREVIEW_COLUMN:
            self.sort_column = self.KEYS[column]
            self.descending = order == Qt.SortOrder.DescendingOrder
            self.set_query(self.profile_id, self.category)

    def row(self, index: QModelIndex) -> Optional[dict[str, Any]]:
        return (
            self.rows[index.row()]
            if index.isValid() and index.row() < len(self.rows)
            else None
        )

    def row_for_pair(self, pair_id: int) -> Optional[int]:
        return next(
            (
                index
                for index, row in enumerate(self.rows)
                if int(row.get("pair_id") or 0) == pair_id
            ),
            None,
        )

    def set_photo_loading(self, pair_id: int) -> None:
        row_index = self.row_for_pair(pair_id)
        if row_index is None:
            return
        self.rows[row_index]["_photo_summary"] = "loading…"
        self.rows[row_index][
            "_photo_tooltip"
        ] = "Reading both complete photo sets and calculating perceptual hashes."
        self.dataChanged.emit(
            self.index(row_index, self.PHOTO_PREVIEW_COLUMN),
            self.index(row_index, self.REJECT_COLUMN),
        )

    def set_photo_analysis(
        self,
        pair_id: int,
        preview: Optional[QPixmap],
        summary: str,
        tooltip: str,
        *,
        ready: bool,
    ) -> None:
        row_index = self.row_for_pair(pair_id)
        if row_index is None:
            return
        row = self.rows[row_index]
        row["_photo_preview"] = preview
        row["_photo_summary"] = summary
        row["_photo_tooltip"] = tooltip
        row["_photo_ready"] = ready
        self.dataChanged.emit(
            self.index(row_index, self.PHOTO_PREVIEW_COLUMN),
            self.index(row_index, self.REJECT_COLUMN),
        )

    def clear_photo_analysis(self, pair_id: int) -> None:
        """Return a row to the un-analysed state so it is queued again."""
        row_index = self.row_for_pair(pair_id)
        if row_index is None:
            return
        row = self.rows[row_index]
        for key in (
            "_photo_preview",
            "_photo_summary",
            "_photo_tooltip",
            "_photo_ready",
        ):
            row.pop(key, None)
        self.dataChanged.emit(
            self.index(row_index, self.PHOTO_PREVIEW_COLUMN),
            self.index(row_index, self.REJECT_COLUMN),
        )

    def remove_pair(self, pair_id: int) -> Optional[int]:
        row_index = self.row_for_pair(pair_id)
        if row_index is None:
            return None
        self.beginRemoveRows(QModelIndex(), row_index, row_index)
        self.rows.pop(row_index)
        if self.total is not None:
            self.total = max(0, self.total - 1)
        self.endRemoveRows()
        return row_index


class _DashboardCountSignals(QObject):
    finished = Signal(int, object)


class _DashboardCountWorker(QRunnable):
    """Count every dashboard category without blocking Qt's GUI thread."""

    def __init__(
        self,
        coordinator: ReconciliationCoordinator,
        profile_id: int,
        generation: int,
    ) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.profile_id = profile_id
        self.generation = generation
        self.signals = _DashboardCountSignals()

    def run(self) -> None:
        counts: dict[str, object] = {}
        try:
            for key, _label in CATEGORIES:
                counts[key] = self.coordinator.db.dashboard_count(self.profile_id, key)
        except Exception as exc:
            counts = {"__error__": str(exc)}
        finally:
            self.coordinator.db.close_thread_connection()
        try:
            self.signals.finished.emit(self.generation, counts)
        except RuntimeError:
            pass


class _FlowLayout(QLayout):
    """Left-to-right layout that WRAPS onto additional rows when it runs out of width.

    Exists because this window's action row has thirteen buttons with long
    labels. In a QHBoxLayout that row cannot wrap, so its width became the
    window's minimum width — 2663 logical pixels, which on a scale-2 4K panel
    is a 5326-physical-pixel window that opens wider than the display and
    cannot be resized down.

    The one property that matters is ``minimumSize``: it reports the WIDEST
    SINGLE ITEM rather than the sum, so the window can shrink to any usable
    width and the buttons reflow. ``hasHeightForWidth`` is what lets the
    surrounding QVBoxLayout give back the extra rows' height.
    """

    def __init__(self, parent=None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)

    # QLayout plumbing -------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802 - Qt override
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802 - Qt override
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt override
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._reflow(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt override
        super().setGeometry(rect)
        self._reflow(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt override
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _reflow(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        left = rect.x() + margins.left()
        right = rect.right() - margins.right()
        x, y = left, rect.y() + margins.top()
        row_height = 0
        for item in self._items:
            hint = item.sizeHint()
            if x > left and x + hint.width() > right:
                x = left
                y += row_height + self.spacing()
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            row_height = max(row_height, hint.height())
        return y + row_height + margins.bottom() - rect.y()


class ReconciliationWindow(QMainWindow):
    def __init__(
        self,
        coordinator: ReconciliationCoordinator,
        profile: ReconciliationProfile,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(
            f"MO ↔ iNaturalist Reconciliation — {profile.inat_login} / {profile.mo_login}"
        )
        self.resize(1180, 760)
        # Never open larger than the display. Qt will happily honour a resize
        # (or a layout's minimum) that puts the title bar's edges off-screen,
        # and a window wider than the panel cannot be dragged back into reach.
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(self.width(), available.width() - 40),
                min(self.height(), available.height() - 60),
            )
        self.coordinator = coordinator
        self.profile = profile
        self.model = ReconciliationTableModel(coordinator, self)
        self._thumbnail_cache: dict[MediaIdentity, QPixmap] = {}
        self._pending_thumbnails: set[MediaIdentity] = set()
        self._selected_thumbnail_identities: set[MediaIdentity] = set()
        self._thumbnail_sources: dict[MediaIdentity, str] = {}
        self._detail_payloads: dict[str, object] = {}
        self._selected_pair: Optional[tuple[int, int]] = None
        self._selected_detail: Optional[dict[str, Any]] = None
        self._ui_busy = False
        self._count_generation = 0
        self._count_pool = QThreadPool(self)
        self._count_pool.setMaxThreadCount(1)
        self._live_count_signals: set[QObject] = set()
        self._photo_prefetch_generation = 0
        self._photo_prefetch_token = _PhotoPrefetchToken()
        self._photo_prefetch_pool = QThreadPool(self)
        self._photo_prefetch_pool.setMaxThreadCount(2)
        self._pending_photo_analyses: set[int] = set()
        self._live_photo_analysis_signals: set[QObject] = set()
        # Finished analyses, keyed by pair_id and surviving the model resets
        # that a reload performs. Without this, every decision, sort, or
        # category revisit would re-download BOTH complete photo sets for every
        # visible candidate -- rate-limited iNaturalist reads and lock-
        # serialized Mushroom Observer reads for work already done. Entries are
        # the model's presentation values, not the decoded images, so the
        # QImages are released as soon as the mosaic has been rendered.
        self._photo_analysis_cache: dict[int, dict[str, Any]] = {}
        # Scrolling emits valueChanged for every step of the wheel or the
        # scrollbar drag; coalesce them so one gesture queues one batch.
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(120)
        self._prefetch_timer.timeout.connect(self._schedule_candidate_photo_prefetch)
        self._last_pair_review: Optional[tuple[int, str, int, int]] = None
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.timeout.connect(self._expire_pair_undo)

        root = QWidget()
        layout = QVBoxLayout(root)
        header = _FlowLayout()
        header.addWidget(QLabel("Inventory"))
        self.scan_button = QPushButton("Refresh inventory")
        self.full_button = QPushButton("Full inventory")
        self.cancel_button = QPushButton("Cancel")
        self.advanced_toggle = QPushButton("Advanced actions ▸")
        self.advanced_toggle.setCheckable(True)
        self.bindings_button = QPushButton("Field bindings…")
        self.mo_key_button = QPushButton("MO API key…")
        self.repairs_button = QPushButton("Preview link repairs…")
        self.its_button = QPushButton("Compare ITS…")
        self.coordinates_button = QPushButton("Compare coordinates…")
        self.photos_button = QPushButton("Preview photo synchronization…")
        self.create_missing_button = QPushButton("Create missing observation…")
        self.consolidate_button = QPushButton("Consolidate duplicate observations…")
        self.history_button = QPushButton("View consolidation history…")
        self.name_button = QPushButton("Propose name…")
        self.cancel_button.setEnabled(False)
        self.scan_button.setToolTip(
            "Read recent remote changes. Inventory scans are read-only."
        )
        self.full_button.setToolTip(
            "Re-read the complete inventory. This is normally needed only for "
            "the first scan or recovery and can take about half an hour."
        )
        self.status = QLabel(
            "Inventory scans and candidate review are read-only remotely. "
            "Remote changes always require a separate explicit preview."
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMinimumWidth(220)
        self.progress.setTextVisible(True)
        self.progress.setFormat("")
        self.progress.hide()
        # Highest scan step reported so far; lives for one scan (see
        # _scan_progress, which never lets the step or the bar move backwards).
        self._scan_step = 0
        # Per-phase 0..1 progress for the running scan, keyed by base part name.
        self._scan_fractions: dict[str, float] = {}
        # Which scan is waiting on the sign-in pre-flight (see _request_scan):
        # the force_full flag, or None when no scan is pending.
        self._pending_scan_full: Optional[bool] = None
        for widget in (
            self.scan_button,
            self.full_button,
            self.cancel_button,
            self.advanced_toggle,
        ):
            header.addWidget(widget)
        layout.addLayout(header)
        self.inventory_summary = QLabel("")
        layout.addWidget(self.inventory_summary)
        self.advanced_panel = QWidget()
        advanced = _FlowLayout()
        advanced.addWidget(QLabel("Setup"))
        advanced.addWidget(self.bindings_button)
        advanced.addWidget(self.mo_key_button)
        advanced.addWidget(QLabel("Confirmed-pair tools"))
        for widget in (
            self.repairs_button,
            self.its_button,
            self.coordinates_button,
            self.photos_button,
            self.name_button,
        ):
            advanced.addWidget(widget)
        advanced.addWidget(QLabel("Specialist workflows"))
        for widget in (
            self.create_missing_button,
            self.consolidate_button,
            self.history_button,
        ):
            advanced.addWidget(widget)
        self.advanced_panel.setLayout(advanced)
        self.advanced_panel.hide()
        layout.addWidget(self.advanced_panel)
        self.workflow_hint = QLabel(
            "Start with Candidate pairs: select a row, compare both "
            "observations, then confirm or reject the local pairing."
        )
        self.workflow_hint.setWordWrap(True)
        layout.addWidget(self.workflow_hint)
        # Status and progress get their OWN row rather than trailing the buttons.
        # In the wrapping row they would reflow to wherever the last button
        # ended, so the status line would jump around as the window resized.
        status_row = QHBoxLayout()
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.progress)
        layout.addLayout(status_row)
        splitter = QSplitter()
        self.categories = QListWidget()
        for key, label in CATEGORIES:
            self.categories.addItem(label)
            item = self.categories.item(self.categories.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.UserRole + 1, label)
        self.categories.setFixedWidth(265)
        splitter.addWidget(self.categories)
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        middle_layout.addWidget(self.table)
        # Wrapping, for the same reason as the header row: thirteen buttons in a
        # fixed row made this pane 2134px wide on its own.
        actions = _FlowLayout()
        self.confirm = QPushButton("✓ Confirm pair (C)")
        self.reject = QPushButton("✕ Reject candidate (X)")
        self.confirm.setStyleSheet(
            "QPushButton:enabled { background: #d9f2df; color: #167236; "
            "font-weight: 600; }"
        )
        self.reject.setStyleSheet(
            "QPushButton:enabled { background: #f7d7d7; color: #a51d24; "
            "font-weight: 600; }"
        )
        self.undo_review = QPushButton("Undo last pair decision")
        self.undo_review.setEnabled(False)
        self.review_photos = QPushButton("Compare photos (read-only)…")
        self.exclude = QPushButton("Exclude / reopen")
        self.ignore = QPushButton("Ignore / reopen issue")
        self.missing = QPushButton("Mark / clear confirmed missing")
        self.refresh = QPushButton("Refresh details")
        self.open_site = QPushButton("Open selected observation")
        self.open_other = QPushButton("Open paired observation")
        self.open_consolidation_member = QPushButton("Open consolidation member…")
        self.review_deletion = QPushButton("Review donor deletion…")
        self.resume_deletion = QPushButton("Resume / verify donor deletion")
        self.recover_action = QPushButton("Resume / verify journal action")
        self.cancel_action = QPushButton("Cancel pending journal action")
        for widget in (
            self.open_site,
            self.open_other,
            self.review_photos,
            self.confirm,
            self.reject,
            self.undo_review,
            self.exclude,
            self.ignore,
            self.missing,
            self.refresh,
            self.open_consolidation_member,
            self.review_deletion,
            self.resume_deletion,
            self.recover_action,
            self.cancel_action,
        ):
            actions.addWidget(widget)
        middle_layout.addLayout(actions)
        splitter.addWidget(middle)
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.addWidget(QLabel("Observation comparison"))
        self.thumbnail = QLabel("No displayed thumbnail")
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setMinimumSize(220, 180)
        detail_layout.addWidget(self.thumbnail)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        detail_layout.addWidget(self.detail, 1)
        splitter.addWidget(detail_widget)
        splitter.setSizes([260, 650, 320])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.scan_button.clicked.connect(lambda: self._request_scan(force_full=False))
        self.full_button.clicked.connect(lambda: self._request_scan(force_full=True))
        self.cancel_button.clicked.connect(coordinator.cancel_current_operation)
        self.advanced_toggle.toggled.connect(self._toggle_advanced_actions)
        self.bindings_button.clicked.connect(self._load_field_bindings)
        self.mo_key_button.clicked.connect(self._edit_mo_key)
        self.repairs_button.clicked.connect(self._prepare_repairs)
        self.its_button.clicked.connect(self._prepare_its)
        self.coordinates_button.clicked.connect(self._prepare_coordinates)
        self.photos_button.clicked.connect(self._prepare_photos)
        self.create_missing_button.clicked.connect(self._prepare_observation_creation)
        self.consolidate_button.clicked.connect(self._prepare_consolidation)
        self.history_button.clicked.connect(self._show_consolidation_history)
        self.name_button.clicked.connect(self._prepare_name_proposal)
        self.categories.currentRowChanged.connect(self._category_changed)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self._selection_changed()
        )
        self.table.clicked.connect(self._candidate_table_clicked)
        self.table.verticalScrollBar().valueChanged.connect(
            lambda *_: self._request_candidate_photo_prefetch()
        )
        self.model.rowsInserted.connect(
            lambda *_: self._request_candidate_photo_prefetch()
        )
        self.confirm.clicked.connect(lambda: self._pair_action("confirmed"))
        self.reject.clicked.connect(lambda: self._pair_action("rejected"))
        self.undo_review.clicked.connect(self._undo_pair_action)
        self.review_photos.clicked.connect(self._prepare_photo_identity_review)
        self.exclude.clicked.connect(self._toggle_exclusion)
        self.ignore.clicked.connect(self._toggle_issue)
        self.missing.clicked.connect(self._toggle_missing)
        self.refresh.clicked.connect(self._hydrate_selected)
        self.open_site.clicked.connect(self._open_selected)
        self.open_other.clicked.connect(self._open_other)
        self.open_consolidation_member.clicked.connect(self._open_consolidation_member)
        self.review_deletion.clicked.connect(self._prepare_donor_deletion)
        self.resume_deletion.clicked.connect(self._resume_donor_deletion)
        self.recover_action.clicked.connect(self._recover_selected_action)
        self.cancel_action.clicked.connect(self._cancel_selected_action)
        coordinator.authentication_checked.connect(self._authentication_checked)
        coordinator.scan_started.connect(self._scan_started)
        coordinator.scan_progress.connect(self._scan_progress)
        coordinator.scan_finished.connect(self._scan_finished)
        coordinator.scan_failed.connect(self._scan_failed)
        coordinator.details_loaded.connect(self._detail_loaded)
        coordinator.thumbnail_loaded.connect(self._thumbnail_loaded)
        coordinator.field_candidates_loaded.connect(self._field_candidates_loaded)
        coordinator.link_preview_ready.connect(self._link_preview_ready)
        coordinator.its_preview_ready.connect(self._its_preview_ready)
        coordinator.coordinate_preview_ready.connect(self._coordinate_preview_ready)
        coordinator.photo_identity_ready.connect(self._photo_identity_ready)
        coordinator.photo_preview_ready.connect(self._photo_preview_ready)
        coordinator.observation_creation_preview_ready.connect(
            self._observation_creation_preview_ready
        )
        coordinator.consolidation_preview_ready.connect(
            self._consolidation_preview_ready
        )
        coordinator.deletion_preview_ready.connect(self._deletion_preview_ready)
        coordinator.name_proposal_ready.connect(self._name_proposal_ready)
        coordinator.name_proposal_changed.connect(self._link_actions_changed)
        coordinator.link_action_failed.connect(self._link_action_failed)
        coordinator.link_action_progress.connect(self._link_action_progress)
        coordinator.link_actions_changed.connect(self._link_actions_changed)
        self._confirm_shortcut = QShortcut(QKeySequence("C"), self)
        self._confirm_shortcut.activated.connect(
            lambda: self._quick_shortcut_review("confirmed")
        )
        self._reject_shortcut = QShortcut(QKeySequence("X"), self)
        self._reject_shortcut.activated.connect(
            lambda: self._quick_shortcut_review("rejected")
        )
        self._update_inventory_summary()
        self._reload_counts()
        self.categories.setCurrentRow(2)
        self._update_contextual_controls()

    def _update_inventory_summary(self) -> None:
        run = self.coordinator.db.latest_run(self.profile.profile_id)
        if not run:
            self.inventory_summary.setText(
                "No reconciliation inventory has completed yet."
            )
            return
        outcome = str(run.get("outcome") or "unknown")
        mode = str(run.get("mode") or "scan")
        finished = str(
            run.get("finished_at") or run.get("started_at") or "unknown time"
        )
        text = f"Latest inventory attempt: {mode}, {outcome}, {finished}"
        successful = self.coordinator.db.latest_successful_run(self.profile.profile_id)
        if successful and successful.get("run_id") != run.get("run_id"):
            success_finished = str(
                successful.get("finished_at")
                or successful.get("started_at")
                or "unknown time"
            )
            text += (
                f" — last successful: {successful.get('mode') or 'scan'}, "
                f"{success_finished}"
            )
        self.inventory_summary.setText(text)

    def _sync_sort_indicator(self) -> None:
        """Point the header arrow at the column the model is actually sorted on.

        Signals are blocked because the view connects the header's
        sortIndicatorChanged to the model's sort(), which would call back into
        set_query and re-enter the reload this is reporting the result of.
        """
        header = self.table.horizontalHeader()
        try:
            section = self.model.KEYS.index(self.model.sort_column)
        except ValueError:
            return
        blocked = header.blockSignals(True)
        try:
            header.setSortIndicator(
                section,
                (
                    Qt.SortOrder.DescendingOrder
                    if self.model.descending
                    else Qt.SortOrder.AscendingOrder
                ),
            )
        finally:
            header.blockSignals(blocked)

    def _category_changed(self, row: int) -> None:
        self._cancel_candidate_photo_prefetch()
        item = self.categories.item(row)
        if item:
            category = str(item.data(Qt.ItemDataRole.UserRole))
            if category != self.model.category:
                # Only on a REAL category switch: _reload() also routes through
                # here, and resetting there would throw away a sort the user
                # chose from the header every time the dashboard refreshed.
                self.model.sort_column, self.model.descending = CATEGORY_SORT.get(
                    category, DEFAULT_CATEGORY_SORT
                )
            self.model.set_query(self.profile.profile_id, category)
            self._sync_sort_indicator()
            candidate_mode = category == "candidate_pairs"
            for column in range(
                self.model.PHOTO_PREVIEW_COLUMN,
                self.model.REJECT_COLUMN + 1,
            ):
                self.table.setColumnHidden(column, not candidate_mode)
            self.table.verticalHeader().setDefaultSectionSize(
                76 if candidate_mode else 30
            )
            if candidate_mode:
                self.table.setColumnWidth(self.model.PHOTO_PREVIEW_COLUMN, 190)
                self.table.setColumnWidth(self.model.PHOTO_SUMMARY_COLUMN, 145)
                self.table.setColumnWidth(self.model.CONFIRM_COLUMN, 68)
                self.table.setColumnWidth(self.model.REJECT_COLUMN, 60)
            hints = {
                "link_issues": (
                    "Review link warnings here. Select an issue to see its records; "
                    "only explicitly reviewable issues can produce a repair preview."
                ),
                "link_actions": (
                    "Audit remote-write journal entries and recover only pending or "
                    "outcome-unknown actions."
                ),
                "candidate_pairs": (
                    "Review highest scores first. Open both observations or compare "
                    "their photos, then confirm or reject the local pair."
                ),
                "confirmed_links": (
                    "These pairs are confirmed locally. Advanced tools can compare "
                    "or synchronize specific remote fields after a separate preview."
                ),
                "confirmed_conflicts": (
                    "These reciprocal links still have metadata conflicts. Inspect "
                    "the exact records before using any advanced reconciliation tool."
                ),
                "unpaired": (
                    "Unpaired means possibly missing, not definitely missing. Verify "
                    "absence on the other site before marking a record confirmed missing."
                ),
                "same_site_duplicates": (
                    "Review possible duplicates on one site. Consolidation and deletion "
                    "are specialist workflows and never run automatically."
                ),
                "consolidation_history": (
                    "Audit finalized duplicate consolidations and any retained or "
                    "deleted donor observations."
                ),
                "changed_deleted": (
                    "Review records that changed materially or disappeared since an "
                    "earlier successful inventory."
                ),
                "rejected_excluded": (
                    "Previously rejected or excluded pairs can be reopened after "
                    "reviewing the exact observations."
                ),
                "ignored_resolved": (
                    "Audit issues that were ignored or resolved and reopen them if needed."
                ),
            }
            self.workflow_hint.setText(hints.get(category, "Select a row to continue."))
        self._selected_detail = None
        self._selected_pair = None
        self._detail_payloads = {}
        self._selected_thumbnail_identities.clear()
        self.thumbnail.setText("No displayed thumbnail")
        self.thumbnail.setPixmap(QPixmap())
        self.detail.setPlainText(
            "Select a row to inspect its local and remote details."
        )
        self._update_contextual_controls()
        if item and str(item.data(Qt.ItemDataRole.UserRole)) == "candidate_pairs":
            QTimer.singleShot(0, self._start_candidate_review_queue)

    def _selected(self) -> Optional[dict[str, Any]]:
        indexes = self.table.selectionModel().selectedRows()
        return self.model.row(indexes[0]) if indexes else None

    def _start_candidate_review_queue(self) -> None:
        if self.model.category != "candidate_pairs":
            return
        if not self.table.selectionModel().selectedRows() and self.model.rowCount():
            self.table.selectRow(0)
        self._schedule_candidate_photo_prefetch()

    def _cancel_candidate_photo_prefetch(self) -> None:
        self._prefetch_timer.stop()
        self._photo_prefetch_token.cancelled = True
        # The pool queue is deliberately NOT cleared. QThreadPool.clear() drops
        # queued workers without running them, so they never emit `finished` --
        # and that callback is the only thing that prunes
        # _live_photo_analysis_signals. Every cancelled batch therefore
        # stranded its queued workers' signals objects (each holding a
        # connection to a bound method of this window) for the window's
        # lifetime, and switching in and out of Candidate pairs repeated that
        # indefinitely.
        #
        # Letting them run instead costs nothing: the token is already
        # cancelled, so run() does no remote work at all and reports
        # immediately. Un-queueing by identity (tryTake) was the obvious
        # alternative and is NOT safe here -- these workers are autoDelete, so
        # the C++ object behind a worker that has already run is gone, and
        # touching its Python wrapper from this thread raises.
        self._photo_prefetch_generation += 1
        self._photo_prefetch_token = _PhotoPrefetchToken()
        self._pending_photo_analyses.clear()
        # _live_photo_analysis_signals is not cleared here either: a worker
        # already past its cancellation checks may have queued its `finished`
        # signal, and dropping the last Python reference to a QRunnable's
        # signals object before that delivery is handled is the project-wide GC
        # hazard documented in CLAUDE.md. The generation check in
        # _candidate_photo_analysis_finished discards the stale result, and
        # that callback is what releases the reference.

    def _request_candidate_photo_prefetch(self) -> None:
        """Coalesce prefetch requests from scrolling and row insertion."""
        if self.model.category == "candidate_pairs" and not self._ui_busy:
            self._prefetch_timer.start()

    def _schedule_candidate_photo_prefetch(self) -> None:
        if (
            self.model.category != "candidate_pairs"
            or self._ui_busy
            or self.coordinator.foreground_operation_running
        ):
            return
        count = self.model.rowCount()
        if not count:
            return
        top_index = self.table.indexAt(QPoint(2, 2))
        selected = self.table.selectionModel().selectedRows()
        starts = [
            top_index.row() if top_index.isValid() else 0,
            selected[0].row() if selected else 0,
        ]
        row_indexes: list[int] = []
        for start in starts:
            for row_index in range(start, min(count, start + PHOTO_PREFETCH_ROWS)):
                if row_index not in row_indexes:
                    row_indexes.append(row_index)
        for row_index in row_indexes:
            row = self.model.rows[row_index]
            pair_id = int(row.get("pair_id") or 0)
            if (
                not pair_id
                or pair_id in self._pending_photo_analyses
                or "_photo_summary" in row
            ):
                continue
            cached = self._photo_analysis_cache.get(pair_id)
            if cached is not None:
                # Already analysed in this session; a model reset only cleared
                # the presentation, not the result.
                self.model.set_photo_analysis(pair_id, **cached)
                continue
            self._pending_photo_analyses.add(pair_id)
            self.model.set_photo_loading(pair_id)
            worker = _CandidatePhotoAnalysisWorker(
                self.coordinator,
                self.profile.profile_id,
                pair_id,
                self._photo_prefetch_generation,
                self._photo_prefetch_token,
            )
            signals = worker.signals
            self._live_photo_analysis_signals.add(signals)
            signals.finished.connect(
                lambda generation, completed_pair_id, payload, owned=signals: self._candidate_photo_analysis_finished(
                    owned, generation, completed_pair_id, payload
                )
            )
            self._photo_prefetch_pool.start(worker)

    def _candidate_photo_analysis_finished(
        self,
        signals: QObject,
        generation: int,
        pair_id: int,
        payload: object,
    ) -> None:
        self._live_photo_analysis_signals.discard(signals)
        if generation != self._photo_prefetch_generation:
            return
        self._pending_photo_analyses.discard(pair_id)
        if isinstance(payload, dict) and payload.get("cancelled"):
            # A cancelled worker reports only so the discard above happens. It
            # carries no result, and its generation is stale by construction,
            # so this is normally unreachable -- but never present it as one.
            return
        if isinstance(payload, dict) and payload.get("deferred"):
            # The worker stood down for a scan or an explicit action. Clear the
            # row's "loading…" marker so _schedule_candidate_photo_prefetch
            # picks it up again once the foreground operation releases the
            # remotes, and do not queue anything else in the meantime.
            self.model.clear_photo_analysis(pair_id)
            # Ask for another pass rather than relying on the one
            # _set_action_controls_enabled fires: that request runs on a 120ms
            # timer started the moment the action ends, which is BEFORE this
            # queued signal is delivered, so the scheduler would still see this
            # pair in _pending_photo_analyses and skip it -- and nothing else
            # would ever queue it again. The discard above has already run, so
            # the request made here sees the pair as un-analysed. It is a no-op
            # while the UI is still busy, and that case is the one
            # _set_action_controls_enabled correctly covers.
            self._request_candidate_photo_prefetch()
            return
        if isinstance(payload, dict) and payload.get("error"):
            message = str(payload["error"])
            self._apply_photo_analysis(
                pair_id,
                preview=None,
                summary="unavailable",
                tooltip=f"Photo analysis could not be completed: {message}",
                ready=False,
            )
            self._schedule_candidate_photo_prefetch()
            return
        if not isinstance(payload, _CandidatePhotoAnalysis):
            self._apply_photo_analysis(
                pair_id,
                preview=None,
                summary="unavailable",
                tooltip="Photo analysis returned an invalid result.",
                ready=False,
            )
            self._schedule_candidate_photo_prefetch()
            return
        close = payload.close_matches
        similarities = [
            _photo_similarity_score(distance) for _mo, _inat, distance in close
        ]
        if similarities:
            score = min(similarities)
        elif payload.matches:
            score = max(
                _photo_similarity_score(distance)
                for _mo, _inat, distance in payload.matches
            )
        else:
            score = 0
        coverage = f"{len(close)}/{payload.denominator}"
        if payload.failures:
            band = "incomplete"
        elif len(close) == payload.denominator and score >= 94:
            band = "close set"
        elif close:
            band = "inspect"
        else:
            band = "weak"
        summary = f"{coverage} matched\n{score}/100 · {band}"
        tooltip_lines = [
            f"Photo-set coverage: {len(close)} of {payload.denominator}.",
            (
                "Similarity is 100 minus the percentage of differing bits in "
                "the 64-bit dHash; the displayed score is the weakest close match."
            ),
        ]
        for mo_index, inat_index, distance in payload.matches:
            mo_photo = payload.mo_photos[mo_index].photo
            inat_photo = payload.inat_photos[inat_index].photo
            closeness = "close" if distance <= PHOTO_CLOSE_DISTANCE else "weak"
            tooltip_lines.append(
                f"MO {mo_photo.photo_id} ↔ iNat {inat_photo.photo_id}: "
                f"{_photo_similarity_score(distance)}/100 "
                f"(distance {distance}/64, {closeness})"
            )
        if payload.failures:
            tooltip_lines.extend(payload.failures)
        self._apply_photo_analysis(
            pair_id,
            preview=QPixmap.fromImage(_photo_analysis_mosaic(payload)),
            summary=summary,
            tooltip="\n".join(tooltip_lines),
            ready=payload.ready_for_quick_review,
        )
        self._schedule_candidate_photo_prefetch()

    def _apply_photo_analysis(
        self,
        pair_id: int,
        *,
        preview: Optional[QPixmap],
        summary: str,
        tooltip: str,
        ready: bool,
    ) -> None:
        """Show one analysis and remember it for the rest of the session.

        Caching the RENDERED values rather than the ``_CandidatePhotoAnalysis``
        keeps the decoded full-size QImages out of the cache: only the small
        mosaic pixmap survives, so a long review session holds kilobytes per
        candidate instead of megabytes.
        """
        values = {
            "preview": preview,
            "summary": summary,
            "tooltip": tooltip,
            "ready": ready,
        }
        self._photo_analysis_cache[pair_id] = values
        self.model.set_photo_analysis(pair_id, **values)

    def _quick_review_blocked(self) -> bool:
        """True when the two quick-review entry points must decline.

        The Confirm and Reject BUTTONS are disabled for the whole of a scan or
        a link action by _update_contextual_controls, but neither the in-table
        ✓/✗ cells nor the C/X shortcuts go through a QWidget that can be
        disabled, so they need the same check spelled out. Without it a
        keystroke during a scan opens set_pair_review's BEGIN IMMEDIATE on the
        GUI thread against the write lock the scan worker is holding, and flips
        review state underneath the scan that is re-deriving those very pairs.
        """
        if self._ui_busy or self.coordinator.foreground_operation_running:
            self.status.setText(
                "Quick review is unavailable while another reconciliation "
                "operation is running."
            )
            return True
        return False

    def _candidate_table_clicked(self, index: QModelIndex) -> None:
        if self.model.category != "candidate_pairs" or index.column() not in {
            self.model.CONFIRM_COLUMN,
            self.model.REJECT_COLUMN,
        }:
            return
        if self._quick_review_blocked():
            return
        row = self.model.row(index)
        if not row or not row.get("_photo_ready"):
            self.status.setText(
                "Quick review waits until every photo in both observations "
                "has loaded. Use Compare photos for an incomplete row."
            )
            return
        self.table.selectRow(index.row())
        self._pair_action(
            "confirmed" if index.column() == self.model.CONFIRM_COLUMN else "rejected"
        )

    def _quick_shortcut_review(self, state: str) -> None:
        row = self._selected()
        if (
            self.model.category != "candidate_pairs"
            or not row
            or not row.get("_photo_ready")
        ):
            return
        if self._quick_review_blocked():
            return
        self._pair_action(state)

    def _record_pair_undo(
        self,
        pair_id: int,
        state: str,
        mo_id: int,
        inat_id: int,
    ) -> None:
        """Offer to reverse the decision just made, briefly.

        The quick-review columns and the C/X shortcuts make a decision one
        keystroke or one click away, including on the wrong row, so the
        decision needs a visible way back. Only the most recent decision is
        held, and only until :data:`PAIR_UNDO_SECONDS` elapse, because
        reversing something further back is what reopening the pair from
        Rejected/excluded is for.
        """
        self._last_pair_review = (pair_id, state, mo_id, inat_id)
        verb = "confirmation" if state == "confirmed" else "rejection"
        self.undo_review.setText(f"Undo {verb}: MO {mo_id} ↔ iNat {inat_id}")
        self._undo_timer.start(PAIR_UNDO_SECONDS * 1000)
        self._update_contextual_controls()

    def _expire_pair_undo(self) -> None:
        self._undo_timer.stop()
        self._last_pair_review = None
        self.undo_review.setText("Undo last pair decision")
        self._update_contextual_controls()

    def _undo_pair_action(self) -> None:
        reviewed = self._last_pair_review
        if reviewed is None:
            return
        pair_id, state, mo_id, inat_id = reviewed
        try:
            self.coordinator.db.set_pair_review(
                self.profile.profile_id, pair_id, "candidate"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Undo unavailable", str(exc))
            return
        self._expire_pair_undo()
        # A full reload, not remove_pair's in-place edit: the pair is being put
        # BACK into the candidate list, and where it belongs there depends on
        # the active sort.
        self._reload()
        restored = self.model.row_for_pair(pair_id)
        if restored is not None:
            self.table.selectRow(restored)
        self.status.setText(
            f"Undid the {state} decision for MO {mo_id} ↔ iNaturalist "
            f"{inat_id}; the pair is a candidate again."
        )

    def _selection_changed(self) -> None:
        row = self._selected()
        if not row:
            self._selected_detail = None
            self._selected_pair = None
            self._detail_payloads = {}
            self._selected_thumbnail_identities.clear()
            self.thumbnail.setText("No displayed thumbnail")
            self.thumbnail.setPixmap(QPixmap())
            self.detail.setPlainText(
                "Select a row to inspect its local and remote details."
            )
            self._update_contextual_controls()
            return
        self._schedule_candidate_photo_prefetch()
        self.consolidate_button.setText(
            "Add duplicate to existing consolidation…"
            if row.get("kind") == "consolidation"
            and str(row.get("state")) == "finalized"
            else "Consolidate duplicate observations…"
        )
        self._refresh_deletion_controls()
        if row["kind"] == "pair":
            value = self.coordinator.db.pair_detail(
                self.profile.profile_id, int(row["pair_id"])
            )
            self._selected_pair = (int(row["remote_id"]), int(row["other_id"]))
        elif row["kind"] == "record":
            value = self.coordinator.db.record_detail(
                self.profile.profile_id, row["site"], int(row["remote_id"])
            )
            self._selected_pair = None
        elif row["kind"] == "issue":
            value = (
                self.coordinator.db.issue_detail(
                    self.profile.profile_id, int(row["issue_id"])
                )
                or row
            )
            if row.get("remote_id") and row.get("other_id"):
                identifiers = {
                    str(row.get("site")): int(row["remote_id"]),
                    str(row.get("other_site")): int(row["other_id"]),
                }
                self._selected_pair = (
                    (identifiers["mo"], identifiers["inat"])
                    if {"mo", "inat"}.issubset(identifiers)
                    else None
                )
            else:
                self._selected_pair = None
        elif row["kind"] == "consolidation":
            value = (
                self.coordinator.db.consolidation_detail(
                    self.profile.profile_id,
                    int(str(row["row_key"]).split(":", 1)[1]),
                )
                or row
            )
            self._selected_pair = (
                (int(row["remote_id"]), int(row["other_id"]))
                if row.get("remote_id") and row.get("other_id")
                else None
            )
        elif row["kind"] == "deletion_action":
            # Deletion actions live in their own table and their action_id is
            # a deletion_action_id, which must never be handed to
            # action_detail() -- that id space collides with sync_actions.
            value = (
                self.coordinator.db.deletion_action(
                    self.profile.profile_id, int(row["action_id"])
                )
                or row
            )
            self._selected_pair = None
        else:
            value = (
                self.coordinator.db.action_detail(
                    self.profile.profile_id, int(row["action_id"])
                )
                or row
            )
            self._selected_pair = (
                (int(value["mo_observation_id"]), int(value["inat_observation_id"]))
                if value.get("mo_observation_id") and value.get("inat_observation_id")
                else None
            )
        self._selected_detail = value if isinstance(value, dict) else row
        self._detail_payloads = {}
        self._selected_thumbnail_identities.clear()
        self.detail.setPlainText(_format_local_detail(value))
        self._hydrate_selected()
        self._update_create_missing_button(row)
        self._update_contextual_controls()

    def _create_missing_eligible(self, row: Optional[dict[str, Any]]) -> bool:
        # Section 4: only MO->iNaturalist creation is proven. A record whose
        # state is 'confirmed_missing_on_inat' means it exists on MO and is
        # confirmed absent from iNaturalist -- i.e. MO is the source, iNat is
        # the destination. 'confirmed_missing_on_mo' is the OTHER direction
        # (iNat source -> MO destination), which has no independently proven
        # create endpoint, recovery marker, duplicate prevention, taxon
        # mapping, or verification, so it is deliberately excluded here.
        return bool(
            row
            and row.get("kind") == "record"
            and str(row.get("state", "")) == "confirmed_missing_on_inat"
        )

    def _update_create_missing_button(self, row: Optional[dict[str, Any]]) -> None:
        """Enabled ONLY for a record explicitly marked confirmed missing on
        iNaturalist (the one proven creation direction) -- an unpaired
        record is never automatically a missing one, and the unproven
        iNat->MO direction is never offered. The click handler
        (``_missing_record_row``) still revalidates this independently; this
        is presentation only, never the sole safety check."""
        self.create_missing_button.setEnabled(
            not self._ui_busy and self._create_missing_eligible(row)
        )

    def _toggle_advanced_actions(self, shown: bool) -> None:
        self.advanced_panel.setVisible(shown)
        self.advanced_toggle.setText(
            "Advanced actions ▾" if shown else "Advanced actions ▸"
        )

    @staticmethod
    def _set_available(
        button: QPushButton,
        available: bool,
        unavailable_reason: str = "",
    ) -> None:
        button.setEnabled(available)
        button.setToolTip("" if available else unavailable_reason)

    def _update_contextual_controls(self) -> None:
        """Make presentation match every action handler's real prerequisites."""
        busy_reason = "Wait for the current reconciliation operation to finish."
        row = self._selected()
        detail = self._selected_detail or {}
        kind = str(row.get("kind")) if row else ""
        pair = detail if kind == "pair" else {}
        pair_state = (
            str(pair.get("review_state") or row.get("state") or "") if row else ""
        )
        pair_excluded = bool(pair.get("excluded"))
        is_pair = kind == "pair"
        is_candidate = is_pair and pair_state == "candidate" and not pair_excluded
        is_confirmed = is_pair and pair_state == "confirmed" and not pair_excluded
        available = not self._ui_busy

        self._set_available(self.scan_button, available, busy_reason)
        self._set_available(self.full_button, available, busy_reason)
        self._set_available(
            self.cancel_button,
            self._ui_busy,
            "There is no reconciliation operation to cancel.",
        )
        self._set_available(self.bindings_button, available, busy_reason)
        self._set_available(self.mo_key_button, available, busy_reason)
        self._set_available(self.history_button, available, busy_reason)
        self._set_available(self.consolidate_button, available, busy_reason)

        confirmed_reason = (
            "Select a confirmed, non-excluded pair first. Confirmation is a "
            "local identity decision; remote changes still require a preview."
        )
        for button in (
            self.its_button,
            self.coordinates_button,
            self.photos_button,
            self.name_button,
        ):
            self._set_available(
                button,
                available and is_confirmed,
                busy_reason if self._ui_busy else confirmed_reason,
            )
        repairable_issue = (
            kind == "issue"
            and str(detail.get("issue_type") or "") in REPAIRABLE_LINK_ISSUE_TYPES
        )
        self._set_available(
            self.repairs_button,
            available and (is_confirmed or repairable_issue),
            (
                busy_reason
                if self._ui_busy
                else "Select a confirmed pair or an explicitly repairable link issue."
            ),
        )
        self._set_available(
            self.create_missing_button,
            available and self._create_missing_eligible(row),
            (
                busy_reason
                if self._ui_busy
                else "Select an MO record explicitly marked confirmed missing on iNaturalist."
            ),
        )

        self._set_available(
            self.confirm,
            available and is_candidate,
            (
                busy_reason
                if self._ui_busy
                else "Select a non-excluded candidate pair to confirm locally."
            ),
        )
        self._set_available(
            self.reject,
            available and is_candidate,
            (
                busy_reason
                if self._ui_busy
                else "Select a non-excluded candidate pair to reject."
            ),
        )
        self._set_available(
            self.undo_review,
            available and self._last_pair_review is not None,
            (
                busy_reason
                if self._ui_busy
                else f"A confirm or reject decision can be reversed here for "
                f"{PAIR_UNDO_SECONDS} seconds after you make it."
            ),
        )
        self._set_available(
            self.review_photos,
            available
            and is_pair
            and pair_state in {"candidate", "confirmed"}
            and not pair_excluded,
            (
                busy_reason
                if self._ui_busy
                else "Select a candidate or confirmed, non-excluded pair for read-only photo review."
            ),
        )
        self._set_available(
            self.exclude,
            available and is_pair,
            busy_reason if self._ui_busy else "Select a pair to exclude or reopen.",
        )
        self._set_available(
            self.ignore,
            available and kind == "issue",
            busy_reason if self._ui_busy else "Select an issue to ignore or reopen.",
        )
        self._set_available(
            self.missing,
            available and kind == "record",
            (
                busy_reason
                if self._ui_busy
                else "Select an unpaired inventory record after checking the other site."
            ),
        )
        has_remote = bool(
            row and row.get("site") in {"inat", "mo"} and row.get("remote_id")
        )
        has_other = bool(
            row and row.get("other_site") in {"inat", "mo"} and row.get("other_id")
        )
        self._set_available(
            self.refresh,
            available and has_remote,
            busy_reason if self._ui_busy else "Select a remote observation first.",
        )
        self._set_available(
            self.open_site,
            available and has_remote,
            busy_reason if self._ui_busy else "Select a remote observation first.",
        )
        self._set_available(
            self.open_other,
            available and has_other,
            (
                busy_reason
                if self._ui_busy
                else "The selected row has no paired observation."
            ),
        )
        self._set_available(
            self.open_consolidation_member,
            available and kind == "consolidation",
            (
                busy_reason
                if self._ui_busy
                else "Select a consolidation-history row first."
            ),
        )

        action_state = str(detail.get("state") or "")
        self._set_available(
            self.recover_action,
            available
            and kind == "action"
            and action_state in {"pending", "outcome_unknown"},
            (
                busy_reason
                if self._ui_busy
                else "Select a pending or outcome-unknown journal action."
            ),
        )
        self._set_available(
            self.cancel_action,
            available and kind == "action" and action_state == "pending",
            busy_reason if self._ui_busy else "Select a pending journal action.",
        )
        if self.confirm.isEnabled():
            self.confirm.setToolTip(
                "Record that these observations depict the same specimen. "
                "This changes only the local reconciliation database."
            )
        if self.reject.isEnabled():
            self.reject.setToolTip(
                "Reject this candidate in the local reconciliation database."
            )
        if self.review_photos.isEnabled():
            self.review_photos.setToolTip(
                "Read and display both complete photo sets. This creates no "
                "journal action and cannot change either website."
            )
        if self.photos_button.isEnabled():
            self.photos_button.setToolTip(
                "Freshly compare a confirmed pair and preview at most one "
                "MO-to-iNaturalist photo upload. Nothing is preselected."
            )
        self._refresh_deletion_controls(available)

    def _hydrate_selected(self) -> None:
        row = self._selected()
        if row and row.get("site") in {"inat", "mo"} and row.get("remote_id"):
            self.coordinator.hydrate(
                row["site"], int(row["remote_id"]), self.profile.profile_id
            )
            if row.get("other_id") and row.get("other_site") in {"inat", "mo"}:
                self.coordinator.hydrate(
                    str(row["other_site"]),
                    int(row["other_id"]),
                    self.profile.profile_id,
                )

    def _detail_loaded(self, part: str, value: object) -> None:
        pieces = part.split(":")
        if len(pieces) != 3:
            return
        site, observation_id = pieces[1], int(pieces[2])
        row = self._selected()
        expected = set()
        if row and row.get("remote_id"):
            expected.add((str(row["site"]), int(row["remote_id"])))
            if row.get("other_id") and row.get("other_site"):
                expected.add((str(row["other_site"]), int(row["other_id"])))
        if (site, observation_id) not in expected:
            return
        payload_key = f"{site}:{observation_id}"
        self._detail_payloads[payload_key] = value
        fallbacks: dict[str, dict[str, Any]] = {}
        for expected_site, expected_id in expected:
            local = self.coordinator.db.record_detail(
                self.profile.profile_id, expected_site, expected_id
            )
            if local:
                fallbacks[expected_site] = local
        self.detail.setPlainText(
            _format_remote_comparison(self._detail_payloads, fallbacks)
        )

        # A failed read must never contribute (or appear to contribute)
        # identity evidence: skip any payload that is an error envelope.
        def usable(prefix: str) -> Optional[object]:
            for payload_id, payload in self._detail_payloads.items():
                if not payload_id.startswith(prefix):
                    continue
                if isinstance(payload, dict) and payload.get("error"):
                    continue
                return payload
            return None

        mo_payload = usable("mo:")
        inat_payload = usable("inat:")
        if self._selected_pair and mo_payload is not None and inat_payload is not None:
            overlap = _distinctive_note_overlap(mo_payload, inat_payload)
            if overlap:
                from observation_workbench.reconciliation.types import (
                    EvidenceFamily,
                    EvidenceTier,
                    MatchEvidence,
                )

                self.coordinator.add_deep_evidence(
                    self.profile.profile_id,
                    self._selected_pair[0],
                    self._selected_pair[1],
                    MatchEvidence(
                        "distinctive_note_overlap",
                        EvidenceFamily.TEXT,
                        15,
                        "Selected records have distinctive in-memory note overlap.",
                        EvidenceTier.DEEP,
                    ),
                )
        media = _first_display_media(value, site)
        if media is None:
            self.thumbnail.setText("No displayed thumbnail")
            self.thumbnail.setPixmap(QPixmap())
            return
        identity, url = media
        self._selected_thumbnail_identities.add(identity)
        self._thumbnail_sources[identity] = hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()
        cached = self._thumbnail_cache.get(identity)
        if cached is not None:
            self._show_thumbnail(cached)
        else:
            self.thumbnail.setText("Loading displayed thumbnail…")
            self._pending_thumbnails.add(identity)
            self.coordinator.fetch_thumbnail(identity, url)

    def _thumbnail_loaded(self, identity: object, data: object) -> None:
        if (
            not isinstance(identity, MediaIdentity)
            or identity not in self._pending_thumbnails
        ):
            return
        self._pending_thumbnails.discard(identity)
        pixmap = QPixmap()
        if isinstance(data, bytes) and data and pixmap.loadFromData(data):
            self._thumbnail_cache[identity] = pixmap
            exact_hash, perceptual_hash = _pixel_hashes(pixmap)
            self.coordinator.record_displayed_media_hash(
                self.profile.profile_id,
                identity,
                self._thumbnail_sources.get(identity, ""),
                exact_hash,
                perceptual_hash,
            )
            if identity in self._selected_thumbnail_identities:
                self._show_thumbnail(pixmap)
        else:
            if identity in self._selected_thumbnail_identities:
                self.thumbnail.setText("Thumbnail unavailable")

    def _show_thumbnail(self, pixmap: QPixmap) -> None:
        self.thumbnail.setText("")
        self.thumbnail.setPixmap(
            pixmap.scaled(
                self.thumbnail.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _load_field_bindings(self) -> None:
        self.bindings_button.setEnabled(False)
        self.status.setText("Resolving exact iNaturalist text-field definitions…")
        self.coordinator.load_field_candidates()

    def _edit_mo_key(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Mushroom Observer API key")
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "The key is used only for explicitly confirmed Gate 1B link writes, Gate 1C ITS writes, and "
            "Gate 1D coordinate/name reconciliation reads. "
            "Leave persistence unchecked to keep it in memory for this application session only. "
            "Submitting an empty value clears both memory and any stored value."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("API key (not displayed or logged)")
        layout.addWidget(key_edit)
        persist = QCheckBox("Persist this key as plaintext in QSettings")
        layout.addWidget(persist)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        value = key_edit.text().strip()
        if persist.isChecked() and value:
            warning = (
                "This stores the Mushroom Observer API key as plaintext in the application's "
                "QSettings file. Anyone able to read that file can recover the key. Continue?"
            )
            if (
                QMessageBox.warning(
                    self,
                    "Plaintext credential storage",
                    warning,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        self.coordinator.set_mo_api_key(
            self.profile.profile_id, value, persist_plaintext=persist.isChecked()
        )
        self.status.setText(
            "Mushroom Observer API key stored, but not yet verified against the selected account."
            if value
            else "Mushroom Observer API key cleared; MO writes are blocked."
        )

    def _prepare_repairs(self) -> None:
        row = self._selected()
        if not row or row["kind"] not in {"pair", "issue"}:
            QMessageBox.information(
                self,
                "Select a pair or link issue",
                "Select a confirmed pair or an explicitly reviewable link issue first.",
            )
            return
        if row["kind"] == "pair":
            detail = self.coordinator.db.pair_detail(
                self.profile.profile_id, int(row["pair_id"])
            )
            if not detail or detail.get("review_state") != "confirmed":
                QMessageBox.warning(
                    self,
                    "Confirmed pair required",
                    "Only a currently confirmed, non-excluded pair can produce link additions.",
                )
                return
            self.coordinator.prepare_link_repairs(
                self.profile.profile_id, pair_id=int(row["pair_id"])
            )
            return

        issue_id = int(row["issue_id"])
        issue = self.coordinator.db.issue_detail(self.profile.profile_id, issue_id)
        if (
            not issue
            or str(issue.get("issue_type") or "") not in REPAIRABLE_LINK_ISSUE_TYPES
        ):
            QMessageBox.warning(
                self, "Link issue required", "This issue cannot produce a link repair."
            )
            return
        records = {
            str(item.get("site")): int(item["observation_id"])
            for item in issue.get("records") or []
            if item.get("site") in {"mo", "inat"} and item.get("observation_id")
        }
        mo_id = records.get("mo")
        inat_id = records.get("inat")
        if not mo_id:
            mo_id, accepted = QInputDialog.getInt(
                self,
                "Exact Mushroom Observer record",
                "Mushroom Observer observation ID reviewed for this issue:",
                1,
                1,
            )
            if not accepted:
                return
        if not inat_id:
            inat_id, accepted = QInputDialog.getInt(
                self,
                "Exact iNaturalist record",
                "iNaturalist observation ID reviewed for this issue:",
                1,
                1,
            )
            if not accepted:
                return
        intent_label, accepted = QInputDialog.getItem(
            self,
            "Explicit link-issue review",
            "Reviewed intended final state:",
            (
                "Reciprocal link between these exact records",
                "Remove reviewed incorrect rows only",
            ),
            0,
            False,
        )
        if not accepted:
            return
        intent = (
            "reciprocal" if intent_label.startswith("Reciprocal") else "remove_only"
        )
        confirmation = (
            f"Mark issue {issue_id} as explicitly reviewed for MO {mo_id} and iNaturalist "
            f"{inat_id}? This only authorizes a preview; it does not write remotely."
        )
        if (
            QMessageBox.question(
                self,
                "Confirm reviewed records",
                confirmation,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.coordinator.db.review_link_issue(
                self.profile.profile_id, issue_id, intent, mo_id, inat_id
            )
        except Exception as exc:
            QMessageBox.warning(self, "Issue review unavailable", str(exc))
            return
        self.coordinator.prepare_link_repairs(
            self.profile.profile_id, issue_id=issue_id
        )

    def _link_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, LinkRepairPreview):
            self._link_action_failed("The preview response was invalid.")
            return
        dialog = LinkRepairPreviewDialog(payload, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.coordinator.execute_link_repairs(payload, dialog.selected_options())
        else:
            self.status.setText(
                "Link-repair preview closed; no remote writes were made."
            )

    def _prepare_its(self) -> None:
        row = self._selected()
        if not row or row.get("kind") != "pair":
            QMessageBox.information(
                self,
                "Select a confirmed pair",
                "ITS comparison and writes are available only for a selected confirmed pair.",
            )
            return
        detail = self.coordinator.db.pair_detail(
            self.profile.profile_id, int(row["pair_id"])
        )
        if (
            not detail
            or detail.get("review_state") != "confirmed"
            or detail.get("excluded")
        ):
            QMessageBox.warning(
                self,
                "Confirmed pair required",
                "Confirm this exact non-excluded pair before comparing ITS data.",
            )
            return
        self.coordinator.prepare_its_comparison(
            self.profile.profile_id, int(row["pair_id"])
        )

    def _its_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, ITSComparisonPreview):
            self._link_action_failed("The ITS comparison response was invalid.")
            return
        dialog = ITSComparisonDialog(payload, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            option = dialog.selected_option()
            if option is not None:
                self.coordinator.execute_its_action(payload, option)
        else:
            self.status.setText("ITS comparison closed; no remote write was made.")

    def _confirmed_pair_row(self) -> Optional[dict[str, Any]]:
        row = self._selected()
        if not row or row.get("kind") != "pair":
            QMessageBox.information(
                self,
                "Select a confirmed pair",
                "This action is available only for a selected confirmed pair.",
            )
            return None
        detail = self.coordinator.db.pair_detail(
            self.profile.profile_id, int(row["pair_id"])
        )
        if (
            not detail
            or detail.get("review_state") != "confirmed"
            or detail.get("excluded")
        ):
            QMessageBox.warning(
                self,
                "Confirmed pair required",
                "Confirm this exact non-excluded pair first.",
            )
            return None
        return row

    def _prepare_coordinates(self) -> None:
        row = self._confirmed_pair_row()
        if row is not None:
            self.coordinator.prepare_coordinate_comparison(
                self.profile.profile_id, int(row["pair_id"])
            )

    def _coordinate_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, CoordinateComparisonPreview):
            self._link_action_failed("The coordinate comparison response was invalid.")
            return
        dialog = CoordinateComparisonDialog(payload, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            option = dialog.selected_option()
            if option is not None:
                self.coordinator.execute_coordinate_action(payload, option)
        else:
            self.status.setText(
                "Coordinate comparison closed; no remote write was made."
            )

    def _prepare_photo_identity_review(self) -> None:
        row = self._selected()
        if not row or row.get("kind") != "pair":
            QMessageBox.information(
                self,
                "Select a pair",
                "Photo identity review is available for a selected candidate "
                "or confirmed pair.",
            )
            return
        detail = self.coordinator.db.pair_detail(
            self.profile.profile_id, int(row["pair_id"])
        )
        if (
            not detail
            or str(detail.get("review_state") or "") not in {"candidate", "confirmed"}
            or detail.get("excluded")
        ):
            QMessageBox.warning(
                self,
                "Reviewable pair required",
                "Select a candidate or confirmed, non-excluded pair.",
            )
            return
        self.coordinator.prepare_photo_identity_review(
            self.profile.profile_id, int(row["pair_id"])
        )

    def _photo_identity_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, PhotoIdentityPreview):
            self._link_action_failed(
                "The read-only photo comparison response was invalid."
            )
            return
        dialog = PhotoIdentityReviewDialog(
            payload, self.coordinator.inat_client.download_image, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.decision:
            self.status.setText(
                "Photo identity review closed; no decision or remote change was made."
            )
            return
        try:
            self.coordinator.db.set_pair_review(
                self.profile.profile_id, payload.pair_id, dialog.decision
            )
        except Exception as exc:
            QMessageBox.warning(self, "Pair action unavailable", str(exc))
            return
        self._record_pair_undo(
            payload.pair_id,
            dialog.decision,
            payload.mo_observation_id,
            payload.inat_observation_id,
        )
        self._advance_past_decided_pair(payload.pair_id)
        verb = "confirmed" if dialog.decision == "confirmed" else "rejected"
        self.status.setText(
            f"Pair {verb} locally after photo review: MO "
            f"{payload.mo_observation_id} ↔ iNaturalist "
            f"{payload.inat_observation_id}. No remote data was changed."
        )

    def _prepare_photos(self) -> None:
        row = self._confirmed_pair_row()
        if row is not None:
            self.coordinator.prepare_photo_comparison(
                self.profile.profile_id, int(row["pair_id"])
            )

    def _photo_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, PhotoComparisonPreview):
            self._link_action_failed("The photo comparison response was invalid.")
            return
        dialog = PhotoComparisonDialog(
            payload, self.coordinator.inat_client.download_image, self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            option = dialog.selected_option()
            if option is not None:
                self.coordinator.execute_photo_action(payload, option)
        else:
            self.status.setText(
                "Photo synchronization preview closed; no remote write was made."
            )

    def _missing_record_row(self) -> Optional[dict[str, Any]]:
        """Gate 2A entry gate: only a record already marked confirmed-missing
        on iNaturalist (via the existing 'Mark / clear confirmed missing'
        toggle) may start a creation preview — an unpaired record is not
        automatically missing, and only MO->iNaturalist creation is proven
        (section 4). This independently revalidates
        ``_create_missing_eligible``, which only controls button enablement."""
        row = self._selected()
        if not self._create_missing_eligible(row):
            if (
                row
                and row.get("kind") == "record"
                and str(row.get("state", "")) == "confirmed_missing_on_mo"
            ):
                QMessageBox.information(
                    self,
                    "Direction not supported",
                    "Creating a missing observation on Mushroom Observer from an iNaturalist source "
                    "is not supported yet — only Mushroom Observer -> iNaturalist creation has been "
                    "proven safe.",
                )
            else:
                QMessageBox.information(
                    self,
                    "Confirmed missing record required",
                    "Select an MO record already marked 'confirmed missing on iNaturalist' "
                    "(use the 'Mark / clear confirmed missing' button first).",
                )
            return None
        return row

    def _prepare_observation_creation(self) -> None:
        row = self._missing_record_row()
        if row is not None:
            self.coordinator.prepare_observation_creation(
                self.profile.profile_id,
                str(row["site"]),
                int(row["remote_id"]),
            )

    def _observation_creation_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, ObservationCreationPreview):
            self._link_action_failed(
                "The observation creation preview response was invalid."
            )
            return
        dialog = ObservationCreationPreviewDialog(
            payload, self.coordinator.inat_client.download_image, self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.coordinator.execute_observation_creation_action(
                payload, dialog.selected_items()
            )
        else:
            self.status.setText(
                "Observation creation closed; no remote write was made."
            )

    def _prepare_consolidation(self) -> None:
        mo_ids: tuple[int, ...] = ()
        inat_ids: tuple[int, ...] = ()
        row = self._selected()
        if row and row.get("kind") == "issue" and row.get("issue_id"):
            detail = self.coordinator.db.issue_detail(
                self.profile.profile_id, int(row["issue_id"])
            )
            records = detail.get("records", ()) if detail else ()
            mo_ids = tuple(
                int(item["observation_id"])
                for item in records
                if str(item.get("site")) == "mo"
            )
            inat_ids = tuple(
                int(item["observation_id"])
                for item in records
                if str(item.get("site")) == "inat"
            )
        elif row and row.get("kind") == "consolidation":
            detail = self.coordinator.db.consolidation_detail(
                self.profile.profile_id,
                int(str(row["row_key"]).split(":", 1)[1]),
            )
            if detail and str(detail.get("state")) == "finalized":
                if detail.get("canonical_mo_observation_id") is not None:
                    mo_ids = (int(detail["canonical_mo_observation_id"]),)
                if detail.get("canonical_inat_observation_id") is not None:
                    inat_ids = (int(detail["canonical_inat_observation_id"]),)
        dialog = DuplicateSetDialog(mo_ids=mo_ids, inat_ids=inat_ids, parent=self)
        if row and row.get("kind") == "consolidation":
            dialog.setWindowTitle("Add duplicate to existing consolidation")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status.setText(
                "Duplicate-set entry cancelled; no journal rows were created."
            )
            return
        self.coordinator.prepare_consolidation(
            self.profile.profile_id,
            dialog.candidates(),
        )

    def _consolidation_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, ConsolidationPreview):
            self._link_action_failed("The consolidation preview response was invalid.")
            return
        dialog = ConsolidationPreviewDialog(
            payload,
            self.coordinator.inat_client.download_image,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.approved_preview is not None:
                self.coordinator.execute_consolidation_action(
                    dialog.approved_preview,
                )
        else:
            self.status.setText(
                "Consolidation preview closed; no journal rows or remote writes were created."
            )

    def _prepare_donor_deletion(self) -> None:
        row = self._selected()
        if (
            not row
            or row.get("kind") != "consolidation"
            or str(row.get("state")) != "finalized"
        ):
            QMessageBox.information(
                self,
                "Select finalized consolidation",
                "Donor deletion review is available only from one finalized "
                "consolidation-history row.",
            )
            return
        consolidation_id = int(str(row["row_key"]).split(":", 1)[1])
        self.coordinator.prepare_donor_deletion(
            self.profile.profile_id,
            consolidation_id,
        )

    def _resume_donor_deletion(self) -> None:
        row = self._selected()
        if not row or row.get("kind") != "consolidation":
            return
        consolidation_id = int(str(row["row_key"]).split(":", 1)[1])
        self.coordinator.resume_donor_deletion(
            self.profile.profile_id,
            consolidation_id,
        )

    def _deletion_preview_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, DonorDeletionPreview):
            self._link_action_failed(
                "The donor-deletion readiness response was invalid."
            )
            return
        dialog = DonorDeletionPreviewDialog(payload, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.coordinator.execute_donor_deletion(
                payload,
                dialog.approved_member_ids,
            )
        else:
            self.status.setText(
                "Deletion review closed; no deletion plan was journaled and "
                "no remote request was sent."
            )

    def _show_consolidation_history(self) -> None:
        for index in range(self.categories.count()):
            item = self.categories.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) == "consolidation_history":
                self.categories.setCurrentRow(index)
                return

    def _prepare_name_proposal(self) -> None:
        row = self._confirmed_pair_row()
        if row is not None:
            self.coordinator.prepare_name_proposal(
                self.profile.profile_id, int(row["pair_id"])
            )

    def _name_proposal_ready(self, payload: object) -> None:
        self._set_action_controls_enabled(True)
        if not isinstance(payload, NameProposalPreview):
            self._link_action_failed("The name proposal response was invalid.")
            return
        dialog = NameProposalDialog(payload, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            candidate = dialog.selected_candidate()
            if candidate is None:
                return
            if candidate.target_site is RemoteSite.INAT:
                self.coordinator.delegate_inat_identification(payload, candidate)
            else:
                self.coordinator.record_mo_proposal_draft(payload, candidate)
        else:
            self.status.setText("Name proposal closed; no proposal was made.")

    def _recover_selected_action(self) -> None:
        row = self._selected()
        if not row or row.get("kind") != "action":
            return
        action = self.coordinator.db.action(
            self.profile.profile_id, int(row["action_id"])
        )
        if not action:
            return
        state = str(action["state"])
        if state == "outcome_unknown":
            self.coordinator.verify_unknown_link_action(
                self.profile.profile_id, int(action["action_id"])
            )
        elif state == "pending":
            self.coordinator.resume_link_action_group(
                self.profile.profile_id, int(action["action_group_id"])
            )
        else:
            QMessageBox.information(
                self,
                "No safe resume available",
                "Only pending actions can resume. Outcome-unknown actions can be verified without retrying.",
            )

    def _cancel_selected_action(self) -> None:
        row = self._selected()
        if not row or row.get("kind") != "action":
            return
        if self.coordinator.db.cancel_pending_action(
            self.profile.profile_id, int(row["action_id"])
        ):
            self.status.setText(
                "Pending journal action group cancelled before execution."
            )
            self._reload()
        else:
            QMessageBox.information(
                self,
                "Cannot cancel action",
                "Only a pending action can be cancelled directly.",
            )

    def _link_action_progress(self, message: str) -> None:
        self.status.setText(message)
        self._ui_busy = True
        self._update_contextual_controls()

    def _link_action_failed(self, message: str) -> None:
        self._set_action_controls_enabled(True)
        self.status.setText("Reconciliation action stopped safely.")
        QMessageBox.warning(self, "Reconciliation action unavailable", message)

    def _link_actions_changed(self) -> None:
        self._set_action_controls_enabled(True)
        self._reload()

    def _refresh_deletion_controls(self, enabled: bool = True) -> None:
        """Gate both deletion controls on the current selection.

        Deletion is irreversible, so neither control may present itself as
        available unless the selected row can actually accept it.
        """
        row = self._selected() if enabled else None
        review = False
        resumable = False
        if row is not None and row.get("kind") == "consolidation":
            review = str(row.get("state")) == "finalized"
            try:
                unresolved = self.coordinator.db.unresolved_deletion_for_consolidation(
                    self.profile.profile_id,
                    int(str(row["row_key"]).split(":", 1)[1]),
                )
            except Exception as exc:
                # This reader RAISES on a corrupt deletion ledger (multiple
                # unresolved attempts for one consolidation). It is called
                # from the selectionChanged slot, so letting it escape aborts
                # the rest of the selection handler -- and the process under
                # PySide6 -- on nothing worse than clicking a row. Fail
                # closed: offer neither destructive control and say why.
                self.status.setText(f"Donor deletion controls unavailable: {exc}")
                self.review_deletion.setEnabled(False)
                self.resume_deletion.setEnabled(False)
                return
            resumable = bool(unresolved and unresolved.get("resumable"))
        self.review_deletion.setEnabled(review)
        self.resume_deletion.setEnabled(resumable)

    def _set_action_controls_enabled(self, enabled: bool) -> None:
        self._ui_busy = not enabled
        self._update_contextual_controls()
        if enabled:
            # Candidate rows whose workers stood down for this action are back
            # in the un-analysed state; pick them up now the remotes are free.
            self._request_candidate_photo_prefetch()

    def _field_candidates_loaded(self, payload: object) -> None:
        self.bindings_button.setEnabled(not self._ui_busy)
        if not isinstance(payload, dict) or payload.get("error"):
            QMessageBox.warning(
                self,
                "Field resolution failed",
                str(payload.get("error") if isinstance(payload, dict) else payload),
            )
            return
        fields = (
            ("mo_url", "Mushroom Observer URL", "text"),
            ("its", "DNA Barcode ITS", "dna"),
            ("its_accession", "Genbank Accession Number", "text"),
        )
        for purpose, exact_name, datatype in fields:
            definitions = payload.get(purpose) or []
            if not definitions:
                if purpose == "mo_url":
                    QMessageBox.warning(
                        self,
                        "Required field unavailable",
                        "No exact text field named Mushroom Observer URL was found. Link evidence is disabled.",
                    )
                continue
            selected = definitions[0]
            override = len(definitions) > 1
            if override:
                labels = [
                    f"ID {item.get('id')} — {item.get('name')} ({item.get('datatype')})"
                    for item in definitions
                ]
                label, accepted = QInputDialog.getItem(
                    self,
                    f"Select {exact_name}",
                    f"Multiple exact {datatype} fields named {exact_name} qualify. Select the profile-specific binding:",
                    labels,
                    0,
                    False,
                )
                if not accepted:
                    continue
                selected = definitions[labels.index(label)]
            self.coordinator.db.save_field_binding(
                self.profile.profile_id,
                purpose,
                int(selected["id"]),
                exact_name,
                datatype,
                "verified",
                is_override=override,
            )
        self.status.setText(
            "Field bindings verified. The next scan will revalidate them."
        )

    def _pair_action(self, state: str) -> None:
        row = self._selected()
        if not row or row["kind"] != "pair":
            return
        pair_id = int(row["pair_id"])
        mo_id = int(row["remote_id"])
        inat_id = int(row["other_id"])
        try:
            self.coordinator.db.set_pair_review(self.profile.profile_id, pair_id, state)
        except Exception as exc:
            QMessageBox.warning(self, "Pair action unavailable", str(exc))
            return
        self._record_pair_undo(pair_id, state, mo_id, inat_id)
        self._advance_past_decided_pair(pair_id)
        verb = "confirmed" if state == "confirmed" else "rejected"
        self.status.setText(
            f"Pair {verb} locally: MO {mo_id} ↔ iNaturalist {inat_id}. "
            "No remote data was changed."
        )

    def _advance_past_decided_pair(self, pair_id: int) -> None:
        """Drop the decided row and select whatever moved up into its place.

        A decided pair leaves the candidate list, so the reviewer should land
        on the NEXT candidate. Reloading instead would reset the model and send
        the selection back to row 0 -- returning a reviewer working through
        candidate 57 to the top of the list -- and would additionally discard
        every photo analysis loaded so far.
        """
        self._photo_analysis_cache.pop(pair_id, None)
        self._pending_photo_analyses.discard(pair_id)
        removed = (
            self.model.remove_pair(pair_id)
            if self.model.category == "candidate_pairs"
            else None
        )
        if removed is None:
            # Some other category is showing, so the row's new state may still
            # belong in it. Fall back to rebuilding the list.
            self._reload()
            return
        self._reload_counts()
        if self.model.rowCount():
            self.table.selectRow(min(removed, self.model.rowCount() - 1))
        else:
            self._selection_changed()
        self._request_candidate_photo_prefetch()

    def _toggle_exclusion(self) -> None:
        row = self._selected()
        if row and row["kind"] == "pair":
            pair_id = int(row["pair_id"])
            excluded = self.coordinator.db.pair_is_excluded(
                self.profile.profile_id, pair_id
            )
            self.coordinator.db.set_pair_excluded(
                self.profile.profile_id, pair_id, not excluded
            )
            self._reload()
            self.status.setText(
                "Pair reopened for review."
                if excluded
                else "Pair excluded locally. No remote data was changed."
            )

    def _toggle_issue(self) -> None:
        row = self._selected()
        if row and row["kind"] == "issue":
            state = "open" if row["state"] in {"ignored", "resolved"} else "ignored"
            self.coordinator.db.set_issue_state(
                self.profile.profile_id, int(row["issue_id"]), state
            )
            self._reload()
            self.status.setText(
                f"Issue marked {state} locally. No remote data was changed."
            )

    def _toggle_missing(self) -> None:
        row = self._selected()
        if row and row["kind"] == "record":
            missing = not str(row["state"]).startswith("confirmed_missing")
            self.coordinator.db.set_confirmed_missing(
                self.profile.profile_id, row["site"], int(row["remote_id"]), missing
            )
            self._reload()
            self.status.setText(
                "Record marked confirmed missing locally."
                if missing
                else "Confirmed-missing mark cleared locally."
            )

    def _open_selected(self) -> None:
        row = self._selected()
        if not row or not row.get("remote_id"):
            return
        url = (
            f"https://www.inaturalist.org/observations/{int(row['remote_id'])}"
            if row["site"] == "inat"
            else f"https://mushroomobserver.org/obs/{int(row['remote_id'])}"
        )
        QDesktopServices.openUrl(QUrl(url))

    def _open_other(self) -> None:
        row = self._selected()
        if row and row.get("other_id") and row.get("other_site"):
            site = str(row["other_site"])
            observation_id = int(row["other_id"])
            url = (
                f"https://www.inaturalist.org/observations/{observation_id}"
                if site == "inat"
                else f"https://mushroomobserver.org/obs/{observation_id}"
            )
            QDesktopServices.openUrl(QUrl(url))

    def _open_consolidation_member(self) -> None:
        row = self._selected()
        if not row or row.get("kind") != "consolidation":
            QMessageBox.information(
                self,
                "Select consolidation history",
                "Select a row in Consolidation history first.",
            )
            return
        consolidation_id = int(str(row["row_key"]).split(":", 1)[1])
        detail = self.coordinator.db.consolidation_detail(
            self.profile.profile_id,
            consolidation_id,
        )
        members = list(detail.get("members", ())) if detail else []
        if not members:
            QMessageBox.information(
                self,
                "No consolidation members",
                "This consolidation has no retained member records.",
            )
            return
        choices = [
            (
                f"{str(member['site']).upper()} #{int(member['observation_id'])} — "
                f"{member['local_state']}"
            )
            for member in members
        ]
        selected, accepted = QInputDialog.getItem(
            self,
            "Open consolidation member",
            "Remote observation",
            choices,
            0,
            False,
        )
        if not accepted:
            return
        member = members[choices.index(selected)]
        QDesktopServices.openUrl(QUrl(str(member["remote_url"])))

    def _reload(self, *, select_first: bool = False) -> None:
        self._reload_counts()
        self._category_changed(self.categories.currentRow())
        if select_first and self.model.rowCount() > 0:
            self.table.selectRow(0)

    def _reload_counts(self) -> None:
        self._count_generation += 1
        generation = self._count_generation
        worker = _DashboardCountWorker(
            self.coordinator,
            self.profile.profile_id,
            generation,
        )
        signals = worker.signals
        self._live_count_signals.add(signals)
        signals.finished.connect(
            lambda gen, counts, owned=signals: self._dashboard_counts_loaded(
                owned, gen, counts
            )
        )
        self._count_pool.start(worker)

    def _dashboard_counts_loaded(
        self,
        signals: QObject,
        generation: int,
        payload: object,
    ) -> None:
        self._live_count_signals.discard(signals)
        if generation != self._count_generation or not isinstance(payload, dict):
            return
        error = payload.get("__error__")
        if error:
            self.status.setText(f"Dashboard counts could not be refreshed: {error}")
            return
        for index in range(self.categories.count()):
            item = self.categories.item(index)
            key = str(item.data(Qt.ItemDataRole.UserRole))
            label = str(item.data(Qt.ItemDataRole.UserRole + 1))
            count = payload.get(key)
            if isinstance(count, int):
                item.setText(f"{label} ({count:,})")

    def _request_scan(self, *, force_full: bool) -> None:
        """Check the sign-in before committing to a scan.

        A scan reads tens of thousands of records over several minutes, and the
        only authenticated call in it is the deleted feed. Discovering a stale
        token there means finding out late, so ask first — one request against
        /users/me, which is the only way to learn whether iNaturalist still
        accepts the stored token.

        The buttons are deliberately NOT disabled while the probe runs. The
        answer arrives on a coordinator signal that is dropped if the
        generation moves, and a dropped answer would leave the controls dead
        with no way back; a second click merely re-probes, and coordinator.scan
        already refuses to start a scan on top of a running one.
        """
        self._pending_scan_full = force_full
        self.status.setText("Checking iNaturalist sign-in…")
        self.coordinator.check_authentication(self.profile.profile_id)

    def _authentication_checked(self, state: str, detail: str) -> None:
        if self._pending_scan_full is None:
            return
        force_full = self._pending_scan_full
        self._pending_scan_full = None
        # 'unavailable' means the probe itself could not run. Proceed and let
        # the scan report the real network fault rather than blaming the token.
        if state in ("ok", "unavailable"):
            self.coordinator.scan(self.profile.profile_id, force_full=force_full)
            return
        reasons = {
            "unauthenticated": "You are not signed in to iNaturalist.",
            "mismatch": (
                f"You are signed in as {detail or 'another account'}, but this profile "
                f"reconciles {self.profile.inat_login}."
            ),
            "rejected": (
                "iNaturalist rejected the stored sign-in — it has most likely expired "
                "(tokens last about a day)."
            ),
        }
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("iNaturalist sign-in unavailable")
        box.setText(
            reasons.get(state, "The iNaturalist sign-in could not be confirmed.")
        )
        box.setInformativeText(
            "A scan will still run, but read-only and public: deleted records will not be "
            "detected, private coordinates stay hidden, and pairs that need them will not be "
            "confirmed automatically.\n\nSign in again first for a complete scan."
        )
        scan_anyway = box.addButton("Scan anyway", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(scan_anyway)
        box.exec()
        if box.clickedButton() is scan_anyway:
            self.coordinator.scan(self.profile.profile_id, force_full=force_full)
        else:
            self.status.setText("Scan cancelled; sign in to iNaturalist and try again.")

    def closeEvent(self, event) -> None:
        """Stop the photo prefetch before Qt waits for its pool to drain.

        ``_photo_prefetch_pool`` is a child QObject, so its destructor calls
        ``waitForDone()`` with no timeout. Left alone, the GUI thread would sit
        there for as long as the whole queued batch takes -- both complete
        photo sets per row, through Mushroom Observer's serialized requests and
        iNaturalist's one-per-second limiter -- which reads as a hang on quit.

        Cancelling first makes each worker stop at its next check -- one still
        queued does no remote work at all when its turn comes -- so the
        unavoidable wait is bounded by the one image download already in flight
        per pool thread rather than by the rest of the batch.
        """
        self._cancel_candidate_photo_prefetch()
        super().closeEvent(event)

    def _scan_started(self) -> None:
        self._ui_busy = True
        self._update_contextual_controls()
        self._scan_step = 0
        self._scan_fractions = {}
        # Reset the VALUE too, not just the range: _scan_progress only ever
        # raises it, so a second scan in the same session would otherwise
        # inherit the previous run's 100% and sit there.
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.progress.show()
        self.status.setText("Starting scan (remote read-only)…")

    def _scan_progress(self, part: str, current: int, total: int) -> None:
        """Report one worker's progress as an overall scan position.

        Reports arrive INTERLEAVED, because paired phases run concurrently in
        separate pools and finish at different times. Two consequences are
        handled here and neither is optional:

        * A phase's fraction is scored against ITS OWN step, never against the
          furthest step reached. A slow Mushroom Observer worker completing
          after the plan phase has already started is still step 5 finishing —
          crediting its 100% to step 6 would show 86% while two phases remain.
        * A step is scored as its SLOWEST member, because both concurrent
          workers must finish before the step is done. Taking the faster one
          would park the bar at the top of the step while the other still had
          ten minutes of Mushroom Observer requests left to make.
        * The bar is monotonic as a final guard. It should not need to be, given
          the two rules above, but a progress bar that ticks backwards reads as
          a fault and no arithmetic here is worth that risk.
        """
        base, _, stage = part.partition(":")
        label = SCAN_PHASE_LABELS.get(base, base)
        if stage:
            label = f"{label} — {SCAN_STAGE_LABELS.get(stage, stage)}"
        step = SCAN_PHASE_STEPS.get(base, self._scan_step or 1)
        self._scan_step = max(self._scan_step, step)
        # (0, 0) is the phase-start ping: the phase has begun but has no count
        # to report yet, so show the name alone rather than a meaningless zero.
        counted = (
            f": {current:,} / {total:,}"
            if total
            else "" if not current else f": {current:,}"
        )
        self.status.setText(f"Step {step} of {SCAN_STEP_COUNT} — {label}{counted}")
        within = min(max((current / total) if total else 0.0, 0.0), 1.0)
        # Stage-aware, so a phase's fraction keeps climbing across its stages
        # instead of restarting at each one.
        self._scan_fractions[base] = max(
            self._scan_fractions.get(base, 0.0),
            _stage_fraction(base, stage, within),
        )
        peers = [
            self._scan_fractions[peer]
            for peer in SCAN_STEP_PARTS.get(step, (base,))
            if peer in self._scan_fractions
        ]
        fraction = min(peers) if peers else within
        overall = ((step - 1) + fraction) / SCAN_STEP_COUNT
        self.progress.setRange(0, 1000)
        self.progress.setValue(max(self.progress.value(), int(overall * 1000)))
        self.progress.setFormat("%p%")

    def _scan_finished(self, message: str) -> None:
        # A scan is the one thing that can change either side's photo set, so
        # analyses computed before it must not be reused afterwards.
        self._photo_analysis_cache.clear()
        self._scan_stopped(message)
        self._reload()

    def _scan_failed(self, message: str) -> None:
        self._scan_stopped(message)

    def _scan_stopped(self, message: str) -> None:
        self._ui_busy = False
        self._update_contextual_controls()
        self._request_candidate_photo_prefetch()
        self._update_inventory_summary()
        self.progress.hide()
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self._scan_step = 0
        self._scan_fractions = {}
        if message.startswith("Reconciled "):
            message += (
                " Next: choose Candidate pairs, review the highest scores, "
                "then confirm or reject each pair locally."
            )
        self.status.setText(message)


def open_reconciliation_window(
    coordinator: ReconciliationCoordinator, authenticated_login: str, parent=None
):
    setup = ReconciliationSetupDialog(coordinator, authenticated_login, parent)
    if setup.exec() != QDialog.DialogCode.Accepted or setup.profile is None:
        return None
    window = ReconciliationWindow(coordinator, setup.profile, parent)
    window.show()
    return window


def _first_display_media(
    payload: object, site: str
) -> Optional[tuple[MediaIdentity, str]]:
    if not isinstance(payload, dict):
        return None
    raw = payload
    results = raw.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        raw = results[0]
    if site == "inat":
        rows = raw.get("observation_photos") or raw.get("photos") or []
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            photo = row.get("photo") if isinstance(row.get("photo"), dict) else row
            photo_id = photo.get("id")
            url = str(photo.get("url") or photo.get("medium_url") or "")
            if photo_id and url.startswith("https://"):
                return MediaIdentity(RemoteSite.INAT, str(photo_id), "display"), url
    else:
        rows = raw.get("images") or []
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            photo_id = row.get("id")
            url = str(row.get("url") or row.get("medium_url") or row.get("src") or "")
            if photo_id and url.startswith("https://"):
                return MediaIdentity(RemoteSite.MO, str(photo_id), "display"), url
    return None


def _pixel_hashes(pixmap: QPixmap) -> tuple[str, str]:
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = bytes(image.constBits())[: image.sizeInBytes()]
    exact = hashlib.sha256(
        image.width().to_bytes(4, "big") + image.height().to_bytes(4, "big") + pixels
    ).hexdigest()
    tiny = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        8,
        8,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    samples = bytes(tiny.constBits())[: tiny.sizeInBytes()]
    values = [
        samples[row * tiny.bytesPerLine() + column]
        for row in range(8)
        for column in range(8)
    ]
    average = sum(values) / len(values)
    bits = "".join("1" if value >= average else "0" for value in values)
    perceptual = f"{int(bits, 2):016x}"
    return exact, perceptual


def _distinctive_note_overlap(left: object, right: object) -> bool:
    """Compare selected details in memory; tokens and raw text are never persisted."""

    def collect(value: object, key: str = "") -> str:
        if isinstance(value, dict):
            return " ".join(
                collect(item, str(name))
                for name, item in value.items()
                if str(name).casefold() in {"notes", "description", "body", "comments"}
                or isinstance(item, (dict, list))
            )
        if isinstance(value, list):
            return " ".join(collect(item, key) for item in value)
        return (
            str(value)
            if key.casefold() in {"notes", "description", "body", "comments"}
            else ""
        )

    def tokens(value: object) -> set[str]:
        return {
            word
            for word in re.findall(r"[a-z]{5,}", collect(value).casefold())
            if word
            not in {
                "observation",
                "mushroom",
                "species",
                "inaturalist",
                "location",
                "photo",
            }
        }

    return len(tokens(left).intersection(tokens(right))) >= 3


def _format_local_detail(value: object) -> str:
    if not isinstance(value, dict):
        return str(value or "No detail available")
    lines: list[str] = []
    if value.get("action_id"):
        its_action = str(value.get("action_type") or "").startswith(
            ("inat_its_", "mo_sequence_")
        )
        lines.extend(
            [
                f"Journal action {value.get('action_id')} — {value.get('action_type')}",
                f"State: {value.get('state')}   Phase: {value.get('last_phase')}",
                f"MO {value.get('mo_observation_id')} ↔ iNaturalist {value.get('inat_observation_id')}",
                f"Exact remote row: {value.get('remote_row_uuid') or value.get('remote_row_id') or 'new row'}",
                f"Destructive: {'yes' if value.get('destructive') else 'no'}",
                f"Verification: {value.get('verification_state') or 'not completed'}",
                f"Error code: {value.get('last_error_code') or 'none'}",
                f"Attempts: {value.get('attempt_count') or 0}",
            ]
        )
        if its_action:
            lines.extend(
                [
                    f"Source: {value.get('source_site')} record {value.get('source_record_id')}, "
                    f"row {value.get('source_sequence_remote_id') or 'unknown'}",
                    f"Evidence: {value.get('evidence_type') or 'unknown'}",
                    f"Sequence fingerprint: {value.get('sequence_fingerprint') or 'none'}",
                    f"Normalized accession: {value.get('normalized_accession') or 'none'}",
                    f"Resulting remote row: {value.get('server_row_uuid') or value.get('server_row_id') or 'not returned'}",
                ]
            )
        else:
            lines.extend(
                [
                    f"Current target: {value.get('current_target_id') or 'none'}",
                    f"Proposed target: {value.get('desired_target_id') or 'row removed'}",
                ]
            )
        actions = value.get("group_actions") or []
        if len(actions) > 1:
            lines.extend(["", "Action group"])
            for item in actions:
                lines.append(
                    f"• {item.get('ordinal')}: {item.get('action_type')} — {item.get('state')}"
                )
        return "\n".join(lines)
    if value.get("issue_type"):
        lines.extend(
            [
                str(value.get("title") or "Issue"),
                f"State: {value.get('state', '')}   Severity: {value.get('severity', '')}",
                "",
                str(value.get("detail") or ""),
            ]
        )
        records = value.get("records") or []
        if records:
            lines.extend(["", "Associated records"])
            for record in records:
                lines.append(
                    f"• {record.get('site')} {record.get('observation_id')} — "
                    f"{record.get('taxon_name') or 'unknown taxon'}; "
                    f"{record.get('observed_on') or 'unknown date'}; "
                    f"scope={record.get('scope_state') or 'unavailable'}"
                )
        return "\n".join(lines)
    if value.get("consolidation_id"):
        lines.extend(
            [
                f"Stable consolidation #{value.get('consolidation_id')}",
                f"State: {value.get('state')}",
                f"Canonical MO: {value.get('canonical_mo_observation_id') or 'not participating'}",
                "Canonical iNaturalist: "
                f"{value.get('canonical_inat_observation_id') or 'not participating'}",
                "Current finalized canonical baseline: "
                f"attempt #{value.get('current_finalized_attempt_id') or 'none'}",
                "Phase 2C: deletion is permanent; only immutable lossless reviews "
                "may enter the deletion ledger.",
            ]
        )
        members = value.get("members") or []
        if members:
            lines.extend(["", "Stable members"])
            for member in members:
                provenance = (
                    f"; added by attempt #{member.get('added_by_attempt_id') or '?'}"
                )
                if member.get("originally_proposed_by_attempt_id") and member.get(
                    "originally_proposed_by_attempt_id"
                ) != member.get("added_by_attempt_id"):
                    provenance += (
                        f"; originally proposed by attempt "
                        f"#{member.get('originally_proposed_by_attempt_id')}"
                    )
                if member.get("superseded_by_attempt_id"):
                    provenance += (
                        f"; superseded by attempt "
                        f"#{member.get('superseded_by_attempt_id')}"
                        f" at {member.get('superseded_at') or 'unknown time'}"
                    )
                lines.append(
                    f"• {str(member.get('site')).upper()} "
                    f"#{member.get('observation_id')} — {member.get('role')}; "
                    f"{member.get('local_state')}; remote="
                    f"{member.get('remote_state') or 'online'}{provenance}\n"
                    f"  {member.get('remote_url') or 'remote URL unavailable'}"
                )
                if str(member.get("remote_state") or "online") == "deleted":
                    lines.append(
                        f"  Deleted remotely on "
                        f"{member.get('deleted_remotely_at') or 'unknown date'}\n"
                        f"  Canonical: MO "
                        f"{member.get('canonical_destination_mo_id') or '—'} / "
                        f"iNaturalist "
                        f"{member.get('canonical_destination_inat_id') or '—'}\n"
                        f"  Phase 2C attempt "
                        f"#{member.get('deleted_by_deletion_attempt_id') or '?'}; "
                        "historical URL retained and may no longer resolve"
                    )
        attempts = value.get("attempts") or []
        if attempts:
            lines.extend(["", "Immutable attempts"])
            for attempt in attempts:
                lines.append(
                    f"Attempt #{attempt.get('attempt_id')} — {attempt.get('state')}; "
                    f"action group #{attempt.get('action_group_id')}"
                    + (
                        "; ORIGINAL CONSOLIDATION ATTEMPT"
                        if attempt.get("is_original_attempt")
                        else ""
                    )
                    + (
                        "; CURRENT FINALIZED BASELINE"
                        if attempt.get("is_current_finalized_baseline")
                        else ""
                    )
                    + (
                        f"; supersedes #{attempt.get('supersedes_attempt_id')}"
                        if attempt.get("supersedes_attempt_id")
                        else ""
                    )
                )
                proposed = [
                    member
                    for member in (attempt.get("members") or [])
                    if member.get("participation_role") == "new_donor"
                ]
                for member in proposed:
                    disposition = (
                        "admitted and superseded"
                        if attempt.get("state") == "succeeded"
                        else "proposed but not admitted"
                    )
                    lines.append(
                        f"  + donor {str(member.get('site')).upper()} "
                        f"#{member.get('observation_id')} — {disposition}"
                    )
                canonical_context = [
                    member
                    for member in (attempt.get("members") or [])
                    if member.get("participation_role") == "canonical_context"
                ]
                for member in canonical_context:
                    lines.append(
                        f"  canonical snapshot {str(member.get('site')).upper()} "
                        f"#{member.get('observation_id')}: owner account "
                        f"{member.get('reviewed_owner_account_id') or 'unknown'}, "
                        f"display login "
                        f"{member.get('reviewed_owner_login') or 'unknown'}"
                    )
                if attempt.get("state") == "succeeded":
                    lines.append(
                        "  complete canonical snapshot fingerprints established: "
                        f"MO={attempt.get('canonical_mo_preflight_fingerprint') or 'none'}; "
                        f"iNat={attempt.get('canonical_inat_preflight_fingerprint') or 'none'}"
                    )
                    for member in canonical_context:
                        lines.append(
                            f"  stable identity {str(member.get('site')).upper()} "
                            f"#{member.get('observation_id')}: "
                            f"{member.get('reviewed_identity_fingerprint') or 'unavailable'}"
                        )
                for evidence in attempt.get("evidence") or []:
                    evidence_label = (
                        "IDENTITY"
                        if evidence.get("evidence_strength") == "strong"
                        else "SUPPORTING ONLY"
                    )
                    lines.append(
                        f"  evidence [{evidence_label}; "
                        f"{evidence.get('evidence_strength')}] "
                        f"{str(evidence.get('left_site')).upper()} "
                        f"#{evidence.get('left_observation_id')} ↔ "
                        f"{str(evidence.get('right_site')).upper()} "
                        f"#{evidence.get('right_observation_id')}: "
                        f"{evidence.get('display_summary')}"
                    )
                for action in attempt.get("actions") or []:
                    lines.append(
                        f"  action {action.get('ordinal')}: "
                        f"{action.get('action_type')} — {action.get('state')}"
                        + (
                            f" ({action.get('verification_state')})"
                            if action.get("verification_state")
                            else ""
                        )
                    )
        deletion_attempts = value.get("deletion_attempts") or []
        if deletion_attempts:
            lines.extend(["", "Immutable Phase 2C deletion attempts"])
            for attempt in deletion_attempts:
                lines.append(
                    f"Deletion attempt "
                    f"#{attempt.get('deletion_attempt_id')} — "
                    f"{attempt.get('state')}; finalized Phase 2B baseline "
                    f"#{attempt.get('base_finalized_attempt_id')}"
                )
                for item in attempt.get("items") or []:
                    lines.append(
                        f"  donor {str(item.get('site')).upper()} "
                        f"#{item.get('observation_id')} — {item.get('state')}; "
                        f"stable member #{item.get('stable_member_id')}"
                    )
                    for parity in item.get("parity_items") or []:
                        lines.append(
                            f"    parity {parity.get('source_content_type')}/"
                            f"{parity.get('source_content_identity')}: "
                            f"{parity.get('eligibility_result')} via "
                            f"{parity.get('match_method') or 'none'}"
                            + (
                                f" — {parity.get('blocking_reason')}"
                                if parity.get("blocking_reason")
                                else ""
                            )
                        )
                for action in attempt.get("actions") or []:
                    lines.append(
                        f"  action #{action.get('deletion_action_id')} "
                        f"{action.get('action_type')} — {action.get('state')}; "
                        f"verification="
                        f"{action.get('verification_state') or 'not definitive'}"
                    )
        return "\n".join(lines)
    if value.get("pair_id"):
        lines.extend(
            [
                f"MO {value.get('mo_observation_id')}  ↔  iNaturalist {value.get('inat_observation_id')}",
                f"Review: {value.get('review_state')}   Link: {value.get('link_state')}",
                f"Score: {value.get('score')} ({value.get('classification')})",
                f"Confirmed by: {value.get('confirmed_by') or 'not confirmed'}",
                f"Historical confirmation: {value.get('historical_confirmed_by') or 'none'}",
                f"Excluded: {'yes' if value.get('excluded') else 'no'}",
            ]
        )
        evidence = value.get("evidence") or []
        if evidence:
            lines.extend(["", "Evidence"])
            for item in evidence:
                lines.append(
                    f"• {item.get('family')}: +{item.get('score')} — {item.get('explanation')}"
                )
        return "\n".join(lines)
    return "\n".join(
        [
            f"{value.get('site', '')} {value.get('remote_observation_id', '')}",
            f"Taxon: {value.get('taxon_name') or 'unknown'}",
            f"Observed: {value.get('observed_on') or 'unknown'}",
            f"Owner: {value.get('owner_login') or value.get('owner_id') or 'unknown'}",
            f"Locality: {value.get('public_locality') or 'not provided'}",
            f"Fungi status: {value.get('fungi_status') or 'unknown'}",
            f"Scope: {value.get('scope_state') or 'unknown'}",
            f"Availability: {value.get('availability_state') or 'unknown'}",
            f"Deleted: {'yes' if value.get('is_deleted') else 'no'}",
            f"Unpaired state: {value.get('unpaired_state') or ''}",
        ]
    )


def _format_remote_comparison(
    payloads: dict[str, object],
    inventory_fallbacks: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    sections: list[str] = []
    fallbacks = inventory_fallbacks or {}
    for key, value in sorted(payloads.items()):
        site = key.split(":", 1)[0]
        fallback = fallbacks.get(site, {})
        raw = value
        # A failed read is emitted as {"error": ...} by the coordinator's
        # hydrate error slots. Rendering it through the normal path below
        # would print "Record: unknown / photos: 0 / Coordinates: not
        # available" -- indistinguishable from a record that genuinely has
        # none, which is exactly the state an operator reads before deciding
        # to run a coordinate or photo transfer. Say so instead.
        if isinstance(raw, dict) and raw.get("error"):
            sections.append(
                f"{site.upper()}\nDetail could not be read: {raw['error']}\n"
                "This is a failed remote read, NOT an empty record. Nothing below "
                "should be treated as this record's contents."
            )
            continue
        if isinstance(raw, dict):
            rows = raw.get("results")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                raw = rows[0]
        if not isinstance(raw, dict):
            sections.append(f"{site.upper()}\nDetail unavailable")
            continue
        user = (
            raw.get("user")
            if isinstance(raw.get("user"), dict)
            else raw.get("owner") or {}
        )
        if site == "mo":
            # Mushroom Observer serializes ``name`` as an object in high detail
            # and as a bare name id in low detail. Only the object form is a
            # taxon; an id rendered as text would print "Taxon: 12345".
            taxon = (
                raw.get("consensus")
                if isinstance(raw.get("consensus"), dict)
                else raw.get("name") if isinstance(raw.get("name"), dict) else {}
            )
            location = raw.get("location")
            locality = (
                str(location.get("name") or location.get("display_name") or "")
                if isinstance(location, dict)
                else ""
            )
            locality = str(raw.get("location_name") or locality).strip() or str(
                fallback.get("public_locality") or ""
            )
        else:
            taxon = (
                raw.get("taxon")
                if isinstance(raw.get("taxon"), dict)
                else raw.get("name") or {}
            )
            locality = str(
                raw.get("place_guess") or raw.get("location_name") or ""
            ).strip() or str(fallback.get("public_locality") or "")
        # Field order matches each site's own parser, so the detail pane and
        # the list cannot disagree about the same observation's taxon:
        # mo_parsing._observation_identity reads text_name before name, while
        # iNaturalist has no text_name at all.
        name_keys = ("text_name", "name") if site == "mo" else ("name", "text_name")
        taxon_name = (
            str(
                next(
                    (taxon.get(key) for key in name_keys if taxon.get(key)),
                    "",
                )
            ).strip()
            if isinstance(taxon, dict)
            else str(taxon or "").strip()
        )
        taxon_name = (
            taxon_name
            or str(raw.get("species_guess") or "").strip()
            or str(fallback.get("taxon_name") or "")
        )
        taxon_rank = (
            str(taxon.get("rank") or "").strip() if isinstance(taxon, dict) else ""
        ) or str(fallback.get("taxon_rank") or "")
        # Presence only. The raw point -- including private_geojson, which is
        # populated for obscured observations whenever the read was authorized
        # -- is never rendered here, matching CoordinateComparisonDialog's
        # invariant that raw coordinates are never rendered, persisted,
        # logged, copied, or put in a map URL by this UI.
        private_point = raw.get("private_geojson") or raw.get("private_location")
        public_point = (
            raw.get("latitude") is not None and raw.get("longitude") is not None
            if site == "mo"
            else raw.get("geojson") or raw.get("location")
        )
        if private_point:
            coordinates = (
                "present (private/obscured; exact point intentionally not shown)"
            )
        elif public_point:
            coordinates = "present (exact point intentionally not shown)"
        else:
            coordinates = "not available"
        geoprivacy = raw.get("geoprivacy") or raw.get("gps_hidden")
        if geoprivacy:
            coordinates += f"; privacy={geoprivacy}"
        if site == "mo":
            photo_count = mo_observation_photo_count(raw)
        else:
            photos = raw.get("observation_photos") or raw.get("photos") or []
            photo_count = (
                len(photos)
                if isinstance(photos, list)
                else 1 if isinstance(photos, dict) else 0
            )
        notes_present = bool(
            raw.get("description") or raw.get("notes") or raw.get("comments")
        )
        sections.append(
            "\n".join(
                [
                    site.upper(),
                    f"Record: {raw.get('id') or raw.get('observation_id') or 'unknown'}",
                    (
                        f"Owner: {user.get('login') or user.get('name') or user.get('id') or 'unknown'}"
                        if isinstance(user, dict)
                        else "Owner: unknown"
                    ),
                    f"Observed: {raw.get('observed_on') or raw.get('when') or raw.get('date') or 'unknown'}",
                    f"Taxon: {taxon_name or 'unknown'}"
                    + (f" ({taxon_rank})" if taxon_rank else ""),
                    f"Locality: {locality or 'not provided'}",
                    f"Coordinates: {coordinates}",
                    f"Displayed photos available: {photo_count}",
                    f"Notes/comments available for in-memory comparison: {'yes' if notes_present else 'no'}",
                ]
            )
        )
    return "\n\n".join(sections) if sections else "No remote detail available"
