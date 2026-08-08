#!/usr/bin/env python3
"""Disposable offline proof for Gate 2C.

This is not a repository test. It uses temporary SQLite databases and in-memory
remote records, never reads credentials, never opens a network connection, and
never calls either production deletion endpoint.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from observation_workbench.reconciliation.deletion import (  # noqa: E402
    CapabilityStatus, DeletionContent, DeletionError, DeletionService, DeleteDispatch,
    DonorDeletionPreview, ExternalDependency, RemoteDeletionRecord,
    SiteDeletionCapability, ThirdPartyContribution, analyze_lossless_parity,
    _activity_inventory_from_raw,
)
from observation_workbench.reconciliation.consolidation_identity import (  # noqa: E402
    canonical_stable_identity_fingerprint,
)
from observation_workbench.reconciliation.types import RemoteSite  # noqa: E402
import observation_workbench.ui.reconciliation as reconciliation_ui  # noqa: E402
from observation_workbench.ui.reconciliation import DonorDeletionPreviewDialog  # noqa: E402
from tools.gate_2b_saga_harness import (  # noqa: E402
    Environment, INAT_USER_ID, MO_USER_ID, PROFILE_ID,
)


def _capability(site: RemoteSite) -> SiteDeletionCapability:
    return SiteDeletionCapability(
        site=site,
        owned_observation_delete=CapabilityStatus.OFFLINE_PROVEN,
        stable_remote_identity=CapabilityStatus.OFFLINE_PROVEN,
        complete_donor_content_read=CapabilityStatus.OFFLINE_PROVEN,
        third_party_enumeration=CapabilityStatus.OFFLINE_PROVEN,
        definitive_post_delete_verification=CapabilityStatus.OFFLINE_PROVEN,
        unknown_outcome_recovery=CapabilityStatus.OFFLINE_PROVEN,
        safe_for_phase_2c=CapabilityStatus.OFFLINE_PROVEN,
        endpoint="offline fake only",
        disabled_reason="",
        execution_enabled=True,
    )


OFFLINE_CAPABILITIES = {
    RemoteSite.MO: _capability(RemoteSite.MO),
    RemoteSite.INAT: _capability(RemoteSite.INAT),
}


def _content(
    kind: str, identity: str, value: str = "", *,
    media: str = "", byte_fp: str = "", license_label: str = "",
    holder: str = "", attribution: str = "",
) -> DeletionContent:
    fingerprint = f"fp:{kind}:{identity}:{value}:{byte_fp}"
    return DeletionContent(
        kind, identity, value, fingerprint, f"{kind} {identity}",
        media, byte_fp, license_label, holder, attribution,
    )


def _record(
    site: RemoteSite, observation_id: int,
    contents: tuple[DeletionContent, ...] = (), *,
    third_party: tuple[ThirdPartyContribution, ...] = (),
    dependencies: tuple[ExternalDependency, ...] = (),
    owner_id: int | None = None,
    targets: tuple[tuple[RemoteSite, int], ...] = (),
    complete: bool = True,
) -> RemoteDeletionRecord:
    expected_owner = INAT_USER_ID if site is RemoteSite.INAT else MO_USER_ID
    remote_uuid = f"inat-uuid-{observation_id}" if site is RemoteSite.INAT else ""
    return RemoteDeletionRecord(
        site=site,
        observation_id=observation_id,
        remote_uuid=remote_uuid,
        owner_id=expected_owner if owner_id is None else owner_id,
        owner_login="inat_user" if site is RemoteSite.INAT else "mo_user",
        updated_at="2026-07-24T00:00:00+00:00",
        record_fingerprint=f"record:{site.value}:{observation_id}:v1",
        contents=contents,
        third_party=third_party,
        dependencies=dependencies,
        content_enumeration_complete=complete,
        third_party_enumeration_complete=complete,
        dependency_search_complete=complete,
        reciprocal_targets=targets,
    )


class AmbiguousDelete(RuntimeError):
    outcome_unknown = True
    status_code = None


class FakeDispatch(DeleteDispatch):
    def __init__(self, records: dict[tuple[RemoteSite, int], RemoteDeletionRecord]):
        self.records = records
        self.delete_calls: list[tuple[RemoteSite, int]] = []
        self.lose_response_for: set[tuple[RemoteSite, int]] = set()
        self.reject_for: set[tuple[RemoteSite, int]] = set()
        self.ambiguous_verify_for: set[tuple[RemoteSite, int]] = set()
        self.present_verify_for: set[tuple[RemoteSite, int]] = set()
        self.after_delete = None
        self.after_verify = None
        self._lock = threading.Lock()

    def refresh_record(self, profile, site, observation_id, cancelled):
        del profile
        if cancelled():
            raise RuntimeError("cancelled")
        with self._lock:
            return self.records[(site, int(observation_id))]

    def delete_exact(
        self, profile, record, request_correlation, cancelled,
    ) -> None:
        del profile, request_correlation
        key = (record.site, record.observation_id)
        with self._lock:
            self.delete_calls.append(key)
            if key in self.reject_for:
                raise RuntimeError("offline rejection")
            self.records[key] = replace(record, exists=False)
            hook = self.after_delete
        if hook:
            hook(key)
        if key in self.lose_response_for or cancelled():
            raise AmbiguousDelete("offline lost response")

    def verify_exact_absence(
        self, profile, site, observation_id, remote_uuid, owner_id, cancelled,
    ) -> str:
        del profile, remote_uuid, owner_id, cancelled
        key = (site, int(observation_id))
        if key in self.ambiguous_verify_for:
            return "ambiguous"
        with self._lock:
            if key in self.present_verify_for:
                verdict = "present"
            else:
                verdict = "deleted" if not self.records[key].exists else "present"
            hook = self.after_verify
        if hook:
            hook(key, verdict)
        return verdict


class Scenario:
    def __init__(self) -> None:
        self.env = Environment((10, 11), (20, 21))
        preview = self.env.preview((10, 11), (20, 21), 10, 20)
        self.consolidation_id, _, group_id = self.env.journal(preview)
        assert self.env.run(group_id)[-1].state == "succeeded"
        records = {
            (RemoteSite.MO, 10): _record(
                RemoteSite.MO, 10, targets=((RemoteSite.INAT, 20),),
            ),
            (RemoteSite.MO, 11): _record(RemoteSite.MO, 11),
            (RemoteSite.INAT, 20): _record(
                RemoteSite.INAT, 20, targets=((RemoteSite.MO, 10),),
            ),
            (RemoteSite.INAT, 21): _record(RemoteSite.INAT, 21),
        }
        self.dispatch = FakeDispatch(records)
        self.service = DeletionService(
            self.env.db, self.env.inat, self.env.mo, lambda: self.env.auth,
            lambda _profile_id: "offline-mo-key", lambda: 0, lambda: 0,
            dispatch=self.dispatch, capabilities=OFFLINE_CAPABILITIES,
            phase_2b_closed_provider=lambda: True,
        )

    def preview(self) -> DonorDeletionPreview:
        return self.service.prepare_preview(
            PROFILE_ID, self.consolidation_id, lambda: False,
        )

    def close(self) -> None:
        self.env.close()


def parity_scenarios() -> None:
    empty = _record(RemoteSite.MO, 1)
    assert not analyze_lossless_parity(empty, _record(RemoteSite.MO, 2))[1]

    unique = _record(
        RemoteSite.MO, 1,
        (_content("photo", "p1", media="mo:photo:1", license_label="CC0"),),
    )
    _, reasons = analyze_lossless_parity(unique, _record(RemoteSite.MO, 2))
    assert "Blocked: unique photo" in reasons

    same_photo = _content(
        "photo", "p1", media="source:photo:7", license_label="CC-BY",
        holder="owner", attribution="owner / CC-BY",
    )
    canonical_photo = replace(same_photo, identity="canonical-photo")
    assert not analyze_lossless_parity(
        _record(RemoteSite.MO, 1, (same_photo,)),
        _record(RemoteSite.MO, 2, (canonical_photo,)),
    )[1]

    thumbnail_only = _content("photo", "thumb-similar")
    assert analyze_lossless_parity(
        _record(RemoteSite.MO, 1, (thumbnail_only,)),
        _record(RemoteSite.MO, 2, (thumbnail_only,)),
    )[1]

    for donor_item, canonical_item in (
        (_content("description", "description", "one"),
         _content("description", "description", "two")),
        (_content("voucher", "voucher_number", "V-1"),
         _content("collection_number", "collection_number", "V-1")),
        (_content("date", "observed_on", "2026-01-01"),
         _content("date", "observed_on", "2026-01-02")),
        (_content("coordinates", "coordinates", "1,2"),
         _content("coordinates", "coordinates", "1,3")),
    ):
        assert analyze_lossless_parity(
            _record(RemoteSite.MO, 1, (donor_item,)),
            _record(RemoteSite.MO, 2, (canonical_item,)),
        )[1]

    sequence = _content("sequence", "sequence:1:ABC", "ABC")
    assert not analyze_lossless_parity(
        _record(RemoteSite.MO, 1, (sequence,)),
        _record(RemoteSite.MO, 2, (sequence,)),
    )[1]
    print("eligibility parity matrix: PASS")


def hardening_regressions() -> None:
    donor_photos = (
        DeletionContent(
            "photo", "donor-photo-1", source_media_identity="same-source",
        ),
        DeletionContent(
            "photo", "donor-photo-2", source_media_identity="same-source",
        ),
    )
    canonical_photo = (
        DeletionContent(
            "photo", "canonical-photo-1", source_media_identity="same-source",
        ),
    )
    _, reasons = analyze_lossless_parity(
        _record(RemoteSite.MO, 1, donor_photos),
        _record(RemoteSite.MO, 2, canonical_photo),
    )
    assert "Blocked: unique photo" in reasons

    owner, external, issues = _activity_inventory_from_raw(
        {
            "comments": [
                {"id": 1, "user_id": MO_USER_ID, "body": "owner note"},
                {"id": 2, "body": "unknown author"},
                "malformed",
            ],
        },
        MO_USER_ID,
    )
    assert [item.content_type for item in owner] == ["owner_comment"]
    assert any(item.contributor_id == 0 for item in external)
    assert issues
    print(
        "one-to-one parity / owner activity / unknown authorship / "
        "malformed activity fail closed: PASS"
    )

    scenario = Scenario()
    try:
        preview = scenario.preview()
        first, second = preview.donors
        scenario.env.db.journal_deletion_attempt(
            preview, (first.stable_member_id,),
        )
        try:
            scenario.env.db.journal_deletion_attempt(
                preview, (second.stable_member_id,),
            )
            raise AssertionError("parallel consolidation deletion was accepted")
        except ValueError:
            pass
        unresolved = scenario.env.db.unresolved_deletion_for_consolidation(
            PROFILE_ID, scenario.consolidation_id,
        )
        assert unresolved is not None
        blocked = scenario.preview()
        assert all(
            "Blocked: unresolved deletion attempt" in donor.blocking_reasons
            for donor in blocked.donors
        )
        current = scenario.env.db.get_consolidation(
            PROFILE_ID, scenario.consolidation_id,
        )["current_finalized_attempt_id"]
        baseline_blocked = False
        try:
            scenario.env.db.connection().execute(
                "UPDATE sync_consolidations "
                "SET current_finalized_attempt_id=? "
                "WHERE profile_id=? AND consolidation_id=?",
                (int(current) + 9999, PROFILE_ID, scenario.consolidation_id),
            )
        except sqlite3.IntegrityError:
            baseline_blocked = True
        assert baseline_blocked, "baseline advanced during deletion attempt"
        assert scenario.env.db.get_consolidation(
            PROFILE_ID, scenario.consolidation_id,
        )["current_finalized_attempt_id"] == current
        print(
            "one unresolved attempt per consolidation / preview guard / "
            "baseline serialization: PASS"
        )
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        selected = tuple(item.stable_member_id for item in preview.donors)
        attempt_id, group_id = scenario.env.db.journal_deletion_attempt(
            preview, selected,
        )
        attempt = scenario.env.db.deletion_attempt_for_group(
            PROFILE_ID, group_id,
        )
        items = scenario.env.db.deletion_items(PROFILE_ID, attempt_id)
        first = scenario.service._execute_one(
            PROFILE_ID, attempt, items[0], lambda: False,
            lambda _message: None,
        )
        assert first.state == "succeeded"
        assert scenario.env.db.cancel_deletion_tail(
            PROFILE_ID, attempt_id, 2,
        ) == 1
        assert scenario.env.db.deletion_attempt(
            PROFILE_ID, attempt_id,
        )["state"] == "partial"
        assert [
            item["state"]
            for item in scenario.env.db.deletion_items(PROFILE_ID, attempt_id)
        ] == ["succeeded", "cancelled"]
        assert not scenario.env.db.unresolved_deletion_for_consolidation(
            PROFILE_ID, scenario.consolidation_id,
        )["resumable"]
        retry_preview = scenario.preview()
        assert len(retry_preview.donors) == 1
        retry_attempt_id, _ = scenario.env.db.journal_deletion_attempt(
            retry_preview, (retry_preview.donors[0].stable_member_id,),
        )
        assert scenario.env.db.deletion_attempt(
            PROFILE_ID, attempt_id,
        )["state"] == "superseded"
        assert scenario.env.db.deletion_attempt(
            PROFILE_ID, retry_attempt_id,
        )["state"] == "pending"
        print("successful prefix / cancelled tail records partial attempt: PASS")
    finally:
        scenario.close()


def readiness_and_confirmation_scenarios(app: QApplication) -> None:
    scenario = Scenario()
    try:
        preview = scenario.preview()
        assert len(preview.donors) == 2
        assert all(item.eligible for item in preview.donors)
        dialog = DonorDeletionPreviewDialog(preview)
        assert not dialog.selected_member_ids()
        phrase_one = preview.typed_phrase(
            (preview.donors[0].stable_member_id,)
        )
        assert phrase_one in {"DELETE MO 11", "DELETE INAT 21"}
        assert preview.typed_phrase(
            tuple(item.stable_member_id for item in preview.donors)
        ) == "DELETE 2 DONORS"
        first_fp = preview.confirmation_fingerprint(
            (preview.donors[0].stable_member_id,)
        )
        second_fp = preview.confirmation_fingerprint(
            tuple(item.stable_member_id for item in preview.donors)
        )
        assert first_fp != second_fp
        dialog.close()

        member_id = preview.donors[0].stable_member_id
        original_question = reconciliation_ui.QMessageBox.question
        original_warning = reconciliation_ui.QMessageBox.warning
        original_get_text = reconciliation_ui.QInputDialog.getText
        defaults: list[object] = []
        try:
            no_dialog = DonorDeletionPreviewDialog(preview)
            no_dialog._checks[member_id].setChecked(True)

            def answer_no(*args, **kwargs):
                assert len(args) + len(kwargs) >= 5, (
                    f"unexpected QMessageBox.question call: args={args} kwargs={kwargs}"
                )
                default_button = kwargs.get("defaultButton", args[4] if len(args) > 4 else None)
                defaults.append(default_button)
                return reconciliation_ui.QMessageBox.StandardButton.No

            reconciliation_ui.QMessageBox.question = answer_no
            no_dialog._confirm()
            assert not no_dialog.approved_member_ids
            assert defaults[-1] == reconciliation_ui.QMessageBox.StandardButton.No
            no_dialog.close()

            wrong_dialog = DonorDeletionPreviewDialog(preview)
            wrong_dialog._checks[member_id].setChecked(True)
            reconciliation_ui.QMessageBox.question = (
                lambda *_args: reconciliation_ui.QMessageBox.StandardButton.Yes
            )
            reconciliation_ui.QMessageBox.warning = lambda *_args: None
            reconciliation_ui.QInputDialog.getText = (
                lambda *_args: ("DELETE SOMETHING", True)
            )
            wrong_dialog._confirm()
            assert not wrong_dialog.approved_member_ids
            wrong_dialog.close()

            changed_dialog = DonorDeletionPreviewDialog(preview)
            changed_dialog._checks[member_id].setChecked(True)

            def change_selection(*_args):
                changed_dialog._checks[member_id].setChecked(False)
                return (preview.typed_phrase((member_id,)), True)

            reconciliation_ui.QInputDialog.getText = change_selection
            changed_dialog._confirm()
            assert not changed_dialog.approved_member_ids
            changed_dialog.close()
        finally:
            reconciliation_ui.QMessageBox.question = original_question
            reconciliation_ui.QMessageBox.warning = original_warning
            reconciliation_ui.QInputDialog.getText = original_get_text

        contribution = ThirdPartyContribution(
            "identification", "id-7", 777,
            "Third-party identification by account 777", "third-party-fp",
        )
        key = (RemoteSite.MO, 11)
        scenario.dispatch.records[key] = replace(
            scenario.dispatch.records[key], third_party=(contribution,),
        )
        blocked = scenario.preview()
        row = next(item for item in blocked.donors if item.site is RemoteSite.MO)
        assert not row.eligible
        blocked_dialog = DonorDeletionPreviewDialog(blocked)
        check = blocked_dialog._checks[row.stable_member_id]
        assert not check.isEnabled()
        check.setChecked(True)
        assert row.stable_member_id not in blocked_dialog.selected_member_ids()
        blocked_dialog.close()
        app.processEvents()
        print("nothing selected / blocked unselectable / plan-tied phrase: PASS")
    finally:
        scenario.close()


def blocked_readiness_scenarios() -> None:
    scenario = Scenario()
    try:
        key = (RemoteSite.MO, 11)
        scenario.dispatch.records[key] = replace(
            scenario.dispatch.records[key], content_enumeration_complete=False,
        )
        assert any(
            "complete donor content" in reason
            for reason in next(
                item for item in scenario.preview().donors
                if item.site is RemoteSite.MO
            ).blocking_reasons
        )
        scenario.dispatch.records[key] = replace(
            scenario.dispatch.records[key],
            content_enumeration_complete=True,
            dependencies=(ExternalDependency(
                "reverse_link", "other:1", "mo:11",
                "Other observation links to donor", False,
            ),),
        )
        assert "Blocked: external dependency" in next(
            item for item in scenario.preview().donors
            if item.site is RemoteSite.MO
        ).blocking_reasons
        scenario.dispatch.records[key] = replace(
            scenario.dispatch.records[key], dependencies=(), owner_id=999,
        )
        preview_before_drift = scenario.preview()
        assert "Blocked: donor no longer owned" in next(
            item for item in preview_before_drift.donors
            if item.site is RemoteSite.MO
        ).blocking_reasons
        baseline_fingerprint = preview_before_drift.canonical_mutable_snapshot_fingerprint
        scenario.dispatch.records[(RemoteSite.MO, 10)] = replace(
            scenario.dispatch.records[(RemoteSite.MO, 10)],
            record_fingerprint="canonical-drift",
        )
        drifted = scenario.preview()
        assert drifted.canonical_mutable_snapshot_fingerprint
        assert drifted.canonical_mutable_snapshot_fingerprint != baseline_fingerprint
        print("dependency / ownership / drift: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        _, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        key = (donor.site, donor.observation_id)
        scenario.dispatch.records[key] = replace(
            scenario.dispatch.records[key],
            record_fingerprint="donor-fingerprint-drift",
        )
        try:
            scenario.service.execute_group(
                PROFILE_ID, group_id, lambda: False, lambda _message: None,
            )
            raise AssertionError("fingerprint drift reached deletion")
        except DeletionError as exc:
            assert exc.code == "review_fingerprint_drift"
        assert not scenario.dispatch.delete_calls
        assert not scenario.env.db.deletion_actions_for_attempt(
            PROFILE_ID,
            int(scenario.env.db.deletion_attempt_for_group(
                PROFILE_ID, group_id,
            )["deletion_attempt_id"]),
        )
        print("reviewed fingerprint drift blocks before delete-action journal: PASS")
    finally:
        scenario.close()


def saga_success_and_partial() -> None:
    scenario = Scenario()
    try:
        preview = scenario.preview()
        selected = tuple(item.stable_member_id for item in preview.donors)
        attempt_id, group_id = scenario.env.db.journal_deletion_attempt(
            preview, selected,
        )
        results = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )
        assert [item.state for item in results] == ["succeeded", "succeeded"]
        assert len(scenario.dispatch.delete_calls) == 2
        assert scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        ) == []
        for action in scenario.env.db.deletion_actions_for_attempt(
            PROFILE_ID, attempt_id,
        ):
            assert scenario.env.db.settle_deletion_success(
                PROFILE_ID, int(action["deletion_action_id"]),
            )
        attempt = scenario.env.db.deletion_attempt(PROFILE_ID, attempt_id)
        assert attempt and attempt["state"] == "succeeded"
        members = scenario.env.db.list_consolidation_members(
            PROFILE_ID, scenario.consolidation_id,
        )
        assert all(
            member["remote_state"] == "deleted"
            for member in members if member["role"] == "donor"
        )
        assert all(
            member["remote_state"] == "online"
            for member in members if member["role"] == "canonical"
        )
        print("two sequential successes / idempotent resume / canonical untouched: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        second = preview.donors[1]
        scenario.dispatch.reject_for.add((second.site, second.observation_id))
        selected = tuple(item.stable_member_id for item in preview.donors)
        _, group_id = scenario.env.db.journal_deletion_attempt(preview, selected)
        results = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )
        # Exceptions after write_started_at are never treated as definitive
        # rejection: the second action is unknown and the verified first
        # tombstone remains intact.
        assert [item.state for item in results] == [
            "succeeded", "outcome_unknown",
        ]
        assert len(scenario.dispatch.delete_calls) == 2
        first_member = scenario.env.db.connection().execute(
            "SELECT remote_state FROM sync_consolidation_members "
            "WHERE consolidation_member_id=?",
            (preview.donors[0].stable_member_id,),
        ).fetchone()
        second_member = scenario.env.db.connection().execute(
            "SELECT remote_state FROM sync_consolidation_members "
            "WHERE consolidation_member_id=?",
            (preview.donors[1].stable_member_id,),
        ).fetchone()
        assert first_member["remote_state"] == "deleted"
        assert second_member["remote_state"] == "online"
        print("partial completion retained / no rollback recreation: PASS")
    finally:
        scenario.close()


def saga_unknown_and_cancellation() -> None:
    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        key = (donor.site, donor.observation_id)
        scenario.dispatch.lose_response_for.add(key)
        _, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        result = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )[0]
        assert result.state == "outcome_unknown"
        assert scenario.dispatch.delete_calls == [key]
        action_id = scenario.env.db.deletion_items(
            PROFILE_ID,
            int(scenario.env.db.deletion_attempt_for_group(
                PROFILE_ID, group_id,
            )["deletion_attempt_id"]),
        )[0]["action_id"]
        verified = scenario.service.verify_unknown(
            PROFILE_ID, int(action_id), lambda: False,
        )
        assert verified.state == "succeeded"
        assert scenario.dispatch.delete_calls == [key]
        print("lost response / verify absence / never resend: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        _, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        assert not scenario.dispatch.delete_calls
        # No action has been journaled yet, so cancellation of the worker tail
        # sends nothing and leaves the immutable review pending.
        assert scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: True, lambda _message: None,
        ) == []
        assert not scenario.dispatch.delete_calls
        print("cancellation before request sends nothing: PASS")
    finally:
        scenario.close()

    for verdict in ("present", "ambiguous"):
        scenario = Scenario()
        try:
            preview = scenario.preview()
            donor = preview.donors[0]
            key = (donor.site, donor.observation_id)
            scenario.dispatch.lose_response_for.add(key)
            if verdict == "present":
                scenario.dispatch.present_verify_for.add(key)
            else:
                scenario.dispatch.ambiguous_verify_for.add(key)
            _, group_id = scenario.env.db.journal_deletion_attempt(
                preview, (donor.stable_member_id,),
            )
            initial = scenario.service.execute_group(
                PROFILE_ID, group_id, lambda: False, lambda _message: None,
            )[0]
            assert initial.state == "outcome_unknown"
            attempt = scenario.env.db.deletion_attempt_for_group(
                PROFILE_ID, group_id,
            )
            item = scenario.env.db.deletion_items(
                PROFILE_ID, int(attempt["deletion_attempt_id"]),
            )[0]
            result = scenario.service.verify_unknown(
                PROFILE_ID, int(item["action_id"]), lambda: False,
            )
            assert result.state == (
                "retry_required" if verdict == "present" else "outcome_unknown"
            )
            assert scenario.dispatch.delete_calls == [key]
            if verdict == "present":
                scenario.dispatch.records[key] = replace(
                    scenario.dispatch.records[key], exists=True,
                )
                scenario.dispatch.present_verify_for.discard(key)
                retry_preview = scenario.preview()
                retry_donor = next(
                    item for item in retry_preview.donors
                    if item.stable_member_id == donor.stable_member_id
                )
                retry_attempt_id, _ = scenario.env.db.journal_deletion_attempt(
                    retry_preview, (retry_donor.stable_member_id,),
                )
                retry_attempt = scenario.env.db.deletion_attempt(
                    PROFILE_ID, retry_attempt_id,
                )
                assert (
                    retry_attempt["supersedes_attempt_id"]
                    == attempt["deletion_attempt_id"]
                )
        finally:
            scenario.close()
    print("unknown verifier present / ambiguous / no blind resend: PASS")

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        cancelled = {"value": False}
        scenario.dispatch.after_delete = (
            lambda _key: cancelled.update(value=True)
        )
        _, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        result = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: cancelled["value"],
            lambda _message: None,
        )[0]
        assert result.state == "outcome_unknown"
        assert len(scenario.dispatch.delete_calls) == 1
        print("cancellation after write preserves ambiguity: PASS")
    finally:
        scenario.close()


def claimed_action_exception_recovery() -> None:
    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        attempt_id, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        original_refresh = scenario.dispatch.refresh_record

        def fail_claimed_refresh(profile, site, observation_id, cancelled):
            running = scenario.env.db.connection().execute(
                "SELECT 1 FROM sync_deletion_actions "
                "WHERE profile_id=? AND state='running' LIMIT 1",
                (PROFILE_ID,),
            ).fetchone()
            if running and observation_id == donor.observation_id:
                raise RuntimeError("offline pre-write parser failure")
            return original_refresh(profile, site, observation_id, cancelled)

        scenario.dispatch.refresh_record = fail_claimed_refresh
        result = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )[0]
        assert result.state == "pending"
        item = scenario.env.db.deletion_items(PROFILE_ID, attempt_id)[0]
        action = scenario.env.db.deletion_action(
            PROFILE_ID, int(item["action_id"]),
        )
        assert action["state"] == "pending"
        assert action["write_started_at"] is None
        assert not scenario.dispatch.delete_calls
        scenario.dispatch.refresh_record = original_refresh
        resumed = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )
        assert [row.state for row in resumed] == ["succeeded"]
        print("claimed pre-write exception normalizes without restart: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        attempt_id, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        original_verify = scenario.dispatch.verify_exact_absence

        def verifier_raises(*_args, **_kwargs):
            raise RuntimeError("offline verifier parser failure")

        scenario.dispatch.verify_exact_absence = verifier_raises
        result = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )[0]
        assert result.state == "outcome_unknown"
        assert len(scenario.dispatch.delete_calls) == 1
        item = scenario.env.db.deletion_items(PROFILE_ID, attempt_id)[0]
        scenario.dispatch.verify_exact_absence = original_verify
        resumed = scenario.service.verify_unknown(
            PROFILE_ID, int(item["action_id"]), lambda: False,
        )
        assert resumed.state == "succeeded"
        assert len(scenario.dispatch.delete_calls) == 1
        print("post-write verifier exception is unknown / never resent: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        attempt_id, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        item = scenario.env.db.deletion_items(PROFILE_ID, attempt_id)[0]
        identity = canonical_stable_identity_fingerprint(
            donor.site, donor.observation_id, donor.remote_uuid,
            MO_USER_ID if donor.site is RemoteSite.MO else INAT_USER_ID,
        )
        action_id = scenario.env.db.mint_deletion_action(
            PROFILE_ID, attempt_id, int(item["deletion_item_id"]),
            site=donor.site.value, observation_id=donor.observation_id,
            remote_uuid=donor.remote_uuid,
            reviewed_identity_fingerprint=identity,
            request_correlation="offline-running-resume",
        )
        assert scenario.env.db.claim_deletion_action(PROFILE_ID, action_id)
        resumed = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )
        assert [row.state for row in resumed] == ["succeeded"]
        assert len(scenario.dispatch.delete_calls) == 1
        print("Resume normalizes discovered running action without restart: PASS")
    finally:
        scenario.close()


def restart_concurrency_and_rollback() -> None:
    scenario = Scenario()
    try:
        preview = scenario.preview()
        selected = tuple(item.stable_member_id for item in preview.donors)
        _, group_id = scenario.env.db.journal_deletion_attempt(preview, selected)
        attempt = scenario.env.db.deletion_attempt_for_group(
            PROFILE_ID, group_id,
        )
        first_item = scenario.env.db.deletion_items(
            PROFILE_ID, int(attempt["deletion_attempt_id"]),
        )[0]
        first = scenario.service._execute_one(
            PROFILE_ID, attempt, first_item, lambda: False,
            lambda _message: None,
        )
        assert first.state == "succeeded"
        scenario.env.reopen()
        scenario.service.db = scenario.env.db
        rest = scenario.service.execute_group(
            PROFILE_ID, group_id, lambda: False, lambda _message: None,
        )
        assert [item.state for item in rest] == ["succeeded"]
        assert len(scenario.dispatch.delete_calls) == 2
        print("restart between donor ordinals resumes tail only: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        _, group_id = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        barrier = threading.Barrier(2)
        original_refresh = scenario.dispatch.refresh_record

        def synchronized_refresh(profile, site, observation_id, cancelled):
            record = original_refresh(
                profile, site, observation_id, cancelled,
            )
            if observation_id == donor.observation_id and record.exists:
                try:
                    barrier.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
            return record

        scenario.dispatch.refresh_record = synchronized_refresh
        outputs: list[object] = []

        def run() -> None:
            try:
                outputs.append(scenario.service.execute_group(
                    PROFILE_ID, group_id, lambda: False,
                    lambda _message: None,
                ))
            except Exception as exc:
                outputs.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(scenario.dispatch.delete_calls) == 1, outputs
        assert not any(isinstance(value, Exception) for value in outputs), outputs
        print("concurrent resume sends one delete request: PASS")
    finally:
        scenario.close()

    scenario = Scenario()
    try:
        preview = scenario.preview()
        donor = preview.donors[0]
        attempt_id, _ = scenario.env.db.journal_deletion_attempt(
            preview, (donor.stable_member_id,),
        )
        attempt = scenario.env.db.deletion_attempt(PROFILE_ID, attempt_id)
        item = scenario.env.db.deletion_items(PROFILE_ID, attempt_id)[0]
        action_id = scenario.env.db.mint_deletion_action(
            PROFILE_ID, attempt_id, int(item["deletion_item_id"]),
            site=donor.site.value, observation_id=donor.observation_id,
            remote_uuid=donor.remote_uuid,
            reviewed_identity_fingerprint=canonical_stable_identity_fingerprint(
                donor.site, donor.observation_id, donor.remote_uuid,
                MO_USER_ID if donor.site is RemoteSite.MO else INAT_USER_ID,
            ),
            request_correlation="offline-rollback-correlation",
        )
        assert scenario.env.db.claim_deletion_action(PROFILE_ID, action_id)
        assert scenario.env.db.mark_deletion_write_started(PROFILE_ID, action_id)
        assert scenario.env.db.mark_deletion_verified(
            PROFILE_ID, action_id, "verified_deleted"
        )
        scenario.env.db.connection().execute(
            "UPDATE sync_consolidation_members SET local_state='active' "
            "WHERE consolidation_member_id=?",
            (donor.stable_member_id,),
        )
        try:
            scenario.env.db.settle_deletion_success(PROFILE_ID, action_id)
            raise AssertionError("broken finalization invariant was accepted")
        except ValueError:
            pass
        assert scenario.env.db.deletion_action(
            PROFILE_ID, action_id,
        )["state"] == "running"
        assert scenario.env.db.deletion_items(
            PROFILE_ID, attempt_id,
        )[0]["state"] == "running"
        assert scenario.env.db.connection().execute(
            "SELECT remote_state FROM sync_consolidation_members "
            "WHERE consolidation_member_id=?",
            (donor.stable_member_id,),
        ).fetchone()["remote_state"] == "online"
        print("finalization invariant failure rolls back every local update: PASS")
    finally:
        scenario.close()


def migration_invariants() -> None:
    scenario = Scenario()
    try:
        conn = scenario.env.db.connection()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT COUNT(*) FROM sync_deletion_attempts"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sync_consolidation_members "
            "WHERE remote_state='deleted'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sync_consolidation_members "
            "WHERE local_state='superseded' AND remote_state='online'"
        ).fetchone()[0] == 2
        print("v17 migration integrity / no invented attempts or tombstones: PASS")
    finally:
        scenario.close()


def main() -> int:
    if not __debug__:
        print(
            "This harness verifies every scenario with `assert`; it was started with "
            "Python optimizations enabled (-O / PYTHONOPTIMIZE), which strips asserts "
            "and would make every check silently pass. Re-run without -O.",
            file=sys.stderr,
        )
        return 2
    app = QApplication.instance() or QApplication([])
    parity_scenarios()
    hardening_regressions()
    readiness_and_confirmation_scenarios(app)
    blocked_readiness_scenarios()
    saga_success_and_partial()
    saga_unknown_and_cancellation()
    claimed_action_exception_recovery()
    restart_concurrency_and_rollback()
    migration_invariants()
    print("ALL OFFLINE GATE 2C HARNESS SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
