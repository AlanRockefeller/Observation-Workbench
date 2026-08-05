"""Dialogs for supervised bulk disagree-to-taxon workflow."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QEvent, QObject, QRunnable, QStringListModel, QThreadPool, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QKeyEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatAPIError, INatClient
from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    ObservationURLQuery,
    extract_optional_single_taxon_id_from_observation_query,
    extract_provisional_species_name_from_observation_query,
    parse_observations_url,
)
from observation_workbench.models import StudyTaxon
from observation_workbench.services.bulk_disagree import (
    DQA_DISABLED_MESSAGE,
    DQA_POSTING_ENABLED,
    BulkDisagreeCandidate,
    BulkDisagreePlanStats,
    BulkDisagreeResult,
    post_alternate_identification,
    resolve_taxon,
    taxon_is_strict_ancestor,
)
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.ui.external_links import open_external_url_silently
from observation_workbench.ui.table_sort import (
    SortableTableWidgetItem,
    enable_click_sorting,
    sorting_suspended,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RecentAlternateTaxon:
    taxon_id: int
    name: str
    rank: str
    body: str
    disagreement: bool


@dataclass(frozen=True)
class _PendingAlternatePost:
    post_taxon: _RecentAlternateTaxon
    remember_taxon: _RecentAlternateTaxon
    optimistic_hide: bool


class _ResolveSignals(QObject):
    resolved = Signal(object)
    error = Signal(str)


class _TaxonResolveWorker(QRunnable):
    def __init__(self, client: INatClient, taxon_id: int, generation: int, get_gen) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.taxon_id = taxon_id
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _ResolveSignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            taxon = resolve_taxon(self.client, self.taxon_id)
            if self.get_gen() == self.generation:
                self.signals.resolved.emit(taxon)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(str(exc))


class _AutocompleteSignals(QObject):
    results = Signal(list)
    error = Signal(str)


class _TaxonAutocompleteWorker(QRunnable):
    def __init__(self, client: INatClient, query: str, generation: int, get_gen) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.query = query
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _AutocompleteSignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            raw = self.client.get_taxa_autocomplete(self.query, per_page=10)
            items = []
            for result in raw.get("results") or []:
                sci = result.get("name") or ""
                common = result.get("preferred_common_name") or ""
                rank = result.get("rank") or ""
                rank_text = rank.replace("_", " ").title() if rank else "Unknown rank"
                if StudyTaxon.show_common_names and common and common != sci:
                    label = f"{common} ({sci}) — {rank_text}"
                else:
                    label = f"{sci} — {rank_text}"
                taxon_id = result.get("id")
                if sci and taxon_id:
                    items.append((sci, label, int(taxon_id), rank))
            if self.get_gen() == self.generation:
                self.signals.results.emit(items)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(str(exc))


class _GalleryImageSignals(QObject):
    loaded = Signal(int, int, object)
    failed = Signal(int, int, str)


class _GalleryImageWorker(QRunnable):
    def __init__(
        self,
        *,
        obs_id: int,
        photo_id: int,
        image_url: str,
        client: INatClient,
        disk_cache: ImageCache,
        size: str = "large",
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.obs_id = obs_id
        self.photo_id = photo_id
        self.image_url = image_url
        self.client = client
        self.disk_cache = disk_cache
        self.size = size
        self.signals = _GalleryImageSignals()

    def run(self) -> None:
        try:
            data = self.disk_cache.get(self.photo_id, self.size)
            if data is None:
                data = self.client.download_image(self.image_url)
                ext = _detect_image_ext(data)
                self.disk_cache.put(self.photo_id, self.size, data, ext)
            self.signals.loaded.emit(self.obs_id, self.photo_id, data)
        except Exception as exc:
            self.signals.failed.emit(self.obs_id, self.photo_id, str(exc))


def _photo_fetch_target(photo, preferred_size: str) -> Optional[Tuple[str, str]]:
    """Pick the ``(size, url)`` to fetch, labelled honestly for the disk cache.

    ``StudyPhoto.candidate_size_urls`` collapses to a single ``("square", url)``
    entry when the photo URL carries no substitutable size token (Flickr-hosted
    legacy photos), where every derived size is the SAME url. Deriving the url
    directly and captioning it ``"large"``/``"original"`` would cache thumbnail
    bytes under a larger size key in the shared :class:`ImageCache`, which the
    prefetcher then trusts as full quality and never upgrades. Returning the
    honest size keeps that cache entry truthful; ``None`` means the photo has no
    usable url at all.
    """
    candidates = photo.candidate_size_urls()
    if not candidates:
        return None
    for size, url in candidates:
        if size == preferred_size:
            return size, url
    return candidates[0]


class _AlternatePostSignals(QObject):
    finished = Signal(object, object)
    error = Signal(object, str)


class _AlternatePostWorker(QRunnable):
    def __init__(
        self,
        *,
        client: INatClient,
        api_token: str,
        login: str,
        candidate: BulkDisagreeCandidate,
        target_taxon_id: int,
        target_taxon_name: str,
        body: str,
        disagreement: bool,
        require_source_taxon_match: bool,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.api_token = api_token
        self.login = login
        self.candidate = candidate
        self.target_taxon_id = target_taxon_id
        self.target_taxon_name = target_taxon_name
        self.body = body
        self.disagreement = disagreement
        self.require_source_taxon_match = require_source_taxon_match
        self.signals = _AlternatePostSignals()

    def run(self) -> None:
        try:
            result = post_alternate_identification(
                self.client,
                self.api_token,
                self.login,
                self.candidate,
                target_taxon_id=self.target_taxon_id,
                target_taxon_name=self.target_taxon_name,
                body=self.body,
                disagreement=self.disagreement,
                require_source_taxon_match=self.require_source_taxon_match,
            )
            self.signals.finished.emit(self.candidate, result)
        except Exception as exc:
            message = str(exc)
            if isinstance(exc, INatAPIError) and exc.status_code is not None:
                message = f"HTTP status: {exc.status_code}\n{message}"
            self.signals.error.emit(self.candidate, message)


def _find_taxon_item(
    items: List[Tuple[str, str, int, str]],
    text: str,
) -> Optional[Tuple[str, str, int, str]]:
    needle = text.strip().casefold()
    for sci, label, taxon_id, rank in items:
        if needle in {sci.casefold(), label.casefold()}:
            return sci, label, taxon_id, rank
    return None


def _taxon_text_matches_selected(text: str, taxon_name: str, display_label: str) -> bool:
    needle = text.strip().casefold()
    return bool(needle and needle in {taxon_name.casefold(), display_label.casefold()})


def _format_taxon_with_rank(taxon_name: str, taxon_rank: str) -> str:
    rank = (taxon_rank or "").strip().replace("_", " ")
    return f"{taxon_name} ({rank})" if rank else taxon_name


class _CompleterPopupPositioner(QObject):
    """Keep a QCompleter popup pinned below its line edit so it never covers it.

    Qt's default placement flips the completion popup *above* the field when it
    thinks there is not enough room below, which can end up covering the text the
    user is typing. This event filter (installed on the popup view) repositions
    the popup directly beneath the edit whenever it is shown, moved, or resized,
    only placing it above when the field is genuinely too close to the bottom of
    the screen for the popup to fit — and even then it anchors the popup's bottom
    to the top of the edit so the field stays visible.
    """

    def __init__(self, edit: QLineEdit, completer: QCompleter) -> None:
        super().__init__(completer)
        self._edit = edit
        self._repositioning = False
        popup = completer.popup()
        if popup is not None:
            popup.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            not self._repositioning
            and event.type() in (QEvent.Type.Show, QEvent.Type.Move, QEvent.Type.Resize)
        ):
            self._reposition(obj)
        return False

    def _reposition(self, popup) -> None:
        edit = self._edit
        if edit is None or not edit.isVisible():
            return
        self._repositioning = True
        try:
            if popup.width() < edit.width():
                popup.setMinimumWidth(edit.width())
            top_left = edit.mapToGlobal(edit.rect().topLeft())
            bottom_left = edit.mapToGlobal(edit.rect().bottomLeft())
            height = popup.height()
            screen = edit.screen()
            avail = screen.availableGeometry() if screen is not None else None
            if (
                avail is not None
                and bottom_left.y() + height > avail.bottom()
                and top_left.y() - height >= avail.top()
            ):
                popup.move(top_left.x(), top_left.y() - height)
            else:
                popup.move(bottom_left.x(), bottom_left.y())
        finally:
            self._repositioning = False


def _show_completer_below(completer: QCompleter, edit: QLineEdit) -> None:
    popup = completer.popup()
    if popup is not None:
        popup.setMinimumWidth(edit.width())
    completer.complete(edit.rect())


_OBS_URL_ID_RE = re.compile(r"observations/(\d+)")


def parse_observation_id_tokens(text: str) -> Tuple[List[int], List[str]]:
    """Parse a free-form list of observation numbers.

    Tokens may be separated by spaces, commas, or new lines. Bare numbers and
    pasted ``/observations/<id>`` URLs are both accepted. Returns the unique
    observation IDs in entry order plus any tokens that could not be parsed.
    """
    ids: List[int] = []
    invalid: List[str] = []
    seen: set[int] = set()
    for token in re.split(r"[\s,]+", text.strip()):
        if not token:
            continue
        obs_id: Optional[int] = None
        if token.isdigit():
            obs_id = int(token)
        else:
            match = _OBS_URL_ID_RE.search(token)
            if match:
                obs_id = int(match.group(1))
        if obs_id and obs_id > 0:
            if obs_id not in seen:
                seen.add(obs_id)
                ids.append(obs_id)
        else:
            invalid.append(token)
    return ids, invalid


class _TargetTaxonAutocomplete:
    """Debounced, generation-guarded taxon autocomplete shared by setup dialogs.

    A hosting dialog must, in ``__init__``, create ``self._target_edit`` (a
    ``QLineEdit``), ``self._target_model`` (a ``QStringListModel``),
    ``self._target_completer`` and ``self._target_timer`` (wired to
    ``_fetch_target_autocomplete``), and provide ``self._client``,
    ``self._pool`` and ``self._live_signals``. It should also initialise
    ``self._target_gen``/``_target_items``/``_target_taxon_id``/
    ``_target_taxon_name``/``_target_display_label``. Subclasses override
    :meth:`_after_target_changed` to refresh their own validation/UI and may
    set :attr:`_TARGET_DEBOUNCE_MS`.
    """

    _TARGET_DEBOUNCE_MS = 350

    # Attributes the hosting dialog must create in __init__ (declared here for
    # type-checkers; not assigned, so they never shadow the instance values).
    _client: INatClient
    _pool: QThreadPool
    _live_signals: set
    _target_edit: QLineEdit
    _target_model: QStringListModel
    _target_completer: QCompleter
    _target_timer: QTimer
    _target_gen: int
    _target_items: List[Tuple[str, str, int, str]]
    _target_taxon_id: Optional[int]
    _target_taxon_name: str
    _target_taxon_rank: str
    _target_display_label: str

    def __init__(self, *args, **kwargs) -> None:
        # Cooperative init so hosting dialogs (mixin first in the MRO) still
        # reach QDialog.__init__ via super().
        super().__init__(*args, **kwargs)

    def _pin_target_completer_popup(self) -> None:
        """Pin the autocomplete popup below the taxon edit so it never covers it."""
        self._target_popup_positioner = _CompleterPopupPositioner(
            self._target_edit, self._target_completer
        )

    def _after_target_changed(self) -> None:
        """Hook: refresh dialog-specific UI after the target selection changes."""

    def _on_target_text_changed(self, text: str) -> None:
        if (
            self._target_taxon_id
            and _taxon_text_matches_selected(text, self._target_taxon_name, self._target_display_label)
        ):
            self._after_target_changed()
            return
        self._target_taxon_id = None
        self._target_taxon_name = ""
        self._target_taxon_rank = ""
        self._target_display_label = ""
        self._target_edit.setToolTip("Select a taxon from autocomplete.")
        if len(text.strip()) >= 2:
            self._target_timer.start(self._TARGET_DEBOUNCE_MS)
        else:
            self._target_timer.stop()
        self._after_target_changed()

    def _fetch_target_autocomplete(self) -> None:
        query = self._target_edit.text().strip()
        if not query:
            return
        self._target_gen += 1
        gen = self._target_gen
        worker = _TaxonAutocompleteWorker(
            self._client,
            query,
            gen,
            lambda: self._target_gen,
        )
        sigs = worker.signals
        self._live_signals.add(sigs)
        sigs.results.connect(
            lambda items, s=sigs: (
                self._live_signals.discard(s),
                self._on_target_results(items),
            )
        )
        sigs.error.connect(lambda _msg, s=sigs: self._live_signals.discard(s))
        self._pool.start(worker)

    @Slot(list)
    def _on_target_results(self, items: List[Tuple[str, str, int, str]]) -> None:
        self._target_items = items
        self._target_model.setStringList([label for _sci, label, _tid, _rank in items])
        match = _find_taxon_item(items, self._target_edit.text())
        if match is not None:
            sci, label, taxon_id, rank = match
            self._set_target_taxon(sci, taxon_id, label, rank)
            return
        if items and self._target_edit.hasFocus():
            _show_completer_below(self._target_completer, self._target_edit)

    @Slot(str)
    def _on_target_selected(self, text: str) -> None:
        match = _find_taxon_item(self._target_items, text)
        if match is not None:
            sci, label, taxon_id, rank = match
            self._set_target_taxon(sci, taxon_id, label, rank)
            QTimer.singleShot(0, self._restore_selected_target_text)
        self._after_target_changed()

    def _set_target_taxon(
        self,
        taxon_name: str,
        taxon_id: int,
        display_label: Optional[str] = None,
        rank: str = "",
    ) -> None:
        self._target_taxon_id = int(taxon_id)
        self._target_taxon_name = taxon_name
        self._target_taxon_rank = rank or ""
        self._target_display_label = display_label or _format_taxon_with_rank(
            taxon_name, self._target_taxon_rank
        )
        self._target_edit.blockSignals(True)
        self._target_edit.setText(taxon_name)
        self._target_edit.blockSignals(False)
        self._target_edit.setToolTip(f"Taxon: {taxon_name}  (id={taxon_id})")
        self._after_target_changed()

    def _restore_selected_target_text(self) -> None:
        if not self._target_taxon_id or not self._target_taxon_name:
            return
        self._target_edit.blockSignals(True)
        self._target_edit.setText(self._target_taxon_name)
        self._target_edit.blockSignals(False)


class AlternateIdentificationDialog(_TargetTaxonAutocomplete, QDialog):
    def __init__(
        self,
        client: INatClient,
        candidate: BulkDisagreeCandidate,
        *,
        default_taxon_id: Optional[int] = None,
        default_taxon_name: str = "",
        default_taxon_rank: str = "",
        default_comment: str = "",
        recent_alternate_taxa: Optional[List[_RecentAlternateTaxon]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Post Alternate Identification")
        self.resize(760, 390)
        self._client = client
        self._candidate = candidate
        self._pool = QThreadPool.globalInstance()
        self._live_signals: set[QObject] = set()
        self._recent_alternate_taxa = list(recent_alternate_taxa or [])
        self._target_items: List[Tuple[str, str, int, str]] = []
        self._target_taxon_id: Optional[int] = None
        self._target_taxon_name = ""
        self._target_taxon_rank = ""
        self._target_display_label = ""
        self._target_gen = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            f"Observation {candidate.observation.obs_id} will be removed from the planned "
            "bulk disagreement run if this alternate ID posts successfully."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        body_row = QHBoxLayout()
        body_row.setSpacing(12)

        grid = QGridLayout()
        grid.addWidget(QLabel("Alternate taxon:"), 0, 0)
        self._target_edit = QLineEdit()
        self._target_edit.setPlaceholderText("Start typing a taxon name, then select from autocomplete")
        self._target_model = QStringListModel(self)
        self._target_completer = QCompleter(self._target_model, self)
        self._target_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._target_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._target_completer.setMaxVisibleItems(12)
        self._target_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._target_edit.setCompleter(self._target_completer)
        self._target_timer = QTimer(self)
        self._target_timer.setSingleShot(True)
        self._target_timer.timeout.connect(self._fetch_target_autocomplete)
        self._target_edit.textEdited.connect(self._on_target_text_changed)
        self._target_completer.activated[str].connect(self._on_target_selected)
        self._pin_target_completer_popup()
        grid.addWidget(self._target_edit, 0, 1)

        self._target_summary_label = QLabel("")
        self._target_summary_label.setWordWrap(True)
        grid.addWidget(self._target_summary_label, 1, 1)

        grid.addWidget(QLabel("Comment:"), 2, 0)
        self._comment_edit = QTextEdit()
        self._comment_edit.setAcceptRichText(False)
        self._comment_edit.setPlainText(default_comment)
        self._comment_edit.setFixedHeight(110)
        self._comment_edit.textChanged.connect(self._update_post_enabled)
        grid.addWidget(self._comment_edit, 2, 1)
        body_row.addLayout(grid, 1)

        recent_panel = self._make_recent_alternate_panel()
        if recent_panel is not None:
            body_row.addWidget(recent_panel)
        layout.addLayout(body_row)

        self._disagreement_cb = QCheckBox("Explicitly disagree with the current/source taxon")
        self._disagreement_cb.setToolTip(
            "When checked, the alternate ID is posted with iNaturalist's disagreement flag."
        )
        layout.addWidget(self._disagreement_cb)

        self._validation_label = QLabel("Select a real taxon from autocomplete.")
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet("QLabel { color: #b00020; }")
        layout.addWidget(self._validation_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._post_btn = QPushButton("Post alternate ID")
        self._post_btn.setEnabled(False)
        buttons.addButton(self._post_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._post_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)
        if default_taxon_id and default_taxon_name:
            self._set_target_taxon(
                default_taxon_name,
                int(default_taxon_id),
                rank=default_taxon_rank,
            )

    @property
    def target_taxon_id(self) -> int:
        assert self._target_taxon_id is not None
        return self._target_taxon_id

    @property
    def target_taxon_name(self) -> str:
        return self._target_taxon_name

    @property
    def target_taxon_rank(self) -> str:
        return self._target_taxon_rank

    def comment(self) -> str:
        return self._comment_edit.toPlainText().strip()

    def disagreement(self) -> bool:
        return self._disagreement_cb.isChecked()

    _TARGET_DEBOUNCE_MS = 250

    def _after_target_changed(self) -> None:
        self._update_target_summary()
        self._update_post_enabled()

    def _make_recent_alternate_panel(self) -> Optional[QWidget]:
        if not self._recent_alternate_taxa:
            return None
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(8, 8, 8, 8)
        panel_layout.setSpacing(6)
        header = QLabel("Recent alternate IDs")
        header.setWordWrap(True)
        panel_layout.addWidget(header)
        for recent in self._recent_alternate_taxa:
            button = QPushButton(f"ID as {recent.name}")
            tooltip = (
                "Fill this dialog with the previous alternate ID taxon, "
                "comment, and disagreement setting."
            )
            if recent.rank:
                tooltip += f"\nTaxon rank: {recent.rank}"
            tooltip += f"\nTaxon id: {recent.taxon_id}"
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda _checked=False, r=recent: self._apply_recent_alternate(r)
            )
            panel_layout.addWidget(button)
        panel_layout.addStretch(1)
        return panel

    def _apply_recent_alternate(self, recent: _RecentAlternateTaxon) -> None:
        self._set_target_taxon(recent.name, recent.taxon_id, rank=recent.rank)
        self._comment_edit.setPlainText(recent.body)
        self._disagreement_cb.setChecked(recent.disagreement)

    def _update_target_summary(self) -> None:
        if not self._target_taxon_id or not self._target_taxon_name:
            self._target_summary_label.setText("")
            return
        self._target_summary_label.setText(
            "Selected alternate taxon: "
            f"{_format_taxon_with_rank(self._target_taxon_name, self._target_taxon_rank)} "
            f"(id={self._target_taxon_id})"
        )

    def _update_post_enabled(self) -> None:
        ready = bool(self._target_taxon_id and self._target_taxon_name)
        if ready:
            self._validation_label.setText("")
        else:
            self._validation_label.setText("Select a real taxon from autocomplete.")
        self._post_btn.setEnabled(ready)


class BulkDisagreeSetupDialog(_TargetTaxonAutocomplete, QDialog):
    def __init__(
        self,
        client: INatClient,
        *,
        prefill_url: str = "",
        defaults: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bulk Disagree to Taxon from URL")
        self.resize(780, 680)
        self._client = client
        self._pool = QThreadPool.globalInstance()
        self._live_signals: set = set()
        self._source_gen = 0
        self._target_gen = 0
        self._source_query: Optional[ObservationURLQuery] = None
        self._source_taxon: Optional[StudyTaxon] = None
        self._source_taxon_id: Optional[int] = None
        # True only when the URL carries a numeric taxon_id. When False the
        # workflow runs without a source-taxon safety check, unless the URL
        # instead carries a Provisional Species Name field filter (below).
        self._url_has_taxon_id = False
        # Set when the URL has no taxon_id but filters by a Provisional Species
        # Name field value; that value then acts as the source identity.
        self._source_provisional_name = ""
        self._target_taxon_id: Optional[int] = None
        self._target_taxon_name = ""
        self._target_taxon_rank = ""
        self._target_display_label = ""
        self._target_items: List[Tuple[str, str, int, str]] = []
        # Source identity (provisional name or "taxon:<id>") already defaulted
        # into the target field, so re-validating the URL does not re-prefill.
        self._prefilled_target_source: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Paste an iNaturalist observations URL. If it has one numeric taxon_id, "
            "that taxon becomes the source/current taxon safety constraint. A URL "
            "filtered by a Provisional Species Name field (field:Provisional Species "
            "Name=…) uses that provisional name as the source. A URL with no taxon_id "
            "and no provisional name filter is also allowed; identifications are then "
            "posted to every matching observation."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        layout.addLayout(grid)

        grid.addWidget(QLabel("iNaturalist observations URL:"), 0, 0)
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://www.inaturalist.org/observations?...&taxon_id=123")
        grid.addWidget(self._url_edit, 0, 1)

        grid.addWidget(QLabel("Resolved Source Taxon:"), 1, 0)
        source_row = QWidget()
        source_row_layout = QHBoxLayout(source_row)
        source_row_layout.setContentsMargins(0, 0, 0, 0)
        source_row_layout.setSpacing(6)
        self._copy_source_btn = QPushButton("Copy")
        self._copy_source_btn.setFixedWidth(64)
        self._copy_source_btn.setToolTip(
            "Copy the resolved source taxon into the target taxon field."
        )
        self._copy_source_btn.clicked.connect(self._copy_source_to_target)
        source_row_layout.addWidget(self._copy_source_btn)
        self._source_label = QLabel("Enter an observations URL with taxon_id.")
        self._source_label.setWordWrap(True)
        source_row_layout.addWidget(self._source_label, 1)
        grid.addWidget(source_row, 1, 1)

        grid.addWidget(QLabel("Target taxon to add:"), 2, 0)
        self._target_edit = QLineEdit()
        self._target_edit.setPlaceholderText("Type and select a taxon, e.g. Sarcosphaera")
        self._target_model = QStringListModel(self)
        completer = QCompleter(self._target_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        completer.activated[str].connect(self._on_target_selected)
        self._target_edit.setCompleter(completer)
        self._target_completer = completer
        self._target_edit.textEdited.connect(self._on_target_text_changed)
        self._pin_target_completer_popup()
        grid.addWidget(self._target_edit, 2, 1)

        self._relationship_label = QLabel("Select a target taxon to check its relationship to the source.")
        self._relationship_label.setWordWrap(True)
        layout.addWidget(self._relationship_label)

        comment_label = QLabel("Identification comment:")
        layout.addWidget(comment_label)
        self._comment_edit = QTextEdit()
        self._comment_edit.setAcceptRichText(False)
        self._comment_edit.setFixedHeight(90)
        self._comment_edit.textChanged.connect(self._update_plan_enabled)
        layout.addWidget(self._comment_edit)

        self._skip_dna_cb = QCheckBox("Skip observations with DNA Barcode ITS observation field")
        self._skip_dna_cb.setChecked(True)
        self._skip_dna_cb.toggled.connect(self._on_skip_dna_toggled)
        layout.addWidget(self._skip_dna_cb)

        self._only_dna_cb = QCheckBox(
            "Only process observations with a DNA Barcode ITS observation field"
        )
        self._only_dna_cb.setChecked(False)
        self._only_dna_cb.toggled.connect(self._on_only_dna_toggled)
        layout.addWidget(self._only_dna_cb)

        self._dqa_cb = QCheckBox("Also vote “ID is already as good as it can be” in the Data Quality Assessment")
        self._dqa_cb.setChecked(False)
        if not DQA_POSTING_ENABLED:
            self._dqa_cb.setEnabled(False)
            self._dqa_cb.setToolTip(
                "DQA voting is visible for future support, but currently disabled because "
                "the endpoint and payload have not been verified."
            )
        else:
            self._dqa_cb.setToolTip(
                "The DQA vote is posted only after the refreshed community taxon, "
                "or current taxon when no community taxon exists, matches the target taxon."
            )
        layout.addWidget(self._dqa_cb)
        dqa_warning = QLabel(
            "This can affect the observation’s quality grade. Use only when the community taxon truly "
            "cannot be improved from the available evidence."
        )
        dqa_warning.setWordWrap(True)
        layout.addWidget(dqa_warning)
        if not DQA_POSTING_ENABLED:
            dqa_disabled = QLabel(DQA_DISABLED_MESSAGE)
            dqa_disabled.setWordWrap(True)
            dqa_disabled.setStyleSheet("QLabel { color: #b36b00; }")
            layout.addWidget(dqa_disabled)
            dqa_future_note = QLabel(
                "DQA voting is visible for future support, but currently disabled because "
                "the endpoint and payload have not been verified."
            )
            dqa_future_note.setWordWrap(True)
            layout.addWidget(dqa_future_note)
        else:
            dqa_guard = QLabel(
                "When enabled, the DQA vote is posted only after the identification succeeds "
                "and the refreshed community taxon, or current taxon when no community taxon "
                "exists, matches the target taxon."
            )
            dqa_guard.setWordWrap(True)
            layout.addWidget(dqa_guard)

        self._require_source_cb = QCheckBox(
            "Require current/community taxon to still match URL taxon before posting"
        )
        self._require_source_cb.setChecked(True)
        layout.addWidget(self._require_source_cb)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Maximum observations to process:"))
        self._max_spin = QSpinBox()
        self._max_spin.setRange(1, 999_999)
        self._max_spin.setValue(100)
        options_row.addWidget(self._max_spin)
        options_row.addSpacing(16)
        options_row.addWidget(QLabel("Delay:"))
        self._delay_min_spin = QSpinBox()
        self._delay_min_spin.setRange(0, 3600)
        self._delay_min_spin.setSuffix("s min")
        self._delay_min_spin.setValue(10)
        self._delay_max_spin = QSpinBox()
        self._delay_max_spin.setRange(0, 3600)
        self._delay_max_spin.setSuffix("s max")
        self._delay_max_spin.setValue(30)
        self._delay_min_spin.valueChanged.connect(self._on_delay_min_changed)
        self._delay_max_spin.valueChanged.connect(self._on_delay_max_changed)
        options_row.addWidget(self._delay_min_spin)
        options_row.addWidget(self._delay_max_spin)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        self._dry_run_cb = QCheckBox("Preview only / dry run")
        self._dry_run_cb.setChecked(False)
        layout.addWidget(self._dry_run_cb)

        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet("QLabel { color: #b00020; }")
        layout.addWidget(self._validation_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._plan_btn = QPushButton("Plan")
        self._plan_btn.setEnabled(False)
        buttons.addButton(self._plan_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._plan_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._url_timer = QTimer(self)
        self._url_timer.setSingleShot(True)
        self._url_timer.timeout.connect(self._validate_url)
        self._target_timer = QTimer(self)
        self._target_timer.setSingleShot(True)
        self._target_timer.timeout.connect(self._fetch_target_autocomplete)
        self._url_edit.textChanged.connect(lambda _text: self._url_timer.start(350))

        self._apply_defaults(defaults or {}, prefill_url)

    @property
    def observation_query(self) -> ObservationURLQuery:
        assert self._source_query is not None
        return self._source_query

    @property
    def source_taxon(self) -> Optional[StudyTaxon]:
        """Resolved source taxon, or None when the URL has no taxon_id."""
        return self._source_taxon

    def has_source_taxon(self) -> bool:
        return self._source_taxon is not None

    def source_provisional_name(self) -> str:
        """Provisional Species Name from the URL, or "" when none is present."""
        return self._source_provisional_name

    @property
    def target_taxon_id(self) -> int:
        assert self._target_taxon_id is not None
        return self._target_taxon_id

    @property
    def target_taxon_name(self) -> str:
        return self._target_taxon_name

    @property
    def target_taxon_rank(self) -> str:
        return self._target_taxon_rank

    def source_url(self) -> str:
        return self._url_edit.text().strip()

    def comment(self) -> str:
        return self._comment_edit.toPlainText().strip()

    def skip_with_dna_barcode_its(self) -> bool:
        return self._skip_dna_cb.isChecked()

    def only_with_dna_barcode_its(self) -> bool:
        return self._only_dna_cb.isChecked()

    def dqa_vote_requested(self) -> bool:
        return self._dqa_cb.isChecked()

    def dqa_vote_planned(self) -> bool:
        return self._dqa_cb.isChecked() and not self.dry_run()

    def require_source_taxon_match(self) -> bool:
        return self._require_source_cb.isChecked()

    def max_observations(self) -> int:
        return self._max_spin.value()

    def delay_min_seconds(self) -> int:
        return self._delay_min_spin.value()

    def delay_max_seconds(self) -> int:
        return self._delay_max_spin.value()

    def dry_run(self) -> bool:
        return self._dry_run_cb.isChecked()

    def _apply_defaults(self, defaults: dict, prefill_url: str) -> None:
        url = prefill_url.strip() or str(defaults.get("url") or "").strip()
        self._comment_edit.setPlainText(str(defaults.get("comment") or ""))
        only_with_dna = _bool_default(defaults.get("only_with_dna_barcode_its"), False)
        skip_with_dna = _bool_default(defaults.get("skip_with_dna_barcode_its"), True)
        if only_with_dna:
            skip_with_dna = False
        self._skip_dna_cb.setChecked(skip_with_dna)
        self._only_dna_cb.setChecked(only_with_dna)
        self._dqa_cb.setChecked(
            DQA_POSTING_ENABLED
            and _bool_default(defaults.get("dqa_vote_requested"), False)
        )
        self._require_source_cb.setChecked(
            _bool_default(defaults.get("require_source_taxon_match"), True)
        )
        self._max_spin.setValue(_int_default(defaults.get("max_observations"), 100))
        delay_min = max(0, _int_default(defaults.get("delay_min_seconds"), 10))
        delay_max = max(delay_min, _int_default(defaults.get("delay_max_seconds"), 30))
        self._delay_min_spin.setValue(delay_min)
        self._delay_max_spin.setValue(delay_max)
        self._dry_run_cb.setChecked(_bool_default(defaults.get("dry_run"), False))

        target_id = _int_or_none(defaults.get("target_taxon_id"))
        target_name = str(defaults.get("target_taxon_name") or "").strip()
        if target_id and target_name:
            target_rank = str(defaults.get("target_taxon_rank") or "").strip()
            self._set_target_taxon(target_name, target_id, rank=target_rank)

        if url:
            self._url_edit.setText(url)
            QTimer.singleShot(0, self._validate_url)
        else:
            self._update_plan_enabled()

    def _validate_url(self) -> None:
        self._source_gen += 1
        self._source_query = None
        self._source_taxon = None
        self._source_taxon_id = None
        self._url_has_taxon_id = False
        self._source_provisional_name = ""
        self._source_label.setText("No source taxon resolved.")
        self._update_source_copy_enabled()
        text = self._url_edit.text().strip()
        if not text:
            self._set_validation("Paste an iNaturalist observations URL.")
            self._update_plan_enabled()
            return
        try:
            query = parse_observations_url(text)
            if query is None:
                self._set_validation("Paste an iNaturalist observations URL.")
                self._update_plan_enabled()
                return
            source_taxon_id = extract_optional_single_taxon_id_from_observation_query(query)
        except (ObservationURLParseError, ValueError) as exc:
            self._set_validation(str(exc))
            self._update_relationship()
            self._update_plan_enabled()
            return

        self._source_query = query

        if source_taxon_id is None:
            self._url_has_taxon_id = False
            self._source_taxon_id = None
            self._source_taxon = None
            provisional = extract_provisional_species_name_from_observation_query(query)
            self._source_provisional_name = provisional or ""
            if provisional:
                # The Provisional Species Name field value is the source identity.
                self._source_label.setText(
                    f"Source provisional name from URL: {provisional}"
                )
                self._update_source_copy_enabled()
                self._maybe_prefill_target_from_source(provisional, provisional)
            else:
                # No taxon_id in the URL: run without a source-taxon safety check.
                self._source_label.setText(
                    "No taxon_id in URL — identifications will be posted to every matching "
                    "observation, without a source-taxon safety check."
                )
            self._set_validation("")
            self._update_relationship()
            self._update_plan_enabled()
            return

        self._url_has_taxon_id = True
        self._source_taxon_id = source_taxon_id
        self._source_label.setText(f"Resolving source taxon_id {source_taxon_id}…")
        self._set_validation("")
        gen = self._source_gen
        worker = _TaxonResolveWorker(
            self._client,
            source_taxon_id,
            gen,
            lambda: self._source_gen,
        )
        sigs = worker.signals
        self._live_signals.add(sigs)
        sigs.resolved.connect(
            lambda taxon, s=sigs: (
                self._live_signals.discard(s),
                self._on_source_resolved(taxon),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_signals.discard(s),
                self._on_source_error(msg),
            )
        )
        self._pool.start(worker)
        self._update_plan_enabled()

    @Slot(object)
    def _on_source_resolved(self, taxon: StudyTaxon) -> None:
        self._source_taxon = taxon
        self._source_label.setText(f"Source taxon from URL: {taxon.name} ({taxon.taxon_id})")
        self._update_source_copy_enabled()
        self._maybe_prefill_target_from_source(
            f"taxon:{taxon.taxon_id}", taxon.name, taxon_id=taxon.taxon_id
        )
        self._update_relationship()
        self._update_plan_enabled()

    def _maybe_prefill_target_from_source(
        self,
        source_key: str,
        text: str,
        *,
        taxon_id: Optional[int] = None,
    ) -> None:
        """Default the target taxon field to the source identity from the URL.

        A resolved source taxon is selected directly; a provisional name (not yet
        a real taxon) is dropped into the search box so autocomplete can resolve
        it. Only an empty target is filled, so a saved default or a choice the
        user has already made or typed is never clobbered, and each source value
        is applied at most once while the URL is still being typed.
        """
        if not text.strip():
            return
        if self._prefilled_target_source == source_key:
            return
        if self._target_taxon_id is not None or self._target_edit.text().strip():
            return
        self._prefilled_target_source = source_key
        if taxon_id:
            self._set_target_taxon(text, int(taxon_id), rank=self._source_taxon.rank if self._source_taxon else "")
            return
        self._target_taxon_id = None
        self._target_taxon_name = ""
        self._target_taxon_rank = ""
        self._target_display_label = ""
        self._target_edit.setText(text)
        self._fetch_target_autocomplete()
        self._update_relationship()
        self._update_plan_enabled()

    def _on_source_error(self, msg: str) -> None:
        self._source_taxon = None
        self._source_label.setText("Could not resolve source taxon from URL.")
        self._update_source_copy_enabled()
        self._set_validation(f"Could not resolve source taxon from URL: {msg}")
        self._update_relationship()
        self._update_plan_enabled()

    def _copy_source_to_target(self, _checked: bool = False) -> None:
        source = self._source_copy_payload()
        if source is None:
            self._set_validation("Resolve a source taxon or source provisional name before copying it.")
            self._url_edit.setFocus()
            return
        name, taxon_id, rank = source
        if taxon_id:
            self._set_target_taxon(name, taxon_id, rank=rank)
        else:
            self._target_taxon_id = None
            self._target_taxon_name = ""
            self._target_taxon_rank = ""
            self._target_display_label = ""
            self._target_edit.setText(name)
            self._fetch_target_autocomplete()
            self._update_relationship()
            self._update_plan_enabled()
        self._target_edit.setFocus()
        self._copy_source_btn.setText("Copied")
        QTimer.singleShot(900, lambda: self._copy_source_btn.setText("Copy"))

    def _source_copy_payload(self) -> Optional[Tuple[str, Optional[int], str]]:
        if self._source_taxon and self._source_taxon.taxon_id:
            return (
                self._source_taxon.name,
                int(self._source_taxon.taxon_id),
                self._source_taxon.rank,
            )
        label_text = self._source_label.text().strip()
        match = re.search(r"Source taxon from URL:\s*(.*?)\s*\((\d+)\)\s*$", label_text)
        if match:
            return match.group(1).strip(), int(match.group(2)), ""
        if self._source_provisional_name.strip():
            return self._source_provisional_name.strip(), None, ""
        return None

    def _update_source_copy_enabled(self) -> None:
        if self._source_copy_payload() is not None:
            self._copy_source_btn.setToolTip(
                "Copy the source identity into the target taxon field."
            )
        else:
            self._copy_source_btn.setToolTip(
                "Resolve an observations URL source before copying it."
            )

    def _on_skip_dna_toggled(self, checked: bool) -> None:
        if checked and self._only_dna_cb.isChecked():
            self._only_dna_cb.setChecked(False)

    def _on_only_dna_toggled(self, checked: bool) -> None:
        if checked and self._skip_dna_cb.isChecked():
            self._skip_dna_cb.setChecked(False)

    def _after_target_changed(self) -> None:
        self._update_relationship()
        self._update_plan_enabled()

    def _update_relationship(self) -> None:
        # URL has no taxon_id: there is no source taxon to relate the target to.
        if (
            self._source_taxon is None
            and self._source_query is not None
            and not self._url_has_taxon_id
        ):
            if self._target_taxon_id and self._source_provisional_name:
                self._relationship_label.setText(
                    "Source is the Provisional Species Name "
                    f"“{self._source_provisional_name}” from the URL. Identifications "
                    "will be posted as normal IDs (no explicit ancestor-disagreement "
                    "flag); when the safety check is enabled, an observation is skipped "
                    "if it no longer carries this provisional name."
                )
                self._relationship_label.setStyleSheet("QLabel { color: #157f1f; }")
            elif self._target_taxon_id:
                self._relationship_label.setText(
                    "This URL has no source taxon. Identifications will be posted as "
                    "normal IDs (no explicit ancestor-disagreement flag) to every "
                    "matching observation, with no source-taxon safety check."
                )
                self._relationship_label.setStyleSheet("QLabel { color: #b36b00; font-weight: 700; }")
            else:
                self._relationship_label.setText("Select a target taxon to add to the matching observations.")
                self._relationship_label.setStyleSheet("")
            return
        if not self._source_taxon or not self._target_taxon_id:
            self._relationship_label.setText(
                "Select a resolved source URL and a target taxon to check their relationship."
            )
            self._relationship_label.setStyleSheet("")
            return
        if self._source_target_same():
            self._relationship_label.setText(
                "The source taxon and target taxon are the same. Choose a different target taxon."
            )
            self._relationship_label.setStyleSheet("QLabel { color: #b00020; font-weight: 700; }")
        elif taxon_is_strict_ancestor(self._source_taxon, self._target_taxon_id):
            self._relationship_label.setText(
                "Target taxon is an ancestor of the source taxon. Identifications will be "
                "posted with iNaturalist's explicit disagreement flag."
            )
            self._relationship_label.setStyleSheet("QLabel { color: #157f1f; }")
        else:
            self._relationship_label.setText(
                "The target taxon does not appear to be an ancestor of the source taxon. "
                "This may be a correction to an unrelated taxon or a same-rank synonym-style "
                "correction. Identifications will be posted as normal conflicting IDs, not "
                "with iNaturalist's explicit ancestor-disagreement flag."
            )
            self._relationship_label.setStyleSheet("QLabel { color: #b00020; font-weight: 700; }")

    def _update_plan_enabled(self) -> None:
        # A source taxon is required only when the URL carries a taxon_id;
        # otherwise the workflow runs without a source-taxon safety check.
        source_ok = bool(self._source_taxon) or (
            self._source_query is not None and not self._url_has_taxon_id
        )
        ready = bool(
            self._source_query
            and source_ok
            and self._target_taxon_id
            and self._target_taxon_name
        )
        if ready and self._source_target_same():
            self._set_validation("Source taxon and target taxon are the same. Choose a different target taxon.")
            self._plan_btn.setEnabled(False)
            return
        if self._validation_label.text() in {
            "Enter the identification comment to post.",
            "Source taxon and target taxon are the same. Choose a different target taxon.",
        }:
            self._set_validation("")
        self._plan_btn.setEnabled(ready)

    def _source_target_same(self) -> bool:
        return bool(
            self._source_taxon
            and self._target_taxon_id
            and self._source_taxon.taxon_id == int(self._target_taxon_id)
        )

    def _set_validation(self, text: str) -> None:
        self._validation_label.setText(text)

    def _on_delay_min_changed(self, value: int) -> None:
        if value > self._delay_max_spin.value():
            self._delay_max_spin.blockSignals(True)
            self._delay_max_spin.setValue(value)
            self._delay_max_spin.blockSignals(False)

    def _on_delay_max_changed(self, value: int) -> None:
        if value < self._delay_min_spin.value():
            self._delay_min_spin.blockSignals(True)
            self._delay_min_spin.setValue(value)
            self._delay_min_spin.blockSignals(False)


class ProposeNameSetupDialog(_TargetTaxonAutocomplete, QDialog):
    """Collect a list of observation numbers, a target taxon, and a comment.

    Modeled on :class:`BulkDisagreeSetupDialog`, but the observations are typed
    in by number rather than discovered from an observations URL, so there is no
    source taxon and no source-taxon safety check.
    """

    def __init__(
        self,
        client: INatClient,
        *,
        defaults: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Propose a Name to Observation Numbers")
        self.resize(640, 560)
        self._client = client
        self._pool = QThreadPool.globalInstance()
        self._live_signals: set = set()
        self._target_gen = 0
        self._target_taxon_id: Optional[int] = None
        self._target_taxon_name = ""
        self._target_taxon_rank = ""
        self._target_display_label = ""
        self._target_items: List[Tuple[str, str, int, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Enter iNaturalist observation numbers separated by spaces, commas, or "
            "new lines. Pasted observation URLs are also accepted. Each observation "
            "is refreshed from iNaturalist so you can review it before the proposed "
            "identification is posted. The explicit disagreement flag is set only "
            "when the proposed name differs from the observation's current taxon; an "
            "identification that agrees with the current taxon is posted as a plain ID."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("Observation numbers:"))
        self._numbers_edit = QTextEdit()
        self._numbers_edit.setAcceptRichText(False)
        self._numbers_edit.setPlaceholderText("e.g. 12345, 67890 98765")
        self._numbers_edit.setFixedHeight(110)
        self._numbers_edit.textChanged.connect(self._on_numbers_changed)
        layout.addWidget(self._numbers_edit)

        self._numbers_status = QLabel("")
        self._numbers_status.setWordWrap(True)
        layout.addWidget(self._numbers_status)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("Name to propose:"), 0, 0)
        self._target_edit = QLineEdit()
        self._target_edit.setPlaceholderText("Type and select a taxon, e.g. Amanita muscaria")
        self._target_model = QStringListModel(self)
        completer = QCompleter(self._target_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        completer.activated[str].connect(self._on_target_selected)
        self._target_edit.setCompleter(completer)
        self._target_completer = completer
        self._target_edit.textEdited.connect(self._on_target_text_changed)
        self._pin_target_completer_popup()
        grid.addWidget(self._target_edit, 0, 1)
        self._target_summary_label = QLabel("")
        self._target_summary_label.setWordWrap(True)
        grid.addWidget(self._target_summary_label, 1, 1)
        layout.addLayout(grid)

        layout.addWidget(QLabel("Comment to post with the proposed name:"))
        self._comment_edit = QTextEdit()
        self._comment_edit.setAcceptRichText(False)
        self._comment_edit.setFixedHeight(90)
        self._comment_edit.textChanged.connect(self._update_plan_enabled)
        layout.addWidget(self._comment_edit)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Delay:"))
        self._delay_min_spin = QSpinBox()
        self._delay_min_spin.setRange(0, 3600)
        self._delay_min_spin.setSuffix("s min")
        self._delay_min_spin.setValue(10)
        self._delay_max_spin = QSpinBox()
        self._delay_max_spin.setRange(0, 3600)
        self._delay_max_spin.setSuffix("s max")
        self._delay_max_spin.setValue(30)
        self._delay_min_spin.valueChanged.connect(self._on_delay_min_changed)
        self._delay_max_spin.valueChanged.connect(self._on_delay_max_changed)
        options_row.addWidget(self._delay_min_spin)
        options_row.addWidget(self._delay_max_spin)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        self._tag_others_cb = QCheckBox(
            "Tag users who proposed a different identification"
        )
        self._tag_others_cb.setChecked(False)
        self._tag_others_cb.setToolTip(
            "Append a blank line and @-mentions of users whose current ID differs "
            "from the name you are proposing, e.g. @scottostuni @johnplischke."
        )
        layout.addWidget(self._tag_others_cb)

        self._dry_run_cb = QCheckBox("Preview only / dry run")
        self._dry_run_cb.setChecked(False)
        layout.addWidget(self._dry_run_cb)

        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet("QLabel { color: #b00020; }")
        layout.addWidget(self._validation_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._plan_btn = QPushButton("Plan")
        self._plan_btn.setEnabled(False)
        buttons.addButton(self._plan_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._plan_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._target_timer = QTimer(self)
        self._target_timer.setSingleShot(True)
        self._target_timer.timeout.connect(self._fetch_target_autocomplete)

        self._observation_ids: List[int] = []
        self._invalid_tokens: List[str] = []
        self._apply_defaults(defaults or {})
        self._on_numbers_changed()

    # -- public accessors --------------------------------------------------

    def observation_ids(self) -> List[int]:
        return list(self._observation_ids)

    def invalid_tokens(self) -> List[str]:
        return list(self._invalid_tokens)

    @property
    def target_taxon_id(self) -> int:
        assert self._target_taxon_id is not None
        return self._target_taxon_id

    @property
    def target_taxon_name(self) -> str:
        return self._target_taxon_name

    @property
    def target_taxon_rank(self) -> str:
        return self._target_taxon_rank

    def comment(self) -> str:
        return self._comment_edit.toPlainText().strip()

    def delay_min_seconds(self) -> int:
        return self._delay_min_spin.value()

    def delay_max_seconds(self) -> int:
        return self._delay_max_spin.value()

    def dry_run(self) -> bool:
        return self._dry_run_cb.isChecked()

    def tag_other_identifiers(self) -> bool:
        return self._tag_others_cb.isChecked()

    # -- defaults ----------------------------------------------------------

    def _apply_defaults(self, defaults: dict) -> None:
        self._comment_edit.setPlainText(str(defaults.get("comment") or ""))
        delay_min = max(0, _int_default(defaults.get("delay_min_seconds"), 10))
        delay_max = max(delay_min, _int_default(defaults.get("delay_max_seconds"), 30))
        self._delay_min_spin.setValue(delay_min)
        self._delay_max_spin.setValue(delay_max)
        # Dry run and tag-others always start unchecked, regardless of last use.
        self._dry_run_cb.setChecked(False)
        self._tag_others_cb.setChecked(False)
        target_id = _int_or_none(defaults.get("target_taxon_id"))
        target_name = str(defaults.get("target_taxon_name") or "").strip()
        if target_id and target_name:
            target_rank = str(defaults.get("target_taxon_rank") or "").strip()
            self._set_target_taxon(target_name, target_id, rank=target_rank)

    # -- observation numbers -----------------------------------------------

    def _on_numbers_changed(self) -> None:
        ids, invalid = parse_observation_id_tokens(self._numbers_edit.toPlainText())
        self._observation_ids = ids
        self._invalid_tokens = invalid
        parts = [f"{len(ids)} observation number(s) recognized."]
        if invalid:
            shown = ", ".join(invalid[:5])
            if len(invalid) > 5:
                shown += ", …"
            parts.append(f"Ignoring unrecognized: {shown}")
        self._numbers_status.setText(" ".join(parts))
        self._update_plan_enabled()

    # -- target taxon autocomplete (mirrors BulkDisagreeSetupDialog) -------

    # -- validation / options ----------------------------------------------

    def _after_target_changed(self) -> None:
        self._update_target_summary()
        self._update_plan_enabled()

    def _update_target_summary(self) -> None:
        if not self._target_taxon_id or not self._target_taxon_name:
            self._target_summary_label.setText("")
            return
        self._target_summary_label.setText(
            "Selected proposed taxon: "
            f"{_format_taxon_with_rank(self._target_taxon_name, self._target_taxon_rank)} "
            f"(id={self._target_taxon_id})"
        )

    def _update_plan_enabled(self) -> None:
        ready = bool(
            self._observation_ids
            and self._target_taxon_id
            and self._target_taxon_name
        )
        if ready and not self._comment_edit.toPlainText().strip():
            self._validation_label.setText("Enter the comment to post with the proposed name.")
            ready = False
        elif not self._observation_ids:
            self._validation_label.setText("Enter at least one observation number.")
        elif not self._target_taxon_id:
            self._validation_label.setText("Select a name to propose from autocomplete.")
        else:
            self._validation_label.setText("")
        self._plan_btn.setEnabled(ready)

    def _on_delay_min_changed(self, value: int) -> None:
        if value > self._delay_max_spin.value():
            self._delay_max_spin.blockSignals(True)
            self._delay_max_spin.setValue(value)
            self._delay_max_spin.blockSignals(False)

    def _on_delay_max_changed(self, value: int) -> None:
        if value < self._delay_min_spin.value():
            self._delay_min_spin.blockSignals(True)
            self._delay_min_spin.setValue(value)
            self._delay_min_spin.blockSignals(False)


class _FullScreenPhotoOverlay(QWidget):
    """Frameless full-screen photo shown while the user holds the mouse button on a thumbnail."""

    def __init__(self, parent=None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window,
        )
        self.setStyleSheet("background: #000000;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("QLabel { background: #000000; }")
        self._image_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._dismiss_label = QLabel("Release the mouse button or press Esc to return", self)
        self._dismiss_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dismiss_label.setStyleSheet(
            "QLabel { background: #202020; color: #ffffff; padding: 8px; }"
        )
        self._dismiss_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        overlay_layout = QVBoxLayout(self)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.setSpacing(0)
        overlay_layout.addWidget(self._image_label, 1)
        overlay_layout.addWidget(self._dismiss_label)
        self._pixmap: Optional[QPixmap] = None

    def show_pixmap(self, pixmap: QPixmap, screen) -> None:
        self._pixmap = pixmap
        if screen is not None:
            self.setGeometry(screen.geometry())
        self._rescale()
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def update_pixmap(self, pixmap: QPixmap) -> None:
        """Swap in a higher-resolution pixmap while the overlay is already visible."""
        self._pixmap = pixmap
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        scaled = self._pixmap.scaled(
            max(1, self._image_label.width()),
            max(1, self._image_label.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.hide()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)


class _HoldToZoomLabel(QLabel):
    """Photo thumbnail that expands to a full-screen view while the left mouse button is held down."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._full_pixmap: Optional[QPixmap] = None
        self._have_original = False
        self._original_fetcher: Optional[Callable[[], None]] = None
        self._original_requested = False
        self._overlay: Optional[_FullScreenPhotoOverlay] = None

    def set_full_pixmap(self, pixmap: QPixmap, *, is_original: bool = False) -> None:
        self._full_pixmap = pixmap
        if is_original:
            self._have_original = True
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            "Hold down the left mouse button to view this photo full screen; "
            "release it or press Esc to return."
        )
        if self._overlay is not None and self._overlay.isVisible():
            self._overlay.update_pixmap(pixmap)

    def set_original_fetcher(self, fetcher: Callable[[], None]) -> None:
        """Register a callback that downloads the full-resolution original for this photo."""
        self._original_fetcher = fetcher

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._full_pixmap is not None
            and not self._full_pixmap.isNull()
        ):
            self._show_overlay()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._overlay is not None:
            self._hide_overlay()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _show_overlay(self) -> None:
        if self._overlay is None:
            self._overlay = _FullScreenPhotoOverlay(self)
        screen = self.screen() or QApplication.primaryScreen()
        self._overlay.show_pixmap(self._full_pixmap, screen)
        # Upgrade to the full-resolution original on first zoom; the large image is
        # shown instantly and swapped out when the original download completes.
        if not self._have_original and not self._original_requested and self._original_fetcher is not None:
            self._original_requested = True
            self._original_fetcher()

    def _hide_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.hide()


