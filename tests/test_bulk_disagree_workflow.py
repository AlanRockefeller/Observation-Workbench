import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from observation_workbench.models import StudyObservation, StudyTaxon
from observation_workbench.services.bulk_disagree import (
    _observation_matches_source,
    observation_matches_source_provisional_name,
    taxon_is_ancestor_or_same,
    taxon_is_strict_ancestor,
)
from observation_workbench.ui.bulk_disagree_dialogs import (
    AlternateIdentificationDialog,
    BulkDisagreeSetupDialog,
    _GalleryImageWorker,
    _TaxonAutocompleteWorker,
)


class BulkDisagreeTaxonRelationshipTests(unittest.TestCase):
    def test_strict_ancestor_target_is_accepted(self) -> None:
        source = StudyTaxon(taxon_id=20, name="Genus species", ancestry="1/10")

        self.assertTrue(taxon_is_strict_ancestor(source, 10))

    def test_same_taxon_is_not_strict_ancestor(self) -> None:
        source = StudyTaxon(taxon_id=20, name="Genus species", ancestry="1/10")

        self.assertFalse(taxon_is_strict_ancestor(source, 20))
        self.assertTrue(taxon_is_ancestor_or_same(source, 20))


class ProvisionalNameSourceTests(unittest.TestCase):
    def _obs(self, provisional_name: str) -> StudyObservation:
        return StudyObservation(
            obs_id=1,
            observer_login="someone",
            provisional_species_name=provisional_name,
        )

    def test_provisional_name_matches_case_insensitively(self) -> None:
        obs = self._obs("Hygrocybe sp. 'flavescens-PNW06'")

        self.assertTrue(
            observation_matches_source_provisional_name(
                obs, "hygrocybe sp. 'FLAVESCENS-PNW06'"
            )
        )

    def test_different_provisional_name_does_not_match(self) -> None:
        obs = self._obs("Hygrocybe sp. 'flavescens-PNW06'")

        self.assertFalse(observation_matches_source_provisional_name(obs, "Hygrocybe sp. 'other'"))

    def test_blank_source_provisional_name_never_matches(self) -> None:
        obs = self._obs("Hygrocybe sp. 'flavescens-PNW06'")

        self.assertFalse(observation_matches_source_provisional_name(obs, ""))

    def test_source_helper_prefers_provisional_name_over_taxon(self) -> None:
        obs = self._obs("Hygrocybe sp. 'flavescens-PNW06'")

        self.assertTrue(
            _observation_matches_source(
                obs,
                source_taxon_id=0,
                source_provisional_name="Hygrocybe sp. 'flavescens-PNW06'",
            )
        )


class BulkDisagreeResearchGradeSkipTests(unittest.TestCase):
    def _candidate(self):
        from observation_workbench.services.bulk_disagree import BulkDisagreeCandidate

        return BulkDisagreeCandidate(
            observation=StudyObservation(obs_id=42, observer_login="observer"),
            source_taxon_id=10,
            source_taxon_name="Genus",
            target_taxon_id=55,
            target_taxon_name="Genus species",
        )

    def _research_grade_obs_as_target(self) -> StudyObservation:
        target = StudyTaxon(taxon_id=55, name="Genus species")
        return StudyObservation(
            obs_id=42,
            observer_login="observer",
            taxon=target,
            community_taxon=target,
            quality_grade="research",
        )

    def test_post_skips_research_grade_observation_already_at_target(self) -> None:
        from unittest.mock import Mock, patch

        from observation_workbench.services.bulk_disagree import post_bulk_disagreement

        client = Mock()
        with patch(
            "observation_workbench.services.bulk_disagree.refresh_observation",
            return_value=self._research_grade_obs_as_target(),
        ):
            result = post_bulk_disagreement(
                client,
                "token",
                "me",
                self._candidate(),
                require_source_taxon_match=False,
            )

        self.assertEqual(result.status, "skipped")
        self.assertIn("research grade", result.message.casefold())
        client.create_identification.assert_not_called()

    def test_post_proceeds_when_target_not_yet_research_grade(self) -> None:
        from unittest.mock import Mock, patch

        from observation_workbench.services.bulk_disagree import post_bulk_disagreement

        target = StudyTaxon(taxon_id=55, name="Genus species")
        needs_id_obs = StudyObservation(
            obs_id=42,
            observer_login="observer",
            taxon=target,
            community_taxon=target,
            quality_grade="needs_id",
        )
        client = Mock()
        client.create_identification.return_value = {"id": 1}
        with patch(
            "observation_workbench.services.bulk_disagree.refresh_observation",
            return_value=needs_id_obs,
        ):
            post_bulk_disagreement(
                client,
                "token",
                "me",
                self._candidate(),
                require_source_taxon_match=False,
            )

        # Not research grade yet, so an identification is still posted to help
        # push it there.
        client.create_identification.assert_called_once()


