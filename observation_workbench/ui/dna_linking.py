"""Qt setup, discovery progress, pair review, history, and write recovery UI."""

from __future__ import annotations

import json
import threading
from dataclasses import fields, replace
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import parse_observations_url
from observation_workbench.dna_linking.db import DNALinkingDB
from observation_workbench.dna_linking.discovery import (
    DNADiscovery,
    DiscoveryCancelled,
    DiscoveryDiagnostic,
    scan_fingerprint,
)
from observation_workbench.dna_linking.service import (
    DNALinkingService,
    LinkInspection,
    WriteResult,
)
from observation_workbench.dna_linking.types import (
    ALGORITHM_VERSION,
    DiscoveryProgress,
    ObservationSnapshot,
    ScanConfig,
)
from observation_workbench.storage.settings import AppSettings


class _ScanSignals(QObject):
    progress = Signal(object)
    ready = Signal(int, int, int)  # session, field, pair count
    failed = Signal(str)
    cancelled = Signal()


class _ScanWorker(QRunnable):
    def __init__(
        self, client: INatClient, db: DNALinkingDB, service: DNALinkingService,
        config: ScanConfig, cancel: threading.Event,
    ) -> None:
        super().__init__()
        self.client, self.db, self.service = client, db, service
        self.config, self.cancel = config, cancel
        self.signals = _ScanSignals()

    def run(self) -> None:
        try:
            start_calls = self.client.call_count
            estimated_calls = (
                2 + max(1, (self.config.chunk_size + 199) // 200)
                + self.config.chunk_size + (self.config.chunk_size + 29) // 30
                + self.config.chunk_size * 2
                + (1 if self.config.candidate_login else 0)
            )

            def emit_progress(value: DiscoveryProgress) -> None:
                made = max(0, self.client.call_count - start_calls)
                self.signals.progress.emit(replace(
                    value, calls_made=made,
                    calls_estimated=max(made, estimated_calls, value.calls_estimated),
                ))

            config = self.config
            if config.candidate_login:
                candidate_user = self.client.get_user(config.candidate_login)
                if candidate_user is None:
                    raise DiscoveryDiagnostic(
                        f"No iNaturalist account has the username "
                        f"{config.candidate_login!r}."
                    )
                config = replace(
                    config, candidate_login=str(candidate_user.get("login") or "")
                )
            user_id, login, _generation = self.service.validated_account()
            discovery = DNADiscovery(self.client, self.db)
            field_id, _datatype = discovery.resolve_field()
            endpoint = discovery.prove_source_endpoint(config, field_id)
            fingerprint, source_query = scan_fingerprint(user_id, config, field_id)
            session = self.db.get_or_create_session(
                user_id=user_id, login=login, fingerprint=fingerprint,
                source_query=source_query, radius_m=config.radius_m,
                window_seconds=round(config.window_minutes * 60),
                field_id=field_id, algorithm_version=ALGORITHM_VERSION,
            )
            count = discovery.discover_chunk(
                session_id=int(session["session_id"]), config=config,
                field_id=field_id, endpoint=endpoint,
                is_cancelled=self.cancel.is_set,
                progress=emit_progress,
            )
            self.signals.ready.emit(int(session["session_id"]), field_id, count)
        except DiscoveryCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.db.close_thread_connection()


class _LinkSignals(QObject):
    inspected = Signal(object)
    finished = Signal(object)
    failed = Signal(str)


class _LinkWorker(QRunnable):
    def __init__(
        self, service: DNALinkingService, mode: str, *, candidate_pk: int,
        source_id: int, destination_id: int, field_id: int,
        inspection: Optional[LinkInspection] = None, replace: bool = False,
        operation_id: int = 0, cancel: Optional[threading.Event] = None,
    ) -> None:
        super().__init__()
        self.service, self.mode = service, mode
        self.candidate_pk, self.source_id = candidate_pk, source_id
        self.destination_id, self.field_id = destination_id, field_id
        self.inspection, self.replace = inspection, replace
        self.operation_id = operation_id
        self.cancel = cancel or threading.Event()
        self.signals = _LinkSignals()

    def run(self) -> None:
        try:
            if self.mode == "inspect":
                self.signals.inspected.emit(
                    self.service.inspect_destination(
                        self.destination_id, self.source_id, self.field_id
                    )
                )
            elif self.mode == "verify":
                self.signals.finished.emit(
                    self.service.verify_operation(self.operation_id, self.field_id)
                )
            elif self.mode == "retry":
                self.signals.finished.emit(
                    self.service.retry_write_after_absence(
                        self.operation_id, self.field_id, confirmed=True
                    )
                )
            else:
                assert self.inspection is not None
                self.signals.finished.emit(self.service.apply_link(
                    candidate_pk=self.candidate_pk, source_id=self.source_id,
                    destination_id=self.destination_id, field_id=self.field_id,
                    inspection=self.inspection, replace=self.replace,
                    is_cancelled=self.cancel.is_set,
                ))
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.service.db.close_thread_connection()


class DNALinkingSetupDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Link observations to DNA barcodes")
        self.resize(760, 370)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Discovery is public and unauthenticated. The URL defines only the DNA-bearing "
            "source population; nearby candidate searches never inherit its filters. "
            "Source and candidate observations must belong to different observers. "
            "The username filter is optional; leave it blank to search candidates "
            "from all observers."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.url = QLineEdit(settings.dna_linking_url)
        self.url.setPlaceholderText("https://www.inaturalist.org/observations?taxon_id=47170…")
        form.addRow("Source observations URL:", self.url)
        self.candidate_login = QLineEdit(settings.dna_linking_candidate_login)
        self.candidate_login.setPlaceholderText("Blank searches all observers")
        self.candidate_login.setToolTip(
            "When set, candidates must belong to this observer. Same-observer "
            "source/candidate pairs are always excluded."
        )
        form.addRow("Candidate username (optional):", self.candidate_login)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(1, 10000)
        self.radius.setValue(settings.dna_linking_radius_m)
        self.radius.setSuffix(" m")
        form.addRow("Candidate radius:", self.radius)
        self.window = QDoubleSpinBox()
        self.window.setRange(0.1, 1440)
        self.window.setValue(settings.dna_linking_window_minutes)
        self.window.setSuffix(" minutes")
        form.addRow("Time window (each side):", self.window)
        self.chunk = QSpinBox()
        self.chunk.setRange(1, 2000)
        self.chunk.setValue(settings.dna_linking_chunk_size)
        form.addRow("Field-bearing source rows per chunk:", self.chunk)
        layout.addLayout(form)
        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        layout.addWidget(self.estimate)
        order = QLabel(
            "Sources are scanned by lowest observation ID first. This is ID order, "
            "not observation-date order. Invalid-DNA rows still count toward the chunk."
        )
        order.setWordWrap(True)
        layout.addWidget(order)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Discover next chunk")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.chunk.valueChanged.connect(self._update_estimate)
        self.candidate_login.textChanged.connect(self._update_estimate)
        self._update_estimate()

    def _update_estimate(self) -> None:
        count = self.chunk.value()
        source_pages = max(1, (count + 199) // 200)
        nearby = count
        hydration = (count + 29) // 30
        family_allowance = count * 2
        username_lookup = 1 if self.candidate_login.text().strip() else 0
        calls = 2 + source_pages + nearby + hydration + family_allowance + username_lookup
        self.estimate.setText(
            f"Initial estimate: about {calls} API calls / {calls // 60}m {calls % 60}s at "
            "one request per second. This includes filter proof, source pages, nearby "
            "searches, hydration, and anticipated family lookups; it updates during discovery."
        )

    def _accept(self) -> None:
        try:
            query = parse_observations_url(self.url.text())
            if query is None:
                raise ValueError("Enter an iNaturalist observations URL.")
        except Exception as exc:
            QMessageBox.warning(self, "Invalid source URL", str(exc))
            return
        self.settings.dna_linking_url = self.url.text()
        self.settings.dna_linking_candidate_login = self.candidate_login.text()
        self.settings.dna_linking_radius_m = self.radius.value()
        self.settings.dna_linking_window_minutes = self.window.value()
        self.settings.dna_linking_chunk_size = self.chunk.value()
        self.settings.sync()
        self.config = ScanConfig(
            source_url=query.display_url,
            source_params=tuple(
                param for param, source in zip(query.params, query.parameter_sources)
                if source != "Identify default"
            ), candidate_login=self.candidate_login.text().strip(),
            radius_m=self.radius.value(),
            window_minutes=self.window.value(), chunk_size=self.chunk.value(),
        )
        self.accept()


class DiscoveryProgressDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Discovering DNA-linked candidates")
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.label = QLabel("Resolving the DNA field and proving positive filtering…")
        self.label.setWordWrap(True)
        self.label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        policy = QSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
        )
        policy.setHeightForWidth(True)
        self.label.setSizePolicy(policy)
        # Reserve room for the multi-line progress text up front so the dialog
        # does not open too short and clip once counters start arriving.
        self.label.setMinimumHeight(4 * self.label.fontMetrics().lineSpacing())
        layout.addWidget(self.label, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)
        self.cancel_button = QPushButton("Cancel safely")
        layout.addWidget(self.cancel_button)

    def update_progress(self, progress: DiscoveryProgress) -> None:
        remaining = max(0, progress.calls_estimated - progress.calls_made)
        self.label.setText(
            f"{progress.message}\nAPI calls: {progress.calls_made} / ~{progress.calls_estimated} "
            f"(~{remaining}s remaining) · source rows {progress.source_rows} · "
            f"valid DNA sources {progress.valid_sources} · pairs {progress.pairs}"
        )
        self._grow_to_fit()

    def _grow_to_fit(self) -> None:
        """Extend the dialog downwards when wrapped text needs more room."""
        width = self.label.width()
        if width <= 0:
            return
        extra = self.label.heightForWidth(width) - self.label.height()
        if extra > 0:
            self.resize(self.width(), self.height() + extra)

    def closeEvent(self, event) -> None:
        self.cancel_button.click()
        event.ignore()


class _ObservationCard(QGroupBox):
    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(title, parent)
        layout = QVBoxLayout(self)
        self.photo = QLabel("No photo")
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setMinimumSize(320, 260)
        self.photo.setStyleSheet("background:#202020;color:#ddd")
        layout.addWidget(self.photo, 1)
        photo_row = QHBoxLayout()
        self.photo_choice = QComboBox()
        self.open_photo = QPushButton("Open photo")
        photo_row.addWidget(self.photo_choice, 1)
        photo_row.addWidget(self.open_photo)
        layout.addLayout(photo_row)
        self.info = QLabel()
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setOpenExternalLinks(True)
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self._manager = QNetworkAccessManager(self)
        self._reply: Optional[QNetworkReply] = None
        self._urls: tuple[str, ...] = ()
        self.photo_choice.currentIndexChanged.connect(self._load_photo)
        self.open_photo.clicked.connect(self._open_photo)

    def set_snapshot(self, snapshot: ObservationSnapshot) -> None:
        family = (
            f"{snapshot.family_name} (#{snapshot.family_id})"
            if snapshot.family_id is not None else "unknown"
        )
        accuracy = (
            f"{snapshot.positional_accuracy:g} m"
            if snapshot.positional_accuracy is not None else "unknown"
        )
        obs_url = f"https://www.inaturalist.org/observations/{snapshot.observation_id}"
        self.info.setText(
            f"<b>{snapshot.taxon_name}</b> ({snapshot.taxon_rank})<br>"
            f"Family: {family}<br>Observer: {snapshot.observer}<br>"
            f"Observed: {snapshot.observed_at}<br>Positional accuracy: {accuracy}<br>"
            f'<a href="{obs_url}">Observation #{snapshot.observation_id}</a>'
        )
        self._urls = snapshot.photo_urls
        self.photo_choice.blockSignals(True)
        self.photo_choice.clear()
        for index in range(len(self._urls)):
            self.photo_choice.addItem(f"Photo {index + 1} of {len(self._urls)}")
        self.photo_choice.blockSignals(False)
        self._load_photo(0)

    def _load_photo(self, index: int) -> None:
        if self._reply is not None:
            self._reply.abort()
            self._reply.deleteLater()
            self._reply = None
        if index < 0 or index >= len(self._urls):
            self.photo.setText("No photo")
            self.photo.setPixmap(QPixmap())
            return
        url = _photo_size(self._urls[index], "medium")
        self.photo.setText("Loading photo…")
        reply = self._manager.get(QNetworkRequest(QUrl(url)))
        self._reply = reply
        reply.finished.connect(lambda target=reply: self._photo_finished(target))

    def _photo_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        pixmap = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap.loadFromData(reply.readAll())
        reply.deleteLater()
        if pixmap.isNull():
            self.photo.setText("Photo preview unavailable — use Open photo")
            return
        self.photo.setText("")
        self.photo.setPixmap(pixmap.scaled(
            self.photo.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _open_photo(self) -> None:
        index = self.photo_choice.currentIndex()
        if 0 <= index < len(self._urls):
            QDesktopServices.openUrl(QUrl(_photo_size(self._urls[index], "original")))


class DNALinkingReviewDialog(QDialog):
    def __init__(
        self, db: DNALinkingDB, service: DNALinkingService,
        session_id: int, field_id: int, pool: QThreadPool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.db, self.service = db, service
        self.session_id, self.field_id, self.pool = session_id, field_id, pool
        self.rows = []
        self.index = 0
        self._deferred: set[int] = set()
        self._signals: set[QObject] = set()
        self._write_cancels: set[threading.Event] = set()
        self._closed = False
        self.setWindowTitle("Review DNA barcode observation matches")
        self.resize(1100, 760)
        layout = QVBoxLayout(self)
        self.position = QLabel()
        layout.addWidget(self.position)
        cards = QHBoxLayout()
        self.source_card = _ObservationCard("DNA source observation")
        self.candidate_card = _ObservationCard("Nearby candidate observation")
        cards.addWidget(self.source_card, 1)
        cards.addWidget(self.candidate_card, 1)
        layout.addLayout(cards, 1)
        self.score = QLabel()
        self.score.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.score)
        actions = QHBoxLayout()
        self.same = QPushButton("Same organism")
        self.not_same = QPushButton("Not the same")
        self.skip = QPushButton("Skip for now")
        self.history = QPushButton("History…")
        actions.addWidget(self.same)
        actions.addWidget(self.not_same)
        actions.addWidget(self.skip)
        actions.addStretch(1)
        actions.addWidget(self.history)
        layout.addLayout(actions)
        self.same.clicked.connect(self._same)
        self.not_same.clicked.connect(lambda: self._record("not_same"))
        self.skip.clicked.connect(lambda: self._record("skipped"))
        self.history.clicked.connect(self._show_history)
        self._reload()

    def _reload(self) -> None:
        self.rows = [
            row for row in self.db.queued_candidates(self.session_id)
            if int(row["candidate_pk"]) not in self._deferred
        ]
        self.index = min(self.index, max(0, len(self.rows) - 1))
        self._show_current()

    def _show_current(self) -> None:
        if not self.rows:
            self.position.setText("No queued pairs remain in this scan session.")
            self.same.setEnabled(False)
            self.not_same.setEnabled(False)
            self.skip.setEnabled(False)
            return
        row = self.rows[self.index]
        source = _snapshot_from_json(row["source_json"])
        candidate = _snapshot_from_json(row["candidate_json"])
        self.source_card.set_snapshot(source)
        self.candidate_card.set_snapshot(candidate)
        family = (
            "same family" if float(row["family_score"]) == 20.0
            else "different family" if source.family_id and candidate.family_id
            else "family unknown"
        )
        self.position.setText(f"Pair {self.index + 1} of {len(self.rows)}")
        self.score.setText(
            f"Score {row['score']}/100 · distance {row['distance_m']:.1f} m "
            f"(+{row['distance_score']:.1f}) · time {row['time_difference_seconds'] / 60:.1f} min "
            f"(+{row['time_score']:.1f}) · {family} (+{row['family_score']:.0f})"
        )
        self._set_busy(False)

    def _current_ids(self) -> tuple[int, int, int]:
        row = self.rows[self.index]
        return int(row["candidate_pk"]), int(row["source_id"]), int(row["candidate_id"])

    def _record(self, event: str) -> None:
        candidate_pk, _source_id, _destination_id = self._current_ids()
        self.db.append_review(candidate_pk, event)
        if event == "skipped":
            self._deferred.add(candidate_pk)
        self._reload()

    def _same(self) -> None:
        candidate_pk, source_id, destination_id = self._current_ids()
        self._set_busy(True)
        worker = _LinkWorker(
            self.service, "inspect", candidate_pk=candidate_pk,
            source_id=source_id, destination_id=destination_id, field_id=self.field_id,
        )
        signals = worker.signals
        self._signals.add(signals)
        signals.inspected.connect(
            lambda inspection, s=signals: self._inspection_ready(s, inspection)
        )
        signals.failed.connect(lambda message, s=signals: self._failed(s, message))
        self.pool.start(worker)

    def _inspection_ready(self, signals: QObject, inspection: LinkInspection) -> None:
        self._signals.discard(signals)
        if self._closed:
            return
        candidate_pk, _source_id, _destination_id = self._current_ids()
        self.db.append_review(candidate_pk, "same")
        if inspection.outcome == "blocked":
            self._set_busy(False)
            QMessageBox.warning(self, "Replacement blocked", inspection.diagnostic)
            self._reload()
            return
        replace = False
        if inspection.outcome == "conflict":
            replace = _choose_conflicting_value(self, inspection)
        candidate_pk, source_id, destination_id = self._current_ids()
        cancel = threading.Event()
        self._write_cancels.add(cancel)
        worker = _LinkWorker(
            self.service, "apply", candidate_pk=candidate_pk, source_id=source_id,
            destination_id=destination_id, field_id=self.field_id,
            inspection=inspection, replace=replace, cancel=cancel,
        )
        next_signals = worker.signals
        self._signals.add(next_signals)
        next_signals.finished.connect(
            lambda result, s=next_signals: self._write_finished(s, result)
        )
        next_signals.failed.connect(lambda message, s=next_signals: self._failed(s, message))
        self.pool.start(worker)

    def _write_finished(self, signals: QObject, result: WriteResult) -> None:
        self._signals.discard(signals)
        self._write_cancels.clear()
        if self._closed:
            return
        self._set_busy(False)
        icon = QMessageBox.Icon.Information if result.state == "confirmed" else QMessageBox.Icon.Warning
        QMessageBox(icon, "DNA link result", result.message, parent=self).exec()
        self._reload()

    def _failed(self, signals: QObject, message: str) -> None:
        self._signals.discard(signals)
        self._write_cancels.clear()
        if self._closed:
            return
        self._set_busy(False)
        QMessageBox.warning(self, "DNA linking", message)

    def _set_busy(self, busy: bool) -> None:
        self.same.setEnabled(not busy and bool(self.rows))
        self.not_same.setEnabled(not busy and bool(self.rows))
        self.skip.setEnabled(not busy and bool(self.rows))
        if busy:
            self.position.setText("Reading fresh destination state…")

    def _show_history(self) -> None:
        dialog = DNAHistoryDialog(self.db, self.session_id, self)
        dialog.exec()
        self._reload()

    def closeEvent(self, event) -> None:
        self._closed = True
        for cancel in self._write_cancels:
            cancel.set()
        super().closeEvent(event)


class DNAHistoryDialog(QDialog):
    def __init__(self, db: DNALinkingDB, session_id: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.db, self.session_id = db, session_id
        self.setWindowTitle("DNA linking review history")
        self.resize(850, 480)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Event", "Source", "Candidate", "Score", "Revision", "Decision", "Time"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        reopen = QPushButton("Reopen selected pair")
        restart = QPushButton("Restart source discovery")
        close = QPushButton("Close")
        row.addWidget(reopen)
        row.addWidget(restart)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        reopen.clicked.connect(self._reopen)
        restart.clicked.connect(self._restart)
        close.clicked.connect(self.accept)
        self._refresh()

    def _refresh(self) -> None:
        rows = self.db.history(self.session_id)
        self.table.setRowCount(len(rows))
        for row_index, record in enumerate(rows):
            values = [
                record["event_id"], record["source_id"], record["candidate_id"],
                record["score"], record["revision"], record["event_type"],
                record["created_at"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, int(record["candidate_pk"]))
                self.table.setItem(row_index, column, item)

    def _reopen(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        candidate_pk = int(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        self.db.reopen(candidate_pk)
        self._refresh()

    def _restart(self) -> None:
        if QMessageBox.question(
            self, "Restart discovery",
            "Reset the source ID cursor to the beginning? Prior decisions and events are preserved.",
        ) == QMessageBox.StandardButton.Yes:
            self.db.restart_discovery(self.session_id)
            QMessageBox.information(self, "Discovery reset", "The next launch will scan from the lowest source ID.")


class DNARecoveryDialog(QDialog):
    def __init__(
        self, db: DNALinkingDB, service: DNALinkingService,
        pool: QThreadPool, parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.db, self.service, self.pool = db, service, pool
        self._signals: set[QObject] = set()
        self.setWindowTitle("Recover uncertain DNA-link writes")
        self.resize(760, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Uncertain operations are never resent automatically and block new DNA-link writes "
            "to their destination. Verify the exact canonical state first."
        ))
        self.list = QListWidget()
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        verify = QPushButton("Verify Again")
        close = QPushButton("Close")
        buttons.addWidget(verify)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        verify.clicked.connect(self._verify)
        close.clicked.connect(self.accept)
        self._refresh()

    def _refresh(self) -> None:
        self.rows = self.db.uncertain_operations()
        self.list.clear()
        for row in self.rows:
            self.list.addItem(
                f"Operation #{row['operation_id']} · destination #{row['destination_id']} · "
                f"{row['operation_type']} · updated {row['updated_at']}"
            )

    def _verify(self) -> None:
        index = self.list.currentRow()
        if index < 0:
            return
        row = self.rows[index]
        worker = _LinkWorker(
            self.service, "verify", candidate_pk=int(row["candidate_pk"]),
            source_id=0, destination_id=int(row["destination_id"]),
            field_id=int(row["field_id"]), operation_id=int(row["operation_id"]),
        )
        signals = worker.signals
        self._signals.add(signals)
        signals.finished.connect(lambda result, s=signals: self._done(s, result))
        signals.failed.connect(lambda message, s=signals: self._failed(s, message))
        self.pool.start(worker)

    def _done(self, signals: QObject, result: WriteResult) -> None:
        self._signals.discard(signals)
        if result.expected_absent:
            box = QMessageBox(self)
            box.setWindowTitle("Verification result")
            box.setText(
                result.message
                + "\n\nThis verification proved the expected value absent. Retry is "
                  "still a new write and will repeat the full account/fresh-state preflight."
            )
            retry = box.addButton("Retry Write", QMessageBox.ButtonRole.DestructiveRole)
            leave = box.addButton("Leave uncertain", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(leave)
            box.exec()
            if box.clickedButton() is retry and QMessageBox.question(
                self, "Confirm Retry Write",
                "Send this DNA-link write again now? It will not be retried automatically after this attempt.",
            ) == QMessageBox.StandardButton.Yes:
                row = next(
                    item for item in self.rows
                    if int(item["operation_id"]) == result.operation_id
                )
                worker = _LinkWorker(
                    self.service, "retry", candidate_pk=int(row["candidate_pk"]),
                    source_id=0, destination_id=int(row["destination_id"]),
                    field_id=int(row["field_id"]), operation_id=result.operation_id,
                )
                next_signals = worker.signals
                self._signals.add(next_signals)
                next_signals.finished.connect(
                    lambda retry_result, s=next_signals: self._done(s, retry_result)
                )
                next_signals.failed.connect(
                    lambda message, s=next_signals: self._failed(s, message)
                )
                self.pool.start(worker)
                return
        else:
            QMessageBox.information(self, "Verification result", result.message)
        self._refresh()

    def _failed(self, signals: QObject, message: str) -> None:
        self._signals.discard(signals)
        QMessageBox.warning(self, "Verification failed", message)


class DNALinkingController(QObject):
    """Application-scoped owner for dialogs and background-worker signals."""

    def __init__(
        self, client: INatClient, settings: AppSettings,
        auth_provider: Callable[[], AuthState], auth_generation_provider: Callable[[], int],
        pool: QThreadPool, parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.client, self.settings, self.pool, self.parent_widget = client, settings, pool, parent
        self.db = DNALinkingDB()
        self.service = DNALinkingService(
            client, self.db, auth_provider, auth_generation_provider
        )
        self._signals: set[QObject] = set()
        self._progress: Optional[DiscoveryProgressDialog] = None
        self._cancel: Optional[threading.Event] = None
        self._reviews: set[QDialog] = set()

    def start(self) -> None:
        setup = DNALinkingSetupDialog(self.settings, self.parent_widget)
        if setup.exec() != QDialog.DialogCode.Accepted:
            return
        progress = DiscoveryProgressDialog(self.parent_widget)
        cancel = threading.Event()
        progress.cancel_button.clicked.connect(cancel.set)
        worker = _ScanWorker(self.client, self.db, self.service, setup.config, cancel)
        signals = worker.signals
        self._signals.add(signals)
        self._progress, self._cancel = progress, cancel
        signals.progress.connect(progress.update_progress)
        signals.ready.connect(lambda sid, fid, count, s=signals: self._ready(s, sid, fid, count))
        signals.failed.connect(lambda message, s=signals: self._failed(s, message))
        signals.cancelled.connect(lambda s=signals: self._cancelled(s))
        self.pool.start(worker)
        progress.show()

    def _ready(self, signals: QObject, session_id: int, field_id: int, count: int) -> None:
        self._finish_progress(signals)
        review = DNALinkingReviewDialog(
            self.db, self.service, session_id, field_id, self.pool, self.parent_widget
        )
        self._reviews.add(review)
        review.finished.connect(lambda _result, target=review: self._reviews.discard(target))
        if count == 0 and not self.db.queued_candidates(session_id):
            QMessageBox.information(
                self.parent_widget, "DNA discovery",
                "The chunk completed, but it produced no queued candidate pairs."
            )
            return
        review.show()

    def _failed(self, signals: QObject, message: str) -> None:
        self._finish_progress(signals)
        QMessageBox.warning(self.parent_widget, "DNA discovery blocked", message)

    def _cancelled(self, signals: QObject) -> None:
        self._finish_progress(signals)
        QMessageBox.information(
            self.parent_widget, "DNA discovery cancelled",
            "Cancellation was safe. Any partially paginated source was discarded and its cursor was not advanced."
        )

    def _finish_progress(self, signals: QObject) -> None:
        self._signals.discard(signals)
        if self._progress is not None:
            self._progress.accept()
        self._progress, self._cancel = None, None

    def shutdown(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        for dialog in list(self._reviews):
            dialog.close()

    def recover(self) -> None:
        if not self.db.uncertain_operations():
            QMessageBox.information(
                self.parent_widget, "DNA-link recovery", "There are no uncertain DNA-link writes."
            )
            return
        dialog = DNARecoveryDialog(
            self.db, self.service, self.pool, self.parent_widget
        )
        dialog.exec()


def _snapshot_from_json(value: str) -> ObservationSnapshot:
    raw = json.loads(value)
    raw["photo_urls"] = tuple(raw.get("photo_urls") or ())
    allowed = {item.name for item in fields(ObservationSnapshot)}
    return ObservationSnapshot(**{key: raw[key] for key in allowed})


def _choose_conflicting_value(parent: QWidget, inspection: LinkInspection) -> bool:
    """Show the complete current value without hiding it behind a details toggle."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Existing DNA field value")
    dialog.resize(680, 430)
    layout = QVBoxLayout(dialog)
    message = "The destination already has one conflicting DNA Barcode ITS value."
    if inspection.contains_dna:
        message += (
            "\n\nWARNING: the existing value contains a qualifying DNA sequence. "
            "Replacing it overwrites the complete sequence."
        )
    label = QLabel(message)
    label.setWordWrap(True)
    layout.addWidget(label)
    current = QTextEdit()
    current.setReadOnly(True)
    current.setPlainText(inspection.current_value)
    layout.addWidget(current, 1)
    buttons = QDialogButtonBox()
    keep = buttons.addButton("Keep existing", QDialogButtonBox.ButtonRole.RejectRole)
    replace_button = buttons.addButton(
        "Replace", QDialogButtonBox.ButtonRole.DestructiveRole
    )
    keep.clicked.connect(dialog.reject)
    replace_button.clicked.connect(dialog.accept)
    layout.addWidget(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted


def _photo_size(url: str, size: str) -> str:
    for token in ("square", "thumb", "small", "medium", "large", "original"):
        marker = f"/{token}."
        if marker in url:
            return url.replace(marker, f"/{size}.", 1)
    return url
