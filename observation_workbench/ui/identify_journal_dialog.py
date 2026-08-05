"""Shared journal-safety plumbing for focused Identify write dialogs.

``IdentifyCommentDialog``, ``IdentifyFavoriteDialog``, and
``IdentifyCaptiveDialog`` each capture one piece of user intent, resolve the
observation's UUID through the application-scoped action manager, and journal
the corresponding local action only if authentication and the captured
selection are still valid when resolution completes. That safety machinery --
authentication tracking, generation bookkeeping so a stray late async result
cannot be mistaken for the current attempt, and the ready-to-journal check --
is identical across all three; only what is captured (a comment body, a
Favorite desired state, or a Captive/Cultivated vote) and how it is queued
differ. This base class owns the shared machinery; subclasses implement the
hooks below to supply the action-specific pieces.
"""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QWidget

from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)
from observation_workbench.storage.cache_db import IdentifyEnqueueResult


def _valid_uuid(value: object) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (AttributeError, TypeError, ValueError):
        return ""


class IdentifyJournalDialog(QDialog):
    """Base for focused Identify dialogs that journal one captured action.

    Subclasses build their own widgets in ``_build()`` and must call
    :meth:`_connect_journal_signals` once those widgets (in particular
    ``_auth_status``, ``_status``, ``_view_pending_button``, and
    ``_submit_button``) exist. They must implement:

    - ``_selected_value()`` -- read the value currently selected/entered in
      the dialog's own widgets (may be ``None``/empty if nothing usable is
      selected yet).
    - ``_value_is_selected(value)`` -- whether ``value`` is usable to submit.
    - ``_missing_selection_message`` -- status text shown when submission is
      attempted (or found stale) without a usable selection.
    - ``_action_noun`` -- short phrase naming the action for shared status
      messages (e.g. ``"comment"``, ``"Favorite change"``).
    - ``_queue_action(resolution, value)`` -- call the action manager to
      journal ``value`` and return its result.
    - ``_emit_journaled(action_id, value, cancelled_ids)`` -- emit the
      subclass's own completion signal.
    - ``_build_duplicate_message(result, value)`` -- status text for a
      duplicate-action result.
    - ``_can_submit()`` and ``_set_busy(busy)`` remain subclass-defined since
      they touch dialog-specific widgets.
    """

    pending_actions_requested = Signal()

    def __init__(
        self,
        *,
        action_manager: IdentifyActionManager,
        observation_id: int,
        observation_uuid: str,
        opened_login: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._action_manager = action_manager
        self._observation_id = int(observation_id)
        self._observation_uuid = _valid_uuid(observation_uuid)
        self._opened_login = str(opened_login or "").strip()
        self._closed = False
        self._busy = False
        self._authentication_generation = 0
        # A local nonce distinct from the manager's UUID request ID: it marks
        # whether *this* dialog currently considers a submission active, so a
        # stray late result cannot be mistaken for the current attempt even
        # if a request ID were ever reused.
        self._submission_generation = 0
        self._active_submission_generation: int | None = None
        self._active_submission_authentication_generation: int | None = None
        self._active_submission_value: object = None
        self._resolution_request_id: int | None = None
        # The submission generation captured when the active UUID request
        # started.  It must equal ``_active_submission_generation`` for the
        # request's result to be treated as belonging to the active attempt.
        self._resolution_request_generation: int | None = None

    def _connect_journal_signals(self) -> None:
        """Call once this dialog's own widgets exist, at the end of __init__."""
        self._action_manager.observation_uuid_resolved.connect(self._uuid_resolved)
        self._action_manager.authentication_context_changed.connect(
            self._authentication_changed
        )
        self._refresh_auth_status()

    # -- hooks subclasses must implement --------------------------------

    def _selected_value(self):
        raise NotImplementedError

    def _value_is_selected(self, value: object) -> bool:
        raise NotImplementedError

    @property
    def _missing_selection_message(self) -> str:
        raise NotImplementedError

    @property
    def _action_noun(self) -> str:
        raise NotImplementedError

    def _queue_action(
        self, resolution: ObservationUUIDResolution, value: object
    ) -> IdentifyEnqueueResult:
        raise NotImplementedError

    def _emit_journaled(
        self, action_id: int, value: object, cancelled_ids: tuple[int, ...]
    ) -> None:
        raise NotImplementedError

    def _build_duplicate_message(
        self, result: IdentifyEnqueueResult, value: object
    ) -> str:
        raise NotImplementedError

    def _can_submit(self) -> bool:
        raise NotImplementedError

    def _set_busy(self, busy: bool) -> None:
        raise NotImplementedError

    # -- shared authentication/status plumbing --------------------------

    def _authentication_changed(self) -> None:
        self._authentication_generation += 1
        if self._closed:
            return
        was_busy = self._busy
        if was_busy:
            # Release immediately rather than waiting for the stale UUID read
            # to return.  The manager's in-flight worker is left alone; its
            # result becomes harmless once _resolution_request_id is cleared.
            self._reset_after_submission_attempt()
        self._refresh_auth_status()
        if was_busy:
            self._status.setText(
                f"Authentication changed. The pending {self._action_noun} was cancelled "
                "before any local action was created. Submit again."
            )
        elif not self._matches_opened_account():
            self._status.setText(
                "Authentication changed. Authenticate as the original account or reopen this dialog."
            )

    def _refresh_auth_status(self) -> None:
        snapshot = self._action_manager.current_authentication()
        if self._same_login(snapshot.login, self._opened_login) and snapshot.authenticated:
            self._auth_status.setText(f"Authenticated as {snapshot.login}.")
        elif snapshot.authenticated:
            self._auth_status.setText(
                f"Authenticated as {snapshot.login}; this dialog was opened for {self._opened_login}."
            )
        else:
            self._auth_status.setText(
                f"Authentication is required for the account that opened this dialog ({self._opened_login})."
            )
        self._refresh_submit_enabled()

    def _matches_opened_account(self) -> bool:
        snapshot = self._action_manager.current_authentication()
        return bool(
            self._opened_login
            and snapshot.authenticated
            and self._same_login(snapshot.login, self._opened_login)
        )

    @staticmethod
    def _same_login(left: str, right: str) -> bool:
        return str(left or "").strip().casefold() == str(right or "").strip().casefold()

    def _refresh_submit_enabled(self) -> None:
        if hasattr(self, "_submit_button"):
            self._submit_button.setEnabled(self._can_submit())

    # -- shared submission/resolution flow -------------------------------

    def _submit(self) -> None:
        if self._closed or self._busy:
            return
        if not self._matches_opened_account():
            self._refresh_auth_status()
            self._status.setText(
                "Authenticate as the original account or reopen this dialog before submitting."
            )
            return
        value = self._selected_value()
        if not self._value_is_selected(value):
            self._status.setText(self._missing_selection_message)
            self._refresh_submit_enabled()
            return

        # Capture all user intent before asynchronous UUID resolution.
        self._submission_generation += 1
        self._active_submission_generation = self._submission_generation
        self._active_submission_authentication_generation = self._authentication_generation
        self._active_submission_value = value
        self._set_busy(True)
        self._status.setText("Resolving the observation identity…")
        try:
            self._resolution_request_id = self._action_manager.resolve_observation_uuid(
                self._observation_id,
                self._observation_uuid,
            )
            # Retain the generation associated with this exact UUID request so
            # _uuid_resolved can require it to still equal the active
            # submission generation, rather than merely being non-None.
            self._resolution_request_generation = self._active_submission_generation
        except Exception:
            self._reset_after_submission_attempt()
            self._status.setText(
                "The observation identity could not be prepared. "
                "No local action was created or authorized."
            )

    def _uuid_resolved(self, resolution: object) -> None:
        if self._closed or not isinstance(resolution, ObservationUUIDResolution):
            return
        if self._resolution_request_id is None or self._active_submission_generation is None:
            return
        if (
            resolution.request_id != self._resolution_request_id
            or resolution.observation_id != self._observation_id
            or self._resolution_request_generation != self._active_submission_generation
        ):
            return
        if (
            self._active_submission_authentication_generation
            != self._authentication_generation
        ):
            self._reset_after_submission_attempt()
            self._refresh_auth_status()
            self._status.setText(
                "Authentication changed while the observation identity was being resolved. "
                "No local action was created or authorized; submit again."
            )
            return
        if not resolution.resolved:
            self._reset_after_submission_attempt()
            self._status.setText(
                "Unable to resolve the observation identity. "
                "No local action was created or authorized."
            )
            return
        if not self._is_ready_to_journal(resolution):
            # The only mutable context required by these dialogs is
            # authentication and the explicit selection; explain that race
            # specifically and leave the window usable.
            self._reset_after_submission_attempt()
            if not self._matches_opened_account():
                self._refresh_auth_status()
                self._status.setText(
                    "Authentication is no longer valid for this dialog. "
                    "Authenticate as the original account or reopen it before submitting."
                )
            else:
                self._status.setText(self._missing_selection_message)
            return

        value = self._active_submission_value
        try:
            result = self._queue_action(resolution, value)
        except Exception:
            self._reset_after_submission_attempt()
            self._status.setText(
                f"The {self._action_noun} could not be saved locally. "
                "No local action was created or authorized."
            )
            return

        if result.inserted_action_id is not None:
            action_id = int(result.inserted_action_id)
            cancelled_ids = tuple(int(a) for a in result.cancelled_action_ids)
            # The journal transaction has committed.  Close before notifying
            # the parent so dispatch cannot leave this entry UI ambiguous.
            self.accept()
            self._emit_journaled(action_id, value, cancelled_ids)
            return

        if result.duplicate_action_id is not None:
            self._reset_after_submission_attempt()
            self._view_pending_button.show()
            self._status.setText(self._build_duplicate_message(result, value))
            return

        self._reset_after_submission_attempt()
        self._status.setText(
            f"The {self._action_noun} could not be saved locally. "
            "No local action was created or authorized."
        )

    def _is_ready_to_journal(self, resolution: ObservationUUIDResolution) -> bool:
        """Require the still-current captured context immediately before journaling."""
        snapshot = self._action_manager.current_authentication()
        return bool(
            not self._closed
            and self._busy
            and self._active_submission_generation is not None
            and self._resolution_request_generation == self._active_submission_generation
            and self._resolution_request_id == resolution.request_id
            and resolution.observation_id == self._observation_id
            and self._active_submission_authentication_generation
            == self._authentication_generation
            and snapshot.authenticated
            and self._same_login(snapshot.login, self._opened_login)
            and self._value_is_selected(self._active_submission_value)
        )

    def _reset_after_submission_attempt(self) -> None:
        self._resolution_request_id = None
        self._resolution_request_generation = None
        self._active_submission_generation = None
        self._active_submission_authentication_generation = None
        self._active_submission_value = None
        self._set_busy(False)

    def _open_pending_actions(self) -> None:
        # This is a duplicate-only recovery route.  It neither alters the
        # durable row nor grants it dispatch authorization.
        self.reject()
        self.pending_actions_requested.emit()

    def _deactivate(self) -> None:
        """Make all late local callbacks harmless without cancelling work."""
        if self._closed:
            return
        self._closed = True
        try:
            self._action_manager.observation_uuid_resolved.disconnect(self._uuid_resolved)
            self._action_manager.authentication_context_changed.disconnect(
                self._authentication_changed
            )
        except (RuntimeError, TypeError):
            pass

    def done(self, result: int) -> None:
        self._deactivate()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._deactivate()
        super().closeEvent(event)