class _FakeClient:
    def get_taxa_autocomplete(self, query: str, per_page: int = 10):
        return {"results": []}


class _AutocompleteClient:
    def get_taxa_autocomplete(self, query: str, per_page: int = 10):
        return {
            "results": [
                {
                    "id": 1,
                    "name": "Psathyrella",
                    "rank": "genus",
                },
                {
                    "id": 2,
                    "name": "Psathyrella",
                    "rank": "section",
                },
            ]
        }


class BulkDisagreeSetupDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _dialog(self) -> BulkDisagreeSetupDialog:
        dlg = BulkDisagreeSetupDialog(_FakeClient())
        dlg._source_query = object()
        dlg._comment_edit.setPlainText("A corrective comment.")
        return dlg

    def test_strict_ancestor_target_enables_plan(self) -> None:
        dlg = self._dialog()
        dlg._source_taxon = StudyTaxon(taxon_id=20, name="Genus species", ancestry="1/10")
        dlg._target_taxon_id = 10
        dlg._target_taxon_name = "Genus"

        dlg._update_relationship()
        dlg._update_plan_enabled()

        self.assertTrue(dlg._plan_btn.isEnabled())

    def test_non_ancestor_target_enables_plan_with_warning(self) -> None:
        dlg = self._dialog()
        dlg._source_taxon = StudyTaxon(taxon_id=20, name="Genus species", ancestry="1/10")
        dlg._target_taxon_id = 99
        dlg._target_taxon_name = "Other species"

        dlg._update_relationship()
        dlg._update_plan_enabled()

        # A non-ancestor target still posts (as a normal conflicting ID); the
        # relationship label warns the user but does not block planning.
        self.assertTrue(dlg._plan_btn.isEnabled())
        self.assertIn("ancestor", dlg._relationship_label.text().casefold())

    def test_same_source_and_target_is_rejected(self) -> None:
        dlg = self._dialog()
        dlg._source_taxon = StudyTaxon(taxon_id=20, name="Genus species", ancestry="1/10")
        dlg._target_taxon_id = 20
        dlg._target_taxon_name = "Genus species"

        dlg._update_relationship()
        dlg._update_plan_enabled()

        self.assertFalse(dlg._plan_btn.isEnabled())
        self.assertIn("same", dlg._relationship_label.text().casefold())

    def test_url_without_taxon_id_enables_plan_without_source_taxon(self) -> None:
        dlg = self._dialog()
        # _dialog() leaves _source_taxon = None and _url_has_taxon_id = False,
        # which is the state after validating a URL that has no taxon_id.
        dlg._target_taxon_id = 10
        dlg._target_taxon_name = "Clavaria"

        dlg._update_relationship()
        dlg._update_plan_enabled()

        self.assertIsNone(dlg.source_taxon)
        self.assertFalse(dlg.has_source_taxon())
        self.assertTrue(dlg._plan_btn.isEnabled())
        self.assertIn("no source taxon", dlg._relationship_label.text().casefold())

    def test_provisional_name_url_uses_provisional_source(self) -> None:
        dlg = self._dialog()
        # State after validating a URL filtered by Provisional Species Name and
        # carrying no taxon_id.
        dlg._source_provisional_name = "Hygrocybe sp. 'flavescens-PNW06'"
        dlg._target_taxon_id = 10
        dlg._target_taxon_name = "Hygrocybe"

        dlg._update_relationship()
        dlg._update_plan_enabled()

        self.assertIsNone(dlg.source_taxon)
        self.assertEqual(
            dlg.source_provisional_name(), "Hygrocybe sp. 'flavescens-PNW06'"
        )
        self.assertTrue(dlg._plan_btn.isEnabled())
        self.assertIn("provisional species name", dlg._relationship_label.text().casefold())

    def test_resolved_source_taxon_defaults_into_target(self) -> None:
        dlg = self._dialog()

        dlg._on_source_resolved(StudyTaxon(taxon_id=5, name="Genus"))

        self.assertEqual(dlg._target_taxon_id, 5)
        self.assertEqual(dlg._target_taxon_name, "Genus")
        self.assertEqual(dlg._target_edit.text(), "Genus")

    def test_provisional_name_defaults_into_target_search(self) -> None:
        dlg = self._dialog()

        dlg._maybe_prefill_target_from_source(
            "Hygrocybe sp. 'flavescens-PNW06'", "Hygrocybe sp. 'flavescens-PNW06'"
        )

        # The provisional name is dropped into the search box for autocomplete to
        # resolve; no real taxon is selected yet.
        self.assertEqual(dlg._target_edit.text(), "Hygrocybe sp. 'flavescens-PNW06'")
        self.assertIsNone(dlg._target_taxon_id)

    def test_source_default_does_not_override_existing_target(self) -> None:
        dlg = self._dialog()
        dlg._set_target_taxon("Already chosen", 9)

        dlg._on_source_resolved(StudyTaxon(taxon_id=5, name="Genus"))

        self.assertEqual(dlg._target_taxon_id, 9)
        self.assertEqual(dlg._target_edit.text(), "Already chosen")


class BulkDisagreeAutocompleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_taxon_autocomplete_labels_include_rank(self) -> None:
        worker = _TaxonAutocompleteWorker(
            _AutocompleteClient(),
            "Psathyrella",
            1,
            lambda: 1,
        )
        results = []
        worker.signals.results.connect(results.append)

        worker.run()

        self.assertEqual(
            results,
            [[
                ("Psathyrella", "Psathyrella — Genus", 1, "genus"),
                ("Psathyrella", "Psathyrella — Section", 2, "section"),
            ]],
        )

    def test_alternate_identification_defaults_to_target_taxon_and_blank_comment(self) -> None:
        candidate = type(
            "Candidate",
            (),
            {
                "observation": type("Observation", (), {"obs_id": 123})(),
            },
        )()
        dlg = AlternateIdentificationDialog(
            _FakeClient(),
            candidate,
            default_taxon_id=55,
            default_taxon_name="Psathyrella",
            default_comment="",
        )

        self.assertEqual(dlg.target_taxon_id, 55)
        self.assertEqual(dlg.target_taxon_name, "Psathyrella")
        self.assertEqual(dlg.comment(), "")
        self.assertTrue(dlg._post_btn.isEnabled())


class _FakeDiskCache:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.put_calls = []

    def get(self, photo_id: int, size: str):
        return self.data

    def put(self, photo_id: int, size: str, data: bytes, ext: str = "jpg") -> None:
        self.put_calls.append((photo_id, size, data, ext))


class _UnusedImageClient:
    def download_image(self, url: str) -> bytes:
        raise AssertionError("cached image bytes should be used")


class GalleryImageWorkerTests(unittest.TestCase):
    def test_worker_emits_raw_bytes_without_decoding_pixmap(self) -> None:
        image_bytes = b"not a decodable image"
        worker = _GalleryImageWorker(
            obs_id=1,
            photo_id=2,
            image_url="https://example.invalid/photo.jpg",
            client=_UnusedImageClient(),
            disk_cache=_FakeDiskCache(image_bytes),
        )
        loaded = []
        failed = []
        worker.signals.loaded.connect(lambda obs_id, photo_id, data: loaded.append((obs_id, photo_id, data)))
        worker.signals.failed.connect(lambda obs_id, photo_id, msg: failed.append((obs_id, photo_id, msg)))

        worker.run()

        self.assertEqual(loaded, [(1, 2, image_bytes)])
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
