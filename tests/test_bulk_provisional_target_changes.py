import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from observation_workbench.models import StudyIdentification, StudyObservation, StudyTaxon
from observation_workbench.services.bulk_identification import BulkAgreeCandidate
from observation_workbench.services.identification_actions import AgreeResult, AgreeTarget
from observation_workbench.ui import main_window


class BulkProvisionalTargetChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _make_obs(self, obs_id: int, taxon_id: int, taxon_name: str, login: str, ident_id: int):
        taxon = StudyTaxon(taxon_id=taxon_id, name=taxon_name)
        ident = StudyIdentification(
            ident_id=ident_id,
            taxon=taxon,
            user_login=login,
            created_at="2026-05-03T12:00:00+00:00",
            current=True,
        )
        return StudyObservation(
            obs_id=obs_id,
            observer_login="observer",
            all_identifications=[ident],
        )

    def test_source_change_only_posts_without_changed_dialog(self) -> None:
        preview_obs = self._make_obs(1, 123, "Amanita sp. 'kryorhodon'", "nschwab", 111)
        fresh_obs = self._make_obs(1, 123, "Amanita sp. 'kryorhodon'", "morphie", 222)
        candidate = BulkAgreeCandidate(
            observation=preview_obs,
            target=AgreeTarget(
                observation_id=1,
                taxon_id=123,
                taxon_name="Amanita sp. 'kryorhodon'",
                source_login="nschwab",
                source_ident_id=111,
            ),
        )
        worker = main_window._BulkPostWorker(
            client=Mock(),
            token="token",
            login="me",
            candidate=candidate,
        )
        results = []
        worker.signals.finished.connect(lambda candidate, result: results.append((candidate, result)))

        with patch("observation_workbench.ui.main_window.refresh_observation", return_value=fresh_obs), patch(
            "observation_workbench.ui.main_window.post_agreement",
            return_value=AgreeResult(
                "posted",
                "Added identification: Amanita sp. 'kryorhodon'",
                target=AgreeTarget(
                    observation_id=1,
                    taxon_id=123,
                    taxon_name="Amanita sp. 'kryorhodon'",
                    source_login="morphie",
                    source_ident_id=222,
                ),
                refreshed_observation=fresh_obs,
            ),
        ) as post_mock:
            worker.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1].status, "posted")
        self.assertEqual(post_mock.call_count, 1)
        posted_target = post_mock.call_args[0][4]
        self.assertEqual(posted_target.taxon_id, 123)
        self.assertEqual(posted_target.source_ident_id, 222)

    def test_taxon_change_still_emits_changed_result(self) -> None:
        preview_obs = self._make_obs(1, 123, "Amanita sp. 'kryorhodon'", "nschwab", 111)
        fresh_obs = self._make_obs(1, 456, "Amanita sp. 'other'", "morphie", 222)
        candidate = BulkAgreeCandidate(
            observation=preview_obs,
            target=AgreeTarget(
                observation_id=1,
                taxon_id=123,
                taxon_name="Amanita sp. 'kryorhodon'",
                source_login="nschwab",
                source_ident_id=111,
            ),
        )
        worker = main_window._BulkPostWorker(
            client=Mock(),
            token="token",
            login="me",
            candidate=candidate,
        )
        results = []
        worker.signals.finished.connect(lambda candidate, result: results.append(result))

        with patch("observation_workbench.ui.main_window.refresh_observation", return_value=fresh_obs), patch(
            "observation_workbench.ui.main_window.post_agreement"
        ) as post_mock:
            worker.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "changed")
        post_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