class BulkDisagreePhotoBrowserDialog(QDialog):
    def __init__(
        self,
        candidates: List[BulkDisagreeCandidate],
        *,
        client: INatClient,
        disk_cache: ImageCache,
        api_token: str,
        login: str,
        require_source_taxon_match: bool = True,
        default_comment: str = "",
        dry_run: bool = False,
        on_skip_forever: Optional[Callable[[BulkDisagreeCandidate], None]] = None,
        on_unskip_forever: Optional[Callable[[BulkDisagreeCandidate], None]] = None,
        request_reauthentication: Optional[
            Callable[
                [Callable[[str, str], None], Callable[[str], None]],
                None,
            ]
        ] = None,
        window_title: str = "Browse Bulk Disagree Photos",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(1120, 780)
        self._candidates = list(candidates)
        self._candidate_by_obs_id = {c.observation.obs_id: c for c in self._candidates}
        self._client = client
        self._disk_cache = disk_cache
        self._api_token = api_token
        self._login = login
        self._require_source_taxon_match = require_source_taxon_match
        self._default_comment = default_comment
        self._dry_run = dry_run
        self._on_skip_forever = on_skip_forever
        self._on_unskip_forever = on_unskip_forever
        self._request_reauthentication = request_reauthentication
        self._pool = QThreadPool.globalInstance()
        self._removed_obs_ids: set[int] = set()
        self._kept_hidden_obs_ids: set[int] = set()
        self._keep_undo_stack: List[int] = []
        self._skip_undo_stack: List[Tuple[int, bool]] = []
        self._photo_labels: Dict[Tuple[int, int], QLabel] = {}
        self._photo_columns: Dict[Tuple[int, int], int] = {}
        self._status_labels: Dict[int, QLabel] = {}
        self._card_buttons: Dict[int, List[QPushButton]] = {}
        self._quick_alt_rows: Dict[int, List[QHBoxLayout]] = {}
        self._quick_alt_buttons: Dict[int, List[QPushButton]] = {}
        self._cards: Dict[int, QFrame] = {}
        self._card_order: List[int] = []
        self._recent_alternate_taxa: List[_RecentAlternateTaxon] = []
        self._pending_alternate_posts: Dict[int, _PendingAlternatePost] = {}
        self._optimistically_hidden_obs_ids: set[int] = set()
        self._auth_waiting_obs_ids: set[int] = set()
        self._reauthentication_in_progress = False
        self._live_image_signals: set[QObject] = set()
        self._live_post_signals: set[QObject] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Browse the planned observations before starting. Use Skip this run for fast "
            "visual triage, Skip forever for observations that should never be included in "
            "this workflow, or Post alternate ID when the observation needs a different ID. "
            "Keyboard shortcuts act on the top visible observation: K = Keep, "
            "S = Skip this run, A = Alternate ID, O = Open observation."
        )
        if dry_run:
            intro.setText(intro.text() + " Dry run is enabled, so alternate IDs cannot be posted.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)

        keep_tools = QHBoxLayout()
        self._undo_keep_btn = QPushButton("Undo last Keep")
        self._undo_keep_btn.setEnabled(False)
        self._undo_keep_btn.clicked.connect(self._undo_last_keep)
        self._undo_skip_btn = QPushButton("Undo last skip")
        self._undo_skip_btn.setEnabled(False)
        self._undo_skip_btn.clicked.connect(self._undo_last_skip)
        self._show_kept_btn = QPushButton("Show kept observations")
        self._show_kept_btn.setEnabled(False)
        self._show_kept_btn.clicked.connect(self._show_kept_observations)
        keep_tools.addWidget(self._undo_keep_btn)
        keep_tools.addWidget(self._undo_skip_btn)
        keep_tools.addWidget(self._show_kept_btn)
        keep_tools.addStretch(1)
        layout.addLayout(keep_tools)

        self._browser_status_label = QLabel("")
        self._browser_status_label.setWordWrap(True)
        layout.addWidget(self._browser_status_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        content = QWidget()
        self._scroll_content = content
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(12)
        for candidate in self._candidates:
            self._add_card(candidate)
        self._content_layout.addStretch(1)
        self._scroll.setWidget(content)
        layout.addWidget(self._scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        self._done_btn = close_btn
        if close_btn:
            close_btn.setText("Done")
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
        self._install_shortcuts()
        self._update_count_label()

    def candidates(self) -> List[BulkDisagreeCandidate]:
        return [
            candidate
            for candidate in self._candidates
            if candidate.observation.obs_id not in self._removed_obs_ids
        ]

    def accept(self) -> None:
        if self._pending_alternate_posts:
            self._browser_status_label.setText(
                "Wait for the pending alternate IDs to finish before closing the browser."
            )
            return
        super().accept()

    def reject(self) -> None:
        if self._pending_alternate_posts:
            self._browser_status_label.setText(
                "Wait for the pending alternate IDs to finish before closing the browser."
            )
            return
        super().reject()

    def _add_card(self, candidate: BulkDisagreeCandidate) -> None:
        obs = candidate.observation
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("QFrame { background: #ffffff; } QLabel { color: #000000; }")
        self._cards[obs.obs_id] = frame
        self._card_order.append(obs.obs_id)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        title = QLabel(f"<b>Observation {obs.obs_id}</b> by {obs.observer_login}")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(title)

        meta = QLabel(
            " | ".join(
                [
                    f"Current: {candidate.current_observation_taxon_name or '(none)'}",
                    f"Community: {candidate.community_taxon_name or '(none)'}",
                    f"Location: {obs.place_guess or '(none)'}",
                    f"Planned target: {_format_taxon_with_rank(candidate.target_taxon_name, candidate.target_taxon_rank)}",
                    f"Explicit disagreement: {'yes' if candidate.explicit_disagreement else 'no'}",
                    f"DNA ITS: {'yes' if candidate.has_dna_barcode_its else 'no'}",
                ]
            )
        )
        meta.setWordWrap(True)
        layout.addWidget(meta)

        top_row, top_buttons = self._make_action_row(candidate)
        layout.addLayout(top_row)

        if not obs.photos:
            no_photo = QLabel("No photos on this observation.")
            no_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_photo.setMinimumHeight(120)
            layout.addWidget(no_photo)
        else:
            columns = 2 if len(obs.photos) > 1 else 1
            photo_grid = QGridLayout()
            photo_grid.setSpacing(8)
            photo_grid.setColumnStretch(0, 1)
            if columns > 1:
                photo_grid.setColumnStretch(1, 1)
            for index, photo in enumerate(obs.photos):
                photo_label = _HoldToZoomLabel(f"Loading photo {index + 1} of {len(obs.photos)}...")
                photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                photo_label.setMinimumHeight(240 if columns > 1 else 260)
                photo_label.setStyleSheet("QLabel { background: #111; color: #ddd; }")
                row = index // columns
                col = index % columns
                photo_grid.addWidget(photo_label, row, col)
                self._photo_labels[(obs.obs_id, photo.photo_id)] = photo_label
                self._photo_columns[(obs.obs_id, photo.photo_id)] = columns
                photo_label.set_original_fetcher(
                    lambda c=candidate, p=photo: self._load_original_photo(c, p)
                )
                self._load_photo(candidate, photo)
            layout.addLayout(photo_grid)

        status = QLabel("Kept in planned run.")
        status.setWordWrap(True)
        self._status_labels[obs.obs_id] = status
        layout.addWidget(status)

        bottom_row, bottom_buttons = self._make_action_row(candidate)
        layout.addLayout(bottom_row)
        self._card_buttons[obs.obs_id] = top_buttons + bottom_buttons
        self._content_layout.addWidget(frame)

    def _make_action_row(
        self,
        candidate: BulkDisagreeCandidate,
    ) -> tuple[QHBoxLayout, List[QPushButton]]:
        row = QHBoxLayout()
        keep_btn = QPushButton("Keep (K)")
        skip_btn = QPushButton("Skip this run (S)")
        skip_forever_btn = QPushButton("Skip forever")
        alternate_btn = QPushButton("Post alternate ID... (A)")
        if self._dry_run:
            alternate_btn.setEnabled(False)
            alternate_btn.setToolTip("Dry run is enabled. No alternate identifications will be posted.")
        open_btn = QPushButton("Open observation (O)")
        copy_url_btn = QPushButton("Copy Obs URL")
        copy_url_btn.setToolTip("Copy this iNaturalist observation URL to the clipboard")
        keep_btn.clicked.connect(lambda _checked=False, c=candidate: self._keep_candidate(c))
        skip_btn.clicked.connect(lambda _checked=False, c=candidate: self._skip_candidate(c))
        skip_forever_btn.clicked.connect(lambda _checked=False, c=candidate: self._skip_forever(c))
        alternate_btn.clicked.connect(lambda _checked=False, c=candidate: self._post_alternate(c))
        open_btn.clicked.connect(
            lambda _checked=False, c=candidate: open_external_url_silently(c.observation.url)
        )
        copy_url_btn.clicked.connect(
            lambda _checked=False, c=candidate: self._copy_observation_url(c)
        )
        for button in (
            keep_btn,
            skip_btn,
            skip_forever_btn,
            alternate_btn,
            open_btn,
            copy_url_btn,
        ):
            row.addWidget(button)
        self._quick_alt_rows.setdefault(candidate.observation.obs_id, []).append(row)
        row.addStretch(1)
        return row, [
            keep_btn,
            skip_btn,
            skip_forever_btn,
            alternate_btn,
            open_btn,
            copy_url_btn,
        ]

    def _copy_observation_url(self, candidate: BulkDisagreeCandidate) -> None:
        url = candidate.observation.url
        QApplication.clipboard().setText(url)
        self._browser_status_label.setText(f"Copied observation URL: {url}")

    def _install_shortcuts(self) -> None:
        self._shortcuts: List[QShortcut] = []
        for key, handler in (
            ("K", self._keep_current_candidate),
            ("S", self._skip_current_candidate),
            ("A", self._post_alternate_current_candidate),
            ("O", self._open_current_observation),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    def _current_visible_candidate(self) -> Optional[BulkDisagreeCandidate]:
        obs_id = self._current_visible_obs_id()
        if obs_id is None:
            self._browser_status_label.setText("No visible observation for that shortcut.")
            return None
        return self._candidate_by_obs_id.get(obs_id)

    def _current_visible_obs_id(self) -> Optional[int]:
        top = self._scroll.verticalScrollBar().value()
        bottom = top + self._scroll.viewport().height()

        for obs_id in self._card_order:
            if obs_id in self._removed_obs_ids or obs_id in self._kept_hidden_obs_ids:
                continue
            card = self._cards.get(obs_id)
            if not card or not card.isVisible():
                continue
            card_top = card.pos().y()
            card_bottom = card_top + card.height()
            if card_bottom > top and card_top < bottom:
                return obs_id
        return None

    def _keep_current_candidate(self) -> None:
        candidate = self._current_visible_candidate()
        if candidate:
            self._keep_candidate(candidate)

    def _skip_current_candidate(self) -> None:
        candidate = self._current_visible_candidate()
        if candidate:
            self._skip_candidate(candidate)

    def _post_alternate_current_candidate(self) -> None:
        candidate = self._current_visible_candidate()
        if candidate:
            self._post_alternate(candidate)

    def _open_current_observation(self) -> None:
        candidate = self._current_visible_candidate()
        if candidate:
            open_external_url_silently(candidate.observation.url)

    def _load_photo(self, candidate: BulkDisagreeCandidate, photo) -> None:
        target = _photo_fetch_target(photo, "large")
        if target is None:
            return
        size, url = target
        worker = _GalleryImageWorker(
            obs_id=candidate.observation.obs_id,
            photo_id=photo.photo_id,
            image_url=url,
            client=self._client,
            disk_cache=self._disk_cache,
            size=size,
        )
        sigs = worker.signals
        self._live_image_signals.add(sigs)
        sigs.loaded.connect(
            lambda obs_id, photo_id, pixmap, s=sigs: (
                self._live_image_signals.discard(s),
                self._on_photo_loaded(obs_id, photo_id, pixmap),
            )
        )
        sigs.failed.connect(
            lambda obs_id, photo_id, msg, s=sigs: (
                self._live_image_signals.discard(s),
                self._on_photo_failed(obs_id, photo_id, msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int, object)
    def _on_photo_loaded(self, obs_id: int, photo_id: int, image_data: bytes) -> None:
        label = self._photo_labels.get((obs_id, photo_id))
        if not label:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(image_data)
        if pixmap.isNull():
            label.setText("Could not load photo: downloaded image could not be decoded.")
            return
        if isinstance(label, _HoldToZoomLabel):
            label.set_full_pixmap(pixmap)
        columns = self._photo_columns.get((obs_id, photo_id), 1)
        max_width = 520 if columns > 1 else 920
        max_height = 540 if columns > 1 else 720
        scaled = pixmap.scaled(
            max_width,
            max_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        label.setMinimumHeight(max(180, scaled.height()))

    def _on_photo_failed(self, obs_id: int, photo_id: int, msg: str) -> None:
        label = self._photo_labels.get((obs_id, photo_id))
        if label:
            label.setText(f"Could not load photo: {msg}")

    def _load_original_photo(self, candidate: BulkDisagreeCandidate, photo) -> None:
        target = _photo_fetch_target(photo, "original")
        if target is None:
            return
        size, url = target
        if size != "original":
            # This photo url has no substitutable size token, so there is no
            # higher-resolution tier to upgrade to; what is already shown is
            # everything iNaturalist offers here.
            return
        worker = _GalleryImageWorker(
            obs_id=candidate.observation.obs_id,
            photo_id=photo.photo_id,
            image_url=url,
            client=self._client,
            disk_cache=self._disk_cache,
            size=size,
        )
        sigs = worker.signals
        self._live_image_signals.add(sigs)
        sigs.loaded.connect(
            lambda obs_id, photo_id, image_data, s=sigs: (
                self._live_image_signals.discard(s),
                self._on_original_loaded(obs_id, photo_id, image_data),
            )
        )
        sigs.failed.connect(
            lambda obs_id, photo_id, msg, s=sigs: self._live_image_signals.discard(s)
        )
        self._pool.start(worker)

    @Slot(int, int, object)
    def _on_original_loaded(self, obs_id: int, photo_id: int, image_data: bytes) -> None:
        label = self._photo_labels.get((obs_id, photo_id))
        if not isinstance(label, _HoldToZoomLabel):
            return
        pixmap = QPixmap()
        pixmap.loadFromData(image_data)
        if pixmap.isNull():
            return
        label.set_full_pixmap(pixmap, is_original=True)

    def _keep_candidate(self, candidate: BulkDisagreeCandidate) -> None:
        obs_id = candidate.observation.obs_id
        if obs_id in self._kept_hidden_obs_ids:
            return
        self._kept_hidden_obs_ids.add(obs_id)
        self._keep_undo_stack.append(obs_id)
        self._hide_card_preserving_position(obs_id)
        status = self._status_labels.get(candidate.observation.obs_id)
        if status:
            status.setText("Kept in planned run and hidden from browser.")
        self._browser_status_label.setText(
            f"Kept observation {obs_id}. Use Undo last Keep if that should have been skipped."
        )
        self._update_count_label()

    def _skip_candidate(self, candidate: BulkDisagreeCandidate) -> None:
        self._remove_candidate(candidate, "Skipped for this run.", undoable=True)

    def _skip_forever(self, candidate: BulkDisagreeCandidate) -> None:
        if self._on_skip_forever:
            self._on_skip_forever(candidate)
        self._remove_candidate(candidate, "Skipped forever.", undoable=True, skip_forever=True)

    def _remove_candidate(
        self,
        candidate: BulkDisagreeCandidate,
        reason: str,
        *,
        undoable: bool = False,
        skip_forever: bool = False,
    ) -> None:
        obs_id = candidate.observation.obs_id
        if undoable and obs_id not in self._removed_obs_ids:
            self._skip_undo_stack.append((obs_id, skip_forever))
        self._removed_obs_ids.add(obs_id)
        self._kept_hidden_obs_ids.discard(obs_id)
        self._keep_undo_stack = [oid for oid in self._keep_undo_stack if oid != obs_id]
        self._hide_card_preserving_position(obs_id)
        status = self._status_labels.get(obs_id)
        if status:
            status.setText(reason)
        self._update_count_label()

    def _undo_last_keep(self) -> None:
        while self._keep_undo_stack:
            obs_id = self._keep_undo_stack.pop()
            if obs_id in self._kept_hidden_obs_ids and obs_id not in self._removed_obs_ids:
                self._restore_kept_card(obs_id, scroll_to=True)
                self._browser_status_label.setText(
                    f"Restored observation {obs_id}. You can now skip it if needed."
                )
                self._update_count_label()
                return
        self._browser_status_label.setText("No kept observations to undo.")
        self._update_count_label()

    def _undo_last_skip(self) -> None:
        while self._skip_undo_stack:
            obs_id, was_skip_forever = self._skip_undo_stack.pop()
            if obs_id not in self._removed_obs_ids:
                continue
            candidate = self._candidate_by_obs_id.get(obs_id)
            if not candidate:
                continue
            if was_skip_forever and self._on_unskip_forever:
                self._on_unskip_forever(candidate)
            self._removed_obs_ids.discard(obs_id)
            self._kept_hidden_obs_ids.discard(obs_id)
            self._restore_skipped_card(obs_id)
            self._browser_status_label.setText(
                f"Restored observation {obs_id} from {'skip forever' if was_skip_forever else 'skip this run'}."
            )
            self._update_count_label()
            return
        self._browser_status_label.setText("No skipped observations to undo.")
        self._update_count_label()

    def _show_kept_observations(self) -> None:
        kept_candidates = [
            self._candidate_by_obs_id[obs_id]
            for obs_id in self._card_order
            if (
                obs_id in self._kept_hidden_obs_ids
                and obs_id not in self._removed_obs_ids
                and obs_id in self._candidate_by_obs_id
            )
        ]
        if not kept_candidates:
            self._browser_status_label.setText("No kept observations are hidden.")
            self._update_count_label()
            return
        dlg = BulkDisagreeKeptReviewDialog(
            kept_candidates,
            client=self._client,
            disk_cache=self._disk_cache,
            on_skip=lambda c: self._remove_candidate(
                c,
                "Skipped for this run from kept review.",
                undoable=True,
            ),
            parent=self,
        )
        dlg.exec()
        skipped = dlg.skipped_count()
        if skipped:
            self._browser_status_label.setText(
                f"Skipped {skipped} kept observation(s). Other kept observations remain hidden."
            )
        else:
            self._browser_status_label.setText("Kept observations remain hidden.")
        self._update_count_label()

    def _restore_kept_card(self, obs_id: int, *, scroll_to: bool) -> None:
        self._kept_hidden_obs_ids.discard(obs_id)
        card = self._cards.get(obs_id)
        if card:
            card.setVisible(True)
            if scroll_to:
                QTimer.singleShot(0, lambda c=card: self._scroll.ensureWidgetVisible(c, 0, 40))
        status = self._status_labels.get(obs_id)
        if status:
            status.setText("Kept in planned run.")

    def _restore_skipped_card(self, obs_id: int) -> None:
        card = self._cards.get(obs_id)
        if card:
            card.setVisible(True)
            QTimer.singleShot(0, lambda c=card: self._scroll.ensureWidgetVisible(c, 0, 40))
        status = self._status_labels.get(obs_id)
        if status:
            status.setText("Restored to planned run.")

    def _hide_card_preserving_position(self, obs_id: int) -> None:
        card = self._cards.get(obs_id)
        if not card or not card.isVisible():
            return
        viewport = self._scroll.viewport()
        scrollbar = self._scroll.verticalScrollBar()
        old_value = scrollbar.value()
        next_card = self._next_visible_card(obs_id)
        if next_card is not None:
            anchor = next_card
            card_viewport_y = card.mapTo(viewport, card.rect().topLeft()).y()
            desired_anchor_y = max(0, card_viewport_y)
        else:
            anchor = self._previous_visible_card(obs_id)
            desired_anchor_y = (
                anchor.mapTo(viewport, anchor.rect().topLeft()).y()
                if anchor is not None
                else 0
            )
        focus = QApplication.focusWidget()
        if focus is not None and (focus is card or card.isAncestorOf(focus)):
            self._scroll.setFocus(Qt.FocusReason.OtherFocusReason)
        card.setVisible(False)
        if anchor is None:
            scrollbar.setValue(old_value)
            return
        QTimer.singleShot(
            0,
            lambda a=anchor, desired_y=desired_anchor_y, old_scroll=old_value: (
                self._restore_scroll_after_hide(a, desired_y, old_scroll)
            ),
        )

    def _restore_scroll_after_hide(
        self,
        anchor: QFrame,
        desired_anchor_y: int,
        old_scroll_value: int,
    ) -> None:
        if not anchor.isVisible():
            self._scroll.verticalScrollBar().setValue(old_scroll_value)
            return
        self._content_layout.activate()
        viewport = self._scroll.viewport()
        current_anchor_y = anchor.mapTo(viewport, anchor.rect().topLeft()).y()
        scrollbar = self._scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() + current_anchor_y - desired_anchor_y)

    def _next_visible_card(self, obs_id: int) -> Optional[QFrame]:
        try:
            index = self._card_order.index(obs_id)
        except ValueError:
            return None
        for next_obs_id in self._card_order[index + 1:]:
            card = self._cards.get(next_obs_id)
            if card and card.isVisible():
                return card
        return None

    def _previous_visible_card(self, obs_id: int) -> Optional[QFrame]:
        try:
            index = self._card_order.index(obs_id)
        except ValueError:
            return None
        for previous_obs_id in reversed(self._card_order[:index]):
            card = self._cards.get(previous_obs_id)
            if card and card.isVisible():
                return card
        return None

    def _post_alternate(self, candidate: BulkDisagreeCandidate) -> None:
        if self._dry_run:
            QMessageBox.information(
                self,
                "Dry Run",
                "Dry run is enabled. No alternate identifications will be posted.",
            )
            return
        dlg = AlternateIdentificationDialog(
            self._client,
            candidate,
            default_taxon_id=candidate.target_taxon_id,
            default_taxon_name=candidate.target_taxon_name,
            default_taxon_rank=candidate.target_taxon_rank,
            default_comment="",
            recent_alternate_taxa=self._recent_alternate_taxa,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        obs_id = candidate.observation.obs_id
        status = self._status_labels.get(obs_id)
        if status:
            status.setText(
                f"Refreshing observation and posting alternate ID as {dlg.target_taxon_name}..."
            )
        recent = _RecentAlternateTaxon(
            taxon_id=dlg.target_taxon_id,
            name=dlg.target_taxon_name,
            rank=dlg.target_taxon_rank,
            body=dlg.comment(),
            disagreement=dlg.disagreement(),
        )
        self._start_alternate_post(candidate, recent)

    def _post_quick_alternate(
        self,
        candidate: BulkDisagreeCandidate,
        recent: _RecentAlternateTaxon,
    ) -> None:
        if self._dry_run:
            QMessageBox.information(
                self,
                "Dry Run",
                "Dry run is enabled. No alternate identifications will be posted.",
            )
            return
        obs_id = candidate.observation.obs_id
        status = self._status_labels.get(obs_id)
        if status:
            status.setText(
                f"Refreshing observation and posting alternate ID as {recent.name}..."
            )
        quick_recent = _RecentAlternateTaxon(
            taxon_id=recent.taxon_id,
            name=recent.name,
            rank=recent.rank,
            body="",
            disagreement=recent.disagreement,
        )
        self._start_alternate_post(
            candidate,
            quick_recent,
            remember_recent=recent,
            optimistic_hide=True,
        )

    def _start_alternate_post(
        self,
        candidate: BulkDisagreeCandidate,
        recent: _RecentAlternateTaxon,
        *,
        remember_recent: Optional[_RecentAlternateTaxon] = None,
        optimistic_hide: bool = False,
    ) -> None:
        obs_id = candidate.observation.obs_id
        if obs_id in self._pending_alternate_posts:
            return
        pending = _PendingAlternatePost(
            post_taxon=recent,
            remember_taxon=remember_recent or recent,
            optimistic_hide=optimistic_hide,
        )
        self._pending_alternate_posts[obs_id] = pending
        self._set_card_enabled(obs_id, False)
        if optimistic_hide:
            self._optimistically_hidden_obs_ids.add(obs_id)
            self._hide_card_preserving_position(obs_id)
            self._browser_status_label.setText(
                f"Posting observation {obs_id} as {recent.name} in the background…"
            )
        self._update_pending_post_state()
        self._launch_alternate_post_worker(candidate, pending)

    def _launch_alternate_post_worker(
        self,
        candidate: BulkDisagreeCandidate,
        pending: _PendingAlternatePost,
    ) -> None:
        recent = pending.post_taxon
        api_token = self._api_token
        worker = _AlternatePostWorker(
            client=self._client,
            api_token=api_token,
            login=self._login,
            candidate=candidate,
            target_taxon_id=recent.taxon_id,
            target_taxon_name=recent.name,
            body=recent.body,
            disagreement=recent.disagreement,
            require_source_taxon_match=self._require_source_taxon_match,
        )
        sigs = worker.signals
        self._live_post_signals.add(sigs)
        sigs.finished.connect(
            lambda candidate, result, s=sigs: (
                self._live_post_signals.discard(s),
                self._on_alternate_finished(candidate, result),
            )
        )
        sigs.error.connect(
            lambda candidate, msg, s=sigs, used_token=api_token: (
                self._live_post_signals.discard(s),
                self._on_alternate_error(candidate, msg, used_token),
            )
        )
        self._pool.start(worker)

    @Slot(object, object)
    def _on_alternate_finished(
        self,
        candidate: BulkDisagreeCandidate,
        result: BulkDisagreeResult,
    ) -> None:
        obs_id = candidate.observation.obs_id
        pending = self._pending_alternate_posts.pop(obs_id, None)
        self._auth_waiting_obs_ids.discard(obs_id)
        status = self._status_labels.get(obs_id)
        if result.status == "posted_id":
            if pending is not None:
                order_changed = self._remember_recent_alternate_taxon(
                    pending.remember_taxon
                )
                if order_changed:
                    self._refresh_quick_alternate_buttons_after(obs_id)
            self._optimistically_hidden_obs_ids.discard(obs_id)
            if status:
                status.setText(result.message + " Removed from planned run.")
            self._remove_candidate(candidate, result.message)
            self._update_pending_post_state()
            return
        if (
            result.status == "skipped"
            and "already currently identified" in result.message.casefold()
        ):
            self._optimistically_hidden_obs_ids.discard(obs_id)
            if status:
                status.setText(result.message + " Removed from planned run.")
            self._remove_candidate(candidate, result.message)
            self._update_pending_post_state()
            return
        self._restore_optimistically_hidden_card(obs_id)
        self._set_card_enabled(obs_id, True)
        self._update_pending_post_state()
        if status:
            status.setText(result.message)
        title = (
            "Alternate ID Ambiguous"
            if result.status == "ambiguous_write"
            else "Alternate ID Not Posted"
        )
        QMessageBox.warning(self, title, result.message)

    def _on_alternate_error(
        self,
        candidate: BulkDisagreeCandidate,
        msg: str,
        used_api_token: str,
    ) -> None:
        obs_id = candidate.observation.obs_id
        pending = self._pending_alternate_posts.get(obs_id)
        if pending is None:
            return
        if _is_auth_failure_message(msg):
            if self._api_token and used_api_token != self._api_token:
                self._retry_alternate_with_current_auth(candidate, pending)
                return
            if self._request_reauthentication is not None:
                self._handle_alternate_auth_error(candidate, msg)
                return
        self._pending_alternate_posts.pop(obs_id, None)
        self._auth_waiting_obs_ids.discard(obs_id)
        self._restore_optimistically_hidden_card(obs_id)
        self._set_card_enabled(obs_id, True)
        self._update_pending_post_state()
        status = self._status_labels.get(obs_id)
        if status:
            status.setText("Alternate ID failed: " + msg)
        QMessageBox.warning(self, "Alternate ID Failed", msg)

    def _handle_alternate_auth_error(
        self,
        candidate: BulkDisagreeCandidate,
        msg: str,
    ) -> None:
        obs_id = candidate.observation.obs_id
        self._auth_waiting_obs_ids.add(obs_id)
        self._restore_optimistically_hidden_card(obs_id)
        status = self._status_labels.get(obs_id)
        if status:
            status.setText(
                "Authentication expired. Waiting for a fresh iNaturalist token before retrying."
            )
        self._browser_status_label.setText(
            "iNaturalist authentication expired; pending quick IDs are paused."
        )
        if self._reauthentication_in_progress:
            return
        self._reauthentication_in_progress = True

        box = QMessageBox(self)
        box.setWindowTitle("Authentication Expired")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "iNaturalist rejected the saved API token before the alternate ID could be posted.\n\n"
            "The observation has been restored. Authenticate again to retry the same ID "
            "with a fresh token."
        )
        box.setDetailedText(msg)
        auth_btn = box.addButton("Authenticate Now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel Pending ID", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(auth_btn)
        box.exec()
        if box.clickedButton() is auth_btn:
            request_reauthentication = self._request_reauthentication
            if request_reauthentication is None:
                self._on_reauthentication_failed(
                    "Authentication is unavailable from this window."
                )
                return
            request_reauthentication(
                self._on_reauthenticated,
                self._on_reauthentication_failed,
            )
        else:
            self._on_reauthentication_failed("Authentication was cancelled.")

    def _on_reauthenticated(self, api_token: str, login: str) -> None:
        self._api_token = api_token
        self._login = login
        self._reauthentication_in_progress = False
        waiting_obs_ids = [
            obs_id
            for obs_id in self._card_order
            if obs_id in self._auth_waiting_obs_ids
        ]
        self._auth_waiting_obs_ids.clear()
        self._browser_status_label.setText(
            "Authentication refreshed; retrying pending alternate IDs…"
        )
        for obs_id in waiting_obs_ids:
            pending = self._pending_alternate_posts.get(obs_id)
            candidate = self._candidate_by_obs_id.get(obs_id)
            if pending is None or candidate is None:
                continue
            self._retry_alternate_with_current_auth(candidate, pending)

    def _retry_alternate_with_current_auth(
        self,
        candidate: BulkDisagreeCandidate,
        pending: _PendingAlternatePost,
    ) -> None:
        obs_id = candidate.observation.obs_id
        self._auth_waiting_obs_ids.discard(obs_id)
        if pending.optimistic_hide:
            self._optimistically_hidden_obs_ids.add(obs_id)
            self._hide_card_preserving_position(obs_id)
        status = self._status_labels.get(obs_id)
        if status:
            status.setText(
                f"Authentication refreshed; retrying alternate ID as {pending.post_taxon.name}…"
            )
        self._launch_alternate_post_worker(candidate, pending)

    def _on_reauthentication_failed(self, msg: str) -> None:
        self._reauthentication_in_progress = False
        self._api_token = ""
        self._login = ""
        waiting_obs_ids = [
            obs_id
            for obs_id in self._card_order
            if obs_id in self._auth_waiting_obs_ids
        ]
        self._auth_waiting_obs_ids.clear()
        for obs_id in waiting_obs_ids:
            self._pending_alternate_posts.pop(obs_id, None)
            self._restore_optimistically_hidden_card(obs_id)
            self._set_card_enabled(obs_id, True)
            status = self._status_labels.get(obs_id)
            if status:
                status.setText("Alternate ID not posted: " + msg)
        self._browser_status_label.setText(
            "Authentication was not refreshed; pending alternate IDs were not posted."
        )
        self._update_pending_post_state()

    def _restore_optimistically_hidden_card(self, obs_id: int) -> None:
        if obs_id not in self._optimistically_hidden_obs_ids:
            return
        self._optimistically_hidden_obs_ids.discard(obs_id)
        card = self._cards.get(obs_id)
        if card is not None and obs_id not in self._removed_obs_ids:
            card.setVisible(True)
            QTimer.singleShot(
                0,
                lambda c=card: self._scroll.ensureWidgetVisible(c, 0, 40),
            )

    def _update_pending_post_state(self) -> None:
        if self._done_btn is not None:
            self._done_btn.setEnabled(not self._pending_alternate_posts)
        self._update_count_label()

    def _set_card_enabled(self, obs_id: int, enabled: bool) -> None:
        buttons = self._card_buttons.get(obs_id, []) + self._quick_alt_buttons.get(obs_id, [])
        for button in buttons:
            button.setEnabled(enabled)

    def _remember_recent_alternate_taxon(self, recent: _RecentAlternateTaxon) -> bool:
        previous = list(self._recent_alternate_taxa)
        self._recent_alternate_taxa = [
            item for item in self._recent_alternate_taxa if item.taxon_id != recent.taxon_id
        ]
        self._recent_alternate_taxa.insert(0, recent)
        del self._recent_alternate_taxa[5:]
        return self._recent_alternate_taxa != previous

    def _refresh_quick_alternate_buttons_after(self, obs_id: int) -> None:
        seen_current = False
        for candidate_obs_id in self._card_order:
            if candidate_obs_id == obs_id:
                seen_current = True
                continue
            if not seen_current:
                continue
            if candidate_obs_id in self._removed_obs_ids:
                continue
            self._rebuild_quick_alternate_buttons(candidate_obs_id)

    def _rebuild_quick_alternate_buttons(self, obs_id: int) -> None:
        old_buttons = self._quick_alt_buttons.get(obs_id, [])
        for row in self._quick_alt_rows.get(obs_id, []):
            for index in range(row.count() - 1, -1, -1):
                item = row.itemAt(index)
                widget = item.widget() if item is not None else None
                if widget in old_buttons:
                    row.removeWidget(widget)
        for button in old_buttons:
            button.setParent(None)
            button.deleteLater()
        self._quick_alt_buttons[obs_id] = []
        if self._dry_run or not self._recent_alternate_taxa:
            return
        candidate = self._candidate_by_obs_id.get(obs_id)
        if candidate is None:
            return
        for row in self._quick_alt_rows.get(obs_id, []):
            insert_index = max(0, row.count() - 1)
            for recent in self._recent_alternate_taxa:
                button = QPushButton(f"ID as {recent.name}")
                tooltip = (
                    "Post this alternate ID immediately with a blank comment, "
                    "using the same disagreement setting as the previous use."
                )
                if recent.rank:
                    tooltip += f"\nTaxon rank: {recent.rank}"
                tooltip += f"\nTaxon id: {recent.taxon_id}"
                button.setToolTip(tooltip)
                button.setEnabled(obs_id not in self._pending_alternate_posts)
                button.clicked.connect(
                    lambda _checked=False, c=candidate, r=recent: (
                        self._post_quick_alternate(c, r)
                    )
                )
                row.insertWidget(insert_index, button)
                insert_index += 1
                self._quick_alt_buttons[obs_id].append(button)

    def _update_count_label(self) -> None:
        remaining = len(self.candidates())
        removed = len(self._removed_obs_ids)
        kept_hidden = len(self._kept_hidden_obs_ids)
        text = (
            f"{remaining} candidate(s) will start; {kept_hidden} kept and hidden; "
            f"{removed} removed in this browser."
        )
        pending = len(self._pending_alternate_posts)
        if pending:
            text += f" {pending} alternate ID(s) posting in the background."
        self._count_label.setText(text)
        self._undo_keep_btn.setEnabled(any(
            obs_id in self._kept_hidden_obs_ids and obs_id not in self._removed_obs_ids
            for obs_id in self._keep_undo_stack
        ))
        self._undo_skip_btn.setEnabled(any(
            obs_id in self._removed_obs_ids
            for obs_id, _was_skip_forever in self._skip_undo_stack
        ))
        self._show_kept_btn.setEnabled(kept_hidden > 0)


class BulkDisagreeKeptReviewDialog(QDialog):
    def __init__(
        self,
        candidates: List[BulkDisagreeCandidate],
        *,
        client: INatClient,
        disk_cache: ImageCache,
        on_skip: Callable[[BulkDisagreeCandidate], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Kept Bulk Disagree Observations")
        self.resize(1080, 760)
        self._candidates = list(candidates)
        self._client = client
        self._disk_cache = disk_cache
        self._on_skip = on_skip
        self._pool = QThreadPool.globalInstance()
        self._skipped_obs_ids: set[int] = set()
        self._cards: Dict[int, QFrame] = {}
        self._photo_labels: Dict[Tuple[int, int], QLabel] = {}
        self._photo_columns: Dict[Tuple[int, int], int] = {}
        self._live_image_signals: set[QObject] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "These observations are still kept in the planned run and hidden from the main "
            "browse window. Use Skip this run here only for observations you do not want "
            "included in the final bulk disagree run."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(12)
        for candidate in self._candidates:
            self._add_card(candidate)
        self._content_layout.addStretch(1)
        self._scroll.setWidget(content)
        layout.addWidget(self._scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn:
            close_btn.setText("Done")
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
        self._update_count_label()

    def skipped_count(self) -> int:
        return len(self._skipped_obs_ids)

    def _add_card(self, candidate: BulkDisagreeCandidate) -> None:
        obs = candidate.observation
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("QFrame { background: #ffffff; } QLabel { color: #000000; }")
        self._cards[obs.obs_id] = frame
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        title = QLabel(f"<b>Observation {obs.obs_id}</b> by {obs.observer_login}")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(title)

        meta = QLabel(
            " | ".join(
                [
                    f"Current: {candidate.current_observation_taxon_name or '(none)'}",
                    f"Community: {candidate.community_taxon_name or '(none)'}",
                    f"Location: {obs.place_guess or '(none)'}",
                    f"Planned target: {_format_taxon_with_rank(candidate.target_taxon_name, candidate.target_taxon_rank)}",
                    f"Explicit disagreement: {'yes' if candidate.explicit_disagreement else 'no'}",
                    f"DNA ITS: {'yes' if candidate.has_dna_barcode_its else 'no'}",
                ]
            )
        )
        meta.setWordWrap(True)
        layout.addWidget(meta)

        top_row = self._make_action_row(candidate)
        layout.addLayout(top_row)

        if not obs.photos:
            no_photo = QLabel("No photos on this observation.")
            no_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_photo.setMinimumHeight(120)
            layout.addWidget(no_photo)
        else:
            columns = 2 if len(obs.photos) > 1 else 1
            photo_grid = QGridLayout()
            photo_grid.setSpacing(8)
            photo_grid.setColumnStretch(0, 1)
            if columns > 1:
                photo_grid.setColumnStretch(1, 1)
            for index, photo in enumerate(obs.photos):
                photo_label = _HoldToZoomLabel(f"Loading photo {index + 1} of {len(obs.photos)}...")
                photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                photo_label.setMinimumHeight(240 if columns > 1 else 260)
                photo_label.setStyleSheet("QLabel { background: #111; color: #ddd; }")
                photo_grid.addWidget(photo_label, index // columns, index % columns)
                self._photo_labels[(obs.obs_id, photo.photo_id)] = photo_label
                self._photo_columns[(obs.obs_id, photo.photo_id)] = columns
                photo_label.set_original_fetcher(
                    lambda c=candidate, p=photo: self._load_original_photo(c, p)
                )
                self._load_photo(candidate, photo)
            layout.addLayout(photo_grid)

        bottom_row = self._make_action_row(candidate)
        layout.addLayout(bottom_row)
        self._content_layout.addWidget(frame)

    def _make_action_row(self, candidate: BulkDisagreeCandidate) -> QHBoxLayout:
        row = QHBoxLayout()
        skip_btn = QPushButton("Skip this run")
        open_btn = QPushButton("Open observation")
        copy_url_btn = QPushButton("Copy Obs URL")
        copy_url_btn.setToolTip("Copy this iNaturalist observation URL to the clipboard")
        skip_btn.clicked.connect(lambda _checked=False, c=candidate: self._skip_candidate(c))
        open_btn.clicked.connect(
            lambda _checked=False, c=candidate: open_external_url_silently(c.observation.url)
        )
        copy_url_btn.clicked.connect(
            lambda _checked=False, c=candidate: QApplication.clipboard().setText(c.observation.url)
        )
        row.addWidget(skip_btn)
        row.addWidget(open_btn)
        row.addWidget(copy_url_btn)
        row.addStretch(1)
        return row

    def _load_photo(self, candidate: BulkDisagreeCandidate, photo) -> None:
        target = _photo_fetch_target(photo, "large")
        if target is None:
            return
        size, url = target
        worker = _GalleryImageWorker(
            obs_id=candidate.observation.obs_id,
            photo_id=photo.photo_id,
            image_url=url,
            client=self._client,
            disk_cache=self._disk_cache,
            size=size,
        )
        sigs = worker.signals
        self._live_image_signals.add(sigs)
        sigs.loaded.connect(
            lambda obs_id, photo_id, image_data, s=sigs: (
                self._live_image_signals.discard(s),
                self._on_photo_loaded(obs_id, photo_id, image_data),
            )
        )
        sigs.failed.connect(
            lambda obs_id, photo_id, msg, s=sigs: (
                self._live_image_signals.discard(s),
                self._on_photo_failed(obs_id, photo_id, msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int, object)
    def _on_photo_loaded(self, obs_id: int, photo_id: int, image_data: bytes) -> None:
        label = self._photo_labels.get((obs_id, photo_id))
        if not label:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(image_data)
        if pixmap.isNull():
            label.setText("Could not load photo: downloaded image could not be decoded.")
            return
        if isinstance(label, _HoldToZoomLabel):
            label.set_full_pixmap(pixmap)
        columns = self._photo_columns.get((obs_id, photo_id), 1)
        max_width = 520 if columns > 1 else 920
        max_height = 540 if columns > 1 else 720
        scaled = pixmap.scaled(
            max_width,
            max_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        label.setMinimumHeight(max(180, scaled.height()))

    def _on_photo_failed(self, obs_id: int, photo_id: int, msg: str) -> None:
        label = self._photo_labels.get((obs_id, photo_id))
        if label:
            label.setText(f"Could not load photo: {msg}")

    def _load_original_photo(self, candidate: BulkDisagreeCandidate, photo) -> None:
        target = _photo_fetch_target(photo, "original")
        if target is None:
            return
        size, url = target
        if size != "original":
            # This photo url has no substitutable size token, so there is no
            # higher-resolution tier to upgrade to; what is already shown is
            # everything iNaturalist offers here.
            return
        worker = _GalleryImageWorker(
            obs_id=candidate.observation.obs_id,
            photo_id=photo.photo_id,
            image_url=url,
            client=self._client,
            disk_cache=self._disk_cache,
            size=size,
        )
        sigs = worker.signals
        self._live_image_signals.add(sigs)
        sigs.loaded.connect(
            lambda obs_id, photo_id, image_data, s=sigs: (
                self._live_image_signals.discard(s),
                self._on_original_loaded(obs_id, photo_id, image_data),
            )
        )
        sigs.failed.connect(
            lambda obs_id, photo_id, msg, s=sigs: self._live_image_signals.discard(s)
        )
        self._pool.start(worker)

    @Slot(int, int, object)
    def _on_original_loaded(self, obs_id: int, photo_id: int, image_data: bytes) -> None:
        label = self._photo_labels.get((obs_id, photo_id))
        if not isinstance(label, _HoldToZoomLabel):
            return
        pixmap = QPixmap()
        pixmap.loadFromData(image_data)
        if pixmap.isNull():
            return
        label.set_full_pixmap(pixmap, is_original=True)

    def _skip_candidate(self, candidate: BulkDisagreeCandidate) -> None:
        obs_id = candidate.observation.obs_id
        if obs_id in self._skipped_obs_ids:
            return
        self._skipped_obs_ids.add(obs_id)
        self._on_skip(candidate)
        card = self._cards.get(obs_id)
        if card:
            card.setVisible(False)
        self._update_count_label()

    def _update_count_label(self) -> None:
        remaining = len(self._candidates) - len(self._skipped_obs_ids)
        self._count_label.setText(
            f"{remaining} kept observation(s) still included in the planned run; "
            f"{len(self._skipped_obs_ids)} skipped from this review."
        )


class BulkDisagreePreviewDialog(QDialog):
    def __init__(
        self,
        candidates: List[BulkDisagreeCandidate],
        stats: BulkDisagreePlanStats,
        *,
        client: Optional[INatClient] = None,
        disk_cache: Optional[ImageCache] = None,
        api_token: str = "",
        login: str = "",
        require_source_taxon_match: bool = True,
        default_comment: str = "",
        on_skip_forever: Optional[Callable[[BulkDisagreeCandidate], None]] = None,
        on_unskip_forever: Optional[Callable[[BulkDisagreeCandidate], None]] = None,
        request_reauthentication: Optional[
            Callable[
                [Callable[[str, str], None], Callable[[str], None]],
                None,
            ]
        ] = None,
        dry_run: bool = False,
        window_title: str = "Preview Bulk Disagree to Taxon",
        photo_browser_title: str = "Browse Bulk Disagree Photos",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self._photo_browser_title = photo_browser_title
        self.resize(1220, 580)
        self._candidates = list(candidates)
        self._stats = stats
        self._back_requested = False
        self._client = client
        self._disk_cache = disk_cache
        self._api_token = api_token
        self._login = login
        self._require_source_taxon_match = require_source_taxon_match
        self._default_comment = default_comment
        self._on_skip_forever = on_skip_forever
        self._on_unskip_forever = on_unskip_forever
        self._request_reauthentication = request_reauthentication
        self._dry_run = dry_run

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        warning = QLabel(
            "Preview the affected observations before starting. Each observation will be refreshed "
            "again immediately before posting, and you can skip, pause, or cancel during execution."
        )
        if dry_run:
            warning.setText(warning.text() + " Dry run is enabled, so no writes will be posted.")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        stats_label = QLabel(_format_plan_stats(stats))
        stats_label.setWordWrap(True)
        layout.addWidget(stats_label)

        self._table = QTableWidget(0, 10)
        self._table.setHorizontalHeaderLabels(
            [
                "Observation ID",
                "URL",
                "Observer",
                "Current observation taxon",
                "Community taxon",
                "Source taxon from URL",
                "Target taxon to add",
                "DNA Barcode ITS present?",
                "Your current ID",
                "DQA vote planned?",
            ]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        enable_click_sorting(self._table)
        layout.addWidget(self._table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._back_btn = QPushButton("Back")
        self._back_btn.setToolTip("Return to the bulk disagree setup dialog.")
        self._browse_btn = QPushButton("Browse photos...")
        self._browse_btn.setEnabled(bool(self._client and self._disk_cache and self._candidates))
        self._browse_btn.clicked.connect(self._browse_photos)
        self._start_btn = QPushButton("Start")
        buttons.addButton(self._back_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._browse_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._start_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._back_btn.clicked.connect(self._go_back)
        self._start_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)
        self._start_btn.setDefault(False)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setDefault(True)
        self._populate_table()
        self._update_start_enabled()

    def candidates(self) -> List[BulkDisagreeCandidate]:
        return list(self._candidates)

    def back_requested(self) -> bool:
        return self._back_requested

    def _go_back(self) -> None:
        self._back_requested = True
        self.reject()

    def _populate_table(self) -> None:
        with sorting_suspended(self._table):
            self._table.setRowCount(len(self._candidates))
            for row, candidate in enumerate(self._candidates):
                obs = candidate.observation
                values = [
                    str(obs.obs_id),
                    obs.url,
                    obs.observer_login,
                    candidate.current_observation_taxon_name,
                    candidate.community_taxon_name,
                    _source_taxon_text(candidate),
                    f"{_format_taxon_with_rank(candidate.target_taxon_name, candidate.target_taxon_rank)} ({candidate.target_taxon_id})",
                    "Yes" if candidate.has_dna_barcode_its else "No",
                    candidate.user_current_taxon or "",
                    (
                        f"{_dqa_table_text(candidate)}; "
                        f"explicit disagreement: {'yes' if candidate.explicit_disagreement else 'no'}"
                    ),
                ]
                for col, value in enumerate(values):
                    self._table.setItem(row, col, SortableTableWidgetItem(value))
        self._table.resizeColumnsToContents()

    def _browse_photos(self) -> None:
        if not self._client or not self._disk_cache:
            return
        dlg = BulkDisagreePhotoBrowserDialog(
            self._candidates,
            client=self._client,
            disk_cache=self._disk_cache,
            api_token=self._api_token,
            login=self._login,
            require_source_taxon_match=self._require_source_taxon_match,
            default_comment=self._default_comment,
            dry_run=self._dry_run,
            on_skip_forever=self._on_skip_forever,
            on_unskip_forever=self._on_unskip_forever,
            request_reauthentication=(
                self._request_photo_browser_reauthentication
                if self._request_reauthentication is not None
                else None
            ),
            window_title=self._photo_browser_title,
            parent=self,
        )
        dlg.exec()
        self._candidates = dlg.candidates()
        self._populate_table()
        self._update_start_enabled()

    def _request_photo_browser_reauthentication(
        self,
        on_success: Callable[[str, str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        if self._request_reauthentication is None:
            on_failure("Authentication is unavailable from this window.")
            return

        def authenticated(api_token: str, login: str) -> None:
            self._api_token = api_token
            self._login = login
            on_success(api_token, login)

        def authentication_failed(msg: str) -> None:
            self._api_token = ""
            self._login = ""
            on_failure(msg)

        self._request_reauthentication(authenticated, authentication_failed)

    def _update_start_enabled(self) -> None:
        count = len(self._candidates)
        if self._browse_btn:
            self._browse_btn.setEnabled(bool(self._client and self._disk_cache and count))
        self._start_btn.setEnabled(count > 0)


class BulkDisagreeProgressDialog(QDialog):
    cancel_requested = Signal()
    skip_requested = Signal()
    skip_forever_requested = Signal()
    skip_delay_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    delay_changed = Signal(int, int)
    nav_key_pressed = Signal(object, object)

    _NAV_PASSTHROUGH_KEYS = {
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_BracketLeft,
        Qt.Key.Key_BracketRight,
    }

    def __init__(
        self,
        *,
        delay_min_seconds: int = 10,
        delay_max_seconds: int = 30,
        dry_run: bool = False,
        window_title: str = "Bulk Disagree to Taxon from URL",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(720, 560)
        self.setModal(False)
        self._finished = False
        self._paused = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        if dry_run:
            dry_label = QLabel("Dry run is enabled. No identifications or DQA votes will be posted.")
            dry_label.setWordWrap(True)
            dry_label.setStyleSheet("QLabel { color: #157f1f; font-weight: 700; }")
            layout.addWidget(dry_label)

        delay_row = QHBoxLayout()
        self._delay_label = QLabel("Delay:")
        self._delay_min_spin = QSpinBox()
        self._delay_min_spin.setRange(0, 3600)
        self._delay_min_spin.setSuffix("s min")
        self._delay_min_spin.setValue(delay_min_seconds)
        self._delay_max_spin = QSpinBox()
        self._delay_max_spin.setRange(0, 3600)
        self._delay_max_spin.setSuffix("s max")
        self._delay_max_spin.setValue(max(delay_min_seconds, delay_max_seconds))
        self._delay_min_spin.valueChanged.connect(self._on_delay_min_changed)
        self._delay_max_spin.valueChanged.connect(self._on_delay_max_changed)
        delay_row.addWidget(self._delay_label)
        delay_row.addWidget(self._delay_min_spin)
        delay_row.addWidget(self._delay_max_spin)
        delay_row.addStretch(1)
        layout.addLayout(delay_row)

        self._title = QLabel("Ready")
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        self._target_label = QLabel("")
        target_font = QFont()
        target_font.setPointSize(16)
        target_font.setBold(True)
        self._target_label.setFont(target_font)
        self._target_label.setWordWrap(True)
        layout.addWidget(self._target_label)

        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setAcceptRichText(False)
        layout.addWidget(self._details, 1)

        comment_label = QLabel("Comment to post with this identification (edit as needed):")
        layout.addWidget(comment_label)

        self._comment_edit = QTextEdit()
        self._comment_edit.setAcceptRichText(False)
        self._comment_edit.setFixedHeight(80)
        layout.addWidget(self._comment_edit)

        self._countdown = QLabel("")
        self._countdown.setWordWrap(True)
        layout.addWidget(self._countdown)

        row = QHBoxLayout()
        self._cancel_btn = QPushButton("Cancel")
        self._pause_btn = QPushButton("Pause")
        self._skip_btn = QPushButton("Skip this ID")
        self._skip_forever_btn = QPushButton("Skip forever")
        self._skip_forever_btn.setToolTip(
            "Skip this observation now and remember it for future bulk disagree-to-taxon runs."
        )
        self._skip_delay_btn = QPushButton("Post now / skip delay")
        self._cancel_btn.clicked.connect(self.cancel_requested)
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        self._skip_btn.clicked.connect(self.skip_requested)
        self._skip_forever_btn.clicked.connect(self.skip_forever_requested)
        self._skip_delay_btn.clicked.connect(self.skip_delay_requested)
        row.addWidget(self._cancel_btn)
        row.addWidget(self._pause_btn)
        row.addWidget(self._skip_btn)
        row.addWidget(self._skip_forever_btn)
        row.addWidget(self._skip_delay_btn)
        layout.addLayout(row)
        self._install_nav_passthrough_filter()

    def delay_min_seconds(self) -> int:
        return self._delay_min_spin.value()

    def delay_max_seconds(self) -> int:
        return self._delay_max_spin.value()

    def show_candidate(
        self,
        index: int,
        total: int,
        candidate: BulkDisagreeCandidate,
        *,
        comment: str = "",
    ) -> None:
        obs = candidate.observation
        self._title.setText(f"Item {index + 1} of {total}: observation {obs.obs_id}")
        self._target_label.setText(
            _format_taxon_with_rank(candidate.target_taxon_name, candidate.target_taxon_rank)
        )
        dna_line = (
            f"DNA Barcode ITS: {candidate.dna_barcode_its_value}"
            if candidate.dna_barcode_its_value
            else "DNA Barcode ITS: not present"
        )
        self._details.setPlainText(
            "\n".join(
                [
                    f"Observation: {obs.url}",
                    f"Observer: {obs.observer_login}",
                    f"Source taxon from URL: {_source_taxon_text(candidate)}",
                    f"Current observation taxon: {candidate.current_observation_taxon_name or '(none)'}",
                    f"Community taxon: {candidate.community_taxon_name or '(none)'}",
                    f"Target taxon to add: {_format_taxon_with_rank(candidate.target_taxon_name, candidate.target_taxon_rank)} ({candidate.target_taxon_id})",
                    f"Explicit disagreement flag: {'Yes' if candidate.explicit_disagreement else 'No'}",
                    dna_line,
                    f"DQA vote: {_dqa_table_text(candidate)}",
                    "",
                    "Status: ready",
                ]
            )
        )
        self._comment_edit.setPlainText(comment)
        self._countdown.setText("")
        self._paused = False
        self._pause_btn.setText("Pause")
        self._details.setFocus(Qt.FocusReason.OtherFocusReason)

    def get_comment(self) -> str:
        return self._comment_edit.toPlainText().strip()

    def set_status(self, status: str) -> None:
        text = self._details.toPlainText()
        prefix = text.split("\nStatus:", 1)[0]
        self._details.setPlainText(prefix + f"\nStatus: {status}")

    def set_countdown(self, seconds: int) -> None:
        if seconds <= 0:
            self._countdown.setText("Posting as soon as the API allows.")
            return
        self._countdown.setText(
            f"Posting this ID automatically in {seconds}s. Click 'Post now / skip delay' "
            "to post immediately, or 'Skip this ID' to pass."
        )

    def set_posting(self) -> None:
        self._skip_btn.setEnabled(False)
        self._skip_forever_btn.setEnabled(False)
        self._skip_delay_btn.setEnabled(False)
        # Keep Pause clickable while a write is in flight: with a 0s delay this is
        # the only window in which the user can pause the run. The current item
        # finishes; the pause takes effect before the next one.
        self._pause_btn.setEnabled(True)
        self._comment_edit.setEnabled(False)
        self._countdown.setText("")

    def set_waiting(self) -> None:
        self._skip_btn.setEnabled(True)
        self._skip_forever_btn.setEnabled(True)
        self._skip_delay_btn.setEnabled(True)
        self._pause_btn.setEnabled(True)
        self._comment_edit.setEnabled(True)

    def set_summary(
        self,
        *,
        posted_id: int,
        posted_id_and_dqa: int,
        posted_id_dqa_not_attempted: int,
        posted_id_dqa_skipped: int,
        posted_id_dqa_failed: int,
        skipped: int,
        changed: int,
        failed: int,
        ambiguous_write: int,
        cancelled: bool,
        plan_stats: Optional[BulkDisagreePlanStats] = None,
    ) -> None:
        state = "Cancelled" if cancelled else "Complete"
        self._finished = True
        self._title.setText(state)
        lines = [
            state,
            "",
            f"Posted ID: {posted_id}",
            f"Posted ID and DQA: {posted_id_and_dqa}",
            f"Posted ID; DQA not attempted: {posted_id_dqa_not_attempted}",
            f"Posted ID; DQA skipped: {posted_id_dqa_skipped}",
            f"Posted ID; DQA failed: {posted_id_dqa_failed}",
            f"Skipped: {skipped}",
            f"Changed before posting: {changed}",
            f"Failed: {failed}",
            f"Ambiguous write: {ambiguous_write}",
        ]
        if plan_stats is not None:
            lines.extend(["", _format_plan_stats(plan_stats)])
        if posted_id_dqa_not_attempted:
            lines.extend(["", DQA_DISABLED_MESSAGE])
        if posted_id_dqa_skipped:
            lines.extend([
                "",
                "DQA skipped means the ID was posted, but the refreshed observation did not "
                "show the target taxon as the community taxon, or current taxon when no "
                "community taxon exists, so no DQA vote was posted.",
            ])
        self._details.setPlainText("\n".join(lines))
        self._countdown.setText("")
        self._target_label.setText("")
        self._comment_edit.setPlainText("")
        self._comment_edit.setEnabled(False)
        self._cancel_btn.setText("Close")
        self._cancel_btn.clicked.disconnect(self.cancel_requested)
        self._cancel_btn.clicked.connect(self.accept)
        self._pause_btn.setVisible(False)
        self._skip_btn.setVisible(False)
        self._skip_forever_btn.setVisible(False)
        self._skip_delay_btn.setVisible(False)
        self._delay_label.setVisible(False)
        self._delay_min_spin.setVisible(False)
        self._delay_max_spin.setVisible(False)

    def _on_pause_clicked(self) -> None:
        self._paused = not self._paused
        if self._paused:
            self._pause_btn.setText("Resume")
            self.pause_requested.emit()
        else:
            self._pause_btn.setText("Pause")
            self.resume_requested.emit()

    def reflect_paused(self, paused: bool) -> None:
        """Sync the Pause/Resume button to a pause state set by the controller.

        Used when a new item is displayed while the run is still paused (e.g. the
        user paused during the previous post), without emitting pause/resume.
        """
        self._paused = paused
        self._pause_btn.setText("Resume" if paused else "Pause")

    def _on_delay_min_changed(self, value: int) -> None:
        if value > self._delay_max_spin.value():
            self._delay_max_spin.blockSignals(True)
            self._delay_max_spin.setValue(value)
            self._delay_max_spin.blockSignals(False)
        self.delay_changed.emit(self.delay_min_seconds(), self.delay_max_seconds())

    def _on_delay_max_changed(self, value: int) -> None:
        if value < self._delay_min_spin.value():
            self._delay_min_spin.blockSignals(True)
            self._delay_min_spin.setValue(value)
            self._delay_min_spin.blockSignals(False)
        self.delay_changed.emit(self.delay_min_seconds(), self.delay_max_seconds())

    def _install_nav_passthrough_filter(self) -> None:
        self.installEventFilter(self)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if self._forward_nav_key_if_allowed(event):
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._forward_nav_key_if_allowed(event):
            return
        super().keyPressEvent(event)

    def _forward_nav_key_if_allowed(self, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return False
        key = event.key()
        if key not in self._NAV_PASSTHROUGH_KEYS:
            return False
        if not self._focus_allows_nav_passthrough():
            return False
        self.nav_key_pressed.emit(key, event.modifiers())
        event.accept()
        return True

    def _focus_allows_nav_passthrough(self) -> bool:
        focus = self.focusWidget()
        if focus is None:
            return True
        if focus is self._comment_edit or self._comment_edit.isAncestorOf(focus):
            return False
        cls_name = type(focus).__name__
        if "SpinBox" in cls_name or "ComboBox" in cls_name:
            return False
        if cls_name in {"QLineEdit", "QPlainTextEdit"}:
            return False
        is_read_only = getattr(focus, "isReadOnly", None)
        return not (callable(is_read_only) and not is_read_only())

    def closeEvent(self, event) -> None:
        if not self._finished:
            self.cancel_requested.emit()
        super().closeEvent(event)


def _dqa_table_text(candidate: BulkDisagreeCandidate) -> str:
    if not candidate.dqa_vote_planned:
        return "No"
    if not DQA_POSTING_ENABLED:
        return "Requested (not attempted)"
    return "Yes (after community target match)"


def _source_taxon_text(candidate: BulkDisagreeCandidate) -> str:
    if not candidate.source_taxon_id:
        return "(no source taxon)"
    return f"{candidate.source_taxon_name} ({candidate.source_taxon_id})"


def _detect_image_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _is_auth_failure_message(msg: str) -> bool:
    text = msg.casefold()
    return (
        "http status: 401" in text
        or "http 401" in text
        or "need to sign in" in text
        or "missing inaturalist api token" in text
        or "unauthorized" in text
    )


def _bool_default(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _int_default(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _format_plan_stats(stats: BulkDisagreePlanStats) -> str:
    return (
        f"Scanned {stats.total_url_results_scanned} observation(s); "
        f"{stats.candidate_count} candidate(s). "
        f"Skipped: {stats.skipped_dna_barcode_its} DNA Barcode ITS, "
        f"{stats.skipped_missing_dna_barcode_its} missing DNA Barcode ITS, "
        f"{stats.skipped_already_target} already target ID, "
        f"{stats.skipped_source_no_match} source taxon changed, "
        f"{stats.skipped_permanent} permanent skip, "
        f"{stats.skipped_missing_invalid_data} missing/invalid data, "
        f"{stats.skipped_refresh_failure} refresh failure."
    )
