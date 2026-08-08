"""
Main application window.

Layout:
  ┌─────────────────────────────────────────────┐
  │  Filter Bar                                  │
  ├──────────┬──────────────────┬────────────────┤
  │ Result   │   Image Viewer   │  Metadata +    │
  │ List     │                  │  Taxon Tree    │
  │          │                  │                │
  └──────────┴──────────────────┴────────────────┘
  │  Status bar                                  │
  └──────────────────────────────────────────────┘

Keyboard shortcuts use QShortcut with WindowShortcut context,
plus an application-level event filter to intercept navigation
keys even when autocomplete or list widgets have focus.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Qt,
    QTimer,
    Signal,
    Slot,
    QEvent,
)
from PySide6.QtGui import (
    QAction,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.auth import AuthService, AuthState, normalise_token
from observation_workbench.api.client import INatAPIError
from observation_workbench.api.client import INatClient
from observation_workbench.api.parsers import parse_observation
from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    parse_observations_url,
)
from observation_workbench.models import StudyObservation
from observation_workbench.services.bulk_identification import (
    BulkAgreeCandidate,
    BulkAgreePlanResult,
    BulkAgreePlanStats,
    plan_provisional_candidates,
)
from observation_workbench.services.bulk_disagree import (
    DQA_POSTING_ENABLED,
    BulkDisagreeCandidate,
    BulkDisagreePlanResult,
    BulkDisagreeResult,
    observation_finished_at_target,
    plan_bulk_disagree_candidates,
    plan_propose_name_candidates,
    post_bulk_disagreement,
    taxon_is_strict_ancestor,
)
from observation_workbench.services.identification_actions import (
    AgreeResult,
    agree_with_consensus,
    agree_with_most_recent,
    already_current_taxon,
    build_agreement_comment,
    make_target_from_ident,
    most_recent_non_self_current_identification,
    needs_human_review,
    post_agreement,
    previously_withdrew_taxon,
    refresh_observation,
)
from observation_workbench.services.provisional_swap import (
    ProvisionalSwapPlan,
    ProvisionalSwapResult,
    plan_provisional_name_swap,
    swap_provisional_name,
)
from observation_workbench.services.species_override import (
    PROVISIONAL_SPECIES_FIELD_NAME,
    SPECIES_NAME_OVERRIDE_FIELD_NAME,
    SpeciesOverridePlan,
    SpeciesOverrideResult,
    plan_species_override_update,
    plan_species_override_update_for_observations,
    update_species_overrides,
)
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.services.identify_actions import IdentifyActionManager
from observation_workbench.services.prefetcher import ImagePrefetcher, ImageRequestMode
from observation_workbench.services.study_loader import LoadFilters, StudyLoader
from observation_workbench.reconciliation.coordinator import ReconciliationCoordinator
from observation_workbench.storage.cache_db import CacheDB
from observation_workbench.storage.settings import AppSettings
from observation_workbench.ui.filter_bar import FilterBar
from observation_workbench.ui.external_links import open_external_url_silently
from observation_workbench.ui.metadata_panel import MetadataPanel
from observation_workbench.models import StudyTaxon
from observation_workbench.ui.result_list import ResultList
from observation_workbench.ui.scroll_speed import ScrollSpeedFilter
from observation_workbench.ui.taxon_tree_panel import TaxonTreePanel
from observation_workbench.ui.viewer_panel import ViewerPanel

log = logging.getLogger(__name__)

FIRST_PAGE_SIZE = 30  # small first batch → results appear quickly
SUBSEQUENT_PAGE_SIZE = 100  # larger for "load more" batches
AUTO_LOAD_MARGIN = 15  # auto-fetch next page when within this many obs of the end
ARROW_REPEAT_INITIAL_DELAY_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Background loader worker
# ---------------------------------------------------------------------------


class _LoadSignals(QObject):
    page_loaded = Signal(list, int)  # (observations, total_results)
    error = Signal(str)


class _AuthSignals(QObject):
    authenticated = Signal(str, str)  # token, login
    error = Signal(str)


class _AuthWorker(QRunnable):
    def __init__(self, client: INatClient, token: str) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.token = token
        self.signals = _AuthSignals()

    def run(self) -> None:
        try:
            raw = self.client.get_current_user(self.token)
            login = _extract_login(raw)
            if not login:
                raise RuntimeError("Token validated, but no login was returned.")
            self.signals.authenticated.emit(self.token, login)
        except Exception as exc:
            self.signals.error.emit(str(exc))


class _ConfirmedRefreshSignals(QObject):
    loaded = Signal(object)
    failed = Signal(object)


@dataclass(frozen=True)
class _ConfirmedRefreshRequest:
    observation_id: int
    observation_uuid: str
    auth_generation: int


class _ConfirmedRefreshWorker(QRunnable):
    """One v1 detail GET after a durable action is confirmed."""

    def __init__(
        self, client: INatClient, request: _ConfirmedRefreshRequest, token: str
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._client = client
        self._request = request
        self._token = token
        self.signals = _ConfirmedRefreshSignals()

    def run(self) -> None:
        try:
            self.signals.loaded.emit(
                self._client.get_observation_by_id(
                    self._request.observation_id, self._token
                )
            )
        except Exception as exc:
            self.signals.failed.emit(exc)


class _AgreeSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class _AgreeWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        token: str,
        login: str,
        observation_id: int,
        mode: str,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.token = token
        self.login = login
        self.observation_id = observation_id
        self.mode = mode
        self.signals = _AgreeSignals()

    def run(self) -> None:
        try:
            if self.mode == "consensus":
                result = agree_with_consensus(
                    self.client, self.token, self.login, self.observation_id
                )
            else:
                result = agree_with_most_recent(
                    self.client, self.token, self.login, self.observation_id
                )
            self.signals.finished.emit(result)
        except Exception as exc:
            self.signals.error.emit(_format_api_error(exc))


class _BulkPlanSignals(QObject):
    planned = Signal(object)  # BulkAgreePlanResult
    progress = Signal(int, int)
    error = Signal(str)


class _BulkPlanWorker(QRunnable):
    def __init__(
        self,
        loader: StudyLoader,
        filters: LoadFilters,
        login: str,
        generation: int,
        get_gen,
        options: Optional[dict] = None,
        api_token: str = "",
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.loader = loader
        self.filters = filters
        self.login = login
        self.generation = generation
        self.get_gen = get_gen
        self.options = options or {}
        self.api_token = api_token
        self.signals = _BulkPlanSignals()

    def run(self) -> None:
        try:
            result = plan_provisional_candidates(
                self.loader,
                self.filters,
                self.login,
                api_token=self.api_token,
                max_observations=self.options.get("max_observations"),
                require_dna_barcode_its=self.options.get(
                    "require_dna_barcode_its", True
                ),
                only_if_needed=self.options.get("only_if_needed", True),
                # The URL results are re-scanned on every run, so a cached page
                # could hide a provisional ID added since the last load.
                use_cache=False,
                is_cancelled=lambda: self.get_gen() != self.generation,
                progress=lambda seen, total: self.signals.progress.emit(seen, total),
            )
            if self.get_gen() == self.generation:
                self.signals.planned.emit(result)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _BulkPostSignals(QObject):
    finished = Signal(object, object)
    error = Signal(object, str)


class _BulkPostWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        token: str,
        login: str,
        candidate: BulkAgreeCandidate,
        body: str = "",
        allow_changed_target: bool = False,
        dry_run: bool = False,
        only_if_needed: bool = True,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.token = token
        self.login = login
        self.candidate = candidate
        self.body = body
        self.allow_changed_target = allow_changed_target
        self.dry_run = dry_run
        self.only_if_needed = only_if_needed
        self.signals = _BulkPostSignals()

    def run(self) -> None:
        try:
            fresh = refresh_observation(
                self.client,
                self.token,
                self.candidate.observation.obs_id,
            )
            if fresh is None:
                self.signals.finished.emit(
                    self.candidate,
                    AgreeResult(
                        "skipped", "Could not refresh observation before posting."
                    ),
                )
                return
            ident = most_recent_non_self_current_identification(
                fresh,
                self.login,
                provisional_only=True,
            )
            if ident is None:
                self.signals.finished.emit(
                    self.candidate,
                    AgreeResult(
                        "skipped",
                        "No current non-self provisional identification remains.",
                        refreshed_observation=fresh,
                    ),
                )
                return
            target = make_target_from_ident(fresh, ident)
            taxon_changed = target.taxon_id != self.candidate.target.taxon_id
            source_changed_only = (
                target.source_ident_id != self.candidate.target.source_ident_id
                and not taxon_changed
            )
            if source_changed_only:
                log.info(
                    "Bulk provisional source identification changed without taxon change "
                    "obs=%s preview_source=%s preview_ident_id=%s current_source=%s current_ident_id=%s taxon=%s taxon_id=%s",
                    self.candidate.observation.obs_id,
                    self.candidate.target.source_login or "unknown",
                    self.candidate.target.source_ident_id,
                    target.source_login or "unknown",
                    target.source_ident_id,
                    target.taxon_name,
                    target.taxon_id,
                )
            if previously_withdrew_taxon(fresh, self.login, target.taxon_id):
                self.signals.finished.emit(
                    self.candidate,
                    AgreeResult(
                        "skipped",
                        "Skipped because you previously withdrew this provisional ID.",
                        target=target,
                        refreshed_observation=fresh,
                    ),
                )
                return
            if self.only_if_needed and observation_finished_at_target(
                fresh, target.taxon_id
            ):
                self.signals.finished.emit(
                    self.candidate,
                    AgreeResult(
                        "skipped",
                        f"Skipped because already Research Grade for {target.taxon_name}.",
                        target=target,
                        refreshed_observation=fresh,
                    ),
                )
                return
            if taxon_changed and not self.allow_changed_target:
                self.signals.finished.emit(
                    self.candidate,
                    AgreeResult(
                        "changed",
                        "The current provisional target taxon changed after refresh.",
                        target=target,
                        refreshed_observation=fresh,
                    ),
                )
                return
            if self.dry_run:
                self.signals.finished.emit(
                    self.candidate,
                    AgreeResult(
                        "skipped",
                        (
                            f"Dry run: would agree with {target.taxon_name} on "
                            f"observation {fresh.obs_id}."
                        ),
                        target=target,
                        refreshed_observation=fresh,
                    ),
                )
                return
            result = post_agreement(
                self.client, self.token, self.login, fresh, target, self.body
            )
            self.signals.finished.emit(self.candidate, result)
        except Exception as exc:
            self.signals.error.emit(self.candidate, _format_api_error(exc))


class _BulkDisagreePlanSignals(QObject):
    planned = Signal(object)
    progress = Signal(int, int)
    error = Signal(str)


class _BulkDisagreePlanWorker(QRunnable):
    def __init__(
        self,
        loader: StudyLoader,
        observation_query,
        token: str,
        login: str,
        generation: int,
        get_gen,
        options: dict,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.loader = loader
        self.observation_query = observation_query
        self.token = token
        self.login = login
        self.generation = generation
        self.get_gen = get_gen
        self.options = options
        self.signals = _BulkDisagreePlanSignals()

    def run(self) -> None:
        try:
            result = plan_bulk_disagree_candidates(
                self.loader,
                self.observation_query,
                self.login,
                self.token,
                source_taxon_id=self.options["source_taxon_id"],
                source_taxon_name=self.options["source_taxon_name"],
                target_taxon_id=self.options["target_taxon_id"],
                target_taxon_name=self.options["target_taxon_name"],
                target_taxon_rank=self.options.get("target_taxon_rank", ""),
                source_provisional_name=self.options.get("source_provisional_name", ""),
                skip_with_dna_barcode_its=self.options["skip_with_dna_barcode_its"],
                only_with_dna_barcode_its=self.options.get(
                    "only_with_dna_barcode_its", False
                ),
                require_source_taxon_match=self.options["require_source_taxon_match"],
                max_observations=self.options["max_observations"],
                dqa_vote_planned=self.options["dqa_vote_planned"],
                explicit_disagreement=self.options.get("explicit_disagreement", True),
                is_cancelled=lambda: self.get_gen() != self.generation,
                progress=lambda seen, total: self.signals.progress.emit(seen, total),
            )
            if self.get_gen() == self.generation:
                self.signals.planned.emit(result)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _BulkDisagreePostSignals(QObject):
    finished = Signal(object, object)
    error = Signal(object, str)


class _BulkDisagreePostWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        token: str,
        login: str,
        candidate: BulkDisagreeCandidate,
        body: str,
        options: dict,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.token = token
        self.login = login
        self.candidate = candidate
        self.body = body
        self.options = options
        self.signals = _BulkDisagreePostSignals()

    def run(self) -> None:
        try:
            result = post_bulk_disagreement(
                self.client,
                self.token,
                self.login,
                self.candidate,
                body=self.body,
                skip_with_dna_barcode_its=self.options["skip_with_dna_barcode_its"],
                only_with_dna_barcode_its=self.options.get(
                    "only_with_dna_barcode_its", False
                ),
                require_source_taxon_match=self.options["require_source_taxon_match"],
                dry_run=self.options["dry_run"],
                dqa_posting_enabled=DQA_POSTING_ENABLED,
                explicit_disagreement=self.options.get("explicit_disagreement", True),
            )
            self.signals.finished.emit(self.candidate, result)
        except Exception as exc:
            self.signals.error.emit(self.candidate, _format_api_error(exc))


class _ProposeNamePlanSignals(QObject):
    planned = Signal(object)
    progress = Signal(int, int)
    error = Signal(str)


class _ProposeNamePlanWorker(QRunnable):
    def __init__(
        self,
        loader: StudyLoader,
        token: str,
        login: str,
        generation: int,
        get_gen,
        options: dict,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.loader = loader
        self.token = token
        self.login = login
        self.generation = generation
        self.get_gen = get_gen
        self.options = options
        self.signals = _ProposeNamePlanSignals()

    def run(self) -> None:
        try:
            result = plan_propose_name_candidates(
                self.loader,
                self.login,
                self.token,
                observation_ids=self.options["observation_ids"],
                target_taxon_id=self.options["target_taxon_id"],
                target_taxon_name=self.options["target_taxon_name"],
                target_taxon_rank=self.options.get("target_taxon_rank", ""),
                is_cancelled=lambda: self.get_gen() != self.generation,
                progress=lambda seen, total: self.signals.progress.emit(seen, total),
            )
            if self.get_gen() == self.generation:
                self.signals.planned.emit(result)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _ProvisionalSwapPlanSignals(QObject):
    planned = Signal(object)
    progress = Signal(int, int)
    error = Signal(str)


class _ProvisionalSwapPlanWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        source_name: str,
        generation: int,
        get_gen,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.source_name = source_name
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _ProvisionalSwapPlanSignals()

    def run(self) -> None:
        try:
            plan = plan_provisional_name_swap(
                self.client,
                self.source_name,
                is_cancelled=lambda: self.get_gen() != self.generation,
                progress=lambda seen, total: self.signals.progress.emit(seen, total),
            )
            if self.get_gen() == self.generation:
                self.signals.planned.emit(plan)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _ProvisionalSwapPostSignals(QObject):
    progress = Signal(int, int, int, int, int, str)
    finished = Signal(object)
    error = Signal(str)


class _ProvisionalSwapPostWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        token: str,
        plan: ProvisionalSwapPlan,
        destination_name: str,
        generation: int,
        get_gen,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.token = token
        self.plan = plan
        self.destination_name = destination_name
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _ProvisionalSwapPostSignals()

    def run(self) -> None:
        try:
            result = swap_provisional_name(
                self.client,
                self.token,
                self.plan,
                self.destination_name,
                is_cancelled=lambda: self.get_gen() != self.generation,
                progress=lambda current, total, updated, skipped, failed, message: (
                    self.signals.progress.emit(
                        current,
                        total,
                        updated,
                        skipped,
                        failed,
                        message,
                    )
                ),
            )
            if self.get_gen() == self.generation:
                self.signals.finished.emit(result)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _SpeciesOverridePlanSignals(QObject):
    planned = Signal(object)
    progress = Signal(int, int)
    error = Signal(str)


class _SpeciesOverridePlanWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        provisional_name: str,
        override_name: str,
        source_mode: str,
        observation_ids: list[int],
        genus_filter: str,
        target_field_name: str,
        generation: int,
        get_gen,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.provisional_name = provisional_name
        self.override_name = override_name
        self.source_mode = source_mode
        self.observation_ids = observation_ids
        self.genus_filter = genus_filter
        self.target_field_name = target_field_name
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _SpeciesOverridePlanSignals()

    def run(self) -> None:
        try:
            if self.source_mode == "observations":
                plan = plan_species_override_update_for_observations(
                    self.client,
                    self.observation_ids,
                    self.override_name,
                    target_field_name=self.target_field_name,
                    genus_filter=self.genus_filter,
                    is_cancelled=lambda: self.get_gen() != self.generation,
                    progress=lambda seen, total: self.signals.progress.emit(
                        seen, total
                    ),
                )
            else:
                plan = plan_species_override_update(
                    self.client,
                    self.provisional_name,
                    self.override_name,
                    target_field_name=self.target_field_name,
                    genus_filter=self.genus_filter,
                    is_cancelled=lambda: self.get_gen() != self.generation,
                    progress=lambda seen, total: self.signals.progress.emit(
                        seen, total
                    ),
                )
            if self.get_gen() == self.generation:
                self.signals.planned.emit(plan)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _SpeciesOverridePostSignals(QObject):
    progress = Signal(int, int, int, int, int, str)
    finished = Signal(object)
    error = Signal(str)


class _SpeciesOverridePostWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        token: str,
        plan: SpeciesOverridePlan,
        selected_observation_ids: list[int],
        generation: int,
        get_gen,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.token = token
        self.plan = plan
        self.selected_observation_ids = selected_observation_ids
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _SpeciesOverridePostSignals()

    def run(self) -> None:
        try:
            result = update_species_overrides(
                self.client,
                self.token,
                self.plan,
                self.selected_observation_ids,
                is_cancelled=lambda: self.get_gen() != self.generation,
                progress=lambda current, total, changed, skipped, failed, message: (
                    self.signals.progress.emit(
                        current,
                        total,
                        changed,
                        skipped,
                        failed,
                        message,
                    )
                ),
            )
            if self.get_gen() == self.generation:
                self.signals.finished.emit(result)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(_format_api_error(exc))


class _ObservationRefreshSignals(QObject):
    refreshed = Signal(int, object)
    error = Signal(int, str)


class _ObservationRefreshWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        observation_id: int,
        index: int,
        generation: int,
        get_gen,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.observation_id = observation_id
        self.index = index
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _ObservationRefreshSignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            obs = refresh_observation(self.client, "", self.observation_id)
            if obs is not None and self.get_gen() == self.generation:
                self.signals.refreshed.emit(self.index, obs)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(self.index, _format_api_error(exc))


class _RateLimitSignals(QObject):
    """Emits cross-thread status messages when the API client is rate-limited."""

    status = Signal(str)


class _LoadWorker(QRunnable):
    def __init__(
        self,
        loader: StudyLoader,
        filters: LoadFilters,
        page: int,
        per_page: int,
        generation: int,
        get_gen,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.loader = loader
        self.filters = filters
        self.page = page
        self.per_page = per_page
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _LoadSignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            obs, total = self.loader.load_page(
                filters=self.filters,
                page=self.page,
                per_page=self.per_page,
                generation=self.generation,
                is_cancelled=lambda: self.get_gen() != self.generation,
            )
            if self.get_gen() == self.generation:
                self.signals.page_loaded.emit(obs, total)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Navigation key interceptor (application event filter)
# ---------------------------------------------------------------------------


def _has_command_modifier(modifiers) -> bool:
    """True when Ctrl, Alt or Meta is held (Shift alone does not count)."""
    try:
        return bool(
            modifiers
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        )
    except (TypeError, AttributeError):
        return False


class _NavFilter(QObject):
    """
    Intercepts navigation key presses at the application level.
    Passes them to the main window's handle_nav_key() method.
    Does NOT intercept when focus is in a text-entry widget.
    """

    INPUT_TYPES = (
        "QLineEdit",
        "QTextEdit",
        "QPlainTextEdit",
        "QSpinBox",
        "QDoubleSpinBox",
        "QDateEdit",
        "QTimeEdit",
        "QDateTimeEdit",
    )

    NAV_KEYS = {
        Qt.Key.Key_Left,
        Qt.Key.Key_Right,
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_Space,
        Qt.Key.Key_BracketLeft,
        Qt.Key.Key_BracketRight,
        Qt.Key.Key_G,
        Qt.Key.Key_O,
        Qt.Key.Key_I,
        Qt.Key.Key_L,
        Qt.Key.Key_R,
        Qt.Key.Key_F,
        Qt.Key.Key_A,
    }

    def __init__(self, main_window: "MainWindow") -> None:
        super().__init__(main_window)
        self._mw = main_window

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.ApplicationDeactivate,
            QEvent.Type.WindowDeactivate,
        ):
            self._mw.stop_arrow_navigation("window deactivated")
            return False

        # This filter is application-wide; never steal keys from another
        # top-level workflow (notably the separate Identify window).
        if QApplication.activeWindow() is not self._mw:
            return False
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        from PySide6.QtGui import QKeyEvent

        ke: QKeyEvent = event  # type: ignore[assignment]
        key = ke.key()
        if key not in self.NAV_KEYS:
            return False
        if _has_command_modifier(ke.modifiers()):
            # Shift is meaningful here (Shift+Space, A vs a), but Ctrl/Alt/Meta
            # belong to real shortcuts: Ctrl+A must stay select-all, not post an
            # identification.
            return False
        if event.type() == QEvent.Type.KeyRelease:
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                return self._mw.handle_arrow_key_release(key, ke.isAutoRepeat())
            return False
        # Don't intercept if focus is in a text input
        focus = QApplication.focusWidget()
        if focus is not None:
            cls_name = type(focus).__name__
            if cls_name in self.INPUT_TYPES:
                return False
            # Also skip if it's inside a QComboBox pop-up
            if "ComboBox" in cls_name:
                return False
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            return self._mw.handle_arrow_key_press(
                key,
                ke.modifiers(),
                ke.isAutoRepeat(),
            )
        # Pass to main window
        return self._mw.handle_nav_key(key, ke.modifiers())


def _fit_window_to_available_screen(widget: QWidget) -> None:
    """Shrink and reposition a window so it fits within its screen.

    Guards against windows that open larger than the display, or positioned so
    that the title bar / close button falls off-screen and the user can no
    longer move or close them. Maximized/fullscreen windows are left alone.
    """
    try:
        if widget is None or not widget.isWindow():
            return
        if widget.isMaximized() or widget.isFullScreen() or widget.isMinimized():
            return
        screen = widget.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        geo = widget.geometry()
        frame = widget.frameGeometry()
        # Window-decoration thickness (0 on WMs that don't report it yet).
        extra_w = max(0, frame.width() - geo.width())
        extra_h = max(0, frame.height() - geo.height())
        margin = 8
        max_w = max(1, avail.width() - extra_w - margin)
        max_h = max(1, avail.height() - extra_h - margin)
        if geo.width() > max_w or geo.height() > max_h:
            widget.resize(min(geo.width(), max_w), min(geo.height(), max_h))
            geo = widget.geometry()
            frame = widget.frameGeometry()
        # Keep the whole frame on-screen, prioritizing the top-left so the title
        # bar stays reachable.
        left_deco = geo.x() - frame.x()
        top_deco = geo.y() - frame.y()
        nx = min(frame.x(), avail.right() - frame.width() + 1)
        ny = min(frame.y(), avail.bottom() - frame.height() + 1)
        nx = max(nx, avail.left())
        ny = max(ny, avail.top())
        if nx != frame.x() or ny != frame.y():
            widget.move(nx + left_deco, ny + top_deco)
    except RuntimeError:
        # The underlying C++ window was deleted before the deferred fit ran.
        return


class _ScreenFitFilter(QObject):
    """Clamps dialogs and the main window to the available screen when shown."""

    def __init__(self, main_window: "MainWindow") -> None:
        super().__init__(main_window)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, (QDialog, QMainWindow)):
            # Defer so the window's final geometry (and frame) is in place.
            QTimer.singleShot(0, lambda w=obj: _fit_window_to_available_screen(w))
        return False


class _ElidedStatusLabel(QLabel):
    """Status label that cannot force the main window wider than the screen."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_elided_text()

    def text(self) -> str:
        """Return the untruncated text.

        QLabel.text() would return the elided string. Callers that read the
        status back, edit it and set it again (photo counter, taxon-summary
        suffix) would then make the ellipsis permanent.
        """
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        width = self.contentsRect().width()
        if width <= 0:
            visible_text = ""
        else:
            metrics = QFontMetrics(self.font())
            visible_text = metrics.elidedText(
                self._full_text,
                Qt.TextElideMode.ElideMiddle,
                width,
            )
        if QLabel.text(self) != visible_text:
            QLabel.setText(self, visible_text)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, *, skip_agree_confirmation: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("Observation Workbench")
        # An explicit minimum overrides the layout's minimumSizeHint. Without
        # it, a maximized window whose layout minimum exceeds the screen (easy
        # at QT_SCALE_FACTOR=2 on a scaled WSLg display) commits a buffer
        # larger than the compositor configured — a fatal Wayland protocol
        # error that kills the app on startup.
        self.setMinimumSize(640, 400)
        self._apply_default_geometry()
        self._watched_screen: Optional[QScreen] = None
        self._screen_watch_installed = False
        self._skip_agree_confirmation = skip_agree_confirmation

        # Core dependencies
        self._settings = AppSettings()
        self._db = self._init_db()
        self._client = INatClient(calls_per_second=1.0)
        self._auth_service = AuthService(self._settings)
        self._auth_state: AuthState = self._auth_service.load()
        # ReconciliationCoordinator constructs ReconciliationDB, which runs the
        # schema migration chain in its constructor. Several migrations refuse
        # to proceed when they find provenance they cannot prove safe (e.g. a
        # legacy account identity v15 cannot normalize) and raise. Refusing is
        # deliberate, but it must not take the whole application down: the
        # transaction rolls back, so user_version never advances and every
        # later launch would raise again, leaving the app permanently
        # unstartable with no way in to diagnose it. Degrade to "reconciliation
        # unavailable" instead and surface the reason when it is opened.
        self._reconciliation: Optional[ReconciliationCoordinator] = None
        self._reconciliation_error = ""
        try:
            self._reconciliation = ReconciliationCoordinator(
                self._client,
                lambda: self._auth_state,
                self._settings,
                self,
            )
        except Exception as exc:
            log.exception("Reconciliation subsystem unavailable")
            self._reconciliation_error = str(exc)
        self._identify_actions = IdentifyActionManager(
            self._client,
            self._db,
            lambda: self._auth_state,
            self,
        )
        # Gate 1D: iNaturalist name proposals delegate to the shared Identify
        # subsystem, so the reconciliation coordinator enqueues into this exact
        # application-scoped manager rather than a private one.
        if self._reconciliation is not None:
            self._reconciliation.set_identify_manager(self._identify_actions)
        # Separate client for taxon summary so its requests don't share the
        # rate-limiter with the main identification fetching client.
        self._summary_client = INatClient(calls_per_second=1.0)
        self._disk_cache = self._init_image_cache()
        self._loader = StudyLoader(self._client, self._db)

        # State
        self._loaded_observations: List[StudyObservation] = []
        self._observations: List[StudyObservation] = []
        self._current_obs_idx: int = -1
        self._current_photo_idx: int = 0
        self._display_obs_id: Optional[int] = None
        self._display_photo_id: Optional[int] = None
        self._total_results: int = 0
        self._current_page: int = 1
        # The API paginates by offset (page - 1) * per_page, so the page number
        # only means anything alongside the size it was requested with.
        self._page_size: int = FIRST_PAGE_SIZE
        # Highest raw-row offset requested so far, i.e. page * per_page. Unlike
        # len(self._loaded_observations) this counts rows the client-side
        # filters dropped, so it is the only sound "is there more?" test.
        self._rows_requested: int = 0
        self._loaded_obs_ids: set[int] = set()
        self._pending_summary_mode: str = ""
        self._pending_summary_kwargs: Optional[dict] = None
        self._generation: int = 0
        self._provisional_swap_generation: int = 0
        self._species_override_generation: int = 0
        self._is_loading: bool = False
        self._last_scroll_load_count: int = 0
        self._provisional_name_only: bool = self._settings.provisional_name_only
        self._suppress_auto_load: bool = False

        # IMPORTANT: keep _LoadWorker signal objects alive until callbacks fire.
        # Python GCs the worker (QRunnable, not QObject) after pool.start() returns,
        # which would also GC worker.signals before the queued signal fires.
        self._live_load_signals: set = set()
        self._live_auth_signals: set = set()
        self._live_agree_signals: set = set()
        self._live_bulk_signals: set = set()
        self._live_refresh_signals: set = set()
        self._detail_refreshed_obs_ids: set[int] = set()
        self._detail_refresh_in_flight: set[int] = set()
        self._bulk_candidates: List[BulkAgreeCandidate] = []
        self._bulk_index = 0
        self._bulk_posted = 0
        self._bulk_skipped = 0
        self._bulk_failed = 0
        self._bulk_delay_remaining = 0
        self._bulk_delay_timer: Optional[QTimer] = None
        self._bulk_dialog = None
        self._bulk_resume_after_auth = False
        self._bulk_deferred_review_keys: set[tuple[int, int]] = set()
        self._bulk_agree_options: dict = {}
        self._pending_target_obs_id: Optional[int] = None
        self._pending_target_taxon: str = ""
        self._disagree_candidates: List[BulkDisagreeCandidate] = []
        self._disagree_index = 0
        self._disagree_posted_id = 0
        self._disagree_posted_id_and_dqa = 0
        self._disagree_posted_id_dqa_not_attempted = 0
        self._disagree_posted_id_dqa_skipped = 0
        self._disagree_posted_id_dqa_failed = 0
        self._disagree_skipped = 0
        self._disagree_changed = 0
        self._disagree_failed = 0
        self._disagree_ambiguous_write = 0
        self._disagree_delay_remaining = 0
        self._disagree_delay_timer: Optional[QTimer] = None
        self._disagree_dialog = None
        self._disagree_resume_after_auth = False
        self._disagree_cancelled = False
        self._disagree_paused = False
        self._disagree_posting = False
        self._disagree_default_comment = ""
        self._disagree_options: dict = {}
        self._disagree_plan_stats = None
        self._identify_windows: set = set()
        self._reconciliation_windows: set = set()
        self._pending_identify_actions_dialog = None
        self._identify_refresh_live_signals: set[_ConfirmedRefreshSignals] = set()
        self._identify_refresh_in_flight: dict[int, _ConfirmedRefreshRequest] = {}
        self._identify_refresh_pending: dict[int, _ConfirmedRefreshRequest] = {}
        self._identify_refresh_warnings: dict[int, tuple[str, str]] = {}
        self._identify_refresh_auth_generation = 0
        self._startup_recovery_prompt_scheduled = False
        self._startup_recovery_prompt_shown = False
        self._closing = False
        self._held_arrow_keys: set[Qt.Key] = set()
        self._active_arrow_key: Optional[Qt.Key] = None
        self._navigation_repeat_rate = self._settings.navigation_repeat_rate
        self._navigation_repeat_deadline = 0.0
        self._navigation_repeat_steps = 0
        self._navigation_repeat_started_at = 0.0
        self._navigation_repeat_missed_beats = 0
        self._navigation_repeat_max_work_seconds = 0.0
        self._navigation_repeat_timer = QTimer(self)
        self._navigation_repeat_timer.setSingleShot(True)
        self._navigation_repeat_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._navigation_repeat_timer.timeout.connect(self._repeat_arrow_navigation)
        self._display_requested_at = 0.0

        # Prefetcher
        mem_bytes = self._settings.memory_cache_max_mb * 1024 * 1024
        self._prefetcher = ImagePrefetcher(
            client=self._client,
            disk_cache=self._disk_cache,
            max_memory_bytes=mem_bytes,
            parent=self,
        )
        self._prefetcher.image_ready.connect(self._on_prefetch_ready)

        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(6)

        # Capture the unscaled system font size before any scaling is applied.
        self._system_font_pt = QApplication.font().pointSize()
        if self._system_font_pt <= 0:
            self._system_font_pt = 10  # fallback for pixel-size fonts

        self._build_ui()
        self._wire_rate_limit_feedback()
        self._build_menu()
        self._identify_actions.summary_changed.connect(
            self._identify_actions_summary_changed
        )
        self._identify_actions.running_changed.connect(
            self._identify_actions_running_changed
        )
        self._identify_actions.paused.connect(self._identify_actions_paused)
        self._identify_actions.action_changed.connect(self._identify_actions_changed)
        self._identify_actions.authentication_context_changed.connect(
            self._identify_actions_authentication_changed
        )
        self._identify_actions.observation_refresh_requested.connect(
            self._request_confirmed_observation_refresh
        )
        self._update_identify_actions_ui()
        self._install_nav_filter()
        self._install_scroll_speed_filter()
        self._install_screen_fit_filter()
        self._install_dialog_focus_recovery()
        self._restore_state()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _wire_rate_limit_feedback(self) -> None:
        """Connect API client rate-limit callbacks to the status bar (cross-thread safe)."""
        self._rl_signals = _RateLimitSignals(self)
        self._rl_signals.status.connect(self._status_label.setText)

        def _rl_callback(status_code: int, wait_s: float) -> None:
            self._rl_signals.status.emit(
                f"Rate limited ({status_code}) — retrying in {wait_s:.0f}s…"
            )

        self._client.on_rate_limited = _rl_callback
        self._summary_client.on_rate_limited = _rl_callback

    # ------------------------------------------------------------------
    # Window geometry / screen tracking
    # ------------------------------------------------------------------

    def _apply_default_geometry(self) -> None:
        """Set the unmaximized (restore) size from the actual screen size.

        The window itself is shown maximized on startup; this size is what
        the user gets when they unmaximize.
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1400, 900)
            return
        avail = screen.availableGeometry()
        self.resize(
            min(1400, int(avail.width() * 0.9)),
            min(900, int(avail.height() * 0.9)),
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._screen_watch_installed:
            handle = self.windowHandle()
            if handle is not None:
                self._screen_watch_installed = True
                handle.screenChanged.connect(self._on_screen_changed)
                self._on_screen_changed(handle.screen())
        if not self._startup_recovery_prompt_scheduled:
            self._startup_recovery_prompt_scheduled = True
            QTimer.singleShot(0, self._show_startup_identify_recovery_prompt)

    def _on_screen_changed(self, screen: Optional[QScreen]) -> None:
        if self._watched_screen is not None:
            try:
                self._watched_screen.availableGeometryChanged.disconnect(
                    self._on_screen_geometry_changed
                )
            except (RuntimeError, TypeError):
                pass
        self._watched_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._on_screen_geometry_changed)

    def _on_screen_geometry_changed(self, *_args) -> None:
        # WSLg re-negotiates the virtual display on suspend/resume, so the
        # screen geometry can change under a running app. Defer the refit so
        # Qt finishes updating its screen state first.
        QTimer.singleShot(0, self._refit_to_screen)

    def _refit_to_screen(self) -> None:
        try:
            if self.isMinimized():
                return
            if self.isMaximized() or self.isFullScreen():
                # The compositor resizes maximized surfaces on output changes;
                # a client must not setGeometry() while maximized (fatal
                # protocol error on Wayland).
                return
            _fit_window_to_available_screen(self)
        except RuntimeError:
            pass

    def _init_db(self) -> CacheDB:
        cache_dir = self._settings.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_path = cache_dir / "metadata.db"
        return CacheDB(db_path)

    def _init_image_cache(self) -> ImageCache:
        cache_dir = self._settings.cache_dir / "images"
        cache_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = int(self._settings.cache_max_gb * 1024**3)
        return ImageCache(cache_dir, self._db, max_bytes)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(2)

        # Filter bar
        self._filter_bar = FilterBar(self._client)
        self._filter_bar.load_requested.connect(self._on_load_requested)
        self._filter_bar.cancel_requested.connect(self._cancel_load)
        self._filter_bar.provisional_filter_changed.connect(
            self._on_provisional_filter_changed
        )
        self._filter_bar.navigation_repeat_rate_changed.connect(
            self._set_navigation_repeat_rate
        )
        root.addWidget(
            self._filter_bar, 0
        )  # no vertical stretch — stays at sizeHint height

        # Main splitter: list | viewer | right panel
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        self._result_list = ResultList(self._client, self._disk_cache)
        self._result_list.selection_changed.connect(self._on_result_selected)
        self._result_list.near_bottom_reached.connect(self._on_scroll_near_bottom)
        self._result_list.setMinimumWidth(200)
        self._result_list.setMaximumWidth(340)
        self._splitter.addWidget(self._result_list)

        self._viewer = ViewerPanel()
        self._viewer.setMinimumWidth(400)
        self._splitter.addWidget(self._viewer)

        # Right panel: metadata + taxon tree
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Vertical splitter for metadata / taxon tree
        self._right_splitter = QSplitter(Qt.Orientation.Vertical)

        self._metadata_panel = MetadataPanel()
        self._metadata_panel.open_obs_requested.connect(self._open_obs_in_browser)
        self._metadata_panel.copy_obs_url_requested.connect(self._copy_obs_url)
        self._right_splitter.addWidget(self._metadata_panel)

        self._taxon_tree = TaxonTreePanel(self._summary_client, self._db)
        self._taxon_tree.taxon_selected.connect(self._on_taxon_from_tree)
        self._taxon_tree.summary_finished.connect(self._on_taxon_summary_finished)
        self._right_splitter.addWidget(self._taxon_tree)
        self._right_splitter.setSizes([400, 300])

        right_layout.addWidget(self._right_splitter)
        right_panel.setMinimumWidth(320)
        right_panel.setMaximumWidth(520)
        self._splitter.addWidget(right_panel)

        self._splitter.setSizes([240, 860, 380])
        root.addWidget(self._splitter, 1)  # splitter gets all remaining vertical space

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = _ElidedStatusLabel("Ready")
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setFixedWidth(120)
        self._progress.setVisible(False)
        self._status_bar.addWidget(self._status_label, 1)
        self._status_bar.addPermanentWidget(self._progress)
        self._auth_label = QLabel("")
        self._status_bar.addPermanentWidget(self._auth_label)
        self._identify_actions_status_label = QLabel(
            "Identify actions: no pending work"
        )
        self._identify_actions_status_label.setToolTip(
            "Application-wide durable Identify action status"
        )
        self._status_bar.addPermanentWidget(self._identify_actions_status_label)
        self._api_call_label = QLabel("API calls: 0")
        self._api_call_label.setToolTip(
            "Total iNaturalist API requests made this session"
        )
        self._status_bar.addPermanentWidget(self._api_call_label)

        # Load More button in status bar
        self._load_more_btn = QPushButton("Load more…")
        self._load_more_btn.setFixedWidth(100)
        self._load_more_btn.setVisible(False)
        self._load_more_btn.clicked.connect(self._load_next_page)
        self._status_bar.addPermanentWidget(self._load_more_btn)

        self._api_call_timer = QTimer(self)
        self._api_call_timer.timeout.connect(self._update_api_call_count)
        self._api_call_timer.start(1000)

        self._update_auth_ui()

    def _build_menu(self) -> None:
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")
        act_settings = QAction("&Settings…", self)
        act_settings.triggered.connect(self._show_settings)
        file_menu.addAction(act_settings)
        file_menu.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # View
        view_menu = mb.addMenu("&View")
        act_toggle_zoom = QAction("Toggle zoom (L)", self)
        act_toggle_zoom.triggered.connect(self._viewer.toggle_zoom)
        view_menu.addAction(act_toggle_zoom)

        act_open_obs = QAction("Open observation in browser (O)", self)
        act_open_obs.triggered.connect(self._open_obs_in_browser)
        view_menu.addAction(act_open_obs)

        act_open_img = QAction("Open image in browser (I)", self)
        act_open_img.triggered.connect(self._open_image_in_browser)
        view_menu.addAction(act_open_img)

        # Action
        action_menu = mb.addMenu("&Action")
        self._act_auth = QAction("Authenticate to iNaturalist…", self)
        self._act_auth.triggered.connect(
            lambda _checked=False: self._authenticate_to_inaturalist()
        )
        action_menu.addAction(self._act_auth)
        act_identify = QAction("Identify observations…", self)
        act_identify.setToolTip("Open a read-only local iNaturalist Identify session")
        act_identify.triggered.connect(self._open_identify_setup)
        action_menu.addAction(act_identify)
        self._act_pending_identify_actions = QAction("Pending Identify actions…", self)
        self._act_pending_identify_actions.setToolTip(
            "Review durable local Identify actions; opening this never resumes them"
        )
        self._act_pending_identify_actions.triggered.connect(
            self._open_pending_identify_actions
        )
        action_menu.addAction(self._act_pending_identify_actions)
        self._act_retry_identify_refresh = QAction("Retry safe Identify refresh", self)
        self._act_retry_identify_refresh.setToolTip(
            "Retry a failed read of a confirmed observation; this never resends its write"
        )
        self._act_retry_identify_refresh.setEnabled(False)
        self._act_retry_identify_refresh.triggered.connect(
            self._retry_confirmed_refresh_warning
        )
        action_menu.addAction(self._act_retry_identify_refresh)
        act_reconcile = QAction("Reconcile Mushroom Observer ↔ iNaturalist…", self)
        act_reconcile.setToolTip(
            "Open the Gate 1A reconciliation dashboard with explicitly confirmed Gate 1B link "
            "repairs, Gate 1C ITS synchronization, and Gate 1D coordinate/name reconciliation"
        )
        act_reconcile.triggered.connect(self._open_reconciliation)
        action_menu.addAction(act_reconcile)
        action_menu.addSeparator()

        act_agree_recent = QAction("Agree with most recent ID (a)", self)
        act_agree_recent.setToolTip("Requires iNaturalist authentication")
        act_agree_recent.triggered.connect(lambda: self._agree_current("recent"))
        action_menu.addAction(act_agree_recent)

        act_agree_consensus = QAction("Agree with consensus ID (A)", self)
        act_agree_consensus.setToolTip("Requires iNaturalist authentication")
        act_agree_consensus.triggered.connect(lambda: self._agree_current("consensus"))
        action_menu.addAction(act_agree_consensus)

        act_bulk_provisional = QAction("Agree to provisional IDs…", self)
        act_bulk_provisional.setToolTip(
            "Preview and supervise provisional-name agreements from an observations URL"
        )
        act_bulk_provisional.triggered.connect(self._start_bulk_agree_setup)
        action_menu.addAction(act_bulk_provisional)

        act_bulk_disagree = QAction("Bulk disagree to taxon from URL…", self)
        act_bulk_disagree.setToolTip(
            "Preview and supervise coarser corrective identifications from an observations URL"
        )
        act_bulk_disagree.triggered.connect(self._start_bulk_disagree_setup)
        action_menu.addAction(act_bulk_disagree)

        act_propose_name = QAction("Propose a name to observation numbers…", self)
        act_propose_name.setToolTip(
            "Propose an identification on a typed list of observation numbers"
        )
        act_propose_name.triggered.connect(self._start_propose_name_setup)
        action_menu.addAction(act_propose_name)

        act_provisional_swap = QAction("Provisional Name Swap…", self)
        act_provisional_swap.setToolTip(
            "Search for a Provisional Species Name field value and swap matching values"
        )
        act_provisional_swap.triggered.connect(self._start_provisional_name_swap)
        action_menu.addAction(act_provisional_swap)

        act_species_override = QAction("Update Species Name Override…", self)
        act_species_override.setToolTip(
            "Find observations by provisional name or pasted IDs and set Species Name Override"
        )
        act_species_override.triggered.connect(
            lambda _checked=False: self._start_species_override_update(
                SPECIES_NAME_OVERRIDE_FIELD_NAME
            )
        )
        action_menu.addAction(act_species_override)

        act_provisional_update = QAction("Update Provisional Species Name…", self)
        act_provisional_update.setToolTip(
            "Find observations by provisional name or pasted IDs and set Provisional Species Name"
        )
        act_provisional_update.triggered.connect(
            lambda _checked=False: self._start_species_override_update(
                PROVISIONAL_SPECIES_FIELD_NAME
            )
        )
        action_menu.addAction(act_provisional_update)

        # Debug
        debug_menu = mb.addMenu("&Debug")
        self._act_show_log = QAction("Show &Log Panel", self, checkable=True)
        self._act_show_log.setShortcut("Ctrl+Shift+L")
        self._act_show_log.triggered.connect(self._toggle_log_panel)
        debug_menu.addAction(self._act_show_log)

        act_debug_level = QAction("Verbose (&DEBUG) logging", self, checkable=True)
        act_debug_level.setObjectName("act_debug_level")
        act_debug_level.triggered.connect(self._toggle_debug_logging)
        debug_menu.addAction(act_debug_level)

        debug_menu.addSeparator()
        act_cache_info = QAction("Cache &info…", self)
        act_cache_info.triggered.connect(self._show_cache_info)
        debug_menu.addAction(act_cache_info)

        act_clear_cache = QAction("&Clear all caches…", self)
        act_clear_cache.triggered.connect(self._clear_caches)
        debug_menu.addAction(act_clear_cache)

        # Help
        help_menu = mb.addMenu("&Help")
        act_shortcuts = QAction("&Keyboard shortcuts", self)
        act_shortcuts.triggered.connect(self._show_shortcuts)
        help_menu.addAction(act_shortcuts)

    def _open_identify_setup(self) -> None:
        """Open planning separately so this window remains the study browser."""
        from observation_workbench.ui.identify_setup_dialog import IdentifySetupDialog

        dialog = IdentifySetupDialog(
            self._settings,
            self._client,
            self._auth_state.api_token,
            self,
            metadata_client=self._summary_client,
        )
        dialog.session_ready.connect(self._open_identify_window)
        dialog.exec()

    def _open_identify_window(self, session) -> None:
        from observation_workbench.ui.identify_window import IdentifyWindow

        window = IdentifyWindow(
            session,
            self._settings,
            self._client,
            self._disk_cache,
            self._current_identify_read_token,
            action_manager=self._identify_actions,
        )
        self._identify_windows.add(window)
        window.destroyed.connect(
            lambda *_args, w=window: self._identify_windows.discard(w)
        )
        window.pending_actions_requested.connect(self._open_pending_identify_actions)
        if window.should_open_maximized:
            window.showMaximized()
        else:
            window.show()

    def _current_identify_read_token(self) -> str:
        """Return the current in-memory token for safe Identify detail reads."""
        return self._auth_state.api_token if self._auth_state.is_authenticated else ""

    def _open_pending_identify_actions(self) -> None:
        """Show the one application-owned action center without dispatching work."""
        dialog = self._pending_identify_actions_dialog
        if dialog is not None and not dialog.isVisible():
            # WA_DeleteOnClose schedules deletion. Do not resurrect a closing
            # dialog or let it compete with a newly opened action center.
            dialog.deleteLater()
            self._pending_identify_actions_dialog = None
            dialog = None
        if dialog is None:
            from observation_workbench.ui.identify_pending_actions import (
                PendingIdentifyActionsDialog,
            )

            dialog = PendingIdentifyActionsDialog(self._identify_actions, self)
            dialog.destroyed.connect(
                lambda _object=None, tracked=dialog: self._pending_identify_actions_destroyed(
                    tracked
                )
            )
            self._pending_identify_actions_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _pending_identify_actions_destroyed(self, dialog: object) -> None:
        if self._pending_identify_actions_dialog is dialog:
            self._pending_identify_actions_dialog = None

    def _show_startup_identify_recovery_prompt(self) -> None:
        """Offer recovery only after the visible MainWindow has entered its event loop."""
        if self._closing or self._startup_recovery_prompt_shown:
            return
        self._startup_recovery_prompt_shown = True
        summary = self._identify_actions.action_summary()
        queue = self._identify_actions.queue_summary()
        migrated = self._identify_actions.migrated_manual_retry_count
        relevant = {
            "submitted_unverified": summary.submitted_unverified,
            "ambiguous": summary.ambiguous,
            "failed_retryable": summary.failed_retryable,
            "failed_terminal": summary.failed_terminal,
        }
        if (
            not queue.eligible_for_current_account
            and not queue.queued_for_other_accounts
            and not any(relevant.values())
            and not migrated
        ):
            return
        lines = [
            "Pending or attention-required local Identify actions were found. They are paused.",
            "",
        ]
        if queue.eligible_for_current_account:
            lines.append(
                f"Queued for the current account ({queue.current_login}): "
                f"{queue.eligible_for_current_account}"
            )
        if queue.queued_for_other_accounts:
            owners = ", ".join(queue.other_account_logins)
            suffix = f" ({owners})" if owners else ""
            lines.append(
                f"Queued for other account(s){suffix}: {queue.queued_for_other_accounts}"
            )
        labels = {
            "submitted_unverified": "Submitted; awaiting verification",
            "ambiguous": "Ambiguous",
            "failed_retryable": "Retryable failures",
            "failed_terminal": "Terminal failures — correction required",
        }
        lines.extend(
            f"{labels[key]}: {value}" for key, value in relevant.items() if value
        )
        if migrated:
            lines.append(
                f"Legacy manual retries migrated to linked queued actions: {migrated}"
            )
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Recover pending Identify actions",
            "\n".join(lines),
            parent=self,
        )
        review = box.addButton(
            "Review pending actions", QMessageBox.ButtonRole.ActionRole
        )
        resume = box.addButton(
            "Resume queued actions", QMessageBox.ButtonRole.AcceptRole
        )
        leave = box.addButton("Leave paused", QMessageBox.ButtonRole.RejectRole)
        resume.setEnabled(bool(queue.eligible_for_current_account))
        box.setDefaultButton(leave)
        box.exec()
        clicked = box.clickedButton()
        if clicked is review:
            self._open_pending_identify_actions()
        elif clicked is resume:
            allowed, required = self._identify_actions.can_resume_queued_actions()
            if allowed:
                # The manager authorizes only the visible current-account
                # snapshot; other-account rows and later enqueues stay paused.
                self._identify_actions.resume_queued_actions()
            else:
                QMessageBox.information(
                    self,
                    "Identify actions remain paused",
                    (
                        f"Authenticate as {required} before resuming. No actions were sent."
                        if required
                        else "No queued actions belong to the current authenticated account."
                    ),
                )

    def _identify_actions_summary_changed(self, _summary: object) -> None:
        self._update_identify_actions_ui()

    def _identify_actions_running_changed(self, _running: bool) -> None:
        self._update_identify_actions_ui()

    def _identify_actions_paused(self, _reason: str) -> None:
        self._update_identify_actions_ui()

    def _identify_actions_changed(self, _action: object) -> None:
        self._update_identify_actions_ui()

    def _identify_actions_authentication_changed(self) -> None:
        # Any authenticated refresh started before this change is rejected when
        # it returns, so a private result cannot be applied under a new account.
        self._identify_refresh_auth_generation += 1
        self._identify_refresh_in_flight.clear()
        self._identify_refresh_pending.clear()
        # Warnings describe reads made for the previous account; retrying one
        # under the new account would read an unrelated observation.
        self._identify_refresh_warnings.clear()
        self._update_identify_actions_ui()
        dialog = self._pending_identify_actions_dialog
        if dialog is not None:
            dialog.refresh()

    def _update_identify_actions_ui(self) -> None:
        if not hasattr(self, "_identify_actions_status_label"):
            return
        summary = self._identify_actions.action_summary()
        queue = self._identify_actions.queue_summary()
        pending_count = summary.pending_menu_count
        if summary.attention_count:
            details = []
            if summary.ambiguous:
                details.append(f"{summary.ambiguous} ambiguous")
            if summary.submitted_unverified:
                details.append(f"{summary.submitted_unverified} awaiting verification")
            if summary.failed_retryable:
                details.append(f"{summary.failed_retryable} retryable")
            if summary.failed_terminal:
                details.append(
                    f"{summary.failed_terminal} terminal — correction required"
                )
            status = "Identify actions: attention required · " + ", ".join(details)
        elif self._identify_actions.is_running:
            action_id = self._identify_actions.active_action_id
            status = (
                f"Identify actions: submitting #{action_id}"
                if action_id
                else "Identify actions: working"
            )
        elif pending_count:
            if self._identify_actions.is_paused:
                details = []
                if queue.eligible_for_current_account:
                    details.append(
                        f"{queue.eligible_for_current_account} queued for {queue.current_login}"
                    )
                if queue.queued_for_other_accounts:
                    required = (
                        queue.other_account_logins[0]
                        if queue.other_account_logins
                        else "another account"
                    )
                    details.append(
                        f"{queue.queued_for_other_accounts} queued · sign in as {required}"
                    )
                status = "Identify actions: paused"
                if details:
                    status += " · " + ", ".join(details)
            else:
                status = "Identify actions: authorized dispatch"
        elif self._identify_refresh_warnings:
            observation_id = next(iter(self._identify_refresh_warnings))
            status = (
                f"Identify actions: refresh warning for observation #{observation_id}"
            )
        else:
            status = "Identify actions: no pending work"
        self._identify_actions_status_label.setText(status)
        if hasattr(self, "_act_pending_identify_actions"):
            suffix = f" ({pending_count})" if pending_count else ""
            self._act_pending_identify_actions.setText(
                f"Pending Identify actions…{suffix}"
            )
        if hasattr(self, "_act_retry_identify_refresh"):
            self._act_retry_identify_refresh.setEnabled(
                bool(self._identify_refresh_warnings)
            )

    def _request_confirmed_observation_refresh(
        self, observation_id: int, observation_uuid: str
    ) -> None:
        """Start or coalesce a safe detail GET after a confirmed action."""
        if self._closing:
            return
        request = _ConfirmedRefreshRequest(
            observation_id=int(observation_id),
            observation_uuid=str(observation_uuid),
            auth_generation=self._identify_refresh_auth_generation,
        )
        if request.observation_id in self._identify_refresh_in_flight:
            # The newest confirmation may not have been visible to the first
            # read, so retain one trailing refresh with its expected UUID.
            self._identify_refresh_pending[request.observation_id] = request
            return
        self._start_confirmed_observation_refresh(request)

    def _start_confirmed_observation_refresh(
        self, request: _ConfirmedRefreshRequest
    ) -> None:
        if (
            self._closing
            or request.auth_generation != self._identify_refresh_auth_generation
            or request.observation_id in self._identify_refresh_in_flight
        ):
            return
        worker = _ConfirmedRefreshWorker(
            self._client, request, self._auth_state.api_token
        )
        signals = worker.signals
        self._identify_refresh_live_signals.add(signals)
        self._identify_refresh_in_flight[request.observation_id] = request

        def loaded(raw: object) -> None:
            self._confirmed_observation_refresh_loaded(signals, request, raw)

        def failed(exc: object) -> None:
            self._confirmed_observation_refresh_failed(signals, request, exc)

        signals.loaded.connect(loaded)
        signals.failed.connect(failed)
        self._pool.start(worker)

    def _confirmed_observation_refresh_loaded(
        self,
        signals: _ConfirmedRefreshSignals,
        request: _ConfirmedRefreshRequest,
        raw: object,
    ) -> None:
        current, trailing = self._complete_confirmed_observation_refresh(
            signals, request
        )
        if (
            not current
            or self._closing
            or request.auth_generation != self._identify_refresh_auth_generation
        ):
            return
        records = raw.get("results") if isinstance(raw, dict) else None
        record = records[0] if isinstance(records, list) and records else raw
        observation = parse_observation(record) if isinstance(record, dict) else None
        if observation is None or observation.obs_id != request.observation_id:
            self._record_confirmed_refresh_warning(
                request, "The detail response did not match the confirmed observation."
            )
        elif (
            request.observation_uuid
            and observation.uuid
            and observation.uuid != request.observation_uuid
        ):
            self._record_confirmed_refresh_warning(
                request,
                "The detail response UUID did not match the confirmed observation.",
            )
        else:
            self._identify_refresh_warnings.pop(request.observation_id, None)
            # The original study window is updated only if it already contains this
            # observation; matching Identify windows replace strictly by numeric ID.
            self._replace_observation(observation)
            for window in list(self._identify_windows):
                try:
                    window.apply_confirmed_observation_refresh(observation)
                except RuntimeError:
                    self._identify_windows.discard(window)
            self._update_identify_actions_ui()
        if trailing is not None:
            self._start_confirmed_observation_refresh(trailing)

    def _confirmed_observation_refresh_failed(
        self,
        signals: _ConfirmedRefreshSignals,
        request: _ConfirmedRefreshRequest,
        exc: object,
    ) -> None:
        current, trailing = self._complete_confirmed_observation_refresh(
            signals, request
        )
        if (
            not current
            or self._closing
            or request.auth_generation != self._identify_refresh_auth_generation
        ):
            return
        diagnostic = _safe_confirmed_refresh_diagnostic(exc)
        self._record_confirmed_refresh_warning(request, diagnostic)
        if trailing is not None:
            self._start_confirmed_observation_refresh(trailing)

    def _complete_confirmed_observation_refresh(
        self,
        signals: _ConfirmedRefreshSignals,
        request: _ConfirmedRefreshRequest,
    ) -> tuple[bool, _ConfirmedRefreshRequest | None]:
        """Retire exactly this in-flight read and obtain one coalesced rerun."""
        self._identify_refresh_live_signals.discard(signals)
        if self._identify_refresh_in_flight.get(request.observation_id) != request:
            return False, None
        self._identify_refresh_in_flight.pop(request.observation_id, None)
        if (
            self._closing
            or request.auth_generation != self._identify_refresh_auth_generation
        ):
            return True, None
        pending = self._identify_refresh_pending.pop(request.observation_id, None)
        if pending is None or pending.auth_generation != request.auth_generation:
            return True, None
        return True, pending

    def _record_confirmed_refresh_warning(
        self,
        request: _ConfirmedRefreshRequest,
        diagnostic: str,
    ) -> None:
        self._identify_refresh_warnings[request.observation_id] = (
            request.observation_uuid,
            diagnostic,
        )
        self._update_identify_actions_ui()

    def _retry_confirmed_refresh_warning(self) -> None:
        """Deliberately retry one warning's safe read, never its write."""
        if self._closing or not self._identify_refresh_warnings:
            return
        observation_id, (observation_uuid, _diagnostic) = next(
            iter(self._identify_refresh_warnings.items())
        )
        self._request_confirmed_observation_refresh(observation_id, observation_uuid)

    def _toggle_log_panel(self, checked: bool) -> None:
        if checked:
            self._log_panel.show()
        else:
            self._log_panel.hide()

    def _update_api_call_count(self) -> None:
        network = self._client.call_count + self._summary_client.call_count
        cached = self._loader.cache_hit_count
        if cached:
            self._api_call_label.setText(f"API: {network} network, {cached} cached")
        else:
            self._api_call_label.setText(f"API: {network} network")

    def _update_auth_ui(self) -> None:
        login = self._auth_state.login
        if login:
            self._auth_label.setText(f"iNat: {login}")
            if hasattr(self, "_act_auth"):
                self._act_auth.setText(f"Authenticated as {login}…")
        else:
            self._auth_label.setText("iNat: not authenticated")
            if hasattr(self, "_act_auth"):
                self._act_auth.setText("Authenticate to iNaturalist…")

    def _toggle_debug_logging(self, checked: bool) -> None:
        level = logging.DEBUG if checked else logging.INFO
        logging.getLogger().setLevel(level)
        # Also set the log handler level
        for h in logging.getLogger().handlers:
            if hasattr(h, "signals"):  # our QtLogHandler
                h.setLevel(level)
        log.info("Log level set to %s", "DEBUG" if checked else "INFO")

    def _authenticate_to_inaturalist(
        self,
        *,
        on_success: Optional[Callable[[str, str], None]] = None,
        on_failure: Optional[Callable[[str], None]] = None,
    ) -> None:
        from observation_workbench.ui.auth_dialog import AuthDialog

        dlg = AuthDialog(self._auth_state.login, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if on_failure is not None:
                on_failure("Authentication was cancelled.")
            return
        token = normalise_token(dlg.token)
        if not token:
            QMessageBox.warning(
                self, "Authentication", "Paste an iNaturalist API token first."
            )
            if on_failure is not None:
                on_failure("No iNaturalist API token was provided.")
            return
        self._status_label.setText("Validating iNaturalist token…")
        worker = _AuthWorker(self._client, token)
        sigs = worker.signals
        self._live_auth_signals.add(sigs)

        def authenticated(valid_token: str, login: str) -> None:
            self._live_auth_signals.discard(sigs)
            self._on_authenticated(valid_token, login)
            if on_success is not None:
                on_success(valid_token, login)

        def authentication_failed(msg: str) -> None:
            self._live_auth_signals.discard(sigs)
            self._on_auth_error(msg)
            if on_failure is not None:
                on_failure(msg)

        sigs.authenticated.connect(authenticated)
        sigs.error.connect(authentication_failed)
        self._pool.start(worker)

    def _reauthenticate_photo_browser(
        self,
        on_success: Callable[[str, str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        self._auth_service.clear()
        self._auth_state = AuthState()
        self._identify_actions.authentication_changed()
        self._reconciliation_authentication_changed()
        self._update_auth_ui()
        self._authenticate_to_inaturalist(
            on_success=on_success,
            on_failure=on_failure,
        )

    @Slot(str, str)
    def _on_authenticated(self, token: str, login: str) -> None:
        self._auth_state = self._auth_service.save(token, login)
        self._identify_actions.authentication_changed()
        self._reconciliation_authentication_changed()
        self._update_auth_ui()
        self._status_label.setText(f"Authenticated to iNaturalist as {login}.")
        if self._current_obs_idx >= 0 and self._observations:
            self._metadata_panel.update_observation(
                self._observations[self._current_obs_idx],
                self._current_photo_idx,
                authenticated_login=self._auth_state.login,
            )
        if self._bulk_resume_after_auth:
            self._resume_bulk_after_auth()
        if self._disagree_resume_after_auth:
            self._resume_disagree_after_auth()

    def _on_auth_error(self, msg: str) -> None:
        log.error("Authentication error: %s", msg)
        self._status_label.setText("iNaturalist authentication failed.")
        QMessageBox.warning(
            self,
            "Authentication Failed",
            "Could not validate the iNaturalist token.\n\n" + msg,
        )

    def _require_auth(self) -> bool:
        if self._auth_state.is_authenticated:
            return True
        reply = QMessageBox.question(
            self,
            "Authentication Required",
            "Identifying on iNaturalist requires authentication. Authenticate now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._authenticate_to_inaturalist()
        return False

    def setup_log_panel(self, handler) -> None:
        """Called from main.py after creating the window to attach the log handler."""
        from observation_workbench.ui.log_panel import LogPanel

        self._log_panel = LogPanel(handler, parent=self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._log_panel)
        self._log_panel.hide()  # hidden by default; show via Debug menu or --debug flag

    def install_nav_filter(self) -> None:
        self._install_nav_filter()

    def _install_nav_filter(self) -> None:
        self._nav_filter = _NavFilter(self)
        QApplication.instance().installEventFilter(self._nav_filter)

    def _install_scroll_speed_filter(self) -> None:
        self._scroll_speed_filter = ScrollSpeedFilter(
            self._settings.scroll_speed_multiplier,
            self,
        )
        QApplication.instance().installEventFilter(self._scroll_speed_filter)

    def _install_screen_fit_filter(self) -> None:
        self._screen_fit_filter = _ScreenFitFilter(self)
        QApplication.instance().installEventFilter(self._screen_fit_filter)

    def _install_dialog_focus_recovery(self) -> None:
        """Bring open dialogs back to the front when the app regains focus.

        On some window managers (notably WSLg) a dialog — especially the
        non-modal progress dialogs — can end up hidden behind the main window
        after switching applications, making the app look hung.
        """
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_application_state_changed)

    def _on_application_state_changed(self, state) -> None:
        if state == Qt.ApplicationState.ApplicationActive:
            self._raise_open_dialogs()

    def _raise_open_dialogs(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        dialogs = [
            w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()
        ]
        if not dialogs:
            return
        for dlg in dialogs:
            dlg.raise_()
        # Modeless progress dialogs should stay visible without stealing focus
        # from the main window. Only force activation for modal dialogs, where
        # keyboard input cannot usefully go anywhere else.
        modal = app.activeModalWidget()
        if modal is not None:
            modal.raise_()
            modal.activateWindow()

    def _show_dialog_in_front(self, dialog) -> None:
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_font_scale(self, scale: float) -> None:
        """Scale all in-app fonts/widgets relative to the system default."""
        pt = max(6, round(self._system_font_pt * scale))
        f = QFont(QApplication.font())
        f.setPointSize(pt)
        QApplication.setFont(f)

    def _restore_state(self) -> None:
        s = self._settings
        self._apply_font_scale(s.ui_font_scale)
        self._result_list.set_font_scale(s.result_list_font_scale)
        StudyTaxon.show_common_names = s.show_common_names
        if s.window_geometry:
            self.restoreGeometry(s.window_geometry)
        if s.window_state:
            self.restoreState(s.window_state)
        if s.splitter_state:
            self._splitter.restoreState(s.splitter_state)
        self._filter_bar.restore_state(s)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @Slot(dict)
    def _on_load_requested(self, filters_dict: dict) -> None:
        self.stop_arrow_navigation("new load requested")
        source_text = filters_dict.get("username", "").strip()
        if not source_text:
            self._status_label.setText(
                "Please enter an identifier username or observations URL."
            )
            return
        try:
            observation_query = parse_observations_url(source_text)
        except ObservationURLParseError as exc:
            self._status_label.setText(str(exc))
            QMessageBox.warning(self, "Unsupported URL", str(exc))
            return
        if (
            observation_query is not None
            and observation_query.source_kind == "identify"
        ):
            message = (
                "iNaturalist /observations/identify URLs are account-specific. "
                "Open this URL through the authenticated Identify workflow instead."
            )
            self._status_label.setText(message)
            QMessageBox.warning(self, "Use the Identify Workflow", message)
            return
        is_url_query = observation_query is not None
        taxon_text = (filters_dict.get("taxon_name", "") or "").strip()
        if taxon_text and filters_dict.get("taxon_id") is None:
            msg = (
                f"The Taxon field contains '{taxon_text}', but it has not been "
                "selected from the autocomplete list. Select a taxon from the list "
                "or clear the Taxon field before loading."
            )
            self._status_label.setText(msg)
            QMessageBox.warning(self, "Taxon Not Selected", msg)
            return

        # Cancel any in-progress load
        self._cancel_load()

        self._generation += 1
        gen = self._generation
        self._current_page = 1
        self._page_size = FIRST_PAGE_SIZE
        self._rows_requested = 0
        self._loaded_obs_ids.clear()
        self._loaded_observations = []
        self._observations = []
        self._current_obs_idx = -1
        self._current_photo_idx = 0
        self._display_obs_id = None
        self._display_photo_id = None
        self._total_results = 0
        self._is_loading = True
        self._last_scroll_load_count = 0
        self._provisional_name_only = bool(
            filters_dict.get("provisional_name_only", False)
        )
        self._detail_refreshed_obs_ids.clear()
        self._detail_refresh_in_flight.clear()

        self._result_list.set_observations([])
        self._viewer.clear()
        self._metadata_panel.clear()
        self._filter_bar.set_loading(True)
        self._progress.setVisible(True)
        self._load_more_btn.setVisible(False)
        place = "" if is_url_query else (filters_dict.get("place_name", "") or "")
        taxon = taxon_text if filters_dict.get("taxon_id") is not None else ""
        ctx = " · ".join(p for p in [place, taxon] if p)
        if is_url_query:
            log.debug(
                "Loading observations URL: %s taxon_id=%s",
                observation_query.display_url,
                filters_dict.get("taxon_id"),
            )
            self._status_label.setText(
                "Loading first observations from URL"
                + (f"  [{ctx}]" if ctx else "")
                + "…"
            )
        else:
            log.debug(
                "Loading: user=%s place_id=%s taxon_id=%s",
                source_text,
                filters_dict.get("place_id"),
                filters_dict.get("taxon_id"),
            )
            self._status_label.setText(
                f"Loading first results: {source_text}"
                + (f"  [{ctx}]" if ctx else "")
                + "…"
            )

        # Save filter state
        s = self._settings
        self._filter_bar.save_state(s)
        s.sync()

        # Build LoadFilters
        filters = LoadFilters(
            username=source_text,
            place_id=filters_dict.get("place_id"),
            taxon_id=filters_dict.get("taxon_id"),
            leading_only=filters_dict.get("leading_only", False),
            d1=filters_dict.get("d1"),
            d2=filters_dict.get("d2"),
            rank_level=filters_dict.get("rank_level"),
            rank_name=filters_dict.get("rank_name"),
            exact_rank=filters_dict.get("exact_rank", False),
            provisional_name_only=False,
            observation_query=observation_query,
        )
        self._current_filters = filters

        self._prefetcher.set_observations([], radius=self._settings.prefetch_radius)

        # Cancel any in-flight taxon summary and store params for later.
        # We'll start the summary after page 1 is rendered so it doesn't
        # contend with the initial API fetch and image loading.
        self._taxon_tree.cancel_pending()
        if filters.observation_query:
            self._pending_summary_mode = "observations"
            self._pending_summary_kwargs = dict(
                source_key=filters.observation_query.source_key,
                query_params=filters.observation_query.params,
            )
        else:
            self._pending_summary_mode = "identifications"
            self._pending_summary_kwargs = dict(
                username=source_text,
                taxon_id=filters.taxon_id,
                place_id=filters.place_id,
                d1=filters.d1,
                d2=filters.d2,
            )

        # First page is small so results appear fast; subsequent pages are larger.
        self._fetch_page(filters, page=1, per_page=FIRST_PAGE_SIZE, generation=gen)

    def _fetch_page(
        self, filters: LoadFilters, page: int, per_page: int, generation: int
    ) -> None:
        self._page_size = per_page
        self._rows_requested = max(self._rows_requested, page * per_page)
        worker = _LoadWorker(
            loader=self._loader,
            filters=filters,
            page=page,
            per_page=per_page,
            generation=generation,
            get_gen=lambda: self._generation,
        )
        sigs = worker.signals
        # Keep signals alive until the callback fires (prevents Python GC bug)
        self._live_load_signals.add(sigs)
        sigs.page_loaded.connect(
            lambda obs, total, s=sigs, g=generation: self._on_page_loaded(
                s, obs, total, g
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs, g=generation: self._on_load_error(s, msg, g)
        )
        self._pool.start(worker)

    def _has_more_api_rows(self) -> bool:
        """True when the API holds raw rows beyond the window already requested."""
        return self._total_results > 0 and self._rows_requested < self._total_results

    def _drop_already_loaded(
        self, observations: List[StudyObservation]
    ) -> List[StudyObservation]:
        """Keep only observations not already loaded, and record the new ones."""
        fresh: List[StudyObservation] = []
        for obs in observations:
            if obs.obs_id in self._loaded_obs_ids:
                continue
            self._loaded_obs_ids.add(obs.obs_id)
            fresh.append(obs)
        if len(fresh) != len(observations):
            log.debug(
                "Dropped %d already-loaded observation(s) from this page",
                len(observations) - len(fresh),
            )
        return fresh

    def _on_page_loaded(
        self,
        sigs: object,
        observations: List[StudyObservation],
        total: int,
        generation: int,
    ) -> None:
        self._live_load_signals.discard(sigs)  # release GC hold

        if generation != self._generation:
            log.debug(
                "Discarding stale page result (gen %d vs %d)",
                generation,
                self._generation,
            )
            return

        self._total_results = total
        self._is_loading = False
        self._progress.setVisible(False)
        self._filter_bar.set_loading(False)

        # A larger page size restarts the offset sequence at page 1, so a page
        # can legitimately re-deliver rows that are already loaded.
        observations = self._drop_already_loaded(observations)

        loaded_count = len(observations)
        log.debug(
            "Page loaded: %d observations (total=%d, page=%d)",
            loaded_count,
            total,
            self._current_page,
        )

        per_page = self._page_size
        has_more_api_pages = self._has_more_api_rows()
        if loaded_count == 0 and has_more_api_pages:
            self._status_label.setText(
                "No visible rows on this filtered page; loading the next page…"
            )
            self._current_page += 1
            self._is_loading = True
            self._progress.setVisible(True)
            self._filter_bar.set_loading(True)
            self._fetch_page(
                self._current_filters, self._current_page, per_page, generation
            )
            return

        if loaded_count == 0 and self._loaded_observations:
            self._status_label.setText(
                "No more matching rows after client-side filters."
            )
            self._current_page += 1
            self._load_more_btn.setVisible(False)
            return

        if total == 0 or loaded_count == 0:
            if getattr(self._current_filters, "observation_query", None):
                msg = "No results found. Check the observations URL or try a different query."
            else:
                msg = "No results found. Check username, filters, or try a different query."
            self._status_label.setText(msg)
            self._current_page += 1
            self._load_more_btn.setVisible(False)
            return

        # "First page" means the first page that produced visible rows, not
        # page number 1: earlier pages may have been filtered away entirely,
        # and a page-size change restarts the page numbering.
        is_first_page = not self._loaded_observations
        self._suppress_auto_load = True
        if is_first_page:
            self._loaded_observations = list(observations)
            self._apply_observation_view_filter()
            # First results are now visible — start taxon summary in the background
            # using its own rate-limiter so it won't delay further image loading.
            summary_kwargs = self._pending_summary_kwargs
            if summary_kwargs is not None:
                self._pending_summary_kwargs = None
                if self._pending_summary_mode == "observations":
                    self._taxon_tree.load_observation_summary(**summary_kwargs)
                else:
                    self._taxon_tree.load_summary(**summary_kwargs)
        else:
            preferred_obs_id = (
                self._observations[self._current_obs_idx].obs_id
                if self._current_obs_idx >= 0 and self._observations
                else None
            )
            self._loaded_observations.extend(observations)
            if self._provisional_name_only:
                self._apply_observation_view_filter(preferred_obs_id=preferred_obs_id)
            else:
                # A plain page append does not require rebuilding every list
                # row or invalidating the decoded-image LRU. Keeping both warm
                # avoids a large pause if pagination completes during review.
                self._observations.extend(observations)
                self._prefetcher.append_observations(observations)
                self._result_list.append_observations(observations)
                log.debug(
                    "Appended page in place rows=%d visible=%d; image memory cache preserved",
                    len(observations),
                    len(self._observations),
                )
                self._resume_arrow_navigation_if_held()
        self._suppress_auto_load = False

        visible = len(self._observations)
        taxon_name = (
            self._observations[self._current_obs_idx].display_taxon.display_name
            if self._current_obs_idx >= 0 and self._observations
            else ""
        )
        loaded = len(self._loaded_observations)
        # Client-side filters drop rows, so `loaded < total` stays true even
        # once every page has been requested. Ask the raw-row window instead.
        has_more = self._has_more_api_rows()
        if visible == 0 and self._provisional_name_only:
            status = (
                f"No visible observations match the Provisional Name filter "
                f"({loaded}/{total} fetched)."
            )
        elif self._provisional_name_only and has_more:
            status = (
                f"[{self._current_obs_idx + 1}/{visible} provisional, "
                f"{loaded}/{total} fetched]  {taxon_name}"
            )
        else:
            status = (
                f"[{self._current_obs_idx + 1}/{visible} visible, "
                f"{loaded} loaded, total {total}]  {taxon_name}"
            )
        if is_first_page:
            status += "  · Loading taxon summary…"
        self._status_label.setText(status)

        self._load_more_btn.setVisible(has_more)
        self._current_page += 1
        self._update_api_call_count()
        if visible == 0 and self._provisional_name_only and has_more:
            self._load_next_page()
        elif self._provisional_name_only:
            self._maybe_auto_load_next_page()

    def _on_load_error(self, sigs: object, msg: str, generation: int) -> None:
        self._live_load_signals.discard(sigs)

        if generation != self._generation:
            return
        self._is_loading = False
        self._progress.setVisible(False)
        self._filter_bar.set_loading(False)
        self._status_label.setText(f"Error loading: {msg[:120]}")
        log.error("Load error: %s", msg)
        kind = (
            "observations"
            if getattr(
                getattr(self, "_current_filters", None), "observation_query", None
            )
            else "identifications"
        )
        QMessageBox.warning(self, "Load Error", f"Failed to load {kind}:\n\n{msg}")

    @Slot(bool)
    def _on_provisional_filter_changed(self, checked: bool) -> None:
        self._provisional_name_only = checked
        if self._settings.provisional_name_only != checked:
            self._settings.provisional_name_only = checked
            self._settings.sync()
        if not self._loaded_observations:
            return
        preferred_obs_id = (
            self._observations[self._current_obs_idx].obs_id
            if self._current_obs_idx >= 0 and self._observations
            else None
        )
        self._apply_observation_view_filter(preferred_obs_id=preferred_obs_id)
        if (
            checked
            and not self._observations
            and self._has_more_api_rows()
            and not self._is_loading
        ):
            self._load_next_page()

    def _apply_observation_view_filter(
        self, preferred_obs_id: Optional[int] = None
    ) -> None:
        if self._provisional_name_only:
            self._observations = [
                obs
                for obs in self._loaded_observations
                if _is_provisional_observation(obs)
            ]
            workflow_obs = self._workflow_observation_to_pin()
            if workflow_obs is not None and all(
                obs.obs_id != workflow_obs.obs_id for obs in self._observations
            ):
                self._observations.append(workflow_obs)
        else:
            self._observations = list(self._loaded_observations)

        self._last_scroll_load_count = 0
        self._prefetcher.set_observations(
            self._observations, radius=self._settings.prefetch_radius
        )
        self._result_list.set_observations(self._observations)

        if not self._observations:
            self._current_obs_idx = -1
            self._current_photo_idx = 0
            self._display_obs_id = None
            self._display_photo_id = None
            self._viewer.clear()
            self._metadata_panel.clear()
            self._status_label.setText(
                f"No visible observations match the Provisional Name filter "
                f"({len(self._loaded_observations)} loaded)."
                if self._provisional_name_only
                else "No visible observations."
            )
            self._load_more_btn.setVisible(self._has_more_api_rows())
            return

        idx = 0
        if preferred_obs_id is not None:
            for row, obs in enumerate(self._observations):
                if obs.obs_id == preferred_obs_id:
                    idx = row
                    break
        self._show_observation(idx)
        self._load_more_btn.setVisible(self._has_more_api_rows())

    def _workflow_observation_to_pin(self) -> Optional[StudyObservation]:
        obs_id = self._pending_target_obs_id
        if obs_id is None or not self._is_active_workflow_observation(obs_id):
            return None
        for obs in self._loaded_observations:
            if obs.obs_id == obs_id:
                return obs
        for obs in self._observations:
            if obs.obs_id == obs_id:
                return obs
        return None

    def _is_active_workflow_observation(self, obs_id: int) -> bool:
        if self._pending_target_obs_id != obs_id:
            return False
        bulk_active = (
            getattr(self, "_bulk_dialog", None) is not None
            and not getattr(self, "_bulk_cancelled", False)
            and getattr(self, "_bulk_index", 0)
            < len(getattr(self, "_bulk_candidates", []))
        )
        disagree_active = (
            getattr(self, "_disagree_dialog", None) is not None
            and not getattr(self, "_disagree_cancelled", False)
            and getattr(self, "_disagree_index", 0)
            < len(getattr(self, "_disagree_candidates", []))
        )
        return bulk_active or disagree_active

    def _maybe_auto_load_next_page(self) -> None:
        """Automatically fetch the next page when the user is near the end of the loaded list."""
        if self._is_loading or self._suppress_auto_load:
            return
        visible = len(self._observations)
        loaded = len(self._loaded_observations)
        if visible == 0 or not self._has_more_api_rows():
            return
        if self._current_obs_idx >= visible - AUTO_LOAD_MARGIN:
            log.debug(
                "Auto-loading next page (visible %d/%d, loaded=%d, total=%d)",
                self._current_obs_idx + 1,
                visible,
                loaded,
                self._total_results,
            )
            self._load_next_page()

    @Slot(int)
    def _on_scroll_near_bottom(self, current_count: int) -> None:
        """Triggered when the user scrolls near the bottom of the result list."""
        if self._is_loading:
            return
        if current_count == 0 or not self._has_more_api_rows():
            return

        # Prevent chaining: only auto-load once per list-length boundary
        if current_count <= getattr(self, "_last_scroll_load_count", 0):
            return

        self._last_scroll_load_count = current_count
        log.debug(
            "Auto-loading next page from scroll position (visible rows: %d, total: %d)",
            current_count,
            self._total_results,
        )
        self._load_next_page()

    def _load_next_page(self) -> None:
        if self._is_loading or not hasattr(self, "_current_filters"):
            return
        if self._page_size != SUBSEQUENT_PAGE_SIZE:
            # The API offset is (page - 1) * per_page, so page numbers from the
            # small first page mean nothing at the larger size: page 2 of 100
            # starts at row 100, not at row 30, and would skip everything in
            # between. Restart the larger-page sequence at page 1 and let
            # de-duplication drop the rows already loaded.
            log.debug(
                "Switching page size %d → %d; restarting pagination at page 1",
                self._page_size,
                SUBSEQUENT_PAGE_SIZE,
            )
            self._current_page = 1
        self._is_loading = True
        self._progress.setVisible(True)
        self._load_more_btn.setVisible(False)
        self._filter_bar.set_loading(True)
        self._fetch_page(
            self._current_filters,
            self._current_page,
            per_page=SUBSEQUENT_PAGE_SIZE,
            generation=self._generation,
        )

    def _cancel_load(self) -> None:
        self._generation += 1  # invalidates all in-flight workers
        self._is_loading = False
        self._progress.setVisible(False)
        self._filter_bar.set_loading(False)
        self._detail_refresh_in_flight.clear()
        self._live_refresh_signals.clear()
        self._taxon_tree.cancel_pending()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_observation(self, idx: int) -> None:
        if not self._observations or idx < 0 or idx >= len(self._observations):
            return
        debug_timing = log.isEnabledFor(logging.DEBUG)
        started = time.monotonic() if debug_timing else 0.0
        previous_idx = self._current_obs_idx
        self._current_obs_idx = idx
        self._current_photo_idx = 0
        obs = self._observations[idx]

        # Update list selection
        self._result_list.set_current_index(idx)
        list_ready = time.monotonic() if debug_timing else 0.0

        # Show image
        self._show_photo(obs, 0)
        photo_ready = time.monotonic() if debug_timing else 0.0

        # Update metadata
        self._metadata_panel.update_observation(
            obs,
            0,
            authenticated_login=self._auth_state.login,
            pending_target_taxon=(
                self._pending_target_taxon
                if self._pending_target_obs_id == obs.obs_id
                else ""
            ),
        )
        metadata_ready = time.monotonic() if debug_timing else 0.0

        # Update status
        n = len(self._observations)
        total = self._total_results
        taxon_name = obs.display_taxon.display_name if obs.display_taxon else "?"
        self._status_label.setText(
            f"[{idx+1}/{n}  (total {total})]  {taxon_name}  ·  {obs.observer_login}  ·  {obs.display_date}"
        )

        # Auto-load next page when approaching the end of the loaded list
        self._maybe_auto_load_next_page()
        status_ready = time.monotonic() if debug_timing else 0.0

        # Trigger prefetch
        direction = 1 if idx > previous_idx else -1 if idx < previous_idx else 0
        self._prefetcher.update_position(
            idx,
            0,
            self._settings.prefetch_radius,
            direction=direction,
            include_secondary=self._active_arrow_key is None,
        )
        prefetch_ready = time.monotonic() if debug_timing else 0.0
        if self._active_arrow_key is None:
            self._refresh_observation_details_if_needed(obs, idx)
        if debug_timing:
            finished = time.monotonic()
            log.debug(
                "Observation selected index=%d previous=%d obs=%d direction=%d "
                "repeat=%s total=%.1fms list=%.1fms photo=%.1fms metadata=%.1fms "
                "status=%.1fms prefetch=%.1fms detail=%.1fms",
                idx,
                previous_idx,
                obs.obs_id,
                direction,
                self._active_arrow_key is not None,
                (finished - started) * 1000.0,
                (list_ready - started) * 1000.0,
                (photo_ready - list_ready) * 1000.0,
                (metadata_ready - photo_ready) * 1000.0,
                (status_ready - metadata_ready) * 1000.0,
                (prefetch_ready - status_ready) * 1000.0,
                (finished - prefetch_ready) * 1000.0,
            )

    def _refresh_observation_details_if_needed(
        self, obs: StudyObservation, idx: int
    ) -> None:
        if self._is_active_workflow_observation(obs.obs_id):
            return
        if obs.obs_id in self._detail_refreshed_obs_ids:
            return
        if obs.obs_id in self._detail_refresh_in_flight:
            return
        if obs.comments and obs.all_identifications:
            return
        self._detail_refresh_in_flight.add(obs.obs_id)
        worker = _ObservationRefreshWorker(
            self._client,
            obs.obs_id,
            idx,
            self._generation,
            lambda: self._generation,
        )
        sigs = worker.signals
        self._live_refresh_signals.add(sigs)
        sigs.refreshed.connect(
            lambda row, refreshed, s=sigs: (
                self._live_refresh_signals.discard(s),
                self._on_observation_details_refreshed(row, refreshed),
            )
        )
        sigs.error.connect(
            lambda _row, _msg, obs_id=obs.obs_id, s=sigs: (
                self._live_refresh_signals.discard(s),
                self._detail_refresh_in_flight.discard(obs_id),
                log.error(
                    "Observation detail refresh failed for obs=%s: %s", obs_id, _msg
                ),
            )
        )
        self._pool.start(worker)

    @Slot(int, object)
    def _on_observation_details_refreshed(
        self, idx: int, obs: StudyObservation
    ) -> None:
        self._detail_refresh_in_flight.discard(obs.obs_id)
        if not (0 <= idx < len(self._observations)):
            return
        if self._observations[idx].obs_id != obs.obs_id:
            return
        self._detail_refreshed_obs_ids.add(obs.obs_id)
        if (
            self._observations[idx].target_identification
            and not obs.target_identification
        ):
            obs.target_identification = self._observations[idx].target_identification
        self._replace_observation(obs)

    def _show_photo(self, obs: StudyObservation, photo_idx: int) -> None:
        if not obs.photos or photo_idx < 0 or photo_idx >= len(obs.photos):
            self._display_obs_id = None
            self._display_photo_id = None
            self._viewer.clear()
            return

        photo = obs.photos[photo_idx]
        self._current_photo_idx = photo_idx
        self._display_obs_id = obs.obs_id
        self._display_photo_id = photo.photo_id
        self._display_requested_at = (
            time.monotonic() if log.isEnabledFor(logging.DEBUG) else 0.0
        )
        self._viewer.set_photo_info(photo_idx, len(obs.photos))

        # Held-arrow review must not pause the GUI thread for disk reads or
        # image decoding. Neighbor prefetch populates memory asynchronously;
        # a miss is handed to the high-priority worker below.
        if self._active_arrow_key is not None:
            cached = self._prefetcher.get_best_memory_cached(photo.photo_id)
            cache_scope = "memory-only"
        else:
            cached = self._prefetcher.get_best_cached(photo.photo_id)
            cache_scope = "memory-or-disk"
        if cached is not None:
            pixmap, size = cached
            self._viewer.set_pixmap(pixmap, size)
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "Displayed cached photo obs=%d photo=%d size=%s pixels=%dx%d lookup=%s",
                    obs.obs_id,
                    photo.photo_id,
                    size,
                    pixmap.width(),
                    pixmap.height(),
                    cache_scope,
                )
            if size != "original":
                started_request = self._prefetcher.request_photo(
                    photo,
                    obs.obs_id,
                    self._current_obs_idx,
                    photo_idx,
                    priority=10,
                    request_mode=ImageRequestMode.NORMAL,
                )
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "Requested original upgrade obs=%d photo=%d started=%s",
                        obs.obs_id,
                        photo.photo_id,
                        started_request,
                    )
            return

        # Load current photo at high priority so it jumps the queue ahead of
        # background prefetch workers for neighbouring observations.
        self._viewer.set_loading(True)
        started_request = self._prefetcher.request_photo(
            photo,
            obs.obs_id,
            self._current_obs_idx,
            photo_idx,
            priority=10,
            request_mode=ImageRequestMode.NORMAL,
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Displayed loading state obs=%d photo=%d cache=%s "
                "request_started=%s in_flight=%s",
                obs.obs_id,
                photo.photo_id,
                cache_scope,
                started_request,
                self._prefetcher.is_request_in_flight(photo.photo_id),
            )

    @Slot(int, int, int, int, QPixmap)
    def _on_prefetch_ready(
        self,
        obs_idx: int,
        photo_idx: int,
        obs_id: int,
        photo_id: int,
        pixmap: QPixmap,
    ) -> None:
        """Called when prefetcher finishes loading an image."""
        if obs_id != self._display_obs_id or photo_id != self._display_photo_id:
            return
        self._viewer.set_loading(False)
        if not pixmap.isNull():
            self._viewer.set_pixmap(pixmap)
        if log.isEnabledFor(logging.DEBUG):
            elapsed_ms = max(
                0.0,
                (time.monotonic() - self._display_requested_at) * 1000.0,
            )
            log.debug(
                "Visible image ready obs=%d photo=%d index=%d "
                "elapsed=%.1fms pixels=%dx%d",
                obs_id,
                photo_id,
                obs_idx,
                elapsed_ms,
                pixmap.width(),
                pixmap.height(),
            )

    @Slot(int)
    def _on_result_selected(self, idx: int) -> None:
        if idx != self._current_obs_idx:
            self._show_observation(idx)

    def _next_observation(self) -> None:
        n = len(self._observations)
        if n == 0:
            return
        new_idx = min(self._current_obs_idx + 1, n - 1)
        if new_idx != self._current_obs_idx:
            self._show_observation(new_idx)

    def _prev_observation(self) -> None:
        if not self._observations:
            return
        new_idx = max(self._current_obs_idx - 1, 0)
        if new_idx != self._current_obs_idx:
            self._show_observation(new_idx)

    def _next_photo(self) -> None:
        if self._current_obs_idx < 0:
            return
        obs = self._observations[self._current_obs_idx]
        if not obs.photos:
            return
        new_idx = min(self._current_photo_idx + 1, len(obs.photos) - 1)
        if new_idx != self._current_photo_idx:
            self._current_photo_idx = new_idx
            self._show_photo(obs, new_idx)
            self._metadata_panel.update_observation(
                obs,
                new_idx,
                authenticated_login=self._auth_state.login,
                pending_target_taxon=(
                    self._pending_target_taxon
                    if self._pending_target_obs_id == obs.obs_id
                    else ""
                ),
            )
            n = len(obs.photos)
            self._status_label.setText(
                self._status_label.text().split("  photo")[0]
                + f"  photo {new_idx+1}/{n}"
            )

    def _prev_photo(self) -> None:
        if self._current_obs_idx < 0:
            return
        obs = self._observations[self._current_obs_idx]
        if not obs.photos:
            return
        new_idx = max(self._current_photo_idx - 1, 0)
        if new_idx != self._current_photo_idx:
            self._current_photo_idx = new_idx
            self._show_photo(obs, new_idx)
            self._metadata_panel.update_observation(
                obs,
                new_idx,
                authenticated_login=self._auth_state.login,
                pending_target_taxon=(
                    self._pending_target_taxon
                    if self._pending_target_obs_id == obs.obs_id
                    else ""
                ),
            )
            n = len(obs.photos)
            self._status_label.setText(
                self._status_label.text().split("  photo")[0]
                + f"  photo {new_idx+1}/{n}"
            )

    def _jump_to(self) -> None:
        n = len(self._observations)
        if n == 0:
            return
        idx, ok = QInputDialog.getInt(
            self,
            "Go to result",
            f"Enter result number (1–{n}):",
            self._current_obs_idx + 1,
            1,
            n,
        )
        if ok:
            self._show_observation(idx - 1)

    # ------------------------------------------------------------------
    # Navigation key handler (called by _NavFilter)
    # ------------------------------------------------------------------

    @Slot(float)
    def _set_navigation_repeat_rate(self, rate: float) -> None:
        self._navigation_repeat_rate = max(0.25, float(rate))
        self._settings.navigation_repeat_rate = self._navigation_repeat_rate
        if (
            self._active_arrow_key is not None
            and self._navigation_repeat_timer.isActive()
        ):
            self._navigation_repeat_deadline = (
                time.monotonic() + 1.0 / self._navigation_repeat_rate
            )
            self._schedule_arrow_repeat()
        log.debug("Arrow navigation repeat rate set to %.2f observations/sec", rate)

    def handle_arrow_key_press(self, key, modifiers, auto_repeat: bool) -> bool:
        """Start app-timed Left/Right repetition and swallow OS auto-repeat."""
        del modifiers
        if auto_repeat:
            return True

        was_running = self._active_arrow_key is not None
        self._held_arrow_keys.add(key)
        self._active_arrow_key = key
        now = time.monotonic()
        if not was_running:
            self._navigation_repeat_started_at = now
            self._navigation_repeat_steps = 0
            self._navigation_repeat_missed_beats = 0
            self._navigation_repeat_max_work_seconds = 0.0

        moved = self._step_arrow_navigation(key)
        if moved:
            self._navigation_repeat_steps += 1
            self._navigation_repeat_deadline = now + ARROW_REPEAT_INITIAL_DELAY_SECONDS
            self._schedule_arrow_repeat()
        return True

    def handle_arrow_key_release(self, key, auto_repeat: bool) -> bool:
        """Stop only on the physical release; synthetic repeat releases are ignored."""
        if auto_repeat:
            return key in self._held_arrow_keys
        if key not in self._held_arrow_keys:
            return False
        self._held_arrow_keys.discard(key)
        if key == self._active_arrow_key:
            if self._held_arrow_keys:
                self._active_arrow_key = next(iter(self._held_arrow_keys))
                self._navigation_repeat_deadline = (
                    time.monotonic() + 1.0 / self._navigation_repeat_rate
                )
                self._schedule_arrow_repeat()
            else:
                self.stop_arrow_navigation("key released")
        return True

    def stop_arrow_navigation(self, reason: str = "stopped") -> None:
        """Cancel held-arrow navigation and finish deferred current-item work."""
        if self._active_arrow_key is None and not self._held_arrow_keys:
            return
        if log.isEnabledFor(logging.DEBUG) and (
            self._navigation_repeat_steps > 1
            or self._navigation_repeat_missed_beats > 0
        ):
            elapsed = max(
                0.0,
                time.monotonic() - self._navigation_repeat_started_at,
            )
            log.debug(
                "Arrow navigation stopped reason=%s steps=%d repeats=%d "
                "elapsed=%.3fs actual_rate=%.2f/sec missed_beats=%d "
                "max_step_work=%.1fms",
                reason,
                self._navigation_repeat_steps,
                max(0, self._navigation_repeat_steps - 1),
                elapsed,
                self._navigation_repeat_steps / elapsed if elapsed else 0.0,
                self._navigation_repeat_missed_beats,
                self._navigation_repeat_max_work_seconds * 1000.0,
            )
        self._navigation_repeat_timer.stop()
        self._held_arrow_keys.clear()
        self._active_arrow_key = None
        if not self._closing and 0 <= self._current_obs_idx < len(self._observations):
            if reason in ("key released", "window deactivated"):
                self._prefetcher.update_position(
                    self._current_obs_idx,
                    self._current_photo_idx,
                    self._settings.prefetch_radius,
                    include_secondary=True,
                )
            self._refresh_observation_details_if_needed(
                self._observations[self._current_obs_idx],
                self._current_obs_idx,
            )

    def _repeat_arrow_navigation(self) -> None:
        key = self._active_arrow_key
        if key is None or key not in self._held_arrow_keys:
            return
        started = time.monotonic()
        if self._navigation_repeat_steps == 1 and log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Arrow navigation repeat activated direction=%s "
                "initial_delay=%.0fms rate=%.2f/sec interval=%.1fms",
                self._arrow_direction_name(key),
                ARROW_REPEAT_INITIAL_DELAY_SECONDS * 1000.0,
                self._navigation_repeat_rate,
                1000.0 / self._navigation_repeat_rate,
            )
        moved = self._step_arrow_navigation(key)
        finished = time.monotonic()
        if not moved:
            log.debug(
                "Arrow navigation reached the %s endpoint at index=%d",
                self._arrow_direction_name(key),
                self._current_obs_idx,
            )
            return
        self._navigation_repeat_steps += 1

        # Keep transitions on a monotonic cadence. Work time is subtracted
        # from the delay; if the UI misses one or more beats, skip those beats
        # instead of issuing a burst of catch-up selections.
        interval = 1.0 / self._navigation_repeat_rate
        missed_beats = 0
        self._navigation_repeat_deadline += interval
        while self._navigation_repeat_deadline <= finished:
            self._navigation_repeat_deadline += interval
            missed_beats += 1
        self._navigation_repeat_missed_beats += missed_beats
        self._navigation_repeat_max_work_seconds = max(
            self._navigation_repeat_max_work_seconds,
            finished - started,
        )
        self._schedule_arrow_repeat()

    def _schedule_arrow_repeat(self) -> None:
        delay_ms = max(
            1,
            round((self._navigation_repeat_deadline - time.monotonic()) * 1000.0),
        )
        self._navigation_repeat_timer.start(delay_ms)

    def _resume_arrow_navigation_if_held(self) -> None:
        """Resume a held key when newly-appended rows extend an old endpoint."""
        if (
            self._active_arrow_key is None
            or self._active_arrow_key not in self._held_arrow_keys
            or self._navigation_repeat_timer.isActive()
        ):
            return
        self._navigation_repeat_deadline = (
            time.monotonic() + 1.0 / self._navigation_repeat_rate
        )
        self._schedule_arrow_repeat()

    def _step_arrow_navigation(self, key) -> bool:
        before = self._current_obs_idx
        if key == Qt.Key.Key_Right:
            self._next_observation()
        else:
            self._prev_observation()
        return self._current_obs_idx != before

    @staticmethod
    def _arrow_direction_name(key) -> str:
        return "right" if key == Qt.Key.Key_Right else "left"

    def handle_nav_key(self, key, modifiers=None) -> bool:
        """Handle a navigation key. Returns True if consumed."""
        try:
            shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        except (TypeError, AttributeError):
            shift = False

        if key == Qt.Key.Key_Right or (key == Qt.Key.Key_Space and not shift):
            self._next_observation()
        elif key == Qt.Key.Key_Left or (key == Qt.Key.Key_Space and shift):
            self._prev_observation()
        elif key == Qt.Key.Key_Down or key == Qt.Key.Key_BracketRight:
            self._next_photo()
        elif key == Qt.Key.Key_Up or key == Qt.Key.Key_BracketLeft:
            self._prev_photo()
        elif key == Qt.Key.Key_G:
            self._jump_to()
        elif key == Qt.Key.Key_O:
            self._open_obs_in_browser()
        elif key == Qt.Key.Key_I:
            self._open_image_in_browser()
        elif key == Qt.Key.Key_L:
            self._viewer.toggle_zoom()
        elif key == Qt.Key.Key_R:
            self._filter_bar._on_load_clicked()
        elif key == Qt.Key.Key_F:
            self._filter_bar.focus_username()
        elif key == Qt.Key.Key_A:
            self._agree_current("consensus" if shift else "recent")
        else:
            return False
        return True

    # ------------------------------------------------------------------
    # Identification write actions
    # ------------------------------------------------------------------

    def _agree_current(self, mode: str) -> None:
        if not self._require_auth():
            return
        if self._current_obs_idx < 0 or not self._observations:
            self._status_label.setText("No observation selected.")
            return
        obs = self._observations[self._current_obs_idx]
        label = "consensus ID" if mode == "consensus" else "most recent non-self ID"
        reply = QMessageBox.question(
            self,
            "Post Identification?",
            f"This will refresh observation {obs.obs_id} and, if still valid, post "
            f"an identification as {self._auth_state.login} agreeing with the {label}.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._status_label.setText("Identification cancelled before posting.")
            return
        self._status_label.setText(
            f"Refreshing observation before agreeing with {label}…"
        )
        worker = _AgreeWorker(
            self._client,
            self._auth_state.api_token,
            self._auth_state.login,
            obs.obs_id,
            mode,
        )
        sigs = worker.signals
        self._live_agree_signals.add(sigs)
        sigs.finished.connect(
            lambda result, s=sigs: (
                self._live_agree_signals.discard(s),
                self._on_agree_finished(result),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_agree_signals.discard(s),
                self._on_agree_error(msg),
            )
        )
        self._pool.start(worker)

    @Slot(object)
    def _on_agree_finished(self, result: AgreeResult) -> None:
        if result.refreshed_observation:
            self._replace_observation(result.refreshed_observation)
        self._status_label.setText(result.message)
        if result.status == "posted":
            QMessageBox.information(self, "Identification Posted", result.message)
        elif result.status == "skipped":
            QMessageBox.information(self, "No Identification Posted", result.message)

    def _on_agree_error(self, msg: str) -> None:
        log.error("Identification error: %s", msg)
        self._status_label.setText("Identification failed.")
        QMessageBox.warning(self, "Identification Failed", msg)

    def _neighbour_obs_id(self, idx: int) -> Optional[int]:
        """Return the id of the row after `idx`, else the one before it."""
        if idx + 1 < len(self._observations):
            return self._observations[idx + 1].obs_id
        if idx - 1 >= 0:
            return self._observations[idx - 1].obs_id
        return None

    def _replace_observation(self, obs: StudyObservation) -> None:
        keep_workflow_visible = self._is_active_workflow_observation(obs.obs_id)
        for loaded_idx, loaded in enumerate(self._loaded_observations):
            if loaded.obs_id == obs.obs_id:
                if loaded.target_identification and not obs.target_identification:
                    obs.target_identification = loaded.target_identification
                self._loaded_observations[loaded_idx] = obs
                break

        for idx, existing in enumerate(self._observations):
            if existing.obs_id == obs.obs_id:
                if existing.target_identification and not obs.target_identification:
                    obs.target_identification = existing.target_identification
                if (
                    self._provisional_name_only
                    and not _is_provisional_observation(obs)
                    and not keep_workflow_visible
                ):
                    # This row is about to leave the filtered view. Anchor on a
                    # neighbour so the viewer stays where the user was reading
                    # instead of jumping back to the first result.
                    self._apply_observation_view_filter(
                        preferred_obs_id=self._neighbour_obs_id(idx)
                    )
                    return
                self._observations[idx] = obs
                self._result_list.replace_observation(idx, obs)
                if idx == self._current_obs_idx:
                    self._metadata_panel.update_observation(
                        obs,
                        self._current_photo_idx,
                        authenticated_login=self._auth_state.login,
                        pending_target_taxon=(
                            self._pending_target_taxon
                            if self._pending_target_obs_id == obs.obs_id
                            else ""
                        ),
                    )
                return

    def _start_bulk_agree_setup(self) -> None:
        if not self._require_auth():
            return
        from observation_workbench.ui.bulk_agree_dialogs import BulkAgreeSetupDialog

        dlg = BulkAgreeSetupDialog(
            prefill_url=self._bulk_agree_current_url_prefill(),
            defaults=self._bulk_agree_setup_defaults(),
            current_query_description=self._current_query_description(),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status_label.setText(
                "Provisional agreement workflow cancelled before planning."
            )
            return

        self._save_bulk_agree_setup_defaults(dlg)
        query = dlg.observation_query
        if query is None:
            filters = getattr(self, "_current_filters", None)
            if filters is None:
                QMessageBox.information(
                    self,
                    "Agree to Provisional IDs",
                    "Load a query before running the workflow against it.",
                )
                return
        else:
            filters = LoadFilters(
                query.display_url,
                observation_query=query,
                apply_taxon_filter_to_observation_url=False,
            )
        self._bulk_agree_options = {
            "require_dna_barcode_its": dlg.require_dna_barcode_its(),
            "only_if_needed": dlg.only_if_needed(),
            "max_observations": dlg.max_observations(),
            "delay_min_seconds": dlg.delay_min_seconds(),
            "delay_max_seconds": dlg.delay_max_seconds(),
            "dry_run": dlg.dry_run(),
        }
        self._start_bulk_provisional_plan(filters)

    def _bulk_agree_setup_defaults(self) -> dict:
        return {
            "url": self._settings.bulk_agree_url,
            "source_mode": self._settings.bulk_agree_source_mode,
            "require_dna_barcode_its": self._settings.bulk_agree_require_dna_barcode_its,
            "only_if_needed": self._settings.bulk_agree_only_if_needed,
            "max_observations": self._settings.bulk_agree_max_observations,
            "delay_min_seconds": self._settings.bulk_agree_delay_min_seconds,
            "delay_max_seconds": self._settings.bulk_agree_delay_max_seconds,
            "dry_run": self._settings.bulk_agree_dry_run,
        }

    def _save_bulk_agree_setup_defaults(self, dlg) -> None:
        query = dlg.observation_query
        self._settings.bulk_agree_source_mode = (
            "url" if query is not None else "current"
        )
        if query is not None:
            self._settings.bulk_agree_url = query.display_url
        self._settings.bulk_agree_require_dna_barcode_its = (
            dlg.require_dna_barcode_its()
        )
        self._settings.bulk_agree_only_if_needed = dlg.only_if_needed()
        self._settings.bulk_agree_max_observations = dlg.max_observations()
        self._settings.bulk_agree_delay_min_seconds = dlg.delay_min_seconds()
        self._settings.bulk_agree_delay_max_seconds = dlg.delay_max_seconds()
        self._settings.bulk_agree_dry_run = dlg.dry_run()
        self._settings.sync()

    def _bulk_agree_current_url_prefill(self) -> str:
        if self._settings.bulk_agree_url.strip():
            return ""
        return self._current_observations_url_prefill()

    def _current_query_description(self) -> str:
        """One-line summary of the loaded query, or "" when nothing is loaded."""
        filters = getattr(self, "_current_filters", None)
        if filters is None:
            return ""
        if filters.observation_query is not None:
            return filters.observation_query.display_url
        if filters.username:
            return f"Observations by {filters.username}"
        return "The query currently loaded in the viewer"

    def _start_bulk_provisional_plan(self, filters: LoadFilters) -> None:
        self._bulk_generation = getattr(self, "_bulk_generation", 0) + 1
        gen = self._bulk_generation
        self._status_label.setText("Planning provisional-name agreements…")
        worker = _BulkPlanWorker(
            self._loader,
            filters,
            self._auth_state.login,
            gen,
            lambda: getattr(self, "_bulk_generation", 0),
            self._bulk_agree_options,
            api_token=self._auth_state.api_token,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(self._on_bulk_plan_progress)
        sigs.planned.connect(
            lambda result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_plan_finished(result),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_plan_error(msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int)
    def _on_bulk_plan_progress(self, seen: int, total: int) -> None:
        self._status_label.setText(
            f"Planning provisional agreements: fetched {seen} of {total}…"
        )

    def _on_bulk_plan_finished(self, result: BulkAgreePlanResult) -> None:
        from observation_workbench.ui.bulk_agree_dialogs import (
            BulkAgreePreviewDialog,
            format_agree_plan_stats,
        )

        candidates = result.candidates
        stats = result.stats
        skip_notes = []
        if stats.skipped_no_dna_barcode_its:
            skip_notes.append(
                f"{stats.skipped_no_dna_barcode_its} skipped — no DNA Barcode ITS field"
            )
        if stats.skipped_already_agreed:
            skip_notes.append(
                f"{stats.skipped_already_agreed} skipped — already agreed"
            )
        if stats.skipped_already_research_grade:
            skip_notes.append(
                f"{stats.skipped_already_research_grade} skipped — already Research Grade for proposed taxon"
            )
        if stats.skipped_previously_withdrew:
            skip_notes.append(
                f"{stats.skipped_previously_withdrew} skipped — previously withdrew this provisional ID"
            )
        status_note = f"  ({', '.join(skip_notes)})" if skip_notes else ""

        if not candidates:
            self._status_label.setText(
                f"No provisional non-self IDs need agreement.{status_note}"
            )
            QMessageBox.information(
                self,
                "Agree to Provisional IDs",
                "No current non-self provisional IDs were found that need your "
                "agreement.\n\n" + format_agree_plan_stats(stats),
            )
            return

        self._status_label.setText(
            f"Found {len(candidates)} provisional IDs to preview.{status_note}"
        )
        if self._skip_agree_confirmation:
            self._status_label.setText(
                f"Found {len(candidates)} provisional IDs; starting without preview.{status_note}"
            )
            log.info(
                "Skipping bulk provisional agreement preview confirmation due to --skipagree"
            )
            self._start_bulk_execution(candidates, stats)
            return

        dlg = BulkAgreePreviewDialog(
            candidates,
            stats,
            client=self._client,
            disk_cache=self._disk_cache,
            api_token=self._auth_state.api_token,
            login=self._auth_state.login,
            on_skip_forever=self._add_bulk_agree_skip_for_candidate,
            on_unskip_forever=self._remove_bulk_agree_skip_for_candidate,
            request_reauthentication=self._reauthenticate_photo_browser,
            dry_run=self._bulk_agree_options.get("dry_run", False),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if dlg.back_requested():
                self._status_label.setText("Returned to provisional agreement setup.")
                QTimer.singleShot(0, self._start_bulk_agree_setup)
                return
            self._status_label.setText(
                "Bulk provisional agreement cancelled before posting."
            )
            return
        candidates = dlg.candidates()
        if not candidates:
            self._status_label.setText(
                "Bulk provisional agreement cancelled: no candidates remain."
            )
            QMessageBox.information(
                self,
                "Agree to Provisional IDs",
                "No candidates remain after photo browsing.",
            )
            return
        self._start_bulk_execution(candidates, stats)

    def _add_bulk_agree_skip_for_candidate(self, candidate: BulkAgreeCandidate) -> None:
        self._db.add_bulk_agree_skip(candidate.observation.obs_id)

    def _remove_bulk_agree_skip_for_candidate(
        self, candidate: BulkAgreeCandidate
    ) -> None:
        self._db.remove_bulk_agree_skip(candidate.observation.obs_id)

    def _on_bulk_plan_error(self, msg: str) -> None:
        log.error("Bulk plan error: %s", msg)
        self._status_label.setText("Failed to plan provisional agreements.")
        QMessageBox.warning(self, "Planning Failed", msg)

    def _start_bulk_execution(
        self,
        candidates: List[BulkAgreeCandidate],
        stats: Optional[BulkAgreePlanStats] = None,
    ) -> None:
        from observation_workbench.ui.bulk_agree_dialogs import BulkAgreeProgressDialog

        options = getattr(self, "_bulk_agree_options", {})
        self._bulk_candidates = candidates
        self._bulk_index = 0
        self._bulk_posted = 0
        self._bulk_skipped = 0
        self._bulk_failed = 0
        self._bulk_skipped_no_dna = stats.skipped_no_dna_barcode_its if stats else 0
        self._bulk_skipped_previously_withdrew = (
            stats.skipped_previously_withdrew if stats else 0
        )
        self._bulk_deferred_review_keys = set()
        self._bulk_resume_after_auth = False
        self._bulk_cancelled = False
        self._bulk_paused = False
        self._bulk_dialog = BulkAgreeProgressDialog(
            self,
            delay_min_seconds=options.get("delay_min_seconds", 10),
            delay_max_seconds=options.get("delay_max_seconds", 30),
            dry_run=options.get("dry_run", False),
        )
        self._bulk_dialog.cancel_requested.connect(self._bulk_cancel)
        self._bulk_dialog.skip_requested.connect(self._bulk_skip_current)
        self._bulk_dialog.skip_forever_requested.connect(self._bulk_skip_forever)
        self._bulk_dialog.skip_delay_requested.connect(self._bulk_skip_current_delay)
        self._bulk_dialog.pause_requested.connect(self._bulk_pause)
        self._bulk_dialog.resume_requested.connect(self._bulk_resume)
        self._bulk_dialog.open_all_review_requested.connect(self._bulk_open_all_review)
        self._bulk_dialog.delay_changed.connect(self._bulk_delay_range_changed)
        self._bulk_dialog.nav_key_pressed.connect(self.handle_nav_key)
        self._show_dialog_in_front(self._bulk_dialog)
        self._bulk_show_current()

    def _bulk_show_current(self) -> None:
        while True:
            if self._bulk_cancelled:
                self._bulk_finish(cancelled=True)
                return
            if self._bulk_index >= len(self._bulk_candidates):
                self._bulk_finish(cancelled=False)
                return
            candidate = self._bulk_candidates[self._bulk_index]
            review_reason = self._bulk_review_reason(candidate)
            candidate_key = self._bulk_candidate_key(candidate)
            if not (
                review_reason
                and candidate_key not in self._bulk_deferred_review_keys
                and self._bulk_index < len(self._bulk_candidates) - 1
            ):
                break

            self._bulk_deferred_review_keys.add(candidate_key)
            self._bulk_candidates.append(self._bulk_candidates.pop(self._bulk_index))
            if self._bulk_dialog:
                self._bulk_dialog.set_status(
                    "Moved item needing human review to the end of the queue."
                )
            log.debug(
                "Bulk provisional agreement deferred for human review obs=%s taxon=%s reason=%s",
                candidate.observation.obs_id,
                candidate.target.taxon_name,
                review_reason,
            )

        self._pending_target_obs_id = candidate.observation.obs_id
        self._pending_target_taxon = candidate.target.taxon_name
        self._show_workflow_observation(candidate.observation)
        if self._bulk_dialog:
            comment = build_agreement_comment(
                candidate.observation,
                self._auth_state.login,
                candidate.target.taxon_id,
            )
            self._bulk_dialog.show_candidate(
                self._bulk_index,
                len(self._bulk_candidates),
                candidate,
                comment=comment,
            )
            self._bulk_dialog.set_status("Reviewing refreshed target before posting.")
        self._bulk_start_delay(force_pause_reason=review_reason)

    def _bulk_candidate_key(self, candidate: BulkAgreeCandidate) -> tuple[int, int]:
        return candidate.observation.obs_id, candidate.target.taxon_id

    def _bulk_review_reason(self, candidate: BulkAgreeCandidate) -> str:
        if not self._bulk_dialog:
            return ""
        recent_not_prov, has_comments = needs_human_review(
            candidate.observation,
            candidate.target,
            self._auth_state.login,
        )
        reasons = []
        if recent_not_prov and self._bulk_dialog.pause_on_recent_not_provisional():
            reasons.append("most recent identification is not provisional")
        if has_comments and self._bulk_dialog.pause_on_comments_since():
            reasons.append("comments exist since the provisional name was proposed")
        return " and ".join(reasons)

    def _show_workflow_observation(self, obs: StudyObservation) -> None:
        for idx, existing in enumerate(self._observations):
            if existing.obs_id == obs.obs_id:
                self._show_observation(idx)
                return
        if not any(
            existing.obs_id == obs.obs_id for existing in self._loaded_observations
        ):
            self._loaded_observations.append(obs)
            # Registered so a later page cannot append a second copy of it.
            self._loaded_obs_ids.add(obs.obs_id)
        self._observations.append(obs)
        self._result_list.append_observations([obs])
        self._prefetcher.set_observations(
            self._observations,
            radius=self._settings.prefetch_radius,
        )
        self._total_results = max(self._total_results, len(self._observations))
        self._show_observation(len(self._observations) - 1)

    def _bulk_start_delay(self, *, force_pause_reason: str = "") -> None:
        min_delay, max_delay = self._bulk_delay_range()
        self._bulk_delay_remaining = (
            random.randint(min_delay, max_delay) if max_delay > 0 else 0
        )
        if self._bulk_delay_timer is None:
            self._bulk_delay_timer = QTimer(self)
            self._bulk_delay_timer.timeout.connect(self._bulk_tick_delay)
        if force_pause_reason:
            self._bulk_paused = True
            if self._bulk_dialog:
                self._bulk_dialog.set_paused_for_review(force_pause_reason)
            return
        if self._bulk_dialog:
            self._bulk_dialog.set_waiting()
            self._bulk_dialog.set_countdown(self._bulk_delay_remaining)
            self._bulk_dialog.set_status(
                "Review the observation above, then wait for auto-post or act."
            )
        if self._bulk_delay_remaining <= 0:
            QTimer.singleShot(0, self._bulk_post_current)
            return
        self._bulk_delay_timer.start(1000)

    def _bulk_delay_range(self) -> tuple[int, int]:
        if not self._bulk_dialog:
            return 10, 30
        return (
            self._bulk_dialog.delay_min_seconds(),
            self._bulk_dialog.delay_max_seconds(),
        )

    def _bulk_delay_range_changed(self, min_delay: int, max_delay: int) -> None:
        timer_active = self._bulk_delay_timer and self._bulk_delay_timer.isActive()
        if not timer_active:
            return
        if max_delay <= 0:
            self._bulk_delay_timer.stop()
            self._bulk_delay_remaining = 0
            if self._bulk_dialog:
                self._bulk_dialog.set_countdown(0)
                self._bulk_dialog.set_status(
                    "Delay set to 0; posting as soon as the API allows."
                )
            QTimer.singleShot(0, self._bulk_post_current)
            return
        self._bulk_delay_remaining = max(
            min_delay,
            min(self._bulk_delay_remaining, max_delay),
        )
        if self._bulk_dialog:
            self._bulk_dialog.set_countdown(self._bulk_delay_remaining)

    def _bulk_tick_delay(self) -> None:
        if getattr(self, "_bulk_paused", False):
            return
        self._bulk_delay_remaining -= 1
        if self._bulk_delay_remaining <= 0:
            if self._bulk_delay_timer:
                self._bulk_delay_timer.stop()
            self._bulk_post_current()
            return
        if self._bulk_dialog:
            self._bulk_dialog.set_countdown(self._bulk_delay_remaining)

    def _bulk_skip_current_delay(self) -> None:
        if self._bulk_paused:
            self._bulk_paused = False
            self._bulk_post_current()
        elif self._bulk_delay_timer and self._bulk_delay_timer.isActive():
            self._bulk_delay_timer.stop()
            self._bulk_post_current()

    def _bulk_skip_current(self) -> None:
        timer_active = self._bulk_delay_timer and self._bulk_delay_timer.isActive()
        if self._bulk_paused or timer_active:
            if self._bulk_delay_timer:
                self._bulk_delay_timer.stop()
            self._bulk_paused = False
            self._bulk_skipped += 1
            log.info(
                "Bulk provisional agreement skipped obs=%s taxon=%s",
                self._bulk_candidates[self._bulk_index].observation.obs_id,
                self._bulk_candidates[self._bulk_index].target.taxon_name,
            )
            self._bulk_index += 1
            self._bulk_show_current()
        elif self._bulk_dialog:
            self._bulk_dialog.set_status(
                "Cannot skip while a write request is in flight."
            )

    def _bulk_skip_forever(self) -> None:
        timer_active = self._bulk_delay_timer and self._bulk_delay_timer.isActive()
        if self._bulk_paused or timer_active:
            if self._bulk_delay_timer:
                self._bulk_delay_timer.stop()
            self._bulk_paused = False
            candidate = self._bulk_candidates[self._bulk_index]
            obs_id = candidate.observation.obs_id
            self._db.add_bulk_agree_skip(obs_id)
            self._bulk_skipped += 1
            log.info(
                "Bulk provisional agreement skipped forever obs=%s taxon=%s",
                obs_id,
                candidate.target.taxon_name,
            )
            self._bulk_index += 1
            self._bulk_show_current()
        elif self._bulk_dialog:
            self._bulk_dialog.set_status(
                "Cannot skip while a write request is in flight."
            )

    def _bulk_pause(self) -> None:
        self._bulk_paused = True
        if self._bulk_delay_timer and self._bulk_delay_timer.isActive():
            self._bulk_delay_timer.stop()
        if self._bulk_dialog:
            self._bulk_dialog.set_status("Paused — click Resume to continue.")
            self._bulk_dialog.set_countdown(self._bulk_delay_remaining)

    def _bulk_open_all_review(self) -> None:
        """Open every remaining observation needing human review, skipping all of them.

        Items needing review are deferred to the end of the queue by
        :meth:`_bulk_show_current`, so by the time the reviewer is paused on one
        the rest tend to be bunched at the tail. This lets the reviewer hand the
        whole batch off to their browser at once instead of clicking Resume/Skip
        through each one individually.
        """
        if not self._bulk_dialog or not self._bulk_paused:
            return
        review_indices = [
            i
            for i in range(self._bulk_index, len(self._bulk_candidates))
            if self._bulk_review_reason(self._bulk_candidates[i])
        ]
        if not review_indices:
            return
        for i in review_indices:
            open_external_url_silently(self._bulk_candidates[i].observation.url)
        if self._bulk_delay_timer:
            self._bulk_delay_timer.stop()
        self._bulk_paused = False
        count = len(review_indices)
        for i in sorted(review_indices, reverse=True):
            del self._bulk_candidates[i]
        self._bulk_skipped += count
        log.info(
            "Bulk provisional agreement opened %d observation(s) needing human "
            "review in the browser and skipped them",
            count,
        )
        if self._bulk_dialog:
            self._bulk_dialog.set_status(
                f"Opened {count} observation(s) needing human review in your "
                "browser and skipped them here."
            )
        self._bulk_show_current()

    def _bulk_resume(self) -> None:
        self._bulk_paused = False
        if self._bulk_delay_remaining > 0:
            if self._bulk_delay_timer is None:
                self._bulk_delay_timer = QTimer(self)
                self._bulk_delay_timer.timeout.connect(self._bulk_tick_delay)
            if self._bulk_dialog:
                self._bulk_dialog.set_status("Resumed — review the observation above.")
                self._bulk_dialog.set_countdown(self._bulk_delay_remaining)
            self._bulk_delay_timer.start(1000)
        else:
            self._bulk_post_current()

    def _bulk_cancel(self) -> None:
        self._bulk_cancelled = True
        self._bulk_resume_after_auth = False
        if self._bulk_delay_timer:
            self._bulk_delay_timer.stop()
        self._bulk_finish(cancelled=True)

    def _bulk_post_current(self, allow_changed_target: bool = False) -> None:
        if self._bulk_cancelled or self._bulk_index >= len(self._bulk_candidates):
            self._bulk_finish(cancelled=self._bulk_cancelled)
            return
        candidate = self._bulk_candidates[self._bulk_index]
        body = self._bulk_dialog.get_comment() if self._bulk_dialog else ""
        if self._bulk_dialog:
            self._bulk_dialog.set_posting()
            self._bulk_dialog.set_status(
                "Refreshing observation and posting if still valid…"
            )
        worker = _BulkPostWorker(
            self._client,
            self._auth_state.api_token,
            self._auth_state.login,
            candidate,
            body=body,
            allow_changed_target=allow_changed_target,
            dry_run=getattr(self, "_bulk_agree_options", {}).get("dry_run", False),
            only_if_needed=getattr(self, "_bulk_agree_options", {}).get(
                "only_if_needed", True
            ),
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.finished.connect(
            lambda candidate, result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_post_finished(candidate, result),
            )
        )
        sigs.error.connect(
            lambda candidate, msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_post_error(candidate, msg),
            )
        )
        self._pool.start(worker)

    @Slot(object, object)
    def _on_bulk_post_finished(
        self, candidate: BulkAgreeCandidate, result: AgreeResult
    ) -> None:
        if getattr(self, "_bulk_cancelled", False):
            return
        if result.refreshed_observation:
            self._replace_observation(result.refreshed_observation)
        if result.status == "changed":
            self._handle_bulk_changed_target(candidate, result)
            return
        if result.status == "posted":
            self._bulk_posted += 1
            log.debug(
                "Bulk provisional agreement posted obs=%s taxon=%s",
                candidate.observation.obs_id,
                (
                    result.target.taxon_name
                    if result.target
                    else candidate.target.taxon_name
                ),
            )
        else:
            self._bulk_skipped += 1
            log.debug(
                "Bulk provisional agreement skipped obs=%s reason=%s",
                candidate.observation.obs_id,
                result.message,
            )
        if self._bulk_dialog:
            self._bulk_dialog.set_status(result.message)
        self._bulk_index += 1
        self._bulk_show_current()

    def _handle_bulk_changed_target(
        self,
        candidate: BulkAgreeCandidate,
        result: AgreeResult,
    ) -> None:
        preview_target = candidate.target
        current_target = result.target
        preview_source = preview_target.source_login or "unknown"
        current_source = (
            (current_target.source_login or "unknown") if current_target else "unknown"
        )
        preview_ident_id = (
            preview_target.source_ident_id
            if preview_target.source_ident_id is not None
            else "unknown"
        )
        current_ident_id = (
            current_target.source_ident_id
            if current_target and current_target.source_ident_id is not None
            else "unknown"
        )
        preview_taxon_name = preview_target.taxon_name or "unknown"
        preview_taxon_id = preview_target.taxon_id
        current_taxon_name = current_target.taxon_name if current_target else "unknown"
        current_taxon_id = current_target.taxon_id if current_target else "unknown"
        msg = (
            "The provisional target taxon changed after refresh.\n\n"
            f"Observation: {candidate.observation.obs_id}\n\n"
            f"Preview source: {preview_source}  ident {preview_ident_id}\n"
            f"Preview taxon: {preview_taxon_name}  (taxon_id={preview_taxon_id})\n\n"
            f"Current source: {current_source}  ident {current_ident_id}\n"
            f"Current taxon: {current_taxon_name}  (taxon_id={current_taxon_id})\n\n"
            "Continue will post the refreshed taxon."
        )
        box = QMessageBox(self)
        box.setWindowTitle("Taxon Target Changed")
        box.setText(msg)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        skip_btn = box.addButton("Skip this ID", QMessageBox.ButtonRole.DestructiveRole)
        continue_btn = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            self._bulk_cancel()
        elif clicked is skip_btn:
            self._bulk_skipped += 1
            self._bulk_index += 1
            self._bulk_show_current()
        elif clicked is continue_btn and result.target and result.refreshed_observation:
            self._bulk_candidates[self._bulk_index] = BulkAgreeCandidate(
                observation=result.refreshed_observation,
                target=result.target,
                user_has_different_id=not already_current_taxon(
                    result.refreshed_observation,
                    self._auth_state.login,
                    result.target.taxon_id,
                ),
            )
            self._bulk_post_current(allow_changed_target=True)

    def _on_bulk_post_error(self, candidate: BulkAgreeCandidate, msg: str) -> None:
        if getattr(self, "_bulk_cancelled", False):
            return
        log.error(
            "Bulk post failed: obs=%s taxon=%s — %s",
            candidate.observation.obs_id,
            candidate.target.taxon_name,
            msg,
        )
        if _is_auth_failure_message(msg):
            self._handle_bulk_auth_error(candidate, msg)
            return
        self._bulk_failed += 1
        if self._bulk_dialog:
            self._bulk_dialog.set_status("Error: " + msg)
        box = QMessageBox(self)
        box.setWindowTitle("Identification Error")
        box.setText(
            "A write action failed and the workflow is paused.\n\n"
            f"Observation: {candidate.observation.obs_id}\n"
            f"Target taxon: {candidate.target.taxon_name}\n\n"
            f"{msg}"
        )
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        skip_btn = box.addButton("Skip this ID", QMessageBox.ButtonRole.DestructiveRole)
        continue_btn = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            self._bulk_cancel()
        elif clicked is skip_btn:
            self._bulk_index += 1
            self._bulk_show_current()
        elif clicked is continue_btn:
            self._bulk_post_current()

    def _handle_bulk_auth_error(self, candidate: BulkAgreeCandidate, msg: str) -> None:
        if self._bulk_delay_timer:
            self._bulk_delay_timer.stop()
        self._bulk_paused = True
        self._bulk_resume_after_auth = True

        self._auth_service.clear()
        self._auth_state = AuthState()
        self._identify_actions.authentication_changed()
        self._reconciliation_authentication_changed()
        self._update_auth_ui()

        summary = (
            "iNaturalist authentication expired or was rejected. "
            "The automatic identification workflow is paused and will continue "
            "after a fresh token is validated."
        )
        if self._bulk_dialog:
            self._bulk_dialog.set_status(summary)
        self._status_label.setText(summary)

        box = QMessageBox(self)
        box.setWindowTitle("Authentication Expired")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "iNaturalist rejected the saved API token.\n\n"
            "No identification was posted for the current observation. The "
            "automatic workflow is paused and will retry this same item after "
            "you authenticate with a fresh token.\n\n"
            f"Observation: {candidate.observation.obs_id}\n"
            f"Target taxon: {candidate.target.taxon_name}\n\n"
            "Authenticate again with a fresh iNaturalist API token to continue."
        )
        box.setDetailedText(msg)
        auth_btn = box.addButton("Authenticate Now", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton(
            "Cancel Automatic ID", QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(auth_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is auth_btn:
            self._authenticate_to_inaturalist()
        elif clicked is cancel_btn:
            self._bulk_resume_after_auth = False
            self._bulk_cancel()

    def _resume_bulk_after_auth(self) -> None:
        self._bulk_resume_after_auth = False
        if (
            self._bulk_cancelled
            or not self._bulk_dialog
            or self._bulk_index >= len(self._bulk_candidates)
        ):
            return
        self._bulk_paused = False
        self._bulk_dialog.set_status(
            "Authentication refreshed; retrying current identification…"
        )
        self._status_label.setText(
            "Authentication refreshed; continuing automatic identification."
        )
        QTimer.singleShot(0, self._bulk_post_current)

    def _bulk_finish(self, cancelled: bool) -> None:
        self._pending_target_obs_id = None
        self._pending_target_taxon = ""
        self._bulk_resume_after_auth = False
        if self._bulk_delay_timer:
            self._bulk_delay_timer.stop()
        skipped_no_dna = getattr(self, "_bulk_skipped_no_dna", 0)
        skipped_previously_withdrew = getattr(
            self, "_bulk_skipped_previously_withdrew", 0
        )
        if self._bulk_dialog:
            self._bulk_dialog.set_summary(
                self._bulk_posted,
                self._bulk_skipped,
                self._bulk_failed,
                cancelled,
                skipped_no_dna=skipped_no_dna,
                skipped_previously_withdrew=skipped_previously_withdrew,
            )
        dna_note = (
            f", {skipped_no_dna} skipped (no DNA barcode)" if skipped_no_dna else ""
        )
        withdrew_note = (
            f", {skipped_previously_withdrew} skipped (previously withdrew provisional ID)"
            if skipped_previously_withdrew
            else ""
        )
        dry_run_note = (
            " (dry run — nothing was posted)"
            if getattr(self, "_bulk_agree_options", {}).get("dry_run", False)
            else ""
        )
        self._status_label.setText(
            f"Bulk provisional workflow {'cancelled' if cancelled else 'complete'}: "
            f"{self._bulk_posted} posted, {self._bulk_skipped} skipped, "
            f"{self._bulk_failed} failed{dna_note}{withdrew_note}.{dry_run_note}"
        )

    def _start_provisional_name_swap(self) -> None:
        if not self._require_auth():
            return
        source_name, ok = QInputDialog.getText(
            self,
            "Provisional Name Swap",
            "Provisional name to search for:",
        )
        source_name = source_name.strip()
        if not ok or not source_name:
            self._status_label.setText("Provisional name swap cancelled before search.")
            return

        self._provisional_swap_generation += 1
        gen = self._provisional_swap_generation
        self._status_label.setText(
            f"Searching iNaturalist for Provisional Species Name = {source_name}…"
        )
        worker = _ProvisionalSwapPlanWorker(
            self._client,
            source_name,
            gen,
            lambda: self._provisional_swap_generation,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(
            lambda seen, total, g=gen: (
                self._on_provisional_swap_plan_progress(seen, total)
                if g == self._provisional_swap_generation
                else None
            )
        )
        sigs.planned.connect(
            lambda plan, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_provisional_swap_plan_finished(plan),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_provisional_swap_plan_error(msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int)
    def _on_provisional_swap_plan_progress(self, seen: int, total: int) -> None:
        self._status_label.setText(
            f"Searching provisional names: scanned {seen} of {total} observation(s)…"
        )

    @Slot(object)
    def _on_provisional_swap_plan_finished(self, plan: ProvisionalSwapPlan) -> None:
        count = plan.total_observations
        self._status_label.setText(
            f"Found {count} observation(s) using provisional name {plan.source_name}."
        )
        if count <= 0:
            QMessageBox.information(
                self,
                "Provisional Name Swap",
                f"No observations were found using provisional name:\n\n{plan.source_name}",
            )
            return
        if not plan.field_values:
            QMessageBox.warning(
                self,
                "Provisional Name Swap",
                "iNaturalist returned matching observations, but no editable "
                "Provisional Species Name field value IDs were found in the API response.",
            )
            return

        destination_name, ok = QInputDialog.getText(
            self,
            "Provisional Name Swap",
            (
                f"Found {count} observation(s) using:\n{plan.source_name}\n\n"
                "Destination name:"
            ),
        )
        destination_name = destination_name.strip()
        if not ok or not destination_name:
            self._status_label.setText(
                "Provisional name swap cancelled before writing."
            )
            return
        if destination_name == plan.source_name:
            QMessageBox.information(
                self,
                "Provisional Name Swap",
                "Destination name is the same as the provisional name.",
            )
            return

        missing_note = ""
        if plan.missing_field_value_ids:
            missing_note = (
                f"\n\n{plan.missing_field_value_ids} matching observation(s) do not "
                "have editable field value IDs in the API response and will be skipped."
            )
        reply = QMessageBox.question(
            self,
            "Confirm Provisional Name Swap",
            (
                f"This will update {len(plan.field_values)} iNaturalist observation "
                "field value(s):\n\n"
                f"From: {plan.source_name}\n"
                f"To:   {destination_name}\n\n"
                f"The write will be made as {self._auth_state.login}."
                f"{missing_note}\n\nContinue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._status_label.setText(
                "Provisional name swap cancelled before writing."
            )
            return
        self._start_provisional_swap_post(plan, destination_name)

    def _on_provisional_swap_plan_error(self, msg: str) -> None:
        log.error("Provisional name swap search failed: %s", msg)
        self._status_label.setText("Failed to search provisional names.")
        QMessageBox.warning(self, "Provisional Name Search Failed", msg)

    def _start_provisional_swap_post(
        self,
        plan: ProvisionalSwapPlan,
        destination_name: str,
    ) -> None:
        self._provisional_swap_generation += 1
        gen = self._provisional_swap_generation
        self._status_label.setText("Swapping provisional names on iNaturalist…")
        worker = _ProvisionalSwapPostWorker(
            self._client,
            self._auth_state.api_token,
            plan,
            destination_name,
            gen,
            lambda: self._provisional_swap_generation,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(
            lambda current, total, updated, skipped, failed, message, g=gen: (
                self._on_provisional_swap_post_progress(
                    current, total, updated, skipped, failed, message
                )
                if g == self._provisional_swap_generation
                else None
            )
        )
        sigs.finished.connect(
            lambda result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_provisional_swap_post_finished(result),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_provisional_swap_post_error(msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int, int, int, int, str)
    def _on_provisional_swap_post_progress(
        self,
        current: int,
        total: int,
        updated: int,
        skipped: int,
        failed: int,
        message: str,
    ) -> None:
        self._status_label.setText(
            f"Swapping provisional names: {current}/{total}; "
            f"{updated} updated, {skipped} skipped, {failed} failed. {message}"
        )

    @Slot(object)
    def _on_provisional_swap_post_finished(self, result: ProvisionalSwapResult) -> None:
        self._replace_provisional_name_in_loaded_observations(
            result.updated_ids,
            result.destination_name,
        )
        self._status_label.setText(
            "Provisional name swap complete: "
            f"{result.updated} updated, {result.skipped} skipped, {result.failed} failed."
        )
        msg = (
            f"From: {result.source_name}\n"
            f"To:   {result.destination_name}\n\n"
            f"Updated: {result.updated}\n"
            f"Skipped: {result.skipped}\n"
            f"Failed: {result.failed}"
        )
        if result.errors:
            msg += "\n\nFirst errors:\n" + "\n".join(result.errors[:10])
        QMessageBox.information(self, "Provisional Name Swap Complete", msg)

    def _on_provisional_swap_post_error(self, msg: str) -> None:
        log.error("Provisional name swap failed: %s", msg)
        self._status_label.setText("Provisional name swap failed.")
        if _is_auth_failure_message(msg):
            self._auth_service.clear()
            self._auth_state = AuthState()
            self._identify_actions.authentication_changed()
            self._reconciliation_authentication_changed()
            self._update_auth_ui()
            QMessageBox.warning(
                self,
                "Authentication Expired",
                "iNaturalist rejected the saved API token. Authenticate again, "
                "then restart the provisional name swap.\n\n" + msg,
            )
            return
        QMessageBox.warning(self, "Provisional Name Swap Failed", msg)

    def _replace_provisional_name_in_loaded_observations(
        self,
        observation_ids: list[int],
        destination_name: str,
    ) -> None:
        if not observation_ids:
            return
        updated_ids = set(observation_ids)
        for idx, obs in enumerate(self._observations):
            if obs.obs_id not in updated_ids:
                continue
            obs.provisional_species_name = destination_name
            self._result_list.replace_observation(idx, obs)
            if idx == self._current_obs_idx:
                self._metadata_panel.update_observation(
                    obs,
                    self._current_photo_idx,
                    authenticated_login=self._auth_state.login,
                    pending_target_taxon=(
                        self._pending_target_taxon
                        if self._pending_target_obs_id == obs.obs_id
                        else ""
                    ),
                )

    def _start_species_override_update(
        self,
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    ) -> None:
        if not self._require_auth():
            return
        from observation_workbench.ui.species_override_dialogs import (
            SpeciesOverrideSetupDialog,
        )

        prefill = ""
        if 0 <= self._current_obs_idx < len(self._observations):
            prefill = self._observations[self._current_obs_idx].provisional_species_name
        dlg = SpeciesOverrideSetupDialog(
            prefill_provisional_name=prefill,
            target_field_name=target_field_name,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status_label.setText(
                f"{target_field_name} update cancelled before planning."
            )
            return

        provisional_name = dlg.provisional_name()
        override_name = dlg.override_name()
        source_mode = dlg.source_mode()
        observation_ids = dlg.observation_ids()
        genus_filter = dlg.genus_filter()
        self._species_override_generation += 1
        gen = self._species_override_generation
        if source_mode == "observations":
            status = f"Loading {len(observation_ids)} pasted observation(s) from iNaturalist..."
        else:
            status = (
                "Searching iNaturalist for Provisional Species Name = "
                f"{provisional_name}..."
            )
        if genus_filter:
            status += f" Genus gate: {genus_filter}."
        self._status_label.setText(status)
        worker = _SpeciesOverridePlanWorker(
            self._client,
            provisional_name,
            override_name,
            source_mode,
            observation_ids,
            genus_filter,
            target_field_name,
            gen,
            lambda: self._species_override_generation,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(
            lambda seen, total, g=gen: (
                self._on_species_override_plan_progress(seen, total)
                if g == self._species_override_generation
                else None
            )
        )
        sigs.planned.connect(
            lambda plan, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_species_override_plan_finished(plan),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs, field_name=target_field_name: (
                self._live_bulk_signals.discard(s),
                self._on_species_override_plan_error(msg, field_name),
            )
        )
        self._pool.start(worker)

    @Slot(int, int)
    def _on_species_override_plan_progress(self, seen: int, total: int) -> None:
        self._status_label.setText(
            f"Planning observation field update: scanned {seen} of {total} observation(s)..."
        )

    @Slot(object)
    def _on_species_override_plan_finished(self, plan: SpeciesOverridePlan) -> None:
        from observation_workbench.ui.species_override_dialogs import (
            SpeciesOverridePlanDialog,
        )

        count = plan.total_observations
        self._status_label.setText(
            f"Planned {plan.target_field_name} for {count} observation(s): "
            f"{plan.updatable_row_count} selectable, {plan.skipped_row_count} skipped."
        )
        if count <= 0:
            if plan.source_mode == "observations":
                message = "No observation IDs were provided."
            else:
                message = (
                    "No observations were found using provisional name:\n\n"
                    f"{plan.provisional_name}"
                )
            QMessageBox.information(
                self,
                f"Update {plan.target_field_name}",
                message,
            )
            return
        if not plan.rows:
            QMessageBox.warning(
                self,
                f"Update {plan.target_field_name}",
                "iNaturalist returned no usable observation rows for this plan.",
            )
            return

        dlg = SpeciesOverridePlanDialog(
            plan,
            login=self._auth_state.login,
            client=self._client,
            disk_cache=self._disk_cache,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status_label.setText(
                f"{plan.target_field_name} update cancelled before writing."
            )
            return
        selected_ids = dlg.selected_observation_ids()
        if not selected_ids:
            self._status_label.setText(
                f"{plan.target_field_name} update cancelled: no rows selected."
            )
            return
        self._start_species_override_post(plan, selected_ids)

    def _on_species_override_plan_error(
        self,
        msg: str,
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    ) -> None:
        log.error("%s update planning failed: %s", target_field_name, msg)
        self._status_label.setText(f"Failed to plan {target_field_name} update.")
        QMessageBox.warning(self, f"{target_field_name} Planning Failed", msg)

    def _start_species_override_post(
        self,
        plan: SpeciesOverridePlan,
        selected_observation_ids: list[int],
    ) -> None:
        self._species_override_generation += 1
        gen = self._species_override_generation
        self._status_label.setText(
            f"Updating {plan.target_field_name} on iNaturalist..."
        )
        worker = _SpeciesOverridePostWorker(
            self._client,
            self._auth_state.api_token,
            plan,
            selected_observation_ids,
            gen,
            lambda: self._species_override_generation,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(
            lambda current, total, changed, skipped, failed, message, g=gen: (
                self._on_species_override_post_progress(
                    current,
                    total,
                    changed,
                    skipped,
                    failed,
                    message,
                )
                if g == self._species_override_generation
                else None
            )
        )
        sigs.finished.connect(
            lambda result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_species_override_post_finished(result),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs, field_name=plan.target_field_name: (
                self._live_bulk_signals.discard(s),
                self._on_species_override_post_error(msg, field_name),
            )
        )
        self._pool.start(worker)

    @Slot(int, int, int, int, int, str)
    def _on_species_override_post_progress(
        self,
        current: int,
        total: int,
        changed: int,
        skipped: int,
        failed: int,
        message: str,
    ) -> None:
        self._status_label.setText(
            f"Updating species-name fields: {current}/{total}; "
            f"{changed} changed, {skipped} skipped, {failed} failed. {message}"
        )

    @Slot(object)
    def _on_species_override_post_finished(
        self,
        result: SpeciesOverrideResult,
    ) -> None:
        self._replace_species_override_in_loaded_observations(
            result.applied_ids,
            result.override_name,
            result.target_field_name,
        )
        self._status_label.setText(
            f"{result.target_field_name} update complete: "
            f"{result.changed} changed, {result.unchanged} unchanged, "
            f"{result.skipped} skipped, {result.failed} failed."
        )
        source = result.source_label or (
            f"Provisional Species Name = {result.provisional_name}"
            if result.provisional_name
            else "Pasted observation list"
        )
        msg = (
            f"Source: {source}\n"
            f"{result.target_field_name}:  {result.override_name}\n"
        )
        if result.genus_filter:
            msg += f"Genus gate: {result.genus_filter}\n"
        msg += (
            "\n"
            f"Selected: {result.selected}\n"
            f"Created: {len(result.created_ids)}\n"
            f"Updated: {len(result.updated_ids)}\n"
            f"Unchanged: {result.unchanged}\n"
            f"Skipped: {result.skipped}\n"
            f"Failed: {result.failed}"
        )
        if result.errors:
            msg += "\n\nFirst errors:\n" + "\n".join(result.errors[:10])
        QMessageBox.information(
            self,
            f"Update {result.target_field_name} Complete",
            msg,
        )

    def _on_species_override_post_error(
        self,
        msg: str,
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    ) -> None:
        log.error("%s update failed: %s", target_field_name, msg)
        self._status_label.setText(f"{target_field_name} update failed.")
        if _is_auth_failure_message(msg):
            self._auth_service.clear()
            self._auth_state = AuthState()
            self._identify_actions.authentication_changed()
            self._reconciliation_authentication_changed()
            self._update_auth_ui()
            QMessageBox.warning(
                self,
                "Authentication Expired",
                "iNaturalist rejected the saved API token. Authenticate again, "
                f"then restart the {target_field_name} update.\n\n" + msg,
            )
            return
        QMessageBox.warning(self, f"{target_field_name} Update Failed", msg)

    def _replace_species_override_in_loaded_observations(
        self,
        observation_ids: list[int],
        value: str,
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    ) -> None:
        if not observation_ids:
            return
        updated_ids = set(observation_ids)
        for idx, obs in enumerate(self._observations):
            if obs.obs_id not in updated_ids:
                continue
            if target_field_name == PROVISIONAL_SPECIES_FIELD_NAME:
                obs.provisional_species_name = value
            else:
                obs.species_name_override = value
            self._result_list.replace_observation(idx, obs)
            if idx == self._current_obs_idx:
                self._metadata_panel.update_observation(
                    obs,
                    self._current_photo_idx,
                    authenticated_login=self._auth_state.login,
                    pending_target_taxon=(
                        self._pending_target_taxon
                        if self._pending_target_obs_id == obs.obs_id
                        else ""
                    ),
                )

    def _start_bulk_disagree_setup(self) -> None:
        if not self._require_auth():
            return
        from observation_workbench.ui.bulk_disagree_dialogs import (
            BulkDisagreeSetupDialog,
        )

        prefill_url = self._bulk_disagree_current_url_prefill()
        dlg = BulkDisagreeSetupDialog(
            self._client,
            prefill_url=prefill_url,
            defaults=self._bulk_disagree_setup_defaults(),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status_label.setText(
                "Bulk disagree workflow cancelled before planning."
            )
            return

        self._save_bulk_disagree_setup_defaults(dlg)
        source_taxon = dlg.source_taxon
        source_provisional_name = dlg.source_provisional_name()
        self._disagree_default_comment = dlg.comment()
        if source_taxon is not None:
            source_taxon_id = source_taxon.taxon_id
            source_taxon_name = source_taxon.name
            explicit_disagreement = taxon_is_strict_ancestor(
                source_taxon, dlg.target_taxon_id
            )
            require_source_taxon_match = dlg.require_source_taxon_match()
        elif source_provisional_name:
            # URL had no taxon_id but filters by a Provisional Species Name field
            # value: that name is the source identity. Post plain identifications,
            # but the safety check (when enabled) verifies the field value still
            # matches before posting to each observation.
            source_taxon_id = 0
            source_taxon_name = ""
            explicit_disagreement = False
            require_source_taxon_match = dlg.require_source_taxon_match()
        else:
            # URL had no taxon_id: no source taxon, so there is nothing to match
            # against and nothing to disagree with — post plain identifications.
            source_taxon_id = 0
            source_taxon_name = ""
            explicit_disagreement = False
            require_source_taxon_match = False
        self._disagree_options = {
            "source_taxon_id": source_taxon_id,
            "source_taxon_name": source_taxon_name,
            "source_provisional_name": source_provisional_name,
            "target_taxon_id": dlg.target_taxon_id,
            "target_taxon_name": dlg.target_taxon_name,
            "target_taxon_rank": dlg.target_taxon_rank,
            "explicit_disagreement": explicit_disagreement,
            "skip_with_dna_barcode_its": dlg.skip_with_dna_barcode_its(),
            "only_with_dna_barcode_its": dlg.only_with_dna_barcode_its(),
            "require_source_taxon_match": require_source_taxon_match,
            "max_observations": dlg.max_observations(),
            "dqa_vote_requested": dlg.dqa_vote_requested(),
            "dqa_vote_planned": dlg.dqa_vote_planned(),
            "dry_run": dlg.dry_run(),
            "delay_min_seconds": dlg.delay_min_seconds(),
            "delay_max_seconds": dlg.delay_max_seconds(),
        }
        self._disagree_progress_title = "Bulk Disagree to Taxon from URL"
        self._start_bulk_disagree_plan(dlg.observation_query)

    def _start_propose_name_setup(self) -> None:
        if not self._require_auth():
            return
        from observation_workbench.ui.bulk_disagree_dialogs import (
            ProposeNameSetupDialog,
        )

        dlg = ProposeNameSetupDialog(
            self._client,
            defaults=self._propose_name_setup_defaults(),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status_label.setText(
                "Propose-name workflow cancelled before planning."
            )
            return

        self._save_propose_name_setup_defaults(dlg)
        self._disagree_default_comment = dlg.comment()
        observation_ids = dlg.observation_ids()
        # No source taxon: these are explicit observation numbers, so there is
        # nothing to match against. The proposed name is always posted with the
        # explicit disagreement flag.
        self._disagree_options = {
            "source_taxon_id": 0,
            "source_taxon_name": "",
            "target_taxon_id": dlg.target_taxon_id,
            "target_taxon_name": dlg.target_taxon_name,
            "target_taxon_rank": dlg.target_taxon_rank,
            # None defers to each candidate's per-observation flag, set during
            # planning: disagree only when the proposed name differs from the
            # observation's current taxon.
            "explicit_disagreement": None,
            "skip_with_dna_barcode_its": False,
            "only_with_dna_barcode_its": False,
            "require_source_taxon_match": False,
            "max_observations": len(observation_ids),
            "dqa_vote_requested": False,
            "dqa_vote_planned": False,
            "dry_run": dlg.dry_run(),
            "delay_min_seconds": dlg.delay_min_seconds(),
            "delay_max_seconds": dlg.delay_max_seconds(),
            "observation_ids": observation_ids,
            "tag_other_identifiers": dlg.tag_other_identifiers(),
        }
        self._disagree_progress_title = "Propose a Name to Observation Numbers"
        self._start_propose_name_plan(observation_ids)

    def _propose_name_setup_defaults(self) -> dict:
        return {
            "target_taxon_id": self._settings.propose_name_target_taxon_id,
            "target_taxon_name": self._settings.propose_name_target_taxon_name,
            "target_taxon_rank": self._settings.propose_name_target_taxon_rank,
            "comment": self._settings.propose_name_comment,
            "delay_min_seconds": self._settings.propose_name_delay_min_seconds,
            "delay_max_seconds": self._settings.propose_name_delay_max_seconds,
            "dry_run": self._settings.propose_name_dry_run,
        }

    def _save_propose_name_setup_defaults(self, dlg) -> None:
        self._settings.propose_name_target_taxon_id = dlg.target_taxon_id
        self._settings.propose_name_target_taxon_name = dlg.target_taxon_name
        self._settings.propose_name_target_taxon_rank = dlg.target_taxon_rank
        self._settings.propose_name_comment = dlg.comment()
        self._settings.propose_name_delay_min_seconds = dlg.delay_min_seconds()
        self._settings.propose_name_delay_max_seconds = dlg.delay_max_seconds()
        self._settings.propose_name_dry_run = dlg.dry_run()

    def _start_propose_name_plan(self, observation_ids: List[int]) -> None:
        self._disagree_generation = getattr(self, "_disagree_generation", 0) + 1
        gen = self._disagree_generation
        self._status_label.setText("Refreshing observations for propose-name preview…")
        worker = _ProposeNamePlanWorker(
            self._loader,
            self._auth_state.api_token,
            self._auth_state.login,
            gen,
            lambda: getattr(self, "_disagree_generation", 0),
            self._disagree_options,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(self._on_propose_name_plan_progress)
        sigs.planned.connect(
            lambda result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_propose_name_plan_finished(result),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_disagree_plan_error(msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int)
    def _on_propose_name_plan_progress(self, seen: int, total: int) -> None:
        self._status_label.setText(
            f"Preparing propose-name workflow: refreshed {seen} of {total} observation(s)…"
        )

    def _on_propose_name_plan_finished(self, result: BulkDisagreePlanResult) -> None:
        from observation_workbench.ui.bulk_disagree_dialogs import (
            BulkDisagreePreviewDialog,
        )

        self._disagree_plan_stats = result.stats
        stats_text = _format_propose_name_stats(result.stats)
        if not result.candidates:
            self._status_label.setText("No propose-name candidates found.")
            QMessageBox.information(
                self,
                "Propose a Name",
                "None of the entered observations need this identification.\n\n"
                + stats_text,
            )
            return

        self._status_label.setText(
            f"Found {len(result.candidates)} observation(s) to preview."
        )
        dlg = BulkDisagreePreviewDialog(
            result.candidates,
            result.stats,
            client=self._client,
            disk_cache=self._disk_cache,
            api_token=self._auth_state.api_token,
            login=self._auth_state.login,
            require_source_taxon_match=False,
            default_comment=self._disagree_default_comment,
            on_skip_forever=self._add_bulk_disagree_skip_for_candidate,
            on_unskip_forever=self._remove_bulk_disagree_skip_for_candidate,
            request_reauthentication=self._reauthenticate_photo_browser,
            dry_run=self._disagree_options.get("dry_run", False),
            window_title="Preview Propose a Name",
            photo_browser_title="Browse Propose-a-Name Photos",
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if dlg.back_requested():
                self._status_label.setText("Returned to propose-name setup.")
                QTimer.singleShot(0, self._start_propose_name_setup)
                return
            self._status_label.setText(
                "Propose-name workflow cancelled before posting."
            )
            return
        candidates = dlg.candidates()
        if not candidates:
            self._status_label.setText(
                "Propose-name workflow cancelled: no candidates remain."
            )
            return
        self._start_bulk_disagree_execution(candidates)

    def _bulk_disagree_setup_defaults(self) -> dict:
        return {
            "url": self._settings.bulk_disagree_url,
            "target_taxon_id": self._settings.bulk_disagree_target_taxon_id,
            "target_taxon_name": self._settings.bulk_disagree_target_taxon_name,
            "target_taxon_rank": self._settings.bulk_disagree_target_taxon_rank,
            "comment": self._settings.bulk_disagree_comment,
            "skip_with_dna_barcode_its": self._settings.bulk_disagree_skip_dna_barcode_its,
            "only_with_dna_barcode_its": self._settings.bulk_disagree_only_dna_barcode_its,
            "dqa_vote_requested": self._settings.bulk_disagree_dqa_vote_requested,
            "require_source_taxon_match": self._settings.bulk_disagree_require_source_taxon_match,
            "max_observations": self._settings.bulk_disagree_max_observations,
            "delay_min_seconds": self._settings.bulk_disagree_delay_min_seconds,
            "delay_max_seconds": self._settings.bulk_disagree_delay_max_seconds,
            "dry_run": self._settings.bulk_disagree_dry_run,
        }

    def _save_bulk_disagree_setup_defaults(self, dlg) -> None:
        self._settings.bulk_disagree_url = dlg.observation_query.display_url
        self._settings.bulk_disagree_target_taxon_id = dlg.target_taxon_id
        self._settings.bulk_disagree_target_taxon_name = dlg.target_taxon_name
        self._settings.bulk_disagree_target_taxon_rank = dlg.target_taxon_rank
        self._settings.bulk_disagree_comment = dlg.comment()
        self._settings.bulk_disagree_skip_dna_barcode_its = (
            dlg.skip_with_dna_barcode_its()
        )
        self._settings.bulk_disagree_only_dna_barcode_its = (
            dlg.only_with_dna_barcode_its()
        )
        self._settings.bulk_disagree_dqa_vote_requested = dlg.dqa_vote_requested()
        self._settings.bulk_disagree_require_source_taxon_match = (
            dlg.require_source_taxon_match()
        )
        self._settings.bulk_disagree_max_observations = dlg.max_observations()
        self._settings.bulk_disagree_delay_min_seconds = dlg.delay_min_seconds()
        self._settings.bulk_disagree_delay_max_seconds = dlg.delay_max_seconds()
        self._settings.bulk_disagree_dry_run = dlg.dry_run()
        self._settings.sync()

    def _bulk_disagree_current_url_prefill(self) -> str:
        if self._settings.bulk_disagree_url.strip():
            return ""
        return self._current_observations_url_prefill()

    def _current_observations_url_prefill(self) -> str:
        try:
            source = self._filter_bar.get_filters().get("username", "").strip()
            return source if parse_observations_url(source) is not None else ""
        except ObservationURLParseError:
            return ""

    def _start_bulk_disagree_plan(self, observation_query) -> None:
        self._disagree_generation = getattr(self, "_disagree_generation", 0) + 1
        gen = self._disagree_generation
        self._status_label.setText("Planning bulk disagree-to-taxon candidates…")
        worker = _BulkDisagreePlanWorker(
            self._loader,
            observation_query,
            self._auth_state.api_token,
            self._auth_state.login,
            gen,
            lambda: getattr(self, "_disagree_generation", 0),
            self._disagree_options,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.progress.connect(self._on_bulk_disagree_plan_progress)
        sigs.planned.connect(
            lambda result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_disagree_plan_finished(result),
            )
        )
        sigs.error.connect(
            lambda msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_bulk_disagree_plan_error(msg),
            )
        )
        self._pool.start(worker)

    @Slot(int, int)
    def _on_bulk_disagree_plan_progress(self, seen: int, total: int) -> None:
        self._status_label.setText(
            f"Planning bulk disagree workflow: scanned {seen} of {total} URL results…"
        )

    def _on_bulk_disagree_plan_finished(self, result: BulkDisagreePlanResult) -> None:
        from observation_workbench.ui.bulk_disagree_dialogs import (
            BulkDisagreePreviewDialog,
        )

        self._disagree_plan_stats = result.stats
        stats_text = _format_disagree_stats(result.stats)
        if not result.candidates:
            self._status_label.setText("No bulk disagree candidates found.")
            QMessageBox.information(
                self,
                "Bulk Disagree to Taxon",
                "No observations need this corrective identification.\n\n" + stats_text,
            )
            return

        self._status_label.setText(
            f"Found {len(result.candidates)} bulk disagree candidate(s) to preview."
        )
        dlg = BulkDisagreePreviewDialog(
            result.candidates,
            result.stats,
            client=self._client,
            disk_cache=self._disk_cache,
            api_token=self._auth_state.api_token,
            login=self._auth_state.login,
            require_source_taxon_match=self._disagree_options.get(
                "require_source_taxon_match", True
            ),
            default_comment=self._disagree_default_comment,
            on_skip_forever=self._add_bulk_disagree_skip_for_candidate,
            on_unskip_forever=self._remove_bulk_disagree_skip_for_candidate,
            request_reauthentication=self._reauthenticate_photo_browser,
            dry_run=self._disagree_options.get("dry_run", False),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if dlg.back_requested():
                self._status_label.setText("Returned to bulk disagree setup.")
                QTimer.singleShot(0, self._start_bulk_disagree_setup)
                return
            self._status_label.setText(
                "Bulk disagree workflow cancelled before posting."
            )
            return
        candidates = dlg.candidates()
        if not candidates:
            self._status_label.setText(
                "Bulk disagree workflow cancelled: no candidates remain."
            )
            QMessageBox.information(
                self,
                "Bulk Disagree to Taxon",
                "No candidates remain after photo browsing.",
            )
            return
        self._start_bulk_disagree_execution(candidates)

    def _add_bulk_disagree_skip_for_candidate(
        self,
        candidate: BulkDisagreeCandidate,
    ) -> None:
        reason = (
            "Skipped during bulk disagree-to-taxon photo browser; "
            f"source_taxon_id={candidate.source_taxon_id}; "
            f"target_taxon_id={candidate.target_taxon_id}"
        )
        self._db.add_bulk_disagree_skip(candidate.observation.obs_id, reason)

    def _remove_bulk_disagree_skip_for_candidate(
        self,
        candidate: BulkDisagreeCandidate,
    ) -> None:
        self._db.remove_bulk_disagree_skip(candidate.observation.obs_id)

    def _on_bulk_disagree_plan_error(self, msg: str) -> None:
        log.error("Bulk disagree plan error: %s", msg)
        self._status_label.setText("Failed to plan bulk disagree workflow.")
        if _is_auth_failure_message(msg):
            self._auth_service.clear()
            self._auth_state = AuthState()
            self._identify_actions.authentication_changed()
            self._reconciliation_authentication_changed()
            self._update_auth_ui()
            QMessageBox.warning(
                self,
                "Authentication Expired",
                "iNaturalist rejected the saved API token while planning. "
                "Authenticate again, then restart the bulk disagree workflow.\n\n"
                + msg,
            )
            return
        QMessageBox.warning(self, "Planning Failed", msg)

    def _start_bulk_disagree_execution(
        self,
        candidates: List[BulkDisagreeCandidate],
    ) -> None:
        from observation_workbench.ui.bulk_disagree_dialogs import (
            BulkDisagreeProgressDialog,
        )

        self._disagree_candidates = candidates
        self._disagree_index = 0
        self._disagree_posted_id = 0
        self._disagree_posted_id_and_dqa = 0
        self._disagree_posted_id_dqa_not_attempted = 0
        self._disagree_posted_id_dqa_skipped = 0
        self._disagree_posted_id_dqa_failed = 0
        self._disagree_skipped = 0
        self._disagree_changed = 0
        self._disagree_failed = 0
        self._disagree_ambiguous_write = 0
        self._disagree_resume_after_auth = False
        self._disagree_cancelled = False
        self._disagree_paused = False
        self._disagree_posting = False
        self._disagree_dialog = BulkDisagreeProgressDialog(
            delay_min_seconds=self._disagree_options.get("delay_min_seconds", 10),
            delay_max_seconds=self._disagree_options.get("delay_max_seconds", 30),
            dry_run=self._disagree_options.get("dry_run", False),
            window_title=getattr(
                self, "_disagree_progress_title", "Bulk Disagree to Taxon from URL"
            ),
            parent=self,
        )
        self._disagree_dialog.cancel_requested.connect(self._disagree_cancel)
        self._disagree_dialog.skip_requested.connect(self._disagree_skip_current)
        self._disagree_dialog.skip_forever_requested.connect(
            self._disagree_skip_forever
        )
        self._disagree_dialog.skip_delay_requested.connect(
            self._disagree_skip_current_delay
        )
        self._disagree_dialog.pause_requested.connect(self._disagree_pause)
        self._disagree_dialog.resume_requested.connect(self._disagree_resume)
        self._disagree_dialog.delay_changed.connect(self._disagree_delay_range_changed)
        self._disagree_dialog.nav_key_pressed.connect(self.handle_nav_key)
        self._show_dialog_in_front(self._disagree_dialog)
        self._disagree_show_current()

    def _disagree_show_current(self) -> None:
        if self._disagree_cancelled:
            self._disagree_finish(cancelled=True)
            return
        if self._disagree_index >= len(self._disagree_candidates):
            self._disagree_finish(cancelled=False)
            return
        candidate = self._disagree_candidates[self._disagree_index]
        self._pending_target_obs_id = candidate.observation.obs_id
        self._pending_target_taxon = candidate.target_taxon_name
        self._show_workflow_observation(candidate.observation)
        if self._disagree_dialog:
            self._disagree_dialog.show_candidate(
                self._disagree_index,
                len(self._disagree_candidates),
                candidate,
                comment=self._disagree_default_comment,
            )
            self._disagree_dialog.set_status(
                "Review the observation above, then wait for auto-post or act."
            )
        self._disagree_start_delay()

    def _disagree_start_delay(self) -> None:
        min_delay, max_delay = self._disagree_delay_range()
        self._disagree_delay_remaining = (
            random.randint(min_delay, max_delay) if max_delay > 0 else 0
        )
        if self._disagree_delay_timer is None:
            self._disagree_delay_timer = QTimer(self)
            self._disagree_delay_timer.timeout.connect(self._disagree_tick_delay)
        if self._disagree_dialog:
            self._disagree_dialog.set_waiting()
            self._disagree_dialog.set_countdown(self._disagree_delay_remaining)
        if self._disagree_paused:
            # A pause requested during the previous post persists to this item,
            # so we hold here until the user resumes — even when the delay is 0.
            if self._disagree_dialog:
                self._disagree_dialog.reflect_paused(True)
                self._disagree_dialog.set_status("Paused — click Resume to continue.")
            return
        if self._disagree_delay_remaining <= 0:
            QTimer.singleShot(0, self._disagree_post_current)
            return
        self._disagree_delay_timer.start(1000)

    def _disagree_delay_range(self) -> tuple[int, int]:
        if not self._disagree_dialog:
            return (
                self._disagree_options.get("delay_min_seconds", 10),
                self._disagree_options.get("delay_max_seconds", 30),
            )
        return (
            self._disagree_dialog.delay_min_seconds(),
            self._disagree_dialog.delay_max_seconds(),
        )

    def _disagree_delay_range_changed(self, min_delay: int, max_delay: int) -> None:
        timer_active = (
            self._disagree_delay_timer and self._disagree_delay_timer.isActive()
        )
        if not timer_active:
            return
        if max_delay <= 0:
            self._disagree_delay_timer.stop()
            self._disagree_delay_remaining = 0
            if self._disagree_dialog:
                self._disagree_dialog.set_countdown(0)
                self._disagree_dialog.set_status(
                    "Delay set to 0; posting as soon as the API allows."
                )
            QTimer.singleShot(0, self._disagree_post_current)
            return
        self._disagree_delay_remaining = max(
            min_delay,
            min(self._disagree_delay_remaining, max_delay),
        )
        if self._disagree_dialog:
            self._disagree_dialog.set_countdown(self._disagree_delay_remaining)

    def _disagree_tick_delay(self) -> None:
        if self._disagree_paused:
            return
        self._disagree_delay_remaining -= 1
        if self._disagree_delay_remaining <= 0:
            if self._disagree_delay_timer:
                self._disagree_delay_timer.stop()
            self._disagree_post_current()
            return
        if self._disagree_dialog:
            self._disagree_dialog.set_countdown(self._disagree_delay_remaining)

    def _disagree_skip_current_delay(self) -> None:
        if self._disagree_paused:
            self._disagree_paused = False
            self._disagree_post_current()
        elif self._disagree_delay_timer and self._disagree_delay_timer.isActive():
            self._disagree_delay_timer.stop()
            self._disagree_post_current()

    def _disagree_skip_current(self) -> None:
        timer_active = (
            self._disagree_delay_timer and self._disagree_delay_timer.isActive()
        )
        if self._disagree_paused or timer_active:
            if self._disagree_delay_timer:
                self._disagree_delay_timer.stop()
            self._disagree_paused = False
            candidate = self._disagree_candidates[self._disagree_index]
            self._disagree_skipped += 1
            log.info(
                "Bulk disagree skipped obs=%s target_taxon=%s",
                candidate.observation.obs_id,
                candidate.target_taxon_name,
            )
            self._disagree_index += 1
            self._disagree_show_current()
        elif self._disagree_dialog:
            self._disagree_dialog.set_status(
                "Cannot skip while a write request is in flight."
            )

    def _disagree_skip_forever(self) -> None:
        timer_active = (
            self._disagree_delay_timer and self._disagree_delay_timer.isActive()
        )
        if self._disagree_paused or timer_active:
            if self._disagree_delay_timer:
                self._disagree_delay_timer.stop()
            self._disagree_paused = False
            candidate = self._disagree_candidates[self._disagree_index]
            reason = (
                "Skipped during bulk disagree-to-taxon workflow; "
                f"source_taxon_id={candidate.source_taxon_id}; "
                f"target_taxon_id={candidate.target_taxon_id}"
            )
            self._db.add_bulk_disagree_skip(candidate.observation.obs_id, reason)
            self._disagree_skipped += 1
            log.info(
                "Bulk disagree skipped forever obs=%s source_taxon=%s target_taxon=%s",
                candidate.observation.obs_id,
                candidate.source_taxon_id,
                candidate.target_taxon_id,
            )
            self._disagree_index += 1
            self._disagree_show_current()
        elif self._disagree_dialog:
            self._disagree_dialog.set_status(
                "Cannot skip while a write request is in flight."
            )

    def _disagree_pause(self) -> None:
        self._disagree_paused = True
        if self._disagree_delay_timer and self._disagree_delay_timer.isActive():
            self._disagree_delay_timer.stop()
        if self._disagree_dialog:
            self._disagree_dialog.set_status("Paused — click Resume to continue.")
            self._disagree_dialog.set_countdown(self._disagree_delay_remaining)

    def _disagree_resume(self) -> None:
        self._disagree_paused = False
        if getattr(self, "_disagree_posting", False):
            # The current item is still posting; it will advance on its own once
            # it finishes. Just clear the pause so the next item proceeds.
            if self._disagree_dialog:
                self._disagree_dialog.set_status(
                    "Resumed — finishing the current identification…"
                )
            return
        if self._disagree_delay_remaining > 0:
            if self._disagree_delay_timer is None:
                self._disagree_delay_timer = QTimer(self)
                self._disagree_delay_timer.timeout.connect(self._disagree_tick_delay)
            if self._disagree_dialog:
                self._disagree_dialog.set_status(
                    "Resumed — review the observation above."
                )
                self._disagree_dialog.set_countdown(self._disagree_delay_remaining)
            self._disagree_delay_timer.start(1000)
        else:
            self._disagree_post_current()

    def _disagree_cancel(self) -> None:
        self._disagree_cancelled = True
        self._disagree_resume_after_auth = False
        if self._disagree_delay_timer:
            self._disagree_delay_timer.stop()
        self._disagree_finish(cancelled=True)

    def _disagree_post_current(self) -> None:
        if self._disagree_cancelled or self._disagree_index >= len(
            self._disagree_candidates
        ):
            self._disagree_finish(cancelled=self._disagree_cancelled)
            return
        if getattr(self, "_disagree_posting", False):
            # A write for this item is already in flight; never start a second.
            return
        candidate = self._disagree_candidates[self._disagree_index]
        body = self._disagree_dialog.get_comment() if self._disagree_dialog else ""
        if self._disagree_options.get("tag_other_identifiers"):
            logins = getattr(candidate, "other_identifier_logins", None) or []
            if logins:
                tag_line = " ".join(f"@{login}" for login in logins)
                body = f"{body}\n\n{tag_line}" if body else tag_line
        if self._disagree_dialog:
            self._disagree_dialog.set_posting()
            self._disagree_dialog.set_status(
                "Refreshing observation and posting if still valid…"
            )
        worker = _BulkDisagreePostWorker(
            self._client,
            self._auth_state.api_token,
            self._auth_state.login,
            candidate,
            body,
            self._disagree_options,
        )
        sigs = worker.signals
        self._live_bulk_signals.add(sigs)
        sigs.finished.connect(
            lambda candidate, result, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_disagree_post_finished(candidate, result),
            )
        )
        sigs.error.connect(
            lambda candidate, msg, s=sigs: (
                self._live_bulk_signals.discard(s),
                self._on_disagree_post_error(candidate, msg),
            )
        )
        self._disagree_posting = True
        self._pool.start(worker)

    @Slot(object, object)
    def _on_disagree_post_finished(
        self,
        candidate: BulkDisagreeCandidate,
        result: BulkDisagreeResult,
    ) -> None:
        self._disagree_posting = False
        if getattr(self, "_disagree_cancelled", False):
            return
        if result.refreshed_observation:
            self._replace_observation(result.refreshed_observation)
        if result.status == "ambiguous_write":
            self._disagree_ambiguous_write += 1
            if self._disagree_dialog:
                self._disagree_dialog.set_status(result.message)
            self._handle_disagree_ambiguous_write(candidate, result.message)
            return
        if result.status == "posted_id":
            self._disagree_posted_id += 1
        elif result.status == "posted_id_and_dqa":
            self._disagree_posted_id_and_dqa += 1
        elif result.status == "posted_id_dqa_not_attempted":
            self._disagree_posted_id_dqa_not_attempted += 1
        elif result.status == "posted_id_dqa_skipped":
            self._disagree_posted_id_dqa_skipped += 1
        elif result.status == "posted_id_dqa_failed":
            self._disagree_posted_id_dqa_failed += 1
        elif result.status == "changed":
            self._disagree_changed += 1
        elif result.status == "failed":
            self._disagree_failed += 1
        else:
            self._disagree_skipped += 1

        log.debug(
            "Bulk disagree result obs=%s target_taxon=%s status=%s message=%s",
            candidate.observation.obs_id,
            candidate.target_taxon_name,
            result.status,
            result.message,
        )
        if self._disagree_dialog:
            self._disagree_dialog.set_status(result.message)
        self._disagree_index += 1
        self._disagree_show_current()

    def _handle_disagree_ambiguous_write(
        self,
        candidate: BulkDisagreeCandidate,
        msg: str,
    ) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Ambiguous Write")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "A write may have reached iNaturalist, but the app could not verify the final state.\n\n"
            f"Observation: {candidate.observation.obs_id}\n"
            f"Target taxon: {candidate.target_taxon_name}\n\n"
            "Open or refresh the observation manually before any retry."
        )
        box.setDetailedText(msg)
        cancel_btn = box.addButton("Cancel Workflow", QMessageBox.ButtonRole.RejectRole)
        skip_btn = box.addButton("Skip this ID", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip_btn:
            self._disagree_index += 1
            self._disagree_show_current()
        else:
            self._disagree_cancel()

    def _on_disagree_post_error(
        self,
        candidate: BulkDisagreeCandidate,
        msg: str,
    ) -> None:
        self._disagree_posting = False
        if getattr(self, "_disagree_cancelled", False):
            return
        log.error(
            "Bulk disagree post failed: obs=%s target_taxon=%s — %s",
            candidate.observation.obs_id,
            candidate.target_taxon_name,
            msg,
        )
        if _is_auth_failure_message(msg):
            self._handle_disagree_auth_error(candidate, msg)
            return
        if _is_ambiguous_write_message(msg):
            self._disagree_ambiguous_write += 1
            self._handle_disagree_ambiguous_write(candidate, msg)
            return
        if self._disagree_dialog:
            self._disagree_dialog.set_status("Error: " + msg)
        box = QMessageBox(self)
        box.setWindowTitle("Identification Error")
        box.setText(
            "A write action failed and the workflow is paused.\n\n"
            f"Observation: {candidate.observation.obs_id}\n"
            f"Target taxon: {candidate.target_taxon_name}\n\n"
            f"{msg}"
        )
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        skip_btn = box.addButton("Skip this ID", QMessageBox.ButtonRole.DestructiveRole)
        retry_btn = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            self._disagree_failed += 1
            self._disagree_cancel()
        elif clicked is skip_btn:
            self._disagree_failed += 1
            self._disagree_index += 1
            self._disagree_show_current()
        elif clicked is retry_btn:
            self._disagree_post_current()

    def _handle_disagree_auth_error(
        self,
        candidate: BulkDisagreeCandidate,
        msg: str,
    ) -> None:
        if self._disagree_delay_timer:
            self._disagree_delay_timer.stop()
        self._disagree_paused = True
        self._disagree_resume_after_auth = True

        self._auth_service.clear()
        self._auth_state = AuthState()
        self._identify_actions.authentication_changed()
        self._reconciliation_authentication_changed()
        self._update_auth_ui()

        summary = (
            "iNaturalist authentication expired or was rejected. "
            "The bulk disagree workflow is paused and will continue after a fresh token is validated."
        )
        if self._disagree_dialog:
            self._disagree_dialog.set_status(summary)
        self._status_label.setText(summary)

        box = QMessageBox(self)
        box.setWindowTitle("Authentication Expired")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "iNaturalist rejected the saved API token.\n\n"
            "No identification was posted for the current observation. The workflow "
            "is paused and will retry this same item after you authenticate with a fresh token.\n\n"
            f"Observation: {candidate.observation.obs_id}\n"
            f"Target taxon: {candidate.target_taxon_name}\n\n"
            "Authenticate again with a fresh iNaturalist API token to continue."
        )
        box.setDetailedText(msg)
        auth_btn = box.addButton("Authenticate Now", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Cancel Workflow", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(auth_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is auth_btn:
            self._authenticate_to_inaturalist()
        elif clicked is cancel_btn:
            self._disagree_resume_after_auth = False
            self._disagree_cancel()

    def _resume_disagree_after_auth(self) -> None:
        self._disagree_resume_after_auth = False
        if (
            self._disagree_cancelled
            or not self._disagree_dialog
            or self._disagree_index >= len(self._disagree_candidates)
        ):
            return
        self._disagree_paused = False
        self._disagree_dialog.set_status(
            "Authentication refreshed; retrying current identification…"
        )
        self._status_label.setText(
            "Authentication refreshed; continuing bulk disagree workflow."
        )
        QTimer.singleShot(0, self._disagree_post_current)

    def _disagree_finish(self, cancelled: bool) -> None:
        self._pending_target_obs_id = None
        self._pending_target_taxon = ""
        self._disagree_resume_after_auth = False
        if self._disagree_delay_timer:
            self._disagree_delay_timer.stop()
        if self._disagree_dialog:
            self._disagree_dialog.set_summary(
                posted_id=self._disagree_posted_id,
                posted_id_and_dqa=self._disagree_posted_id_and_dqa,
                posted_id_dqa_not_attempted=self._disagree_posted_id_dqa_not_attempted,
                posted_id_dqa_skipped=self._disagree_posted_id_dqa_skipped,
                posted_id_dqa_failed=self._disagree_posted_id_dqa_failed,
                skipped=self._disagree_skipped,
                changed=self._disagree_changed,
                failed=self._disagree_failed,
                ambiguous_write=self._disagree_ambiguous_write,
                cancelled=cancelled,
                plan_stats=self._disagree_plan_stats,
            )
        self._status_label.setText(
            f"Bulk disagree workflow {'cancelled' if cancelled else 'complete'}: "
            f"{self._disagree_posted_id} ID posted, "
            f"{self._disagree_posted_id_and_dqa} ID+DQA posted, "
            f"{self._disagree_posted_id_dqa_not_attempted} ID posted with DQA not attempted, "
            f"{self._disagree_posted_id_dqa_skipped} ID posted with DQA skipped, "
            f"{self._disagree_posted_id_dqa_failed} ID posted with DQA failed, "
            f"{self._disagree_skipped} skipped, {self._disagree_changed} changed, "
            f"{self._disagree_failed} failed, "
            f"{self._disagree_ambiguous_write} ambiguous."
        )

    # ------------------------------------------------------------------
    # Browser actions
    # ------------------------------------------------------------------

    def _current_observation(self) -> Optional[StudyObservation]:
        if self._current_obs_idx < 0 or self._current_obs_idx >= len(
            self._observations
        ):
            return None
        return self._observations[self._current_obs_idx]

    def _open_obs_in_browser(self) -> None:
        obs = self._current_observation()
        if obs is None:
            return
        open_external_url_silently(obs.url)

    def _copy_obs_url(self) -> None:
        obs = self._current_observation()
        if obs is None:
            return
        QApplication.clipboard().setText(obs.url)
        self._status_label.setText(f"Copied observation URL: {obs.url}")

    def _open_image_in_browser(self) -> None:
        obs = self._current_observation()
        if obs is None:
            return
        if not obs.photos or self._current_photo_idx >= len(obs.photos):
            return
        photo = obs.photos[self._current_photo_idx]
        url = photo.url_original or photo.url_large or photo.url_square
        if url:
            open_external_url_silently(url)

    # ------------------------------------------------------------------
    # Taxon tree integration
    # ------------------------------------------------------------------

    @Slot()
    def _on_taxon_summary_finished(self) -> None:
        """Strip the 'Loading taxon summary…' suffix from the status bar."""
        current = self._status_label.text()
        cleaned = current.replace("  · Loading taxon summary…", "")
        if cleaned != current:
            self._status_label.setText(cleaned)

    def _on_taxon_from_tree(self, taxon_id: int, taxon_name: str) -> None:
        """When user double-clicks a taxon in the summary tree, fill the filter."""
        # This sets the taxon filter field (requires filter_bar access)
        self._filter_bar._taxon_edit.setText(taxon_name)
        self._filter_bar._taxon_id = taxon_id
        self._filter_bar._taxon_name = taxon_name
        self._status_label.setText(
            f"Taxon filter set to: {taxon_name}. Press Load to apply."
        )

    # ------------------------------------------------------------------
    # Settings dialog
    # ------------------------------------------------------------------

    def _show_settings(self) -> None:
        from observation_workbench.ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Apply new settings
            new_max_bytes = int(self._settings.cache_max_gb * 1024**3)
            self._disk_cache._max_bytes = new_max_bytes
            mem_bytes = self._settings.memory_cache_max_mb * 1024 * 1024
            self._prefetcher.set_max_memory(mem_bytes)
            # Apply display settings immediately
            self._apply_font_scale(self._settings.ui_font_scale)
            self._result_list.set_font_scale(self._settings.result_list_font_scale)
            self._scroll_speed_filter.set_multiplier(
                self._settings.scroll_speed_multiplier
            )
            StudyTaxon.show_common_names = self._settings.show_common_names
            if self._observations:
                self._result_list.refresh_display()
                if self._current_obs_idx >= 0:
                    # Update metadata and viewer panels if display rules changed
                    self._metadata_panel.update_observation(
                        self._observations[self._current_obs_idx],
                        self._current_photo_idx,
                        authenticated_login=self._auth_state.login,
                    )
            self._taxon_tree._reload()

    def _show_cache_info(self) -> None:
        total_bytes = self._disk_cache.total_size_bytes()
        total_mb = total_bytes / (1024**2)
        limit_gb = self._settings.cache_max_gb
        cache_dir = str(self._settings.cache_dir)
        QMessageBox.information(
            self,
            "Cache Info",
            f"Image cache: {total_mb:.1f} MB / {limit_gb:.1f} GB limit\n"
            f"Location: {cache_dir}",
        )

    def _clear_caches(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Caches",
            "This will delete all cached images and metadata.\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._disk_cache.clear()
            self._db.clear_all_caches()
            self._status_label.setText("Caches cleared.")

    def _show_shortcuts(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard Shortcuts")

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        # Two-column grid: key(s) | description
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(3)

        # (key_text, description) — None description = section header, both None = spacer
        entries = [
            ("Navigation", None),
            ("Right / Space", "Next observation (hold Right to repeat)"),
            ("Left / Shift+Space", "Previous observation (hold Left to repeat)"),
            ("Down / ]", "Next photo in observation"),
            ("Up / [", "Previous photo in observation"),
            ("G", "Go to result number"),
            (None, None),
            ("Actions", None),
            ("a", "Agree with most recent non-self ID"),
            ("A", "Agree with consensus/community ID"),
            ("O", "Open observation in browser"),
            ("I", "Open image in browser"),
            ("L", "Toggle fit / 1:1 zoom"),
            ("R", "Reload current filters"),
            ("F", "Focus filter bar"),
        ]

        for r, (key, desc) in enumerate(entries):
            if key is None:  # blank spacer row
                grid.setRowMinimumHeight(r, 6)
            elif desc is None:  # section header
                lbl = QLabel(f"<b>{key}</b>")
                grid.addWidget(lbl, r, 0, 1, 2)
            else:
                key_lbl = QLabel(key)
                key_lbl.setStyleSheet("font-family: monospace;")
                grid.addWidget(key_lbl, r, 0)
                grid.addWidget(QLabel(desc), r, 1)

        grid.setColumnStretch(1, 1)  # description column expands
        outer.addLayout(grid)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        outer.addWidget(bb)

        dlg.adjustSize()
        dlg.exec()

    # ------------------------------------------------------------------
    # MO / iNaturalist reconciliation
    # ------------------------------------------------------------------

    def _reconciliation_authentication_changed(self) -> None:
        if self._reconciliation is not None:
            self._reconciliation.authentication_changed()

    def _open_reconciliation(self) -> None:
        from observation_workbench.ui.reconciliation import open_reconciliation_window

        if self._reconciliation is None:
            QMessageBox.critical(
                self,
                "Reconciliation unavailable",
                "The reconciliation database could not be opened or upgraded, so "
                "this feature is disabled for now. The rest of the application is "
                "unaffected and no remote data has been changed.\n\n"
                f"{self._reconciliation_error}",
            )
            return
        if self._reconciliation_windows:
            window = next(iter(self._reconciliation_windows))
            window.show()
            window.raise_()
            window.activateWindow()
            return
        window = open_reconciliation_window(
            self._reconciliation,
            self._auth_state.login if self._auth_state.is_authenticated else "",
            self,
        )
        if window is not None:
            self._reconciliation_windows.add(window)
            window.destroyed.connect(
                lambda _obj=None, target=window: self._reconciliation_windows.discard(
                    target
                )
            )

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._closing = True
        self.stop_arrow_navigation("window closing")
        self._identify_refresh_pending.clear()
        self._identify_refresh_in_flight.clear()
        if self._pending_identify_actions_dialog is not None:
            self._pending_identify_actions_dialog.close()
        for window in list(self._identify_windows):
            window.close()
        for window in list(self._reconciliation_windows):
            window.close()
        s = self._settings
        s.window_geometry = self.saveGeometry()
        s.window_state = self.saveState()
        s.splitter_state = self._splitter.saveState()
        self._filter_bar.save_state(s)
        s.sync()
        # Cancel all in-flight loads
        self._cancel_load()
        self._bulk_cancelled = True
        if self._bulk_delay_timer:
            self._bulk_delay_timer.stop()
        self._disagree_cancelled = True
        if self._disagree_delay_timer:
            self._disagree_delay_timer.stop()
        # Identify windows invalidate their workers before close().  Do not wait
        # indefinitely for shared-client reads here: any late result is stale and
        # ignored by its closed window, and its signal receiver is disconnected.
        self._identify_actions.prepare_shutdown()
        if self._reconciliation is not None:
            self._reconciliation.shutdown()
        # Give already in-flight background workers (image prefetch, Identify
        # detail reads) a short bounded chance to finish before the shared
        # httpx client underneath them closes, so a normal quit does not log
        # spurious "client closed" transport exceptions from a worker thread
        # racing this close. Any worker still running past this is discarded
        # anyway (stale-generation results are ignored by closed windows).
        self._pool.waitForDone(1500)
        self._client.close()
        self._summary_client.close()
        super().closeEvent(event)


def _extract_login(raw: dict) -> str:
    if isinstance(raw.get("results"), list):
        raw_user = raw["results"][0] if raw["results"] else {}
    else:
        raw_user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
    return (raw_user or {}).get("login", "") or ""


def _format_api_error(exc: Exception) -> str:
    if isinstance(exc, INatAPIError):
        parts = []
        if exc.endpoint:
            parts.append(f"Endpoint: {exc.endpoint}")
        if exc.status_code:
            parts.append(f"HTTP status: {exc.status_code}")
        parts.append(str(exc))
        if exc.response_body:
            parts.append("Response: " + exc.response_body[:1000])
        return "\n".join(parts)
    return str(exc)


def _safe_confirmed_refresh_diagnostic(exc: object) -> str:
    """Describe a safe-read failure without exposing response content."""
    if isinstance(exc, INatAPIError):
        return (
            f"HTTP {exc.status_code}"
            if exc.status_code is not None
            else "iNaturalist read failed"
        )
    if isinstance(exc, BaseException):
        return type(exc).__name__
    return "Unknown detail refresh failure"


def _is_auth_failure_message(msg: str) -> bool:
    text = msg.casefold()
    return (
        "http status: 401" in text
        or "http 401" in text
        or "need to sign in" in text
        or "missing inaturalist api token" in text
    )


def _is_ambiguous_write_message(msg: str) -> bool:
    text = msg.casefold()
    return "may have reached inaturalist" in text or "result is unknown" in text


def _format_disagree_stats(stats) -> str:
    return (
        f"Scanned URL results: {stats.total_url_results_scanned}\n"
        f"Candidates: {stats.candidate_count}\n"
        f"Skipped due to DNA Barcode ITS: {stats.skipped_dna_barcode_its}\n"
        f"Skipped due to missing DNA Barcode ITS: {stats.skipped_missing_dna_barcode_its}\n"
        f"Skipped because you already have the target ID: {stats.skipped_already_target}\n"
        f"Skipped because source taxon no longer matched: {stats.skipped_source_no_match}\n"
        f"Skipped due to permanent skip list: {stats.skipped_permanent}\n"
        f"Skipped due to missing/invalid data: {stats.skipped_missing_invalid_data}\n"
        f"Skipped due to refresh failure: {stats.skipped_refresh_failure}"
    )


def _format_propose_name_stats(stats) -> str:
    return (
        f"Observations entered: {stats.total_api_results}\n"
        f"Candidates: {stats.candidate_count}\n"
        f"Skipped (already research grade with this name, or you already have this ID): {stats.skipped_already_target}\n"
        f"Skipped due to permanent skip list: {stats.skipped_permanent}\n"
        f"Skipped due to missing/invalid data: {stats.skipped_missing_invalid_data}\n"
        f"Skipped due to refresh failure (not found): {stats.skipped_refresh_failure}"
    )


def _taxon_has_provisional_name(taxon) -> bool:
    return bool(taxon and "'" in (taxon.name or ""))


def _is_provisional_observation(obs: StudyObservation) -> bool:
    if any(
        ident.current and _taxon_has_provisional_name(ident.taxon)
        for ident in obs.all_identifications
    ):
        return True
    if obs.target_identification and _taxon_has_provisional_name(
        obs.target_identification.taxon
    ):
        return True
    return (
        _taxon_has_provisional_name(obs.community_taxon)
        or _taxon_has_provisional_name(obs.display_taxon)
        or _taxon_has_provisional_name(obs.taxon)
    )
