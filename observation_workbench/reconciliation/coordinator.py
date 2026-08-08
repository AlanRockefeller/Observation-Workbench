"""Application-owned reconciliation scanning and reviewed remote actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import re
import uuid as uuidlib
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatAPIError, INatClient
from observation_workbench.storage.settings import AppSettings

from .db import ReconciliationDB
from .actions import (
    LinkActionResult,
    LinkRepairService,
    simulate_link_repair_final_state,
)
from .inat_reader import (
    ACCESSION_FIELD_NAME,
    INatReconciliationReader,
    ITS_FIELD_NAME,
    MO_FIELD_NAME,
)
from .its import ITSActionResult, ITSSyncService
from .coordinates import CoordinateActionResult, CoordinateSyncService
from .photos import PhotoActionResult, PhotoSyncService, new_observation_photo_uuid
from .observation_creation import (
    ObservationCreationError,
    ObservationCreationResult,
    ObservationCreationService,
)
from .consolidation import ConsolidationActionResult, ConsolidationService
from .deletion import (
    DeletionActionResult,
    DeletionService,
    DonorDeletionPreview,
)
from .proposals import NameProposalResult, NameProposalService
from .matching import (
    build_candidates,
    is_same_site_duplicate,
    score_candidate,
    score_evidence,
    validate_reciprocal_pair,
)
from .mo_client import (
    MO_OBSERVATIONS_PAGE_SIZE,
    MOAPIError,
    MOClient,
    ReconciliationCancelled,
    mo_login_of,
    results_from_payload,
)
from .mo_parsing import (
    TARGET_UNKNOWN,
    parse_mo_external_link,
    parse_mo_observation,
)
from .normalization import (
    parse_inat_observation_url,
    public_fingerprint,
    sequence_digest,
)
from .types import (
    AuthoritativeLinkRow,
    HydratedObservation,
    InventoryObservation,
    MediaIdentity,
    ObservationPair,
    EvidenceFamily,
    EvidenceTier,
    MatchEvidence,
    ReconciliationProfile,
    ReconciliationPlan,
    RemoteRecordKey,
    RemoteSite,
    SyncIssue,
)

# Worker parts that belong to a scan and may therefore drive the scan progress
# display. Every OTHER worker (detail reads, link actions, previews) runs on the
# same _CallableWorker and now emits a phase-start ping, so without this filter
# an ordinary detail fetch would overwrite the scan status line with its own
# internal part name.
SCAN_PROGRESS_PARTS = frozenset(
    {
        "mo",
        "inat",
        "prepared",
        "context_inat",
        "context_mo",
        "validation_input",
        "validation_inat",
        "validation_mo",
        "plan",
        "persist",
    }
)


class _WorkerSignals(QObject):
    result = Signal(str, object, int)
    error = Signal(str, str, int)
    progress = Signal(str, int, int, int)


class _CallableWorker(QRunnable):
    def __init__(
        self,
        part: str,
        generation: int,
        function: Callable[[Any], Any],
        is_cancelled: Callable[[], bool],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.part = part
        self.generation = generation
        self.function = function
        self.is_cancelled = is_cancelled
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            if self.is_cancelled():
                raise ReconciliationCancelled("Reconciliation request cancelled")
            # Phase-start ping. Several scan phases report progress only once,
            # on completion (or never — persistence takes no progress callback
            # at all), so without this the display would sit on the previous
            # phase's name for the whole of the next one. A (0, 0) report means
            # "this phase has begun, count unknown".
            self.signals.progress.emit(self.part, 0, 0, self.generation)
            # A phase may report its own SUB-STAGES: the Mushroom Observer
            # inventory phase alone lists observations, resolves taxon names,
            # discovers the external-site definition, then reads links, which is
            # over ten minutes of work for a large account. Without a stage the
            # display sat on one unchanging phase name for all of it. The stage
            # rides along as a `part:stage` suffix so the progress signal's
            # signature stays fixed.
            value = self.function(
                lambda current, total, stage="": self.signals.progress.emit(
                    f"{self.part}:{stage}" if stage else self.part,
                    current,
                    total,
                    self.generation,
                )
            )
            if self.is_cancelled():
                raise ReconciliationCancelled("Reconciliation request cancelled")
            self.signals.result.emit(self.part, value, self.generation)
        except ReconciliationCancelled:
            self.signals.error.emit(self.part, "cancelled", self.generation)
        except Exception as exc:
            self.signals.error.emit(self.part, _safe_error(exc), self.generation)


@dataclass
class _ScanState:
    profile: ReconciliationProfile
    scan_started_at: str
    run_id: int
    full: bool
    results: dict[str, Any]
    signals: set[QObject]


class ReconciliationCoordinator(QObject):
    """Owns its database, MO client, and worker pools independently of the viewer."""

    profiles_changed = Signal()
    accounts_resolved = Signal(object, object)
    account_resolution_failed = Signal(str)
    authentication_checked = Signal(str, str)
    scan_started = Signal()
    scan_progress = Signal(str, int, int)
    scan_finished = Signal(str)
    scan_failed = Signal(str)
    details_loaded = Signal(str, object)
    thumbnail_loaded = Signal(object, object)
    field_candidates_loaded = Signal(object)
    link_preview_ready = Signal(object)
    its_preview_ready = Signal(object)
    coordinate_preview_ready = Signal(object)
    photo_identity_ready = Signal(object)
    photo_preview_ready = Signal(object)
    observation_creation_preview_ready = Signal(object)
    consolidation_preview_ready = Signal(object)
    deletion_preview_ready = Signal(object)
    name_proposal_ready = Signal(object)
    name_proposal_changed = Signal()
    link_action_failed = Signal(str)
    link_action_progress = Signal(str)
    link_actions_changed = Signal()
    mo_key_changed = Signal(bool)

    def __init__(
        self,
        inat_client: INatClient,
        auth_provider: Callable[[], AuthState],
        settings: AppSettings,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.inat_client = inat_client
        self.auth_provider = auth_provider
        self.settings = settings
        self.db = ReconciliationDB()
        self.mo_client = MOClient()
        self.mo_pool = QThreadPool(self)
        self.mo_pool.setMaxThreadCount(1)
        self.inat_pool = QThreadPool(self)
        self.inat_pool.setMaxThreadCount(2)
        self.action_pool = QThreadPool(self)
        self.action_pool.setMaxThreadCount(1)
        self._generation = 0
        self._live_signals: set[QObject] = set()
        self._lookup: dict[str, Any] = {}
        self._scan: Optional[_ScanState] = None
        self._action_running = False
        self._action_cancel_requested = False
        self._auth_generation = 0
        self._auth_login = self.auth_provider().login.strip().casefold()
        self._mo_key_generation = 0
        self._mo_api_keys: dict[int, str] = {}
        self.db.recover_running_actions()
        self.db.recover_running_deletion_actions()
        self.link_repairs = LinkRepairService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
        )
        self.its_sync = ITSSyncService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
        )
        self.coordinates = CoordinateSyncService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
        )
        self.proposals = NameProposalService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
        )
        self.photos = PhotoSyncService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
        )
        self.observation_creation = ObservationCreationService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
            self.photos,
            self.link_repairs,
        )
        self.consolidation = ConsolidationService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
            self.link_repairs,
        )
        self.deletion = DeletionService(
            self.db,
            self.inat_client,
            self.mo_client,
            self.auth_provider,
            self._mo_key_for_profile,
            lambda: self._auth_generation,
            lambda: self._mo_key_generation,
            # Phase 2B has not been formally closed in this repository. This
            # explicit boundary must be changed only after live acceptance.
            phase_2b_closed_provider=lambda: False,
        )
        self._identify_manager: Optional[Any] = None

    def set_identify_manager(self, manager: Any) -> None:
        """Share the application-scoped Identify manager for iNat name delegation."""
        self._identify_manager = manager

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def foreground_operation_running(self) -> bool:
        """Whether a scan or an explicit action currently owns the remotes.

        This is the same condition every ``prepare_*``/``execute_*`` entry
        point refuses on. It is exposed so background, non-user-initiated
        readers -- currently only the dashboard's candidate photo prefetch,
        which calls :class:`PhotoService` directly -- can stand down instead of
        competing with the operation the user is waiting on for Mushroom
        Observer's request lock and the shared iNaturalist rate limiter. Reads
        two plain attributes, so it is safe to poll from a worker thread.
        """
        return self._scan is not None or self._action_running

    def profiles(self) -> list[ReconciliationProfile]:
        return self.db.profiles()

    def resolve_accounts(self, inat_login: str, mo_login: str) -> None:
        self.cancel()
        generation = self._generation
        self._lookup = {}
        inat_reader = INatReconciliationReader(self.inat_client)
        self._start_worker(
            self.inat_pool,
            "lookup_inat",
            generation,
            lambda _progress: inat_reader.resolve_user(inat_login),
            self._lookup_result,
            self._lookup_error,
        )
        self._start_worker(
            self.mo_pool,
            "lookup_mo",
            generation,
            lambda _progress: self.mo_client.resolve_user(
                mo_login, lambda: generation != self._generation
            ),
            self._lookup_result,
            self._lookup_error,
        )

    def save_profile(
        self, inat_user: dict[str, Any], mo_user: dict[str, Any]
    ) -> ReconciliationProfile:
        inat_id = _id_from(inat_user)
        mo_id = _id_from(mo_user)
        inat_login = str(inat_user.get("login") or "").strip()
        mo_login = mo_login_of(mo_user)
        if not inat_id or not mo_id or not inat_login or not mo_login:
            raise ValueError(
                "Both accounts must have validated numeric IDs and canonical logins"
            )
        profile = self.db.save_profile(inat_id, inat_login, mo_id, mo_login)
        self.profiles_changed.emit()
        return profile

    def check_authentication(self, profile_id: int) -> None:
        """Pre-flight the one authenticated read a scan performs.

        ``AuthState.is_authenticated`` only means a token STRING is stored — it
        proves nothing about whether iNaturalist still accepts it, and the
        tokens expire in about a day. So the check that matters costs a request
        and cannot be answered locally.

        Deliberately advisory: this emits a verdict and starts nothing. The
        caller decides whether a degraded scan is worth running, because a
        public scan is genuinely useful and refusing to run one would be worse
        than the gap it avoids.

        Emits ``authentication_checked(state, detail)`` where state is
        ``ok`` | ``unauthenticated`` | ``mismatch`` | ``rejected`` | ``unavailable``.
        """
        profile = self.db.profile(profile_id)
        auth = self.auth_provider()
        expected = profile.inat_login
        if not auth.is_authenticated:
            self.authentication_checked.emit("unauthenticated", expected)
            return
        if auth.login.casefold() != expected.casefold():
            self.authentication_checked.emit("mismatch", auth.login)
            return
        self._start_worker(
            self.inat_pool,
            "auth_check",
            self._generation,
            lambda _progress: self._probe_token(auth.api_token, expected),
            lambda _part, value, _gen: self.authentication_checked.emit(*value),  # type: ignore[misc]
            # A probe that could not complete is NOT a failed sign-in. Report it
            # as unavailable so the caller proceeds and lets the scan surface
            # the real network error, rather than accusing a valid token.
            lambda _part, error, _gen: self.authentication_checked.emit(
                "unavailable", error
            ),
        )

    def _probe_token(self, api_token: str, expected_login: str) -> tuple[str, str]:
        try:
            payload = self.inat_client.get_current_user(api_token)
        except INatAPIError as exc:
            if exc.status_code in (401, 403):
                return ("rejected", expected_login)
            raise
        results = payload.get("results") or []
        login = (
            str(results[0].get("login") or "")
            if results and isinstance(results[0], dict)
            else ""
        )
        # The token is valid but belongs to somebody else — the scan would read
        # a different account's private data than the profile expects.
        if login and login.casefold() != expected_login.casefold():
            return ("mismatch", login)
        return ("ok", login or expected_login)

    def scan(self, profile_id: int, *, force_full: bool = False) -> None:
        if self._scan is not None or self._action_running:
            return
        self.cancel()
        generation = self._generation
        profile = self.db.profile(profile_id)
        scan_started_at = datetime.now(timezone.utc).isoformat()
        full = force_full or _needs_full_scan(
            self.db.cursor(profile_id, "mo_full_inventory")
        )
        run_id = self.db.start_run(
            profile_id, "full" if full else "incremental", scan_started_at
        )
        self._scan = _ScanState(profile, scan_started_at, run_id, full, {}, set())
        self.scan_started.emit()
        self._start_worker(
            self.mo_pool,
            "mo",
            generation,
            lambda progress: self._scan_mo(profile, full, generation, progress),
            self._scan_result,
            self._scan_error,
        )
        self._start_worker(
            self.inat_pool,
            "inat",
            generation,
            lambda progress: self._scan_inat(profile, full, generation, progress),
            self._scan_result,
            self._scan_error,
        )

    def cancel(self) -> None:
        if self._scan is not None and "persistence_started" in self._scan.results:
            self.scan_progress.emit("Finalizing local transaction", 0, 0)
            return
        self._generation += 1
        if self._scan is not None:
            self.db.finish_run(self._scan.run_id, "cancelled")
            self._scan = None
            self.scan_failed.emit("Scan cancelled; no cursors were advanced.")

    def shutdown(self) -> None:
        self.cancel()
        # Keep both clients alive until every runnable has observed cancellation
        # or returned from its finite network timeout.
        self.mo_pool.waitForDone(-1)
        self.inat_pool.waitForDone(-1)
        self.action_pool.waitForDone(-1)
        self._mo_api_keys.clear()
        self.mo_client.close()
        self.db.close_thread_connection()

    # Gate 1B explicit reciprocal-link actions -----------------------

    def has_mo_api_key(self, profile_id: int) -> bool:
        return bool(self._mo_key_for_profile(profile_id))

    def set_mo_api_key(
        self, profile_id: int, api_key: str, *, persist_plaintext: bool
    ) -> None:
        profile = self.db.profile(profile_id)
        value = api_key.strip()
        if value:
            self._mo_api_keys[profile_id] = value
            if persist_plaintext:
                self.settings.store_reconciliation_mo_api_key(profile.mo_user_id, value)
            else:
                self.settings.clear_reconciliation_mo_api_key(profile.mo_user_id)
        else:
            self._mo_api_keys.pop(profile_id, None)
            self.settings.clear_reconciliation_mo_api_key(profile.mo_user_id)
        self.db.cancel_pending_groups_for_site(
            profile_id, "mo", "mo_credential_changed"
        )
        self._mo_key_generation += 1
        if self._action_running:
            self._action_cancel_requested = True
        self.mo_key_changed.emit(bool(value))

    def clear_memory_mo_api_keys(self) -> None:
        affected_profiles = tuple(self._mo_api_keys)
        self._mo_api_keys.clear()
        for profile_id in affected_profiles:
            self.db.cancel_pending_groups_for_site(
                profile_id, "mo", "mo_credential_changed"
            )
        self._mo_key_generation += 1
        if self._action_running:
            self._action_cancel_requested = True
        self.mo_key_changed.emit(False)

    def authentication_changed(self) -> None:
        """Invalidate previews and unsubmitted writes on every auth transition."""
        current_login = self.auth_provider().login.strip().casefold()
        affected_logins = {
            value for value in (self._auth_login, current_login) if value
        }
        affected_profiles = [
            profile.profile_id
            for profile in self.db.profiles()
            if profile.inat_login.strip().casefold() in affected_logins
        ]
        self.db.cancel_pending_actions(affected_profiles, "authentication_changed")
        self._auth_login = current_login
        self._auth_generation += 1
        if self._action_running:
            self._action_cancel_requested = True
            self.link_action_progress.emit(
                "Authentication changed; no further writes will start. A submitted write will still be verified."
            )

    def _mo_key_for_profile(self, profile_id: int) -> str:
        if profile_id in self._mo_api_keys:
            return self._mo_api_keys[profile_id]
        profile = self.db.profile(profile_id)
        stored = self.settings.reconciliation_mo_api_key(profile.mo_user_id)
        if stored:
            self._mo_api_keys[profile_id] = stored
        return stored

    def prepare_link_repairs(
        self,
        profile_id: int,
        *,
        pair_id: Optional[int] = None,
        issue_id: Optional[int] = None,
    ) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Refreshing both authoritative link resources for preview…"
        )
        self._start_worker(
            self.action_pool,
            "link_preview",
            generation,
            lambda _progress: self.link_repairs.prepare_preview(
                profile_id,
                pair_id=pair_id,
                issue_id=issue_id,
                cancelled=lambda: generation != self._generation
                or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._link_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def prepare_its_comparison(self, profile_id: int, pair_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Hydrating ITS data for the selected confirmed pair…"
        )
        self._start_worker(
            self.action_pool,
            "its_preview",
            generation,
            lambda _progress: self.its_sync.prepare_preview(
                profile_id,
                pair_id,
                cancelled=lambda: generation != self._generation
                or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._its_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_its_action(self, preview: object, option: object) -> None:
        from .types import ITSActionOption, ITSComparisonPreview

        if not isinstance(preview, ITSComparisonPreview) or not isinstance(
            option, ITSActionOption
        ):
            self.link_action_failed.emit("The ITS comparison preview is invalid.")
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication or Mushroom Observer credentials changed after preview. Refresh ITS comparison."
            )
            return
        try:
            group_id, _action_ids = self.db.journal_its_actions(preview, [option])
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self.resume_link_action_group(preview.profile_id, group_id)

    # Gate 1D coordinate synchronization -------------------------------

    def prepare_coordinate_comparison(self, profile_id: int, pair_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Reading coordinates for the selected confirmed pair…"
        )
        self._start_worker(
            self.action_pool,
            "coordinate_preview",
            generation,
            lambda _progress: self.coordinates.prepare_preview(
                profile_id,
                pair_id,
                cancelled=lambda: generation != self._generation
                or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._coordinate_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_coordinate_action(self, preview: object, option: object) -> None:
        from .types import CoordinateActionOption, CoordinateComparisonPreview

        if not isinstance(preview, CoordinateComparisonPreview) or not isinstance(
            option, CoordinateActionOption
        ):
            self.link_action_failed.emit(
                "The coordinate comparison preview is invalid."
            )
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication or Mushroom Observer credentials changed after preview. Refresh coordinate comparison."
            )
            return
        try:
            group_id, _action_ids = self.db.journal_coordinate_actions(
                preview, [option]
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self.resume_link_action_group(preview.profile_id, group_id)

    # Gate 1E photo transfer (MO -> iNat) -------------------------------

    def prepare_photo_identity_review(self, profile_id: int, pair_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Reading both photo sets for read-only identity review…"
        )
        self._start_worker(
            self.action_pool,
            "photo_identity",
            generation,
            lambda _progress: self.photos.prepare_identity_preview(
                profile_id,
                pair_id,
                cancelled=lambda: (
                    generation != self._generation or self._action_cancel_requested
                ),
            ),
            lambda _part, value, _gen: self._photo_identity_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def prepare_photo_comparison(self, profile_id: int, pair_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Reading photos for the selected confirmed pair…"
        )
        self._start_worker(
            self.action_pool,
            "photo_preview",
            generation,
            lambda _progress: self.photos.prepare_preview(
                profile_id,
                pair_id,
                cancelled=lambda: generation != self._generation
                or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._photo_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_photo_action(self, preview: object, option: object) -> None:
        from .types import PhotoActionOption, PhotoComparisonPreview

        if not isinstance(preview, PhotoComparisonPreview) or not isinstance(
            option, PhotoActionOption
        ):
            self.link_action_failed.emit("The photo comparison preview is invalid.")
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication or Mushroom Observer credentials changed after preview. Refresh photo comparison."
            )
            return
        try:
            # Generated and journaled BEFORE the request is ever sent: it is the
            # only key by which a lost response can be resolved without a
            # second, undeletable upload.
            planned_uuid = new_observation_photo_uuid()
            group_id, _action_ids = self.db.journal_photo_actions(
                preview,
                [option],
                planned_observation_photo_uuid=planned_uuid,
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self.resume_link_action_group(preview.profile_id, group_id)

    # Gate 2A missing-observation creation -------------------------------

    def prepare_observation_creation(
        self, profile_id: int, source_site: str, source_observation_id: int
    ) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Refreshing the source record and repeating the missing-record search…"
        )
        self._start_worker(
            self.action_pool,
            "observation_creation_preview",
            generation,
            lambda _progress: self.observation_creation.prepare_preview(
                profile_id,
                source_site,
                source_observation_id,
                cancelled=lambda: generation != self._generation
                or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._observation_creation_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_observation_creation_action(
        self, preview: object, selected_items: object
    ) -> None:
        from .types import ObservationCreationItem, ObservationCreationPreview

        if not isinstance(preview, ObservationCreationPreview):
            self.link_action_failed.emit("The observation creation preview is invalid.")
            return
        items = (
            list(selected_items) if isinstance(selected_items, (list, tuple)) else []
        )
        if not all(isinstance(item, ObservationCreationItem) for item in items):
            self.link_action_failed.emit("The selected creation items are invalid.")
            return
        # Round-3 finding 7: an irreversible-write boundary must not depend
        # on the UI having behaved -- reject any selected item that is not
        # an EXACT member of the immutable preview (by item_type,
        # source_identity, AND metadata_fingerprint, so a forged/edited item
        # object with the right identity but a tampered fingerprint is still
        # caught), that is disabled, or that is selected more than once.
        preview_lookup = {
            (item.item_type, item.source_identity): item for item in preview.items
        }
        if len(preview_lookup) != len(preview.items):
            self.link_action_failed.emit(
                "The observation creation preview is internally inconsistent."
            )
            return
        seen_identities: set[tuple[str, str]] = set()
        for item in items:
            key = (item.item_type, item.source_identity)
            reference = preview_lookup.get(key)
            if (
                reference is None
                or reference.metadata_fingerprint != item.metadata_fingerprint
                or not reference.enabled
            ):
                self.link_action_failed.emit(
                    "A selected item is not an approved, enabled member of the reviewed preview."
                )
                return
            if key in seen_identities:
                self.link_action_failed.emit(
                    "A selected item was submitted more than once."
                )
                return
            seen_identities.add(key)
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication or Mushroom Observer credentials changed after preview. Refresh the creation preview."
            )
            return
        try:
            # The marker is generated and journaled BEFORE the create request is
            # ever sent — the only way a lost response is recoverable without
            # risking a second, undeletable/undeduplicated observation. iNat
            # uses the marker itself as the create's client-supplied uuid; MO
            # embeds it in the observation's notes (disclosed to the user in
            # the preview when applicable).
            marker = (
                str(uuidlib.uuid4())
                if preview.destination_site.value == "inat"
                else (f"[observation-workbench-sync:{uuidlib.uuid4()}]")
            )
            marker_location = (
                "client_uuid_field"
                if preview.destination_site.value == "inat"
                else "public_notes"
            )
            group_id, _action_id, _attempt_id = (
                self.db.journal_observation_creation_actions(
                    preview.profile_id,
                    source_site=preview.source_site.value,
                    source_observation_id=preview.source_observation_id,
                    destination_site=preview.destination_site.value,
                    source_fingerprint=preview.source_fingerprint,
                    correlation_marker=marker,
                    marker_location=marker_location,
                    approved_field_gaps=preview.approved_field_gaps,
                    # Built from the CANONICAL preview_lookup entries, never the
                    # caller-supplied `items` objects directly -- the identity
                    # check above only proves each selection matches a preview
                    # member by (type, identity, metadata_fingerprint); every
                    # OTHER field (e.g. reviewed_byte_fingerprint) is taken from
                    # the immutable preview itself.
                    item_specs=[
                        {
                            "item_type": preview_lookup[
                                (item.item_type, item.source_identity)
                            ].item_type,
                            "source_identity": preview_lookup[
                                (item.item_type, item.source_identity)
                            ].source_identity,
                            "metadata_fingerprint": preview_lookup[
                                (item.item_type, item.source_identity)
                            ].metadata_fingerprint,
                            "reviewed_byte_fingerprint": preview_lookup[
                                (item.item_type, item.source_identity)
                            ].reviewed_byte_fingerprint,
                        }
                        for item in items
                    ],
                    # Section 5: pin the EXACT reviewed destination taxon at
                    # journal time, before any write -- never recomputed later
                    # and treated as equivalent.
                    reviewed_destination_taxon_id=preview.taxon_id,
                    reviewed_destination_taxon_name=preview.resolved_taxon_name,
                    reviewed_source_taxon_name=preview.taxon_name,
                    reviewed_source_taxon_rank=preview.taxon_rank,
                    resolution_mode=preview.resolution_mode,
                    taxon_resolution_fingerprint=preview.taxon_resolution_fingerprint,
                    reviewed_payload_fingerprint=preview.reviewed_payload_fingerprint,
                )
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self.resume_link_action_group(preview.profile_id, group_id)

    # Gate 2B duplicate-observation consolidation ----------------------

    def prepare_consolidation(
        self,
        profile_id: int,
        candidates: Sequence[tuple[str, int]],
    ) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        try:
            normalized = tuple(
                (RemoteSite(str(site)), int(observation_id))
                for site, observation_id in candidates
                if int(observation_id) > 0
            )
        except (TypeError, ValueError):
            self.link_action_failed.emit(
                "The duplicate-set observation ids are invalid."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Freshly reading every explicitly selected duplicate observation…"
        )
        self._start_worker(
            self.action_pool,
            "consolidation_preview",
            generation,
            lambda _progress: self.consolidation.prepare_preview(
                profile_id,
                normalized,
                lambda: generation != self._generation or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._consolidation_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_consolidation_action(self, preview: object) -> None:
        from .types import ConsolidationPreview

        if not isinstance(preview, ConsolidationPreview):
            self.link_action_failed.emit("The consolidation preview is invalid.")
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication or Mushroom Observer credentials changed after preview. "
                "Refresh the consolidation preview."
            )
            return
        try:
            _consolidation_id, _attempt_id, group_id = (
                self.db.journal_consolidation_attempt(preview)
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self.resume_link_action_group(preview.profile_id, group_id)

    # Gate 2C lossless donor-deletion review --------------------------

    def prepare_donor_deletion(
        self,
        profile_id: int,
        consolidation_id: int,
    ) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Freshly inventorying every superseded donor and canonical record…"
        )
        self._start_worker(
            self.action_pool,
            "deletion_preview",
            generation,
            lambda _progress: self.deletion.prepare_preview(
                profile_id,
                consolidation_id,
                lambda: (
                    generation != self._generation or self._action_cancel_requested
                ),
            ),
            lambda _part, value, _gen: self._deletion_preview_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_donor_deletion(
        self,
        preview: object,
        selected_member_ids: Sequence[int],
    ) -> None:
        if not isinstance(preview, DonorDeletionPreview):
            self.link_action_failed.emit("The deletion preview is invalid.")
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication changed after preview. Refresh deletion readiness."
            )
            return
        try:
            _attempt_id, group_id = self.db.journal_deletion_attempt(
                preview,
                selected_member_ids,
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self._resume_deletion_group(preview.profile_id, group_id)

    def _resume_deletion_group(self, profile_id: int, group_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self._start_worker(
            self.action_pool,
            "deletion_execute",
            generation,
            lambda _progress: self.deletion.execute_group(
                profile_id,
                group_id,
                lambda: (
                    generation != self._generation or self._action_cancel_requested
                ),
                self.link_action_progress.emit,
            ),
            lambda _part, value, _gen: self._link_execution_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def resume_donor_deletion(
        self,
        profile_id: int,
        consolidation_id: int,
    ) -> None:
        unresolved = self.db.unresolved_deletion_for_consolidation(
            profile_id,
            consolidation_id,
        )
        if not unresolved:
            self.link_action_failed.emit(
                "This consolidation has no resumable deletion attempt."
            )
            return
        if not unresolved.get("resumable"):
            self.link_action_failed.emit(
                "The prior deletion attempt is a settled partial result. "
                "Use Review donor deletion… for a fresh explicit retry plan."
            )
            return
        unknown_action_id = unresolved.get("unknown_action_id")
        if unknown_action_id is None:
            self._resume_deletion_group(profile_id, int(unresolved["action_group_id"]))
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self._start_worker(
            self.action_pool,
            "deletion_verify",
            generation,
            lambda _progress: [
                self.deletion.verify_unknown(
                    profile_id,
                    int(unknown_action_id),
                    lambda: (
                        generation != self._generation or self._action_cancel_requested
                    ),
                )
            ],
            lambda _part, value, _gen: self._link_execution_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    # Gate 1D name proposals -------------------------------------------

    def prepare_name_proposal(self, profile_id: int, pair_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit("Resolving name proposal candidates…")
        self._start_worker(
            self.action_pool,
            "name_proposal_preview",
            generation,
            lambda _progress: self.proposals.prepare_preview(
                profile_id,
                pair_id,
                cancelled=lambda: generation != self._generation
                or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._name_proposal_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def delegate_inat_identification(self, preview: object, candidate: object) -> None:
        """Freshly revalidate, then enqueue an iNaturalist identification.

        The revalidation rereads both observations (off the UI thread) so a
        preview that has gone stale is caught before any write. The write itself
        is performed by the Identify subsystem (paused by default); reconciliation
        records only the resulting identify action id.
        """
        from .types import NameProposalCandidate, NameProposalPreview

        if not isinstance(preview, NameProposalPreview) or not isinstance(
            candidate, NameProposalCandidate
        ):
            self.link_action_failed.emit("The name proposal preview is invalid.")
            return
        if self._identify_manager is None:
            self.link_action_failed.emit(
                "The Identify subsystem is unavailable for name delegation."
            )
            return
        if preview.auth_generation != self._auth_generation:
            self.link_action_failed.emit(
                "Authentication changed after preview. Refresh the name proposal."
            )
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Revalidating the name proposal against fresh remote state…"
        )
        self._start_worker(
            self.action_pool,
            "inat_delegation",
            generation,
            lambda _progress: self.proposals.inat_delegation_params(
                preview,
                candidate,
                lambda: generation != self._generation or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._complete_inat_delegation(preview, value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def _complete_inat_delegation(self, preview: object, params: object) -> None:
        """Enqueue the freshly revalidated identification on the main thread."""
        from .proposals import INatDelegationParams
        from .types import NameProposalPreview

        self._action_running = False
        if not isinstance(params, INatDelegationParams) or not isinstance(
            preview, NameProposalPreview
        ):
            self.link_action_failed.emit("The name proposal revalidation was invalid.")
            return
        if self._identify_manager is None:
            self.link_action_failed.emit(
                "The Identify subsystem is unavailable for name delegation."
            )
            return
        # Authentication can change in the small interval between the worker's
        # account check and this main-thread enqueue; revalidate the generation
        # before touching the Identify subsystem.
        if preview.auth_generation != self._auth_generation:
            self.link_action_failed.emit(
                "Authentication changed after revalidation. Refresh the name proposal before delegating."
            )
            self.link_actions_changed.emit()
            return
        # Inspect the Identify journal (including terminal states) for a matching
        # action and act on what it actually means — never blindly reuse or
        # duplicate. A fresh enqueue happens only when no prior action exists.
        try:
            existing = self._identify_manager.find_existing_identification(
                account_login=params.account_login,
                observation_id=params.observation_id,
                taxon_id=params.taxon_id,
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            self.link_actions_changed.emit()
            return

        if existing is None:
            self._enqueue_and_record_delegation(preview, params)
            return
        if existing.disposition == "needs_retry":
            self.link_action_failed.emit(
                f"A prior identification for this observation and taxon ended in state "
                f"'{existing.state}' (Identify action {existing.action_id}). Use the Identify subsystem's "
                "retry rather than re-proposing here; no duplicate identification was created."
            )
            self.link_actions_changed.emit()
            return
        if existing.disposition == "needs_recovery":
            # Record the link so the pair points at the unresolved action, but do
            # not enqueue anything — Identify must reconcile the unknown outcome.
            self._try_record_delegation(preview, existing.action_id)
            self.name_proposal_changed.emit()
            self.link_action_progress.emit(
                f"A prior identification for this observation and taxon has an unknown outcome "
                f"(Identify action {existing.action_id}). Recover it in the Identify subsystem before "
                "proposing again; no duplicate identification was created."
            )
            return
        if existing.disposition == "completed":
            self._try_record_delegation(preview, existing.action_id)
            self.name_proposal_changed.emit()
            self.link_action_progress.emit(
                f"This identification was already completed earlier (Identify action {existing.action_id}); "
                "no new identification was queued."
            )
            return
        # Reusable (still unresolved): link to the existing action, no new enqueue.
        self._try_record_delegation(preview, existing.action_id)
        self.name_proposal_changed.emit()
        self.link_action_progress.emit(
            f"Reused the existing queued iNaturalist identification (Identify action {existing.action_id}); "
            "open Identify to review and submit it."
        )

    def _enqueue_and_record_delegation(self, preview: object, params: object) -> None:
        assert self._identify_manager is not None
        try:
            result = self._identify_manager.queue_identification(
                account_login=params.account_login,  # type: ignore[attr-defined]
                observation_id=params.observation_id,  # type: ignore[attr-defined]
                observation_uuid=params.observation_uuid,  # type: ignore[attr-defined]
                taxon_id=params.taxon_id,  # type: ignore[attr-defined]
            )
            action_id = int(result.action_id)
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            self.link_actions_changed.emit()
            return
        if self._try_record_delegation(preview, action_id):
            self.name_proposal_changed.emit()
            self.link_action_progress.emit(
                f"Queued iNaturalist identification as Identify action {action_id}; "
                "open Identify to review and submit it."
            )
        else:
            self.name_proposal_changed.emit()
            self.link_action_progress.emit(
                f"Queued iNaturalist identification as Identify action {action_id}, but recording "
                "reconciliation tracking failed. The identification is safe. Re-running the proposal "
                "reconciles to that existing Identify action rather than creating a second identification."
            )

    def _try_record_delegation(self, preview: object, action_id: int) -> bool:
        try:
            self.db.record_name_delegation(
                preview.profile_id,
                preview.pair_id,
                action_id,  # type: ignore[attr-defined]
            )
            return True
        except Exception:
            return False

    def record_mo_proposal_draft(self, preview: object, candidate: object) -> None:
        from .types import NameProposalCandidate, NameProposalPreview

        if not isinstance(preview, NameProposalPreview) or not isinstance(
            candidate, NameProposalCandidate
        ):
            self.link_action_failed.emit("The name proposal candidate is invalid.")
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Recording the Mushroom Observer name-proposal draft…"
        )
        self._start_worker(
            self.action_pool,
            "mo_proposal_draft",
            generation,
            lambda _progress: self.proposals.record_mo_proposal_draft(
                preview,
                candidate,
                lambda: generation != self._generation or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._name_proposal_action_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def refresh_mo_proposal(self, profile_id: int, pair_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        self.link_action_progress.emit(
            "Rereading the Mushroom Observer consensus name…"
        )
        self._start_worker(
            self.action_pool,
            "mo_proposal_refresh",
            generation,
            lambda _progress: self.proposals.refresh_proposal_effectiveness(
                profile_id,
                pair_id,
                lambda: generation != self._generation or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._name_proposal_action_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def execute_link_repairs(self, preview: object, options: list[object]) -> None:
        from .types import LinkRepairOption, LinkRepairPreview

        if not isinstance(preview, LinkRepairPreview) or not all(
            isinstance(item, LinkRepairOption) for item in options
        ):
            self.link_action_failed.emit("The link-repair preview is invalid.")
            return
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        if (
            preview.auth_generation != self._auth_generation
            or preview.mo_key_generation != self._mo_key_generation
        ):
            self.link_action_failed.emit(
                "Authentication or Mushroom Observer credentials changed after preview. Refresh the preview."
            )
            return
        try:
            operation_order = {"add": 0, "repair": 1, "remove": 2}
            ordered_options = sorted(
                options,
                key=lambda item: operation_order[
                    item.action_type.value.rsplit("_", 1)[-1]
                ],
            )
            simulate_link_repair_final_state(preview, ordered_options)
            group_id, _action_ids = self.db.journal_link_actions(
                preview, ordered_options
            )
        except Exception as exc:
            self.link_action_failed.emit(_safe_error(exc))
            return
        self.resume_link_action_group(preview.profile_id, group_id)

    def resume_link_action_group(self, profile_id: int, group_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        rows = self.db.action_group_rows(profile_id, group_id)
        action_type = str(rows[0].get("action_type") or "") if rows else ""
        service = (
            self.consolidation
            if self.db.consolidation_ledger_for_group(profile_id, group_id)
            else self._service_for_action_type(action_type)
        )
        self._start_worker(
            self.action_pool,
            "link_execute",
            generation,
            lambda _progress: service.execute_group(
                profile_id,
                group_id,
                lambda: generation != self._generation or self._action_cancel_requested,
                self.link_action_progress.emit,
            ),
            lambda _part, value, _gen: self._link_execution_result(value),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def verify_unknown_link_action(self, profile_id: int, action_id: int) -> None:
        if self._scan is not None or self._action_running:
            self.link_action_failed.emit(
                "Wait for the current reconciliation operation to finish."
            )
            return
        generation = self._generation
        self._action_running = True
        self._action_cancel_requested = False
        action = self.db.action(profile_id, action_id) or {}
        service = (
            self.consolidation
            if action.get("action_group_id") is not None
            and self.db.consolidation_ledger_for_group(
                profile_id, int(action["action_group_id"])
            )
            else self._service_for_action_type(str(action.get("action_type") or ""))
        )
        self._start_worker(
            self.action_pool,
            "link_verify",
            generation,
            lambda _progress: service.verify_unknown(
                profile_id,
                action_id,
                lambda: generation != self._generation or self._action_cancel_requested,
            ),
            lambda _part, value, _gen: self._link_execution_result([value]),
            lambda _part, error, _gen: self._link_action_error(error),
        )

    def _service_for_action_type(self, action_type: str):
        """Route a journaled action to its owning execution service by type prefix."""
        if action_type.startswith("inat_coordinate_"):
            return self.coordinates
        if action_type.startswith(("inat_its_", "mo_sequence_")):
            return self.its_sync
        if action_type in ("inat_photo_attach", "mo_photo_attach"):
            return self.photos
        if action_type in (
            "inat_observation_create",
            "mo_observation_create",
            "pair_finalize",
        ):
            return self.observation_creation
        if action_type == "consolidation_finalize":
            return self.consolidation
        return self.link_repairs

    def cancel_link_actions(self) -> None:
        if self._action_running:
            self._action_cancel_requested = True
            self.link_action_progress.emit(
                "Cancellation requested; a submitted write will still be verified before stopping."
            )

    def cancel_current_operation(self) -> None:
        if self._action_running:
            self.cancel_link_actions()
        else:
            self.cancel()

    def _link_preview_result(self, value: object) -> None:
        self._action_running = False
        self.link_preview_ready.emit(value)

    def _its_preview_result(self, value: object) -> None:
        self._action_running = False
        self.its_preview_ready.emit(value)

    def _coordinate_preview_result(self, value: object) -> None:
        self._action_running = False
        self.coordinate_preview_ready.emit(value)

    def _photo_preview_result(self, value: object) -> None:
        self._action_running = False
        self.photo_preview_ready.emit(value)

    def _photo_identity_result(self, value: object) -> None:
        self._action_running = False
        self.photo_identity_ready.emit(value)

    def _observation_creation_preview_result(self, value: object) -> None:
        self._action_running = False
        self.observation_creation_preview_ready.emit(value)

    def _consolidation_preview_result(self, value: object) -> None:
        self._action_running = False
        self.consolidation_preview_ready.emit(value)

    def _deletion_preview_result(self, value: object) -> None:
        self._action_running = False
        self.deletion_preview_ready.emit(value)

    def _name_proposal_result(self, value: object) -> None:
        self._action_running = False
        self.name_proposal_ready.emit(value)

    def _name_proposal_action_result(self, value: object) -> None:
        self._action_running = False
        message = (
            value.message
            if isinstance(value, NameProposalResult)
            else "Name proposal updated."
        )
        self.link_action_progress.emit(message)
        self.name_proposal_changed.emit()

    def _link_execution_result(self, value: object) -> None:
        self._action_running = False
        results = value if isinstance(value, list) else []
        if results:
            final = results[-1]
            message = (
                final.message
                if isinstance(
                    final,
                    (
                        LinkActionResult,
                        ITSActionResult,
                        CoordinateActionResult,
                        PhotoActionResult,
                        ObservationCreationResult,
                        ConsolidationActionResult,
                        DeletionActionResult,
                    ),
                )
                else "Reconciliation action finished."
            )
            self.link_action_progress.emit(message)
        else:
            self.link_action_progress.emit(
                "No pending link actions remain in this group."
            )
        self.link_actions_changed.emit()

    def _link_action_error(self, error: str) -> None:
        self._action_running = False
        self.link_action_failed.emit(error)
        self.link_actions_changed.emit()

    def hydrate(self, site: str, observation_id: int, profile_id: int) -> None:
        generation = self._generation
        profile = self.db.profile(profile_id)
        if site == "inat":
            auth = self.auth_provider()
            token = (
                auth.api_token
                if auth.is_authenticated
                and auth.login.casefold() == profile.inat_login.casefold()
                else ""
            )
            self._start_worker(
                self.inat_pool,
                f"detail:inat:{observation_id}",
                generation,
                lambda _progress: self.inat_client.get_reconciliation_detail(
                    observation_id, token
                ),
                lambda part, value, gen: self._detail_result(
                    profile_id, "inat", observation_id, part, value, bool(token)
                ),
                lambda part, error, gen: self.details_loaded.emit(
                    part, {"error": error}
                ),
            )
        else:
            self._start_worker(
                self.mo_pool,
                f"detail:mo:{observation_id}",
                generation,
                lambda _progress: self.mo_client.observation(
                    observation_id,
                    lambda: generation != self._generation,
                    detail="high",
                ),
                lambda part, value, gen: self._detail_result(
                    profile_id, "mo", observation_id, part, value, True
                ),
                lambda part, error, gen: self.details_loaded.emit(
                    part, {"error": error}
                ),
            )

    def _detail_result(
        self,
        profile_id: int,
        site: str,
        observation_id: int,
        part: str,
        value: object,
        authorized: bool,
    ) -> None:
        generation = self._generation
        self._start_worker(
            self.inat_pool,
            f"detail_reconcile:{site}:{observation_id}",
            generation,
            lambda _progress: self._apply_detail_enrichment(
                profile_id, site, observation_id, value, authorized
            ),
            lambda _part, result, _gen: self.details_loaded.emit(part, result),
            lambda _part, error, _gen: self.details_loaded.emit(part, {"error": error}),
        )

    def _apply_detail_enrichment(
        self,
        profile_id: int,
        site: str,
        observation_id: int,
        value: object,
        authorized: bool,
    ) -> object:
        raw = _first_result(value)
        records = {
            item.key.observation_id: item
            for item in self.db.inventory_records(profile_id, site)
        }
        if raw and observation_id in records:
            detail = _hydrate_record(
                records[observation_id], raw, authorized, include_its=True
            )
            self.db.store_identifiers(
                profile_id,
                site,
                observation_id,
                [
                    *(("voucher", item) for item in detail.voucher_identifiers),
                    *(("collection", item) for item in detail.collection_identifiers),
                    *(("accession", item) for item in detail.accessions),
                ],
                evidence_tier=3,
            )
            self.db.store_sequence_hashes(
                profile_id,
                site,
                observation_id,
                detail.sequence_hashes,
                evidence_tier=3,
            )
            candidates = build_candidates(
                self.db.inventory_records(profile_id, "mo"),
                self.db.inventory_records(profile_id, "inat"),
            )
            for candidate in candidates:
                if (site == "mo" and candidate.mo_observation_id == observation_id) or (
                    site == "inat" and candidate.inat_observation_id == observation_id
                ):
                    self.db.replace_candidate(
                        profile_id,
                        _candidate_with_preserved_deep(self.db, profile_id, candidate),
                    )
        return value

    def fetch_thumbnail(self, identity: MediaIdentity, url: str) -> None:
        """Fetch one displayed rendition into memory; never touch disk cache."""
        generation = self._generation
        self._start_worker(
            self.inat_pool,
            "thumbnail",
            generation,
            lambda _progress: self.inat_client.download_image(url),
            lambda _part, value, _gen: self.thumbnail_loaded.emit(identity, value),
            lambda _part, _error, _gen: self.thumbnail_loaded.emit(identity, b""),
        )

    def load_field_candidates(self) -> None:
        generation = self._generation
        reader = INatReconciliationReader(self.inat_client)
        self._start_worker(
            self.inat_pool,
            "field_candidates",
            generation,
            lambda _progress: {
                "mo_url": reader.resolve_field_definitions(MO_FIELD_NAME),
                "its": reader.resolve_field_definitions(ITS_FIELD_NAME, ("dna",)),
                "its_accession": reader.resolve_field_definitions(ACCESSION_FIELD_NAME),
            },
            lambda _part, value, _gen: self.field_candidates_loaded.emit(value),
            lambda _part, error, _gen: self.field_candidates_loaded.emit(
                {"error": error}
            ),
        )

    def record_displayed_media_hash(
        self,
        profile_id: int,
        identity: MediaIdentity,
        source_fingerprint: str,
        exact_pixel_hash: str,
        perceptual_hash: str,
    ) -> None:
        self.db.store_media_hash(
            profile_id,
            identity.site.value,
            identity.photo_id,
            identity.rendition,
            source_fingerprint,
            exact_pixel_hash,
            perceptual_hash,
        )
        mo_records = {
            item.key.observation_id: item
            for item in self.db.inventory_records(profile_id, "mo")
        }
        inat_records = {
            item.key.observation_id: item
            for item in self.db.inventory_records(profile_id, "inat")
        }
        for mo_id, inat_id, exact in self.db.media_hash_pairs(
            profile_id, identity.site.value, identity.photo_id, identity.rendition
        ):
            if mo_id not in mo_records or inat_id not in inat_records:
                continue
            evidence = MatchEvidence(
                "displayed_pixel_hash" if exact else "perceptual_displayed_image",
                EvidenceFamily.MEDIA,
                50 if exact else 25,
                (
                    "Displayed pixels match exactly."
                    if exact
                    else "Displayed images are perceptual candidates."
                ),
                EvidenceTier.DEEP,
            )
            self.add_deep_evidence(profile_id, mo_id, inat_id, evidence)

    def add_deep_evidence(
        self,
        profile_id: int,
        mo_id: int,
        inat_id: int,
        evidence: MatchEvidence,
    ) -> None:
        mo_records = {
            item.key.observation_id: item
            for item in self.db.inventory_records(profile_id, "mo")
        }
        inat_records = {
            item.key.observation_id: item
            for item in self.db.inventory_records(profile_id, "inat")
        }
        if mo_id not in mo_records or inat_id not in inat_records:
            return
        current = self.db.pair_by_records(profile_id, mo_id, inat_id)
        deep: list[MatchEvidence] = []
        if current:
            for item in current.get("evidence", []):
                if (
                    int(item["tier"]) >= int(EvidenceTier.DEEP)
                    and item["evidence_type"] != evidence.evidence_type
                ):
                    deep.append(
                        MatchEvidence(
                            str(item["evidence_type"]),
                            EvidenceFamily(str(item["family"])),
                            int(item["score"]),
                            str(item["explanation"]),
                            EvidenceTier(int(item["tier"])),
                        )
                    )
        deep.append(evidence)
        candidate = score_candidate(
            mo_records[mo_id], inat_records[inat_id], deep, explicit_review=True
        )
        if candidate:
            state = (
                str(current["link_state"])
                if current and str(current["link_state"]).startswith("link_confirmed")
                else candidate.state
            )
            self.db.replace_candidate(
                profile_id,
                ObservationPair(
                    mo_id, inat_id, state, candidate.score, evidence=candidate.evidence
                ),
            )

    def _start_worker(
        self,
        pool: QThreadPool,
        part: str,
        generation: int,
        function: Callable,
        result_slot: Callable,
        error_slot: Callable,
    ) -> None:
        def invoke(progress: Callable) -> object:
            try:
                return function(progress)
            finally:
                self.db.close_thread_connection()

        worker = _CallableWorker(
            part, generation, invoke, lambda: generation != self._generation
        )
        signals = worker.signals
        self._live_signals.add(signals)
        signals.progress.connect(self._progress)

        # Every `_action_running = False` lives inside an action's result/error
        # slot, and those slots are skipped when the generation has moved on. A
        # dropped action result would therefore leave the flag latched forever,
        # and each guard reading it would refuse every later action with "Wait
        # for the current reconciliation operation to finish" until restart —
        # reachable because `resolve_accounts` bumps the generation without
        # checking `_action_running`, and the coordinator is shared by every
        # reconciliation window. Release the flag here, where it is owned.
        owns_action_flag = pool is self.action_pool

        def settle(gen: int) -> bool:
            self._live_signals.discard(signals)
            if gen == self._generation:
                return True
            if owns_action_flag:
                self._action_running = False
                self.link_actions_changed.emit()
            return False

        def result(p: str, value: object, gen: int) -> None:
            if settle(gen):
                result_slot(p, value, gen)

        def error(p: str, message: str, gen: int) -> None:
            if settle(gen):
                error_slot(p, message, gen)

        signals.result.connect(result)
        signals.error.connect(error)
        pool.start(worker)

    def _progress(self, part: str, current: int, total: int, generation: int) -> None:
        # Match on the base part: a sub-stage arrives as "mo:external_links".
        if (
            generation == self._generation
            and part.split(":", 1)[0] in SCAN_PROGRESS_PARTS
        ):
            self.scan_progress.emit(part, current, total)

    def _lookup_result(self, part: str, value: object, generation: int) -> None:
        if generation != self._generation:
            return
        if not value:
            self.account_resolution_failed.emit(
                f"No exact {part.removeprefix('lookup_')} account match was found."
            )
            self._generation += 1
            return
        self._lookup[part] = value
        if {"lookup_inat", "lookup_mo"}.issubset(self._lookup):
            self.accounts_resolved.emit(
                self._lookup["lookup_inat"], self._lookup["lookup_mo"]
            )

    def _lookup_error(self, part: str, error: str, generation: int) -> None:
        if generation == self._generation:
            self.account_resolution_failed.emit(
                f"{part.removeprefix('lookup_')} lookup failed: {error}"
            )
            self._generation += 1

    def _scan_result(self, part: str, value: object, generation: int) -> None:
        state = self._scan
        if generation != self._generation or state is None:
            return
        state.results[part] = value
        if not {"inat", "mo"}.issubset(state.results):
            return
        if "prepared_started" not in state.results:
            state.results["prepared_started"] = True
            self._start_worker(
                self.inat_pool,
                "prepared",
                generation,
                lambda progress: self._prepare_scan_records(state, progress),
                self._scan_result,
                self._scan_error,
            )
            return
        if "prepared" not in state.results:
            return
        if "context_started" not in state.results:
            state.results["context_started"] = True
            prepared = state.results["prepared"]
            inat_ids = prepared["missing_inat"]
            mo_ids = prepared["missing_mo"]
            if inat_ids:
                self._start_worker(
                    self.inat_pool,
                    "context_inat",
                    generation,
                    lambda progress: self._fetch_inat_context(
                        state.profile,
                        inat_ids,
                        "",
                        generation,
                        progress,
                        state.results["inat"].get("mo_binding"),
                        state.results["inat"].get("its_binding"),
                        set(prepared["linked_missing_inat"]),
                    ),
                    self._scan_result,
                    self._scan_error,
                )
            else:
                state.results["context_inat"] = []
            if mo_ids:
                self._start_worker(
                    self.mo_pool,
                    "context_mo",
                    generation,
                    lambda progress: self._fetch_mo_context(
                        state.profile,
                        mo_ids,
                        generation,
                        progress,
                        state.results["mo"].get("inat_site_id"),
                    ),
                    self._scan_result,
                    self._scan_error,
                )
            else:
                state.results["context_mo"] = []
        if not {"context_inat", "context_mo"}.issubset(state.results):
            return
        if "validation_input_started" not in state.results:
            state.results["validation_input_started"] = True
            self._start_worker(
                self.inat_pool,
                "validation_input",
                generation,
                lambda progress: self._prepare_validation_input(state, progress),
                self._scan_result,
                self._scan_error,
            )
            return
        if "validation_input" not in state.results:
            return
        if "validation_started" not in state.results:
            state.results["validation_started"] = True
            validation_input = state.results["validation_input"]
            reciprocal = validation_input["reciprocal"]
            auth = self.auth_provider()
            token = (
                auth.api_token
                if (
                    auth.is_authenticated
                    and auth.login.casefold() == state.profile.inat_login.casefold()
                )
                else ""
            )
            if reciprocal:
                self._start_worker(
                    self.inat_pool,
                    "validation_inat",
                    generation,
                    lambda progress: self._hydrate_inat_pairs(
                        validation_input["inat"],
                        reciprocal,
                        token,
                        generation,
                        progress,
                    ),
                    self._scan_result,
                    self._scan_error,
                )
                self._start_worker(
                    self.mo_pool,
                    "validation_mo",
                    generation,
                    lambda progress: self._hydrate_mo_pairs(
                        validation_input["mo"], reciprocal, generation, progress
                    ),
                    self._scan_result,
                    self._scan_error,
                )
            else:
                state.results["validation_inat"] = {}
                state.results["validation_mo"] = {}
        if not {"validation_inat", "validation_mo"}.issubset(state.results):
            return
        if "planning_started" not in state.results:
            state.results["planning_started"] = True
            self._start_worker(
                self.inat_pool,
                "plan",
                generation,
                lambda progress: self._build_scan_plan(state, progress),
                self._scan_result,
                self._scan_error,
            )
            return
        if "plan" not in state.results:
            return
        if "persistence_started" not in state.results:
            state.results["persistence_started"] = True
            self._start_worker(
                self.inat_pool,
                "persist",
                generation,
                lambda _progress: self._persist_scan_plan(state),
                self._scan_result,
                self._scan_error,
            )
            return
        if "persist" not in state.results:
            return
        self._scan = None
        self.scan_finished.emit(str(state.results["persist"]))

    def _prepare_scan_records(
        self, state: _ScanState, progress: Callable
    ) -> dict[str, Any]:
        profile_id = state.profile.profile_id
        inat = {
            item.key.observation_id: item
            for item in self.db.inventory_records(
                profile_id,
                "inat",
                scope_states=("in_scope", "linked_context", "out_of_scope"),
            )
        }
        mo = {
            item.key.observation_id: item
            for item in self.db.inventory_records(
                profile_id,
                "mo",
                scope_states=("in_scope", "linked_context", "out_of_scope"),
            )
        }
        prior_inat = dict(inat)
        prior_mo = dict(mo)
        inat_updates = list(state.results["inat"]["records"])
        mo_updates = list(state.results["mo"]["records"])
        inat.update((item.key.observation_id, item) for item in inat_updates)
        mo.update((item.key.observation_id, item) for item in mo_updates)
        inat_links_enabled = bool(state.results["inat"].get("mo_binding")) and not any(
            state.results["inat"].get(key)
            for key in (
                "mo_binding_invalid",
                "mo_binding_missing",
                "mo_binding_ambiguous",
            )
        )
        mo_links_enabled = bool(state.results["mo"].get("inat_site_id")) and not bool(
            state.results["mo"].get("link_config_error")
        )
        if not inat_links_enabled:
            inat = {
                observation_id: _without_authoritative_links(item)
                for observation_id, item in inat.items()
            }
        if not mo_links_enabled:
            mo = {
                observation_id: _without_authoritative_links(item)
                for observation_id, item in mo.items()
            }
        for observation_id in state.results["inat"].get("deleted", ()):
            if int(observation_id) in inat:
                inat[int(observation_id)] = _with_deleted(inat[int(observation_id)])
        for observation_id in state.results["mo"].get("deleted", ()):
            if int(observation_id) in mo:
                mo[int(observation_id)] = _with_deleted(mo[int(observation_id)])

        missing_inat: set[int] = set()
        missing_mo: set[int] = set()
        for record in mo.values():
            if record.deleted:
                continue
            for target_id in record.authoritative_targets:
                target = inat.get(target_id)
                if target is None or target.deleted or target.scope_state != "in_scope":
                    missing_inat.add(target_id)
        for record in inat.values():
            if record.deleted:
                continue
            for target_id in record.authoritative_targets:
                target = mo.get(target_id)
                if target is None or target.deleted or target.scope_state != "in_scope":
                    missing_mo.add(target_id)
        linked_missing_inat = set(missing_inat)
        inat_full = bool(state.results["inat"].get("full_inventory"))
        mo_full = bool(
            state.results["mo"].get("capabilities", {}).get("full_inventory")
        )
        scanned_inat = {item.key.observation_id for item in inat_updates}
        scanned_mo = {item.key.observation_id for item in mo_updates}
        if inat_full:
            missing_inat.update(
                observation_id
                for observation_id, item in prior_inat.items()
                if item.scope_state == "in_scope"
                and observation_id not in scanned_inat
                and observation_id not in state.results["inat"].get("deleted", ())
            )
        for mo_id, inat_id in self.db.confirmed_pair_keys(profile_id):
            if inat_full and inat_id not in scanned_inat:
                missing_inat.add(inat_id)
            if mo_full and mo_id not in scanned_mo:
                missing_mo.add(mo_id)
        changed: list[tuple[str, int, str, str]] = []
        for site, updates, prior in (
            ("inat", inat_updates, prior_inat),
            ("mo", mo_updates, prior_mo),
        ):
            for item in updates:
                old = prior.get(item.key.observation_id)
                if old and old.content_fingerprint != item.content_fingerprint:
                    changed.append(
                        (
                            site,
                            item.key.observation_id,
                            old.content_fingerprint,
                            item.content_fingerprint,
                        )
                    )
        progress(len(inat) + len(mo), len(inat) + len(mo))
        return {
            "inat": inat,
            "mo": mo,
            "missing_inat": sorted(missing_inat),
            "missing_mo": sorted(missing_mo),
            "linked_missing_inat": linked_missing_inat,
            "changed": changed,
        }

    def _fetch_inat_context(
        self,
        profile: ReconciliationProfile,
        observation_ids: list[int],
        token: str,
        generation: int,
        progress: Callable,
        mo_binding: object,
        _its_binding: object,
        linked_context_ids: set[int],
    ) -> list[InventoryObservation]:
        reader = INatReconciliationReader(self.inat_client)
        records: list[InventoryObservation] = []
        for start in range(0, len(observation_ids), 200):
            if generation != self._generation:
                raise ReconciliationCancelled()
            batch = observation_ids[start : start + 200]
            payload = self.inat_client.get_reconciliation_context(batch, token)
            for raw in payload.get("results") or []:
                if isinstance(raw, dict):
                    record = reader.parse_inventory(
                        raw,
                        profile.inat_user_id,
                        mo_binding["id"] if mo_binding else None,
                        None,
                    )
                    scope = (
                        "in_scope"
                        if (
                            record.owner_id == profile.inat_user_id
                            and record.fungi_status == "fungi"
                        )
                        else (
                            "linked_context"
                            if record.key.observation_id in linked_context_ids
                            else "out_of_scope"
                        )
                    )
                    records.append(_with_scope_state(record, scope))
            progress(
                min(start + len(batch), len(observation_ids)), len(observation_ids)
            )
        return records

    def _fetch_mo_context(
        self,
        profile: ReconciliationProfile,
        observation_ids: list[int],
        generation: int,
        progress: Callable,
        inat_site_id: Optional[int],
    ) -> list[InventoryObservation]:
        cancelled = lambda: generation != self._generation
        records: list[InventoryObservation] = []
        for index, observation_id in enumerate(observation_ids, 1):
            try:
                raw = _first_result(
                    self.mo_client.observation(observation_id, cancelled, detail="low")
                )
            except MOAPIError as exc:
                if exc.status_code not in {404, 410}:
                    raise
                raw = None
            if raw:
                records.append(
                    _with_scope_state(
                        parse_mo_observation(raw, profile.mo_user_id), "linked_context"
                    )
                )
            progress(index, len(observation_ids))
        if not records or inat_site_id is None:
            return records
        rows = results_from_payload(
            self.mo_client.external_links(
                (item.key.observation_id for item in records), cancelled
            )
        )
        by_observation: dict[int, list[AuthoritativeLinkRow]] = {}
        for row in rows:
            parsed = parse_mo_external_link(row, inat_site_id)
            if parsed is None:
                continue
            source_id, link = parsed
            by_observation.setdefault(source_id, []).append(link)
        return [
            _with_links(item, by_observation.get(item.key.observation_id, []))
            for item in records
        ]

    def _prepare_validation_input(
        self, state: _ScanState, progress: Callable
    ) -> dict[str, Any]:
        prepared = state.results["prepared"]
        inat = dict(prepared["inat"])
        mo = dict(prepared["mo"])
        inat.update(
            (item.key.observation_id, item) for item in state.results["context_inat"]
        )
        mo.update(
            (item.key.observation_id, item) for item in state.results["context_mo"]
        )
        context_placeholders: list[InventoryObservation] = []
        returned_inat = {
            item.key.observation_id for item in state.results["context_inat"]
        }
        for observation_id in set(prepared["missing_inat"]) - returned_inat:
            existing = inat.get(observation_id)
            if existing and not existing.deleted:
                scope = (
                    "linked_context"
                    if observation_id in prepared["linked_missing_inat"]
                    else "out_of_scope"
                )
                replacement = _with_unavailable_scope(existing, scope)
                inat[observation_id] = replacement
                context_placeholders.append(replacement)
        returned_mo = {item.key.observation_id for item in state.results["context_mo"]}
        for observation_id in set(prepared["missing_mo"]) - returned_mo:
            existing = mo.get(observation_id)
            if existing and not existing.deleted:
                replacement = _with_unavailable_scope(existing, "linked_context")
                mo[observation_id] = replacement
                context_placeholders.append(replacement)
        reciprocal: list[tuple[int, int]] = []
        for mo_id, mo_record in mo.items():
            if (
                mo_record.deleted
                or mo_record.scope_state != "in_scope"
                or mo_record.fungi_status not in {"fungi", "unknown"}
                or mo_record.link_malformed
            ):
                continue
            for inat_id in mo_record.authoritative_targets:
                inat_record = inat.get(inat_id)
                if (
                    inat_record
                    and not inat_record.deleted
                    and inat_record.scope_state == "in_scope"
                    and inat_record.fungi_status == "fungi"
                    and not inat_record.link_malformed
                    and mo_id in inat_record.authoritative_targets
                ):
                    reciprocal.append((mo_id, inat_id))
        progress(len(reciprocal), len(reciprocal))
        return {
            "inat": inat,
            "mo": mo,
            "reciprocal": sorted(set(reciprocal)),
            "context_placeholders": context_placeholders,
        }

    def _build_scan_plan(self, state: _ScanState, progress: Callable) -> dict[str, Any]:
        validation_input = state.results["validation_input"]
        inat = dict(validation_input["inat"])
        mo = dict(validation_input["mo"])
        superseded = self.db.superseded_member_keys(state.profile.profile_id)
        inat = {
            observation_id: record
            for observation_id, record in inat.items()
            if ("inat", observation_id) not in superseded
        }
        mo = {
            observation_id: record
            for observation_id, record in mo.items()
            if ("mo", observation_id) not in superseded
        }
        active_reciprocal = [
            (mo_id, inat_id)
            for mo_id, inat_id in validation_input["reciprocal"]
            if ("mo", mo_id) not in superseded and ("inat", inat_id) not in superseded
        ]
        inat_details: dict[int, HydratedObservation] = state.results["validation_inat"]
        mo_details: dict[int, HydratedObservation] = state.results["validation_mo"]
        invalidated_records = {
            (site, observation_id)
            for site, observation_id, _old, _new in state.results["prepared"]["changed"]
        }
        invalidated_records.update(
            (site, int(observation_id))
            for site, result in (
                ("inat", state.results["inat"]),
                ("mo", state.results["mo"]),
            )
            for observation_id in result.get("deleted", ())
        )
        metadata_by_record: dict[tuple[str, int], tuple[tuple[str, str], ...]] = {
            key: () for key in invalidated_records
        }
        metadata_sequences: list[tuple[str, int, tuple[str, ...]]] = []
        for site, records, details in (
            ("mo", mo, mo_details),
            ("inat", inat, inat_details),
        ):
            for observation_id, detail in details.items():
                if observation_id not in records:
                    continue
                identifiers = tuple(
                    [
                        *(("voucher", value) for value in detail.voucher_identifiers),
                        *(
                            ("collection", value)
                            for value in detail.collection_identifiers
                        ),
                        *(("accession", value) for value in detail.accessions),
                    ]
                )
                metadata_by_record[(site, observation_id)] = identifiers
                records[observation_id] = _with_hydrated_metadata(
                    records[observation_id], detail
                )

        validations: dict[
            tuple[int, int],
            tuple[
                str,
                tuple[str, ...],
                Optional[HydratedObservation],
                Optional[HydratedObservation],
            ],
        ] = {}
        for mo_id, inat_id in active_reciprocal:
            mo_detail = mo_details.get(mo_id)
            inat_detail = inat_details.get(inat_id)
            if mo_detail is None or inat_detail is None:
                validations[(mo_id, inat_id)] = (
                    "ambiguous_link",
                    ("A required detail response was unavailable.",),
                    mo_detail,
                    inat_detail,
                )
            else:
                result = validate_reciprocal_pair(mo_detail, inat_detail)
                validations[(mo_id, inat_id)] = (
                    result[0],
                    result[1],
                    mo_detail,
                    inat_detail,
                )

        candidates = build_candidates(list(mo.values()), list(inat.values()))
        snapshots = self.db.pair_snapshots(state.profile.profile_id)
        candidates = [
            (
                candidate
                if (
                    ("mo", candidate.mo_observation_id) in invalidated_records
                    or ("inat", candidate.inat_observation_id) in invalidated_records
                )
                else _candidate_with_snapshot_deep(snapshots, candidate)
            )
            for candidate in candidates
        ]
        strong_by_mo: dict[int, int] = {}
        strong_by_inat: dict[int, int] = {}
        for candidate in candidates:
            if candidate.score >= 70:
                strong_by_mo[candidate.mo_observation_id] = (
                    strong_by_mo.get(candidate.mo_observation_id, 0) + 1
                )
                strong_by_inat[candidate.inat_observation_id] = (
                    strong_by_inat.get(candidate.inat_observation_id, 0) + 1
                )

        issues: list[SyncIssue] = []
        pairs: list[ObservationPair] = []
        auto_confirm: list[tuple[int, int]] = []
        for candidate in candidates:
            key = (candidate.mo_observation_id, candidate.inat_observation_id)
            mo_record = mo[key[0]]
            inat_record = inat[key[1]]
            validation = validations.get(key)
            mo_direction = key[1] in mo_record.authoritative_targets
            inat_direction = key[0] in inat_record.authoritative_targets
            state_name = candidate.state
            if mo_record.link_malformed or inat_record.link_malformed:
                state_name = "ambiguous_link"
            elif mo_direction and inat_direction:
                state_name = validation[0] if validation else "ambiguous_link"
            elif mo_direction or inat_direction:
                state_name = "one_way_link"
            competing = candidate.score >= 70 and (
                strong_by_mo.get(key[0], 0) > 1 or strong_by_inat.get(key[1], 0) > 1
            )
            if competing:
                state_name = (
                    "ambiguous_link"
                    if mo_direction or inat_direction
                    else "ambiguous_candidate"
                )
                issues.append(
                    _sync_issue(
                        "competing_strong_candidates",
                        "warning",
                        f"Competing strong pair: MO {key[0]} ↔ iNat {key[1]}",
                        "At least one record has multiple strong candidates; user review is required.",
                        public_fingerprint(
                            key,
                            strong_by_mo.get(key[0]),
                            strong_by_inat.get(key[1]),
                            mo_record.content_fingerprint,
                            inat_record.content_fingerprint,
                        ),
                        (("mo", key[0]), ("inat", key[1])),
                    )
                )
            pair = ObservationPair(
                key[0], key[1], state_name, candidate.score, evidence=candidate.evidence
            )
            pairs.append(pair)
            if state_name == "link_confirmed" and not competing:
                auto_confirm.append(key)
            if validation and validation[1]:
                issue_type = (
                    "link_metadata_conflict"
                    if validation[0] == "link_confirmed_with_metadata_conflicts"
                    else "link_validation_unavailable"
                )
                title_prefix = (
                    "Reciprocal link conflict"
                    if issue_type == "link_metadata_conflict"
                    else "Reciprocal link requires review"
                )
                issues.append(
                    _sync_issue(
                        issue_type,
                        "warning",
                        f"{title_prefix}: MO {key[0]} ↔ iNat {key[1]}",
                        " ".join(validation[1]),
                        public_fingerprint(
                            key,
                            (
                                validation[2].inventory.content_fingerprint
                                if validation[2]
                                else ""
                            ),
                            (
                                validation[3].inventory.content_fingerprint
                                if validation[3]
                                else ""
                            ),
                            *validation[1],
                        ),
                        (("mo", key[0]), ("inat", key[1])),
                    )
                )
            previous = snapshots.get(key)
            if (
                previous
                and previous.get("review_state") == "confirmed"
                and previous.get("confirmed_by") == "user"
                and (
                    int(previous.get("score") or 0) != pair.score
                    or str(previous.get("link_state") or "") != pair.state
                )
            ):
                issues.append(
                    _sync_issue(
                        "confirmed_pair_evidence_changed",
                        "warning",
                        f"Evidence changed: MO {key[0]} ↔ iNat {key[1]}",
                        "A user-confirmed inferred pair has changed evidence and should be reviewed.",
                        public_fingerprint(
                            key,
                            previous.get("score"),
                            pair.score,
                            previous.get("link_state"),
                            pair.state,
                            mo_record.content_fingerprint,
                            inat_record.content_fingerprint,
                        ),
                        (("mo", key[0]), ("inat", key[1])),
                    )
                )

        planned_keys = {
            (pair.mo_observation_id, pair.inat_observation_id) for pair in pairs
        }
        for key, previous in snapshots.items():
            if (
                key in planned_keys
                or previous.get("review_state") != "confirmed"
                or previous.get("confirmed_by") != "user"
            ):
                continue
            if ("mo", key[0]) not in invalidated_records and (
                "inat",
                key[1],
            ) not in invalidated_records:
                continue
            issues.append(
                _sync_issue(
                    "confirmed_pair_evidence_changed",
                    "warning",
                    f"Evidence changed: MO {key[0]} ↔ iNat {key[1]}",
                    "A source record changed and the user-confirmed inferred pair no longer appears in the active candidate plan.",
                    public_fingerprint(
                        key,
                        previous.get("score"),
                        previous.get("link_state"),
                        (
                            mo.get(key[0]).content_fingerprint
                            if mo.get(key[0])
                            else "unavailable"
                        ),
                        (
                            inat.get(key[1]).content_fingerprint
                            if inat.get(key[1])
                            else "unavailable"
                        ),
                    ),
                    (("mo", key[0]), ("inat", key[1])),
                )
            )

        issues.extend(self._derive_link_issues(mo, inat))
        issues.extend(self._derive_duplicate_issues(mo, inat, pairs))
        issues.extend(self._derive_configuration_issues(state))
        for site, observation_id, old, new in state.results["prepared"]["changed"]:
            issues.append(
                _sync_issue(
                    "record_changed",
                    "info",
                    f"Changed record: {site} {observation_id}",
                    "The remote public inventory fingerprint changed during this scan.",
                    public_fingerprint(site, observation_id, old, new),
                    ((site, observation_id),),
                )
            )
        for site, values in (
            ("inat", state.results["inat"].get("deleted", ())),
            ("mo", state.results["mo"].get("deleted", ())),
        ):
            for observation_id in values:
                issues.append(
                    _sync_issue(
                        "record_deleted",
                        "warning",
                        f"Deleted record: {site} {int(observation_id)}",
                        "The remote observation was confirmed deleted; affected pairs require review.",
                        public_fingerprint(site, int(observation_id), "deleted"),
                        ((site, int(observation_id)),),
                    )
                )
        progress(len(candidates), len(candidates))
        plan = ReconciliationPlan(
            tuple(pairs),
            tuple(issues),
            tuple((pair.mo_observation_id, pair.inat_observation_id) for pair in pairs),
            tuple(auto_confirm),
        )
        records_to_store = [
            *state.results["inat"]["records"],
            *state.results["mo"]["records"],
            *state.results["context_inat"],
            *state.results["context_mo"],
            *validation_input["context_placeholders"],
        ]
        return {
            "plan": plan,
            "records": records_to_store,
            "metadata_identifiers": [
                (site, observation_id, identifiers)
                for (site, observation_id), identifiers in metadata_by_record.items()
            ],
            "metadata_sequences": metadata_sequences,
            "invalidate_deep_records": sorted(invalidated_records),
        }

    def _derive_link_issues(
        self,
        mo: dict[int, InventoryObservation],
        inat: dict[int, InventoryObservation],
    ) -> list[SyncIssue]:
        issues: list[SyncIssue] = []
        for site, sources, targets in (("mo", mo, inat), ("inat", inat, mo)):
            target_site = "inat" if site == "mo" else "mo"
            for source in sources.values():
                unreadable = [
                    row
                    for row in source.authoritative_links
                    if row.parse_state == TARGET_UNKNOWN
                ]
                malformed = [
                    row
                    for row in source.authoritative_links
                    if row.parse_state not in {"valid", TARGET_UNKNOWN}
                ]
                if unreadable:
                    issues.append(
                        _sync_issue(
                            "unreadable_authoritative_link",
                            "warning",
                            f"MO authoritative link target is unreadable through API2: {source.key.observation_id}",
                            "The API row may be valid imported provenance, but its target identity is not exposed. "
                            "Automatic pairing and all repair/removal actions for that row are disabled.",
                            public_fingerprint(
                                source.content_fingerprint,
                                *(row.fingerprint for row in unreadable),
                            ),
                            ((site, source.key.observation_id),),
                        )
                    )
                if malformed:
                    issues.append(
                        _sync_issue(
                            "malformed_link",
                            "warning",
                            f"Malformed or ambiguous authoritative link: {site} {source.key.observation_id}",
                            "The source has malformed, duplicate, or conflicting authoritative link rows; automatic confirmation is disabled.",
                            public_fingerprint(
                                source.content_fingerprint,
                                *(
                                    row.fingerprint
                                    for row in source.authoritative_links
                                ),
                            ),
                            ((site, source.key.observation_id),),
                        )
                    )
                for target_id in source.authoritative_targets:
                    target = targets.get(target_id)
                    reciprocal = bool(
                        target
                        and source.key.observation_id in target.authoritative_targets
                    )
                    if reciprocal and (
                        source.scope_state != "in_scope"
                        or target.scope_state != "in_scope"
                        or (site == "inat" and source.fungi_status != "fungi")
                        or (target_site == "inat" and target.fungi_status != "fungi")
                        or (
                            site == "mo"
                            and source.fungi_status not in {"fungi", "unknown"}
                        )
                        or (
                            target_site == "mo"
                            and target.fungi_status not in {"fungi", "unknown"}
                        )
                    ):
                        if site == "mo":
                            issues.append(
                                _sync_issue(
                                    "linked_out_of_scope",
                                    "warning",
                                    f"Reciprocal link includes out-of-scope context: MO {source.key.observation_id} ↔ iNat {target_id}",
                                    "The linked record was retained for explanation but cannot be automatically paired in Gate 1A.",
                                    public_fingerprint(
                                        source.content_fingerprint,
                                        target.content_fingerprint,
                                        source.scope_state,
                                        target.scope_state,
                                        source.availability_state,
                                        target.availability_state,
                                        source.fungi_status,
                                        target.fungi_status,
                                    ),
                                    (
                                        (site, source.key.observation_id),
                                        (target_site, target_id),
                                    ),
                                )
                            )
                        continue
                    if reciprocal:
                        continue
                    detail = (
                        "The linked target was fetched as out-of-scope context."
                        if target and target.scope_state == "linked_context"
                        else (
                            "The opposite record does not have a reciprocal authoritative link."
                            if target
                            else "The linked target could not be fetched."
                        )
                    )
                    issues.append(
                        _sync_issue(
                            "one_way_link",
                            "warning",
                            f"One-way link: {site} {source.key.observation_id} → {target_site} {target_id}",
                            detail,
                            public_fingerprint(
                                source.content_fingerprint,
                                target.content_fingerprint if target else "unavailable",
                                source.availability_state,
                                target.availability_state if target else "unavailable",
                            ),
                            (
                                (site, source.key.observation_id),
                                (target_site, target_id),
                            ),
                        )
                    )
        return issues

    def _derive_duplicate_issues(
        self,
        mo: dict[int, InventoryObservation],
        inat: dict[int, InventoryObservation],
        candidates: list[ObservationPair],
    ) -> list[SyncIssue]:
        groups: dict[tuple[str, int], set[int]] = {}
        for pair in candidates:
            if pair.score >= 70:
                groups.setdefault(("mo", pair.inat_observation_id), set()).add(
                    pair.mo_observation_id
                )
                groups.setdefault(("inat", pair.mo_observation_id), set()).add(
                    pair.inat_observation_id
                )
        duplicate_keys: set[tuple[str, int, int]] = set()
        for (site, _), ids in groups.items():
            ordered = sorted(ids)
            for index, first_id in enumerate(ordered):
                for second_id in ordered[index + 1 :]:
                    duplicate_keys.add((site, first_id, second_id))
        for site, records in (("mo", mo), ("inat", inat)):
            media_groups: dict[str, set[int]] = {}
            for record in records.values():
                for media in record.media:
                    media_groups.setdefault(media.photo_id, set()).add(
                        record.key.observation_id
                    )
            for ids in media_groups.values():
                ordered = sorted(ids)
                for index, first_id in enumerate(ordered):
                    for second_id in ordered[index + 1 :]:
                        if is_same_site_duplicate(
                            records[first_id], records[second_id]
                        ):
                            duplicate_keys.add((site, first_id, second_id))
        result: list[SyncIssue] = []
        for site, first_id, second_id in sorted(duplicate_keys):
            records = mo if site == "mo" else inat
            result.append(
                _sync_issue(
                    "same_site_duplicate",
                    "warning",
                    f"Possible {site} duplicates: {first_id} and {second_id}",
                    "The records are constrained strong competitors or share an exact native photo, date, and compatible taxon.",
                    public_fingerprint(
                        site,
                        first_id,
                        second_id,
                        records[first_id].content_fingerprint,
                        records[second_id].content_fingerprint,
                    ),
                    ((site, first_id), (site, second_id)),
                )
            )
        return result

    def _derive_configuration_issues(self, state: _ScanState) -> list[SyncIssue]:
        issues: list[SyncIssue] = []
        inat_result = state.results["inat"]
        mo_error = state.results["mo"].get("link_config_error")
        if mo_error:
            issues.append(
                _sync_issue(
                    "mo_external_site_configuration",
                    "error",
                    "Mushroom Observer iNaturalist external-site binding is unavailable",
                    str(mo_error),
                    public_fingerprint("mo_external_site", mo_error),
                    (),
                )
            )
        if (
            inat_result.get("mo_binding_ambiguous")
            or inat_result.get("mo_binding_invalid")
            or inat_result.get("mo_binding_missing")
        ):
            reason = (
                "ambiguous"
                if inat_result.get("mo_binding_ambiguous")
                else (
                    "missing"
                    if inat_result.get("mo_binding_missing")
                    else "changed or invalid"
                )
            )
            issues.append(
                _sync_issue(
                    "observation_field_link",
                    "warning",
                    "iNaturalist Mushroom Observer URL field binding requires review",
                    f"The exact text-field definition is {reason}; reciprocal auto-confirmation is disabled until an override is selected.",
                    public_fingerprint(
                        reason,
                        *(
                            item.get("id")
                            for item in inat_result.get("mo_definitions", [])
                        ),
                    ),
                    (),
                )
            )
        if inat_result.get("deleted_enabled") and not inat_result.get(
            "deleted_complete", True
        ):
            # Same held-cursor consequence either way, but the remedy differs:
            # a truncated feed resolves itself on the next scan, a rejected
            # token needs the user to sign in again. Say which one happened.
            if inat_result.get("deleted_unauthorized"):
                title = "iNaturalist deleted-observation feed was not authorized"
                detail = (
                    "iNaturalist rejected the stored API token, so deleted records were not "
                    "detected in this scan. Everything else in the scan is public and completed "
                    "normally. Sign in to iNaturalist again and rescan; the deleted cursor was "
                    "not advanced, so the same window is reread and nothing is lost."
                )
            else:
                title = "iNaturalist deleted-observation feed was truncated"
                detail = (
                    "The feed returned fewer ids than it reported and exposes no pagination, so "
                    "some deletions may be unseen. The deleted cursor was not advanced; the next "
                    "scan rereads the same window."
                )
            issues.append(
                _sync_issue(
                    "deleted_feed_incomplete",
                    "warning",
                    title,
                    detail,
                    public_fingerprint(
                        "inat_deleted_incomplete",
                        (
                            "unauthorized"
                            if inat_result.get("deleted_unauthorized")
                            else "truncated"
                        ),
                        len(inat_result.get("deleted", ())),
                    ),
                    (),
                )
            )
        return issues

    def _persist_scan_plan(self, state: _ScanState) -> str:
        inat_result = state.results["inat"]
        mo_result = state.results["mo"]
        computed = state.results["plan"]
        bindings: list[tuple[str, int, str, str, bool]] = []
        if inat_result.get("mo_binding"):
            binding = inat_result["mo_binding"]
            bindings.append(
                (
                    "mo_url",
                    binding["id"],
                    MO_FIELD_NAME,
                    "text",
                    bool(binding.get("override")),
                )
            )
        if inat_result.get("its_binding"):
            binding = inat_result["its_binding"]
            bindings.append(
                (
                    "its",
                    binding["id"],
                    ITS_FIELD_NAME,
                    "dna",
                    bool(binding.get("override")),
                )
            )
        if inat_result.get("its_accession_binding"):
            binding = inat_result["its_accession_binding"]
            bindings.append(
                (
                    "its_accession",
                    binding["id"],
                    ACCESSION_FIELD_NAME,
                    "text",
                    bool(binding.get("override")),
                )
            )
        invalid_bindings: list[str] = []
        if inat_result.get("mo_binding_invalid"):
            invalid_bindings.append("mo_url")
        if inat_result.get("its_binding_invalid"):
            invalid_bindings.append("its")
        if inat_result.get("its_accession_binding_invalid"):
            invalid_bindings.append("its_accession")
        disabled_link_sources: list[str] = []
        if not inat_result.get("mo_binding") or any(
            inat_result.get(key)
            for key in (
                "mo_binding_invalid",
                "mo_binding_missing",
                "mo_binding_ambiguous",
            )
        ):
            disabled_link_sources.append("inat")
        if mo_result.get("link_config_error") or not mo_result.get("inat_site_id"):
            disabled_link_sources.append("mo")
        streams = ["inat_inventory", "mo_inventory"]
        if mo_result.get("capabilities", {}).get("full_inventory"):
            streams.append("mo_full_inventory")
        # Only advance the deleted-feed cursor when the feed was read in full;
        # otherwise the next scan must re-read the same window (see _scan_inat).
        if inat_result.get("deleted_enabled") and inat_result.get(
            "deleted_complete", True
        ):
            streams.append("inat_deleted")
        self.db.apply_reconciliation_scan(
            state.profile.profile_id,
            state.run_id,
            state.scan_started_at,
            computed["records"],
            {
                "inat": inat_result.get("deleted", ()),
                "mo": mo_result.get("deleted", ()),
            },
            computed["plan"],
            streams,
            json.dumps(mo_result["capabilities"], sort_keys=True),
            resolved_issue_types={
                "malformed_link",
                "observation_field_link",
                "link_metadata_conflict",
                "unreadable_authoritative_link",
                "link_state_refresh_required",
                "link_validation_unavailable",
                "link_one_to_one_conflict",
                "one_way_link",
                "linked_out_of_scope",
                "same_site_duplicate",
                "competing_strong_candidates",
                "mo_external_site_configuration",
                "record_changed",
                "record_deleted",
                "confirmed_pair_evidence_changed",
                "deleted_feed_incomplete",
            },
            field_bindings=bindings,
            invalid_bindings=invalid_bindings,
            metadata_identifiers=computed["metadata_identifiers"],
            metadata_sequences=computed["metadata_sequences"],
            invalidate_deep_records=computed["invalidate_deep_records"],
            disabled_link_sources=disabled_link_sources,
        )
        count = len(inat_result["records"]) + len(mo_result["records"])
        return f"Reconciled {count:,} inventory records. No remote data was changed."

    def _scan_error(self, part: str, error: str, generation: int) -> None:
        state = self._scan
        if generation != self._generation or state is None:
            return
        self._generation += 1
        self.db.finish_run(
            state.run_id, "cancelled" if error == "cancelled" else "failed", error=error
        )
        self._scan = None
        self.scan_failed.emit(
            "Scan cancelled; no cursors were advanced."
            if error == "cancelled"
            else f"Reconciliation stage {part} failed: {error}. No cursors were advanced."
        )

    def _scan_mo(
        self,
        profile: ReconciliationProfile,
        full: bool,
        generation: int,
        progress: Callable,
    ) -> dict:
        cancelled = lambda: generation != self._generation
        capabilities = self.mo_client.discover_observation_capabilities(cancelled)
        full = full or not capabilities["updated_at"]
        capabilities["full_inventory"] = full
        previous = self.db.cursor(profile.profile_id, "mo_inventory")
        updated_at = (
            _mo_time_range(_overlap(previous, minutes=10))
            if previous and not full and capabilities["updated_at"]
            else ""
        )
        records: list[InventoryObservation] = []
        page = 1
        while True:
            payload = self.mo_client.observations_page(
                profile.mo_user_id, page, cancelled, updated_at=updated_at
            )
            items = results_from_payload(payload)
            for item in items:
                records.append(parse_mo_observation(item, profile.mo_user_id))
            progress(len(records), _total(payload), "observations")
            # MO chooses the page size; we cannot request one ('limit' is a
            # fatal MO error), so a short page is the only end-of-stream signal.
            if not items or len(items) < MO_OBSERVATIONS_PAGE_SIZE:
                break
            page += 1
        unknown_name_ids = sorted(
            {
                item.taxon_id
                for item in records
                if item.fungi_status == "unknown" and item.taxon_id is not None
            }
        )
        if unknown_name_ids:
            name_rows = results_from_payload(
                self.mo_client.names(
                    unknown_name_ids,
                    cancelled,
                    lambda current, total: progress(current, total, "names"),
                )
            )
            name_scope = {
                _id_from(item): _name_fungi_status(item) for item in name_rows
            }
            records = [
                _with_fungi_status(
                    item, name_scope.get(item.taxon_id, item.fungi_status)
                )
                for item in records
            ]
        records = [
            _with_scope_state(item, _mo_scope_state(item, profile.mo_user_id))
            for item in records
        ]
        records = [_with_sequence_hashes(item, set()) for item in records]
        deleted: list[int] = []
        if full:
            previous_ids = {
                item.key.observation_id
                for item in self.db.inventory_records(
                    profile.profile_id,
                    "mo",
                    scope_states=("in_scope", "linked_context", "out_of_scope"),
                )
            }
            current_ids = {item.key.observation_id for item in records}
            recheck = sorted(previous_ids - current_ids)
            for index, observation_id in enumerate(recheck, 1):
                progress(index, len(recheck), "recover")
                try:
                    payload = self.mo_client.observation(
                        observation_id, cancelled, detail="low"
                    )
                    found = _first_result(payload)
                    if found:
                        recovered = parse_mo_observation(found, profile.mo_user_id)
                        records.append(
                            _with_scope_state(
                                recovered,
                                _mo_scope_state(recovered, profile.mo_user_id),
                            )
                        )
                except MOAPIError as exc:
                    if exc.status_code in {404, 410}:
                        deleted.append(observation_id)
                    else:
                        raise
            recovered_name_ids = sorted(
                {
                    item.taxon_id
                    for item in records
                    if item.fungi_status == "unknown" and item.taxon_id is not None
                }
            )
            if recovered_name_ids:
                recovered_names = results_from_payload(
                    self.mo_client.names(
                        recovered_name_ids,
                        cancelled,
                        lambda current, total: progress(current, total, "names"),
                    )
                )
                recovered_scope = {
                    _id_from(item): _name_fungi_status(item) for item in recovered_names
                }
                records = [
                    _with_scope_state(
                        _with_fungi_status(
                            item, recovered_scope.get(item.taxon_id, item.fungi_status)
                        ),
                        _mo_scope_state(
                            _with_fungi_status(
                                item,
                                recovered_scope.get(item.taxon_id, item.fungi_status),
                            ),
                            profile.mo_user_id,
                        ),
                    )
                    for item in records
                ]
        # Embedded links are ignored; authoritative site and link rows are
        # fetched directly. Site IDs are deployment data, never hard-coded.
        progress(0, 0, "external_sites")
        site_rows = results_from_payload(self.mo_client.external_sites(cancelled))
        inat_site_ids = {
            _id_from(row) for row in site_rows if _is_inat_external_site(row)
        } - {None}
        ids = [item.key.observation_id for item in records]
        link_config_error = ""
        links: list[dict[str, Any]] = []
        if len(inat_site_ids) == 1 and ids:
            links = results_from_payload(
                self.mo_client.external_links(
                    ids,
                    cancelled,
                    lambda current, total: progress(current, total, "external_links"),
                )
            )
        elif len(inat_site_ids) != 1:
            link_config_error = (
                "No unique iNaturalist external-site definition was discovered on Mushroom Observer. "
                "MO authoritative links were disabled for this scan."
            )
        by_obs: dict[int, list[AuthoritativeLinkRow]] = {}
        for link in links:
            parsed = parse_mo_external_link(link, int(next(iter(inat_site_ids))))
            if parsed is None:
                continue
            source_id, parsed_link = parsed
            by_obs.setdefault(source_id, []).append(parsed_link)
        records = [
            _with_links(item, by_obs.get(item.key.observation_id, []))
            for item in records
        ]
        return {
            "records": records,
            "capabilities": capabilities,
            "deleted": deleted,
            "link_config_error": link_config_error,
            "inat_site_id": (
                next(iter(inat_site_ids)) if len(inat_site_ids) == 1 else None
            ),
        }

    def _scan_inat(
        self,
        profile: ReconciliationProfile,
        force_full: bool,
        generation: int,
        progress: Callable,
    ) -> dict:
        if generation != self._generation:
            raise ReconciliationCancelled()
        reader = INatReconciliationReader(self.inat_client)
        mo_defs = reader.resolve_field_definitions(MO_FIELD_NAME)
        if generation != self._generation:
            raise ReconciliationCancelled()
        its_defs = reader.resolve_field_definitions(ITS_FIELD_NAME, ("dna",))
        accession_defs = reader.resolve_field_definitions(ACCESSION_FIELD_NAME)
        stored_mo_binding = self.db.field_binding(profile.profile_id, "mo_url")
        mo_binding = _choose_binding(stored_mo_binding, mo_defs)
        stored_its_binding = self.db.field_binding(profile.profile_id, "its")
        its_binding = _choose_binding(stored_its_binding, its_defs)
        stored_accession_binding = self.db.field_binding(
            profile.profile_id, "its_accession"
        )
        accession_binding = _choose_binding(stored_accession_binding, accession_defs)
        previous = self.db.cursor(profile.profile_id, "inat_inventory")
        full = force_full or not previous
        records: dict[int, InventoryObservation] = {}
        page = 1
        id_above: Optional[int] = 0 if full else None
        updated_since = "" if full else _overlap(previous, minutes=10)
        while True:
            if generation != self._generation:
                raise ReconciliationCancelled()
            payload = reader.inventory_page(
                profile.inat_user_id,
                page,
                id_above=id_above,
                updated_since=updated_since,
            )
            items = [
                item for item in payload.get("results") or [] if isinstance(item, dict)
            ]
            for item in items:
                parsed = reader.parse_inventory(
                    item,
                    profile.inat_user_id,
                    mo_binding["id"] if mo_binding else None,
                    None,
                )
                prior = records.get(parsed.key.observation_id)
                if prior is None or (
                    parsed.updated_at.isoformat() if parsed.updated_at else ""
                ) >= (prior.updated_at.isoformat() if prior.updated_at else ""):
                    records[parsed.key.observation_id] = parsed
            progress(
                len(records),
                int(payload.get("total_results") or len(records)),
                "observations",
            )
            if not items or len(items) < 200:
                break
            if full:
                id_above = max(int(item["id"]) for item in items)
            else:
                page += 1
        auth = self.auth_provider()
        authenticated_match = (
            auth.is_authenticated
            and auth.login.casefold() == profile.inat_login.casefold()
        )
        deleted: set[int] = set()
        # /observations/deleted takes only `since` and `fields` — it exposes no
        # page parameter, so a short page cannot be followed. Its documented
        # response is nonetheless paginated (`total_results`, `page`, `per_page`
        # are all required in ResultsObservationsDeleted), so a truncated read
        # is otherwise indistinguishable from "nothing was deleted". Because the
        # `inat_deleted` cursor advances past the window, every unseen id would
        # be lost permanently: the record stays available, no record_deleted
        # issue is raised, and confirmed pairs are never flagged. Prove the read
        # was whole, and refuse to advance the cursor when it was not.
        deleted_complete = True
        deleted_unauthorized = False
        if authenticated_match:
            deleted_cursor = self.db.cursor(profile.profile_id, "inat_deleted")
            since = _overlap(
                deleted_cursor or previous or datetime.now(timezone.utc).isoformat(),
                days=1,
            )
            if generation != self._generation:
                raise ReconciliationCancelled()
            progress(0, 0, "deleted")
            try:
                payload = reader.deleted_observations(auth.api_token, since)
            except INatAPIError as exc:
                # The deleted feed is the ONLY authenticated read in the scan,
                # and it is an enrichment: everything else here is public and
                # already succeeded by this point. An expired token (iNaturalist
                # issues 24-hour tokens and nothing in this app refreshes them)
                # used to abort the whole run after minutes of inventory paging,
                # discarding all of it and advancing no cursors.
                #
                # Degrade to exactly the truncated-feed contract instead: no
                # deletions recorded, `inat_deleted` cursor HELD so the window
                # is reread once authentication is repaired, and a warning
                # issue raised so the gap is visible rather than silent. Only
                # authentication statuses are absorbed — any other failure is a
                # real fault and must still fail the scan.
                if exc.status_code not in (401, 403):
                    raise
                deleted_unauthorized = True
                deleted_complete = False
            else:
                rows = payload.get("results")
                rows = rows if isinstance(rows, list) else []
                for item in rows:
                    value = _id_from(item)
                    if value:
                        deleted.add(value)
                reported_total = _total(payload)
                deleted_complete = not reported_total or len(rows) >= reported_total
        return {
            "records": list(records.values()),
            "deleted": deleted,
            "deleted_enabled": authenticated_match,
            "deleted_complete": deleted_complete,
            "deleted_unauthorized": deleted_unauthorized,
            "full_inventory": full,
            "mo_binding": mo_binding,
            "its_binding": its_binding,
            "its_accession_binding": accession_binding,
            "mo_definitions": mo_defs,
            "mo_binding_invalid": stored_mo_binding is not None and mo_binding is None,
            "mo_binding_missing": stored_mo_binding is None and not mo_defs,
            "mo_binding_ambiguous": stored_mo_binding is None
            and len(mo_defs) > 1
            and not mo_binding,
            "its_binding_invalid": stored_its_binding is not None
            and its_binding is None,
            "its_accession_binding_invalid": (
                stored_accession_binding is not None and accession_binding is None
            ),
        }

    def _hydrate_inat_pairs(
        self,
        records: dict[int, InventoryObservation],
        pairs: list[tuple[int, int]],
        token: str,
        generation: int,
        progress: Callable,
    ) -> dict[int, HydratedObservation]:
        """Hydrate every reciprocally linked iNaturalist record, BATCHED.

        One request per pair is what this used to do, and behind the 1 req/sec
        limiter that made the phase take one second per link — hours on an
        account with five figures of them. ``get_reconciliation_validation``
        asks the identical question for up to 200 ids at once.

        Results are keyed off each RETURNED id, never off the requested batch:
        iNaturalist silently omits ids it will not serve (deleted or newly
        hidden), so position-based pairing would attribute one observation's
        coordinates to another.
        """
        result: dict[int, HydratedObservation] = {}
        ids = sorted({inat_id for _, inat_id in pairs})
        for start in range(0, len(ids), 200):
            if generation != self._generation:
                raise ReconciliationCancelled()
            batch = ids[start : start + 200]
            payload = self.inat_client.get_reconciliation_validation(batch, token)
            for item in payload.get("results") or []:
                if not isinstance(item, dict):
                    continue
                observation_id = _id_from(item.get("id"))
                inventory = records.get(observation_id) if observation_id else None
                if observation_id is not None and inventory is not None:
                    result[observation_id] = _hydrate_record(
                        inventory, item, bool(token)
                    )
            progress(min(start + len(batch), len(ids)), len(ids))
        return result

    def _hydrate_mo_pairs(
        self,
        records: dict[int, InventoryObservation],
        pairs: list[tuple[int, int]],
        generation: int,
        progress: Callable,
    ) -> dict[int, HydratedObservation]:
        """Hydrate every reciprocally linked MO record, BATCHED — see
        ``_hydrate_inat_pairs`` for why, and for the returned-id keying rule
        that applies here identically (MO omits ids it will not serve)."""
        result: dict[int, HydratedObservation] = {}
        ids = sorted({mo_id for mo_id, _ in pairs})
        cancelled: Callable[[], bool] = lambda: generation != self._generation
        for start in range(0, len(ids), 100):
            if cancelled():
                raise ReconciliationCancelled()
            batch = ids[start : start + 100]
            payload = self.mo_client.observations(batch, cancelled, detail="high")
            for item in results_from_payload(payload):
                observation_id = _id_from(item)
                inventory = records.get(observation_id) if observation_id else None
                if observation_id is not None and inventory is not None:
                    result[observation_id] = _hydrate_record(inventory, item, True)
            progress(min(start + len(batch), len(ids)), len(ids))
        return result


def _choose_binding(
    stored: object, definitions: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    stored_id = int(stored["field_id"]) if stored is not None else None  # type: ignore[index]
    if stored_id:
        for item in definitions:
            if _id_from(item) == stored_id:
                return {"id": stored_id, "override": bool(stored["is_override"])}  # type: ignore[index]
        return None
    if len(definitions) == 1:
        return {"id": _id_from(definitions[0]), "override": False}
    return None


def _with_links(
    item: InventoryObservation,
    links: list[AuthoritativeLinkRow],
) -> InventoryObservation:
    values = item.__dict__.copy()
    targets = [
        link.target_observation_id for link in links if link.target_observation_id
    ]
    distinct = set(targets)
    malformed = any(link.parse_state != "valid" for link in links)
    if len(links) > 1:
        state = (
            "ambiguous"
            if any(link.parse_state == TARGET_UNKNOWN for link in links)
            else "conflicting" if len(distinct) > 1 else "duplicate"
        )
        links = [
            AuthoritativeLinkRow(
                link.row_id,
                link.external_site_id,
                link.target_site,
                link.target_observation_id,
                state if link.parse_state == "valid" else link.parse_state,
                public_fingerprint(
                    link.row_id,
                    link.external_site_id,
                    link.target_observation_id,
                    state if link.parse_state == "valid" else link.parse_state,
                ),
            )
            for link in links
        ]
        malformed = True
    values["authoritative_targets"] = tuple(sorted(set(targets)))
    values["link_malformed"] = malformed
    values["authoritative_links"] = tuple(links)
    values["content_fingerprint"] = public_fingerprint(
        item.content_fingerprint,
        values["authoritative_targets"],
        values["link_malformed"],
        *(link.fingerprint for link in links),
    )
    return InventoryObservation(**values)


def _with_fungi_status(item: InventoryObservation, status: str) -> InventoryObservation:
    values = item.__dict__.copy()
    values["fungi_status"] = (
        status if status in {"fungi", "nonfungal", "unknown"} else "unknown"
    )
    values["content_fingerprint"] = public_fingerprint(
        item.content_fingerprint, values["fungi_status"]
    )
    return InventoryObservation(**values)


def _mo_scope_state(item: InventoryObservation, account_id: int) -> str:
    if item.owner_id != account_id:
        return "linked_context"
    return "in_scope" if item.fungi_status in {"fungi", "unknown"} else "out_of_scope"


def _with_scope_state(item: InventoryObservation, state: str) -> InventoryObservation:
    values = item.__dict__.copy()
    values["scope_state"] = state
    values["availability_state"] = "available"
    return InventoryObservation(**values)


def _with_unavailable_scope(
    item: InventoryObservation, state: str
) -> InventoryObservation:
    values = item.__dict__.copy()
    values["scope_state"] = state
    values["availability_state"] = "unavailable"
    return InventoryObservation(**values)


def _without_authoritative_links(item: InventoryObservation) -> InventoryObservation:
    values = item.__dict__.copy()
    values["authoritative_targets"] = ()
    values["authoritative_links"] = ()
    values["link_malformed"] = False
    return InventoryObservation(**values)


def _is_inat_external_site(raw: dict[str, Any]) -> bool:
    name = " ".join(str(raw.get("name") or "").casefold().split())
    if name in {"inaturalist", "inaturalist.org", "i naturalist"}:
        return True
    value = str(raw.get("url") or raw.get("base_url") or "").strip()
    try:
        return urlsplit(value).hostname in {"inaturalist.org", "www.inaturalist.org"}
    except ValueError:
        return False


def _with_sequence_hashes(
    item: InventoryObservation, hashes: set[str]
) -> InventoryObservation:
    values = item.__dict__.copy()
    values["sequence_hashes"] = tuple(sorted(hashes))
    values["inventory_sequence_hashes"] = tuple(sorted(hashes))
    return InventoryObservation(**values)


def _name_fungi_status(raw: dict[str, Any]) -> str:
    """Classify only from explicit returned taxonomy, never from name heuristics."""
    classification = (
        raw.get("classification") or raw.get("taxonomy") or raw.get("parents")
    )
    if isinstance(classification, dict):
        values = [str(key) for key in classification] + [
            str(value) for value in classification.values()
        ]
    elif isinstance(classification, list):
        values = [
            str(item.get("name") if isinstance(item, dict) else item)
            for item in classification
        ]
    else:
        values = [str(classification or "")]
    normalized = {" ".join(value.casefold().split()) for value in values}
    if "fungi" in normalized:
        return "fungi"
    if any(normalized):
        return "nonfungal"
    return "unknown"


def _first_result(payload: object) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if isinstance(results, list):
        return results[0] if results and isinstance(results[0], dict) else None
    for key in ("observation", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload if payload.get("id") else None


def _hydrate_record(
    inventory: InventoryObservation,
    raw: dict[str, Any],
    authorized: bool,
    *,
    include_its: bool = False,
) -> HydratedObservation:
    vouchers: set[str] = set()
    collections: set[str] = set()
    accessions: set[str] = set()
    # Only normalized, first-class public identifiers leave this function.
    identifier_fields = [
        ("voucher", vouchers),
        ("voucher_number", vouchers),
        ("collection_number", collections),
        ("field_slip", collections),
    ]
    if include_its:
        identifier_fields.extend(
            (
                ("accession", accessions),
                ("genbank_accession", accessions),
            )
        )
    for key, destination in identifier_fields:
        value = raw.get(key)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            normalized = " ".join(str(candidate or "").strip().casefold().split())
            if normalized:
                destination.add(normalized)
    for row in raw.get("ofvs") or raw.get("observation_field_values") or []:
        if not isinstance(row, dict):
            continue
        field = (
            row.get("observation_field")
            if isinstance(row.get("observation_field"), dict)
            else {}
        )
        field_name = " ".join(str(field.get("name") or "").casefold().split())
        destination = None
        if field_name in {"voucher", "voucher number", "specimen voucher"}:
            destination = vouchers
        elif field_name in {"collection number", "field number", "field slip"}:
            destination = collections
        elif include_its and field_name in {
            "genbank accession",
            "accession",
            "bold process id",
        }:
            destination = accessions
        if destination is not None:
            normalized = " ".join(
                str(row.get("value") or "").strip().casefold().split()
            )
            if normalized:
                destination.add(normalized)
    latitude = longitude = accuracy = None
    coordinates_available = False
    coordinate_source = ""
    coordinates = raw.get("private_geojson") if authorized else None
    if not coordinates:
        coordinates = raw.get("geojson") or raw.get("location")
    if isinstance(coordinates, dict):
        point = coordinates.get("coordinates")
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                longitude, latitude = float(point[0]), float(point[1])
                coordinates_available = True
                coordinate_source = (
                    "explicit_authorized_private"
                    if authorized and raw.get("private_geojson")
                    else "explicit_public"
                )
            except (TypeError, ValueError):
                pass
    elif isinstance(coordinates, str) and "," in coordinates:
        try:
            latitude, longitude = (
                float(value.strip()) for value in coordinates.split(",", 1)
            )
            coordinates_available = True
            coordinate_source = "explicit_public"
        except ValueError:
            pass
    try:
        accuracy = (
            float(raw.get("positional_accuracy"))
            if raw.get("positional_accuracy") is not None
            else None
        )
    except (TypeError, ValueError):
        accuracy = None
    return HydratedObservation(
        inventory,
        tuple(sorted(vouchers)),
        tuple(sorted(collections)),
        tuple(sorted(accessions)),
        sequence_hashes=(
            tuple(sorted(_sequence_hashes_from_detail(raw))) if include_its else ()
        ),
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy,
        coordinates_available=coordinates_available,
        coordinate_privacy_state=(
            str(raw.get("geoprivacy") or raw.get("taxon_geoprivacy") or "public")
        ),
        coordinate_source=coordinate_source,
        # Without matching iNaturalist authentication, hidden-vs-absent private
        # coordinates cannot be distinguished and automatic confirmation stops.
        required_values_available=authorized,
        # Gate 2A only: surfaced for a creation preview, never used by matching
        # or specimen-identity conflict logic.
        description=str(raw.get("description") or ""),
        attribution_name=str((raw.get("user") or {}).get("login") or ""),
    )


def _sequence_hashes_from_detail(raw: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif key in {"sequence", "bases", "dna_sequence"}:
            digest = sequence_digest(value)
            if digest:
                hashes.add(digest)

    visit(raw)
    return hashes


def _candidate_with_preserved_deep(
    db: ReconciliationDB,
    profile_id: int,
    candidate: ObservationPair,
) -> ObservationPair:
    current = db.pair_by_records(
        profile_id, candidate.mo_observation_id, candidate.inat_observation_id
    )
    if not current:
        return candidate
    deep = [
        MatchEvidence(
            str(item["evidence_type"]),
            EvidenceFamily(str(item["family"])),
            int(item["score"]),
            str(item["explanation"]),
            EvidenceTier(int(item["tier"])),
        )
        for item in current.get("evidence", [])
        if int(item["tier"]) >= int(EvidenceTier.DEEP)
    ]
    if not deep:
        return candidate
    score = score_evidence((*candidate.evidence, *deep))
    state = (
        str(current["link_state"])
        if str(current["link_state"]).startswith("link_confirmed")
        else candidate.state
    )
    return ObservationPair(
        candidate.mo_observation_id,
        candidate.inat_observation_id,
        state,
        score.total,
        evidence=score.evidence,
    )


def _candidate_with_snapshot_deep(
    snapshots: dict[tuple[int, int], dict[str, Any]],
    candidate: ObservationPair,
) -> ObservationPair:
    current = snapshots.get(
        (candidate.mo_observation_id, candidate.inat_observation_id)
    )
    if not current:
        return candidate
    deep = [
        MatchEvidence(
            str(item["evidence_type"]),
            EvidenceFamily(str(item["family"])),
            int(item["score"]),
            str(item["explanation"]),
            EvidenceTier(int(item["tier"])),
        )
        for item in current.get("evidence", [])
        if int(item["tier"]) >= int(EvidenceTier.DEEP)
    ]
    if not deep:
        return candidate
    score = score_evidence((*candidate.evidence, *deep))
    return ObservationPair(
        candidate.mo_observation_id,
        candidate.inat_observation_id,
        candidate.state,
        score.total,
        evidence=score.evidence,
    )


def _with_hydrated_metadata(
    record: InventoryObservation,
    detail: HydratedObservation,
) -> InventoryObservation:
    values = record.__dict__.copy()
    identifiers = set(record.identifiers)
    identifiers.update(("voucher", value) for value in detail.voucher_identifiers)
    identifiers.update(("collection", value) for value in detail.collection_identifiers)
    identifiers.update(("accession", value) for value in detail.accessions)
    values["identifiers"] = tuple(sorted(identifiers))
    values["sequence_hashes"] = tuple(
        sorted(set(record.sequence_hashes) | set(detail.sequence_hashes))
    )
    return InventoryObservation(**values)


def _with_deleted(record: InventoryObservation) -> InventoryObservation:
    values = record.__dict__.copy()
    values["deleted"] = True
    values["scope_state"] = "out_of_scope"
    values["availability_state"] = "deleted"
    return InventoryObservation(**values)


def _sync_issue(
    issue_type: str,
    severity: str,
    title: str,
    detail: str,
    fingerprint: str,
    records: tuple[tuple[str, int], ...],
) -> SyncIssue:
    return SyncIssue(
        issue_type,
        severity,
        title,
        detail,
        fingerprint,
        tuple(
            RemoteRecordKey(RemoteSite(site), observation_id)
            for site, observation_id in records
        ),
    )


def _needs_full_scan(cursor: str) -> bool:
    if not cursor:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(cursor) >= timedelta(
            days=30
        )
    except ValueError:
        return True


def _overlap(value: str, *, minutes: int = 0, days: int = 0) -> str:
    """Back-date one ISO cursor. iNaturalist consumes this shape directly."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed - timedelta(minutes=minutes, days=days)).isoformat()


