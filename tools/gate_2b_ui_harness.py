#!/usr/bin/env python3
"""Disposable offscreen Gate 2B UI smoke harness.

This is not a repository test. It creates no database, makes no network
request, and journals no action. The deliberately invalid image bytes exercise
the failed-thumbnail presentation path only.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

from observation_workbench.reconciliation.consolidation import (  # noqa: E402
    UNSUPPORTED_ITEM_DISCLOSURES,
)
from observation_workbench.reconciliation.types import (  # noqa: E402
    ConsolidationEligibility,
    ConsolidationEvidenceEdge,
    ConsolidationMemberSnapshot,
    ConsolidationPreview,
    PhotoRecordSnapshot,
    PriorConsolidationMember,
    RemoteSite,
)
from observation_workbench.ui.reconciliation import (  # noqa: E402
    ConsolidationPreviewDialog,
    _format_local_detail,
)


def _member(site: RemoteSite, observation_id: int, *, photo: bool = False):
    return ConsolidationMemberSnapshot(
        site=site,
        observation_id=observation_id,
        remote_uuid=f"{site.value}-uuid-{observation_id}",
        owner_login=f"{site.value}_owner",
        owner_id=100 if site is RemoteSite.MO else 200,
        account_id=100 if site is RemoteSite.MO else 200,
        taxon_id=123,
        taxon_name="Amanita muscaria",
        taxon_rank="species",
        observed_on_string="2026-06-01",
        locality="Some Forest",
        latitude=47.1,
        longitude=-122.1,
        description=f"Notes for {site.value} #{observation_id}",
        voucher_identifiers=("AR-1",),
        sequence_summaries=("Not inspected in Phase 2B.",),
        photos=(
            (
                PhotoRecordSnapshot(
                    site=site,
                    photo_id="photo-1",
                    observation_id=observation_id,
                    source_url="https://invalid.example/photo.jpg",
                    license_label="CC BY",
                    copyright_holder="Owned account",
                ),
            )
            if photo
            else ()
        ),
        remote_updated_at="2026-07-01T00:00:00+00:00",
        record_fingerprint=f"record-{site.value}-{observation_id}",
        preflight_fingerprint=f"stable-{site.value}-{observation_id}",
    )


def main() -> int:
    app = QApplication.instance() or QApplication([])
    preview = ConsolidationPreview(
        profile_id=1,
        consolidation_id=None,
        members=(
            _member(RemoteSite.MO, 10, photo=True),
            _member(RemoteSite.MO, 11),
            _member(RemoteSite.INAT, 20),
        ),
        eligibility=ConsolidationEligibility(
            eligible=True,
            supporting_evidence=("Exact voucher identity. [strong]",),
        ),
        unsupported_items=UNSUPPORTED_ITEM_DISCLOSURES,
        evidence_edges=(
            ConsolidationEvidenceEdge(
                RemoteSite.MO,
                10,
                RemoteSite.MO,
                11,
                "exact_voucher",
                "strong",
                "edge-1",
                "Exact normalized voucher-to-voucher identity.",
            ),
            ConsolidationEvidenceEdge(
                RemoteSite.INAT,
                20,
                RemoteSite.MO,
                10,
                "exact_voucher",
                "strong",
                "edge-2",
                "Exact normalized voucher-to-voucher identity.",
            ),
        ),
    )

    dialog = ConsolidationPreviewDialog(
        preview,
        lambda _url: b"deliberately-not-an-image",
    )
    assert dialog._choices
    assert not any(button.isChecked() for button in dialog._choices.values())
    assert all(
        not dialog.unsupported.cellWidget(row, 0).isEnabled()
        for row in range(dialog.unsupported.rowCount())
    )

    wait = QEventLoop()
    QTimer.singleShot(500, wait.quit)
    wait.exec()
    app.processEvents()
    assert all(
        not dialog.unsupported.cellWidget(row, 0).isEnabled()
        for row in range(dialog.unsupported.rowCount())
    ), "a failed thumbnail made an unsupported transfer selectable"

    visible_text = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert "DONOR OBSERVATIONS WILL REMAIN ONLINE" in visible_text
    assert "Phase 2C" in visible_text
    assert "owner" in dialog.table.item(0, 2).text()
    assert "profile account 100" in dialog.table.item(0, 2).text()

    dialog._choices[(RemoteSite.MO, 10)].setChecked(True)
    dialog._choices[(RemoteSite.INAT, 20)].setChecked(True)
    captured: dict[str, object] = {}
    original_question = QMessageBox.question

    def decline(parent, title, message, buttons, default_button):
        captured.update(
            title=title,
            message=message,
            buttons=buttons,
            default_button=default_button,
        )
        return QMessageBox.StandardButton.No

    QMessageBox.question = decline
    try:
        dialog._confirm()
    finally:
        QMessageBox.question = original_question
    assert captured["default_button"] == QMessageBox.StandardButton.No
    confirmation = str(captured["message"])
    for expected in (
        "Canonical MO: 10",
        "Canonical iNaturalist: 20",
        "Canonical taxon choice(s):",
        "mo #11",
        "Destination accounts:",
        "Donors remain remotely unchanged",
        "later Phase 2C review",
    ):
        assert expected in confirmation, expected
    assert dialog.approved_preview is None
    dialog.close()
    app.processEvents()

    extension = replace(
        preview,
        consolidation_id=7,
        canonical_mo_observation_id=10,
        canonical_inat_observation_id=20,
        is_extension=True,
        previous_members=(
            PriorConsolidationMember(
                RemoteSite.MO,
                9,
                "donor",
                "superseded",
                added_by_attempt_id=1,
                superseded_by_attempt_id=1,
                superseded_at="2026-07-01T00:00:00+00:00",
            ),
        ),
    )
    extension_dialog = ConsolidationPreviewDialog(
        extension,
        lambda _url: b"deliberately-not-an-image",
    )
    assert extension_dialog._choices[(RemoteSite.MO, 10)].isChecked()
    assert extension_dialog._choices[(RemoteSite.INAT, 20)].isChecked()
    assert all(not button.isEnabled() for button in extension_dialog._choices.values())
    extension_text = "\n".join(
        label.text() for label in extension_dialog.findChildren(QLabel)
    )
    assert "canonical choices are fixed" in extension_text
    assert "Previously superseded donors" in extension_text
    assert "attempt #1" in extension_text
    assert "Reviewed donor evidence paths" in extension_text
    extension_dialog.close()
    app.processEvents()
    history_text = _format_local_detail(
        {
            "consolidation_id": 7,
            "state": "finalized",
            "canonical_mo_observation_id": 10,
            "canonical_inat_observation_id": 20,
            "current_finalized_attempt_id": 2,
            "members": [
                {
                    "site": "mo",
                    "observation_id": 11,
                    "role": "donor",
                    "local_state": "superseded",
                    "added_by_attempt_id": 1,
                    "superseded_by_attempt_id": 2,
                    "superseded_at": "now",
                    "remote_url": "https://mushroomobserver.org/obs/11",
                }
            ],
            "attempts": [
                {
                    "attempt_id": 2,
                    "state": "succeeded",
                    "action_group_id": 3,
                    "is_current_finalized_baseline": True,
                    "members": [
                        {
                            "site": "mo",
                            "observation_id": 11,
                            "participation_role": "new_donor",
                        }
                    ],
                    "evidence": [
                        {
                            "evidence_strength": "strong",
                            "left_site": "mo",
                            "left_observation_id": 10,
                            "right_site": "mo",
                            "right_observation_id": 11,
                            "display_summary": "Exact normalized voucher identity.",
                        }
                    ],
                    "actions": [
                        {
                            "ordinal": 1,
                            "action_type": "consolidation_finalize",
                            "state": "succeeded",
                            "verification_state": "verified",
                        }
                    ],
                }
            ],
        }
    )
    for expected in (
        "Stable consolidation #7",
        "Canonical MO: 10",
        "superseded by attempt #2",
        "Immutable attempts",
        "+ donor MO #11",
        "evidence [IDENTITY; strong]",
        "consolidation_finalize",
        "CURRENT FINALIZED BASELINE",
        "admitted and superseded",
        "deletion is permanent",
    ):
        assert expected in history_text, (expected, history_text)
    print(
        "canonical choices unselected / unsupported photos disabled / failed "
        "thumbnail safe / confirmation defaults No / identities displayed / "
        "extension canonicals fixed / prior donors read-only / history provenance: PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
