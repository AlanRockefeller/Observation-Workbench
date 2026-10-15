"""Shared Qt controls for optional Codex screening in bulk preview dialogs."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.services.ai_observation_review import (
    ADAPTIVE_EVIDENCE,
    FULL_EVIDENCE,
    LUNA_MODEL,
    SOL_MODEL,
    TEXT_EVIDENCE,
    AIComparisonResult,
    AIReviewCancelled,
    AIReviewFinding,
    AIReviewRunResult,
    CodexObservationReviewer,
    ObservationReviewInput,
    comparison_rows,
    conservative_union,
    export_comparison_csv,
    finding_priority,
    stable_sample,
)
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.ui.external_links import open_external_url_silently


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str, int, int)


class _ReviewWorker(QRunnable):
    def __init__(
        self,
        reviewer: CodexObservationReviewer,
        operation: str,
        items: Sequence[ObservationReviewInput],
        *,
        model: str,
        evidence_mode: str,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._reviewer = reviewer
        self._operation = operation
        self._items = list(items)
        self._model = model
        self._evidence_mode = evidence_mode
        self._cancel = threading.Event()
        self.signals = _WorkerSignals()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            kwargs = {
                "is_cancelled": self._cancel.is_set,
                "progress": lambda label, value, maximum: self.signals.progress.emit(
                    label, value, maximum
                ),
            }
            if self._operation == "compare_models":
                result = self._reviewer.compare_models(
                    self._items,
                    evidence_mode=self._evidence_mode,
                    **kwargs,
                )
            elif self._operation == "compare_evidence":
                result = self._reviewer.compare_evidence(
                    self._items,
                    model=self._model,
                    **kwargs,
                )
            else:
                result = self._reviewer.analyze(
                    self._items,
                    model=self._model,
                    evidence_mode=self._evidence_mode,
                    **kwargs,
                )
            if self._cancel.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.finished.emit(result)
        except AIReviewCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc) or "AI analysis failed.")


class AIReviewSetupDialog(QDialog):
    def __init__(
        self,
        operation: str,
        *,
        total_count: int,
        risky_count: int,
        selected_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._operation = operation
        titles = {
            "luna": "Analyze with Luna",
            "sol": "Analyze with Sol",
            "compare_models": "Compare Luna vs Sol",
            "compare_evidence": "Compare Text vs Full Evidence",
        }
        self.setWindowTitle(titles[operation])
        self.resize(520, 230)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Codex performs an advisory consistency screen only. A clear result "
            "does not validate the identification."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        layout.addLayout(form)

        self._model_combo = QComboBox()
        self._model_combo.addItem("GPT-6 Luna", LUNA_MODEL)
        self._model_combo.addItem("GPT-6.1 Sol", SOL_MODEL)
        if operation == "compare_evidence":
            form.addRow("Model:", self._model_combo)

        self._evidence_combo = QComboBox()
        self._evidence_combo.addItem("Adaptive — text, then photos if needed", ADAPTIVE_EVIDENCE)
        self._evidence_combo.addItem("Text only", TEXT_EVIDENCE)
        self._evidence_combo.addItem(
            "Full public evidence — text + photos", FULL_EVIDENCE
        )
        if operation != "compare_evidence":
            form.addRow("Evidence:", self._evidence_combo)

        self._scope_combo = QComboBox()
        self._scope_combo.addItem(f"All candidates ({total_count})", "all")
        self._scope_combo.addItem(f"Currently risky ({risky_count})", "risky")
        self._scope_combo.addItem(f"Selected rows ({selected_count})", "selected")
        self._scope_combo.addItem("Stable sample", "sample")
        if operation in {"compare_models", "compare_evidence"}:
            self._scope_combo.setCurrentIndex(3)
        elif operation == "sol" and risky_count:
            self._scope_combo.setCurrentIndex(1)
        form.addRow("Scope:", self._scope_combo)

        self._sample_spin = QSpinBox()
        self._sample_spin.setRange(1, max(1, total_count))
        self._sample_spin.setValue(min(20, max(1, total_count)))
        self._sample_spin.setEnabled(False)
        form.addRow("Sample size:", self._sample_spin)
        self._scope_combo.currentIndexChanged.connect(
            lambda _index: self._sample_spin.setEnabled(self.scope() == "sample")
        )

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        start = QPushButton("Start")
        cancel.clicked.connect(self.reject)
        start.clicked.connect(self.accept)
        start.setDefault(True)
        row.addWidget(cancel)
        row.addWidget(start)
        layout.addLayout(row)

    def model(self) -> str:
        if self._operation == "luna":
            return LUNA_MODEL
        if self._operation == "sol":
            return SOL_MODEL
        return str(self._model_combo.currentData() or LUNA_MODEL)

    def evidence_mode(self) -> str:
        return str(self._evidence_combo.currentData() or ADAPTIVE_EVIDENCE)

    def scope(self) -> str:
        return str(self._scope_combo.currentData() or "all")

    def sample_size(self) -> int:
        return self._sample_spin.value()


class AIComparisonDialog(QDialog):
    def __init__(
        self,
        result: AIComparisonResult,
        targets: Dict[int, str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(result.title)
        self.resize(1260, 650)
        self._result = result
        self._targets = targets
        self.choice = ""

        rows = list(comparison_rows(result))
        agreements = sum(left.verdict == right.verdict for _, left, right in rows)
        left_flags = sum(left.requires_human_review for _, left, _right in rows)
        right_flags = sum(right.requires_human_review for _, _left, right in rows)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Compared {len(rows)} observation(s): {agreements} matching verdict(s), "
            f"{len(rows) - agreements} disagreement(s); {result.left.label} flagged "
            f"{left_flags}, {result.right.label} flagged {right_flags}. Review differences "
            "before choosing which findings become active."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            [
                "Observation",
                "Target",
                f"{result.left.label} verdict",
                f"{result.left.label} reason",
                f"{result.right.label} verdict",
                f"{result.right.label} reason",
                "Difference",
            ]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setRowCount(len(rows))
        for row, (observation_id, left, right) in enumerate(rows):
            different = left.verdict != right.verdict or left.reason_codes != right.reason_codes
            values = [
                str(observation_id),
                targets.get(observation_id, ""),
                f"{left.verdict.replace('_', ' ')} ({left.confidence})",
                left.explanation,
                f"{right.verdict.replace('_', ' ')} ({right.confidence})",
                right.explanation,
                "Different" if different else "Same",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if different:
                    item.setBackground(QColor("#5a3a12"))
                    item.setForeground(QColor("#ffe1a8"))
                if column == 0:
                    item.setToolTip("Double-click to open this observation.")
                elif column in {2, 3}:
                    item.setToolTip(
                        f"Signals: {', '.join(left.reason_labels()) or '(none)'}\n"
                        f"Model: {left.model}\nEvidence: {left.evidence_mode}"
                    )
                elif column in {4, 5}:
                    item.setToolTip(
                        f"Signals: {', '.join(right.reason_labels()) or '(none)'}\n"
                        f"Model: {right.model}\nEvidence: {right.evidence_mode}"
                    )
                self._table.setItem(row, column, item)
        self._table.itemDoubleClicked.connect(self._open_observation)
        self._table.resizeColumnsToContents()
        layout.addWidget(self._table, 1)

        row = QHBoxLayout()
        export = QPushButton("Export summary CSV…")
        export.clicked.connect(self._export_csv)
        left = QPushButton(f"Use {result.left.label}")
        right = QPushButton(f"Use {result.right.label}")
        union = QPushButton("Use conservative union")
        close = QPushButton("Close without applying")
        left.clicked.connect(lambda: self._choose("left"))
        right.clicked.connect(lambda: self._choose("right"))
        union.clicked.connect(lambda: self._choose("union"))
        close.clicked.connect(self.reject)
        row.addWidget(export)
        row.addStretch(1)
        row.addWidget(left)
        row.addWidget(right)
        row.addWidget(union)
        row.addWidget(close)
        layout.addLayout(row)

    def selected_findings(self) -> Dict[int, AIReviewFinding]:
        if self.choice == "left":
            return dict(self._result.left.findings)
        if self.choice == "right":
            return dict(self._result.right.findings)
        if self.choice == "union":
            return {
                observation_id: conservative_union(left, right)
                for observation_id, left, right in comparison_rows(self._result)
            }
        return {}

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def _open_observation(self, item: QTableWidgetItem) -> None:
        obs_item = self._table.item(item.row(), 0)
        if obs_item and obs_item.text().isdigit():
            open_external_url_silently(
                f"https://www.inaturalist.org/observations/{obs_item.text()}"
            )

    def _export_csv(self) -> None:
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export AI Comparison Summary",
            "ai-review-comparison.csv",
            "CSV files (*.csv)",
        )
        if not filename:
            return
        try:
            export_comparison_csv(Path(filename), self._result, self._targets)
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))


class AIReviewFindingsDialog(QDialog):
    def __init__(
        self,
        findings: Dict[int, AIReviewFinding],
        targets: Dict[int, str],
        *,
        title: str = "AI Review Findings",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1180, 620)

        rows = sorted(
            findings.items(),
            key=lambda pair: (finding_priority(pair[1]), pair[0]),
        )
        flagged = sum(finding.requires_human_review for _obs_id, finding in rows)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Codex flagged {flagged} of {len(rows)} observation(s) for human review. "
            "These are model-reported suspicions, not verified errors. Read the specific "
            "signal and explanation before deciding whether the flag is useful."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        table = QTableWidget(len(rows), 6)
        table.setHorizontalHeaderLabels(
            [
                "Observation",
                "Proposed name",
                "Verdict",
                "Confidence",
                "Suspicious signals",
                "Why Codex flagged it",
            ]
        )
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, (observation_id, finding) in enumerate(rows):
            values = [
                str(observation_id),
                targets.get(observation_id, ""),
                finding.verdict.replace("_", " ").title(),
                finding.confidence.title(),
                ", ".join(finding.reason_labels()) or "(none)",
                finding.explanation or "No explanation returned.",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(finding.details_text())
                if finding.requires_human_review:
                    item.setBackground(QColor("#5a2430"))
                    item.setForeground(QColor("#ffd5dc"))
                table.setItem(row, column, item)
        table.itemDoubleClicked.connect(
            lambda item: self._open_observation(table, item.row())
        )
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)

        note = QLabel(
            "Double-click any row to open the observation. Full model and evidence "
            "details are available by hovering over a cell."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def _open_observation(table: QTableWidget, row: int) -> None:
        item = table.item(row, 0)
        if item and item.text().isdigit():
            open_external_url_silently(
                f"https://www.inaturalist.org/observations/{item.text()}"
            )


class AIReviewPanel(QWidget):
    """Four shared AI actions wired to one preview dialog's candidate list."""

    def __init__(
        self,
        *,
        client: INatClient,
        disk_cache: ImageCache,
        inputs_provider: Callable[[], List[ObservationReviewInput]],
        selected_ids_provider: Callable[[], set[int]],
        apply_findings: Callable[[Dict[int, AIReviewFinding]], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._reviewer = CodexObservationReviewer(client, disk_cache)
        self._inputs_provider = inputs_provider
        self._selected_ids_provider = selected_ids_provider
        self._apply_findings = apply_findings
        self._worker: Optional[_ReviewWorker] = None
        self._progress: Optional[QProgressDialog] = None
        self._generation = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Optional AI consistency screen:")
        layout.addWidget(label)
        self._buttons: List[QPushButton] = []
        for text, operation in (
            ("Analyze with Luna…", "luna"),
            ("Analyze with Sol…", "sol"),
            ("Compare Luna vs Sol…", "compare_models"),
            ("Compare Text vs Full…", "compare_evidence"),
        ):
            button = QPushButton(text)
            button.clicked.connect(
                lambda _checked=False, selected=operation: self._configure(selected)
            )
            layout.addWidget(button)
            self._buttons.append(button)
        self._details_button = QPushButton("Review AI findings…")
        self._details_button.clicked.connect(self._show_findings)
        layout.addWidget(self._details_button)
        layout.addStretch(1)

    def refresh_enabled(self) -> None:
        inputs = self._inputs_provider()
        enabled = bool(inputs) and self._worker is None
        for button in self._buttons:
            button.setEnabled(enabled)
        self._details_button.setEnabled(
            self._worker is None
            and any(item.previous_finding is not None for item in inputs)
        )

    def cancel(self) -> None:
        self._generation += 1
        if self._worker is not None:
            self._worker.cancel()

    def _configure(self, operation: str) -> None:
        all_items = self._inputs_provider()
        selected_ids = self._selected_ids_provider()
        risky = [item for item in all_items if item.is_risky]
        dlg = AIReviewSetupDialog(
            operation,
            total_count=len(all_items),
            risky_count=len(risky),
            selected_count=len(selected_ids),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        items = self._items_for_scope(
            all_items,
            dlg.scope(),
            selected_ids,
            dlg.sample_size(),
        )
        if not items:
            QMessageBox.information(
                self,
                "Nothing to Analyze",
                "The selected scope contains no observations.",
            )
            return
        evidence = (
            "text plus up to six public observation photos"
            if dlg.evidence_mode() == FULL_EVIDENCE
            else (
                "text first, then photos only where the model requests them"
                if dlg.evidence_mode() == ADAPTIVE_EVIDENCE
                else "public text evidence without photos"
            )
        )
        if operation == "compare_evidence":
            evidence = "both text-only and photo-backed public evidence"
        if QMessageBox.question(
            self,
            "Send Evidence to OpenAI?",
            f"This will send {evidence} for {len(items)} observation(s) to OpenAI "
            "through your locally logged-in Codex CLI. Raw ITS, descriptions, IDs, "
            "and comments are included. Structured account-name fields are pseudonymized, "
            "but free text is sent as written and may contain usernames; exact coordinates "
            "are omitted. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start(
            operation,
            items,
            model=dlg.model(),
            evidence_mode=dlg.evidence_mode(),
        )

    @staticmethod
    def _items_for_scope(
        all_items: Sequence[ObservationReviewInput],
        scope: str,
        selected_ids: set[int],
        sample_size: int,
    ) -> List[ObservationReviewInput]:
        if scope == "risky":
            return [item for item in all_items if item.is_risky]
        if scope == "selected":
            return [item for item in all_items if item.observation_id in selected_ids]
        if scope == "sample":
            return stable_sample(all_items, sample_size)
        return list(all_items)

    def _start(
        self,
        operation: str,
        items: Sequence[ObservationReviewInput],
        *,
        model: str,
        evidence_mode: str,
    ) -> None:
        self._generation += 1
        generation = self._generation
        worker = _ReviewWorker(
            self._reviewer,
            operation,
            items,
            model=model,
            evidence_mode=evidence_mode,
        )
        self._worker = worker
        self.refresh_enabled()
        progress = QProgressDialog("Starting Codex…", "Cancel", 0, 0, self)
        progress.setWindowTitle("AI Observation Review")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(worker.cancel)
        self._progress = progress

        signals = worker.signals
        signals.progress.connect(
            lambda label, value, maximum, gen=generation: self._on_progress(
                gen, label, value, maximum
            )
        )
        signals.finished.connect(
            lambda result, gen=generation, selected=list(items), sigs=signals: self._on_finished(
                gen, result, selected, sigs
            )
        )
        signals.failed.connect(
            lambda message, gen=generation, sigs=signals: self._on_failed(
                gen, message, sigs
            )
        )
        signals.cancelled.connect(
            lambda gen=generation, sigs=signals: self._on_cancelled(gen, sigs)
        )
        progress.show()
        QThreadPool.globalInstance().start(worker)

    def _on_progress(
        self, generation: int, label: str, value: int, maximum: int
    ) -> None:
        if generation != self._generation or self._progress is None:
            return
        self._progress.setLabelText(label)
        self._progress.setMaximum(maximum)
        self._progress.setValue(value)

    def _on_finished(
        self,
        generation: int,
        result: object,
        items: Sequence[ObservationReviewInput],
        _signals: QObject,
    ) -> None:
        if generation != self._generation:
            return
        self._finish_progress()
        if isinstance(result, AIComparisonResult):
            targets = {item.observation_id: item.target_taxon_name for item in items}
            dlg = AIComparisonDialog(result, targets, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                findings = dlg.selected_findings()
                if findings:
                    self._apply_findings(findings)
        elif isinstance(result, AIReviewRunResult):
            findings = dict(result.findings)
            self._apply_findings(findings)
            targets = {item.observation_id: item.target_taxon_name for item in items}
            AIReviewFindingsDialog(
                findings,
                targets,
                title=f"{result.label} AI Review Findings",
                parent=self,
            ).exec()

    def _on_failed(
        self, generation: int, message: str, _signals: QObject
    ) -> None:
        if generation != self._generation:
            return
        self._finish_progress()
        QMessageBox.warning(self, "AI Analysis Failed", message)

    def _on_cancelled(self, generation: int, _signals: QObject) -> None:
        if generation != self._generation:
            return
        self._finish_progress()

    def _finish_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
        self._progress = None
        self._worker = None
        self.refresh_enabled()

    def _show_findings(self) -> None:
        inputs = self._inputs_provider()
        selected_ids = self._selected_ids_provider()
        analyzed = [item for item in inputs if item.previous_finding is not None]
        selected = [
            item for item in analyzed if item.observation_id in selected_ids
        ]
        shown = selected or analyzed
        findings = {
            item.observation_id: item.previous_finding
            for item in shown
            if item.previous_finding is not None
        }
        if not findings:
            return
        targets = {item.observation_id: item.target_taxon_name for item in shown}
        AIReviewFindingsDialog(findings, targets, parent=self).exec()