def _mo_time_range(start_iso: str) -> str:
    """Render an MO API2 ``updated_at`` range covering ``start_iso`` to now.

    Mushroom Observer does NOT accept ISO 8601 here. Its time parser reports:
    ``expect "YYYYMMDDHHMMSS-YYYYMMDDHHMMSS", "YYYYMMDDHHMM-YYYYMMDDHHMM", …``
    and rejects anything else as a FATAL ``API2::BadParameterValue`` at HTTP
    200, so an ISO cursor failed every incremental scan.

    A SINGLE value is a point in time, not a lower bound, and MO has no
    open-ended range — so an explicit ``start-end`` range is required. The end
    is pinned a day ahead of now to absorb clock skew between this machine and
    MO, since a record updated "in the future" relative to us must still be
    seen rather than silently skipped until the next scan.
    """
    parsed = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    start = parsed.astimezone(timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    return f"{start:%Y%m%d%H%M%S}-{end:%Y%m%d%H%M%S}"


def _id_from(value: object) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("id") or value.get("observation_id")
    try:
        result = int(value)  # type: ignore[arg-type]
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _date(value: object) -> Optional[date]:
    if isinstance(value, dict):
        value = value.get("date") or value.get("start") or value.get("observed_on")
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _datetime(value: object) -> Optional[datetime]:
    try:
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
        )
    except ValueError:
        return None


