"""Interactive common-lineage comparison for Vision candidates."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.models import StudyTaxon
from observation_workbench.services.vision_taxonomy import (
    VisionTaxonResolution,
    most_specific_shared_taxon,
)


class MycomapVisionFamilyDialog(QDialog):
    def __init__(
        self,
        resolutions: tuple[VisionTaxonResolution, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare MycoMap Vision taxa")
        self.setMinimumWidth(620)
        self._resolutions = resolutions
        self._checks: list[tuple[QCheckBox, VisionTaxonResolution]] = []
        self._shared_taxon: StudyTaxon | None = None
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        intro = QLabel(
            "Families and shared taxa use current iNaturalist taxonomy. Check the "
            "Vision suggestions you consider plausible; the box below reports the "
            "most specific taxon that contains all of them.",
            self,
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        candidates_group = QGroupBox("Vision candidates", self)
        candidates_layout = QVBoxLayout(candidates_group)
        for resolution in self._resolutions:
            candidate = resolution.candidate
            confidence = (
                f", {candidate.confidence:.0%} confidence"
                if candidate.confidence is not None
                else ""
            )
            if resolution.family is not None and not resolution.error:
                suffix = f"family {resolution.family.name}{confidence}"
            else:
                suffix = resolution.error or "family unavailable"
            check = QCheckBox(
                f"{candidate.name} ({candidate.rank}) — {suffix}",
                candidates_group,
            )
            check.setEnabled(
                bool(resolution.taxon and resolution.lineage and not resolution.error)
            )
            check.toggled.connect(self._selection_changed)
            candidates_layout.addWidget(check)
            self._checks.append((check, resolution))
        outer.addWidget(candidates_group)

        shared_group = QGroupBox("Most specific shared taxon", self)
        shared_layout = QVBoxLayout(shared_group)
        self._shared_label = QLabel(
            "Check one or more candidates to compare their lineages.", shared_group
        )
        self._shared_label.setWordWrap(True)
        shared_layout.addWidget(self._shared_label)
        self._copy_button = QPushButton("Copy shared taxon name", shared_group)
        self._copy_button.setEnabled(False)
        self._copy_button.clicked.connect(self._copy_shared_name)
        shared_layout.addWidget(self._copy_button)
        outer.addWidget(shared_group)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _selection_changed(self) -> None:
        selected = tuple(
            resolution
            for check, resolution in self._checks
            if check.isChecked()
        )
        self._shared_taxon = most_specific_shared_taxon(selected)
        if not selected:
            text = "Check one or more candidates to compare their lineages."
        elif self._shared_taxon is None:
            text = "No shared iNaturalist taxon could be resolved."
        else:
            text = (
                f"{self._shared_taxon.name} "
                f"({self._shared_taxon.rank or 'unranked'})"
            )
        self._shared_label.setText(text)
        self._copy_button.setEnabled(self._shared_taxon is not None)

    def _copy_shared_name(self) -> None:
        if self._shared_taxon is not None:
            QApplication.clipboard().setText(self._shared_taxon.name)