def _total(payload: dict[str, Any]) -> int:
    # Mushroom Observer reports 'number_of_records'; iNaturalist reports
    # 'total_results'. A key that is ABSENT must be skipped, not returned as 0:
    # `int(payload.get(key) or 0)` never raises, so an unconditional return in
    # the first iteration made every later key unreachable and reported a total
    # of 0 for the whole MO inventory phase.
    for key in ("total_results", "number_of_records", "total", "number_of_results"):
        if payload.get(key) is None:
            continue
        try:
            return int(payload[key])
        except (TypeError, ValueError):
            continue
    return 0


def _safe_error(exc: Exception) -> str:
    # Never persist request parameters, response bodies, or values echoed by a
    # remote service. Endpoint attributes are fixed paths supplied by clients.
    endpoint_value = str(getattr(exc, "endpoint", "")).split("?", 1)[0]
    endpoint = urlsplit(endpoint_value).path or endpoint_value
    endpoint = re.sub(
        r"(?<=/observation_field_values/)[^/]+", "{field_value_id}", endpoint
    )
    endpoint = re.sub(r"(?<=/observations/)[^/]+", "{observation_id}", endpoint)
    status = getattr(exc, "status_code", None)
    if endpoint:
        return f"{type(exc).__name__}: endpoint={endpoint[:120]} status={status or 'unknown'}"
    message = re.sub(r"https?://\S+", "[remote-url]", str(exc).replace("\n", " "))
    message = re.sub(r"\?[A-Za-z0-9_.%=&,+:/-]+", "?[redacted]", message)
    return f"{type(exc).__name__}: {message[:300]}"
