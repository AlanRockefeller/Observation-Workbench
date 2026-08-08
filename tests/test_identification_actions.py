import unittest

from observation_workbench.models import (
    StudyComment,
    StudyIdentification,
    StudyObservation,
    StudyTaxon,
)
from observation_workbench.services.identification_actions import (
    AgreeTarget,
    needs_human_review,
)


class NeedsHumanReviewTests(unittest.TestCase):
    def _make_observation(self, comments):
        taxon = StudyTaxon(taxon_id=1, name="Genus species'")
        ident = StudyIdentification(
            ident_id=10,
            taxon=taxon,
            user_login="other_user",
            created_at="2026-05-01T17:00:00+00:00",
            current=True,
        )
        return StudyObservation(
            obs_id=1,
            observer_login="observer",
            all_identifications=[ident],
            comments=comments,
        )

    def test_comment_before_provisional_name_does_not_trigger_pause(self) -> None:
        obs = self._make_observation(
            [
                StudyComment(
                    comment_id=1,
                    user_login="commenter",
                    created_at="2026-05-01T16:30:00+00:00",
                )
            ]
        )
        target = AgreeTarget(
            observation_id=1,
            taxon_id=1,
            taxon_name="Genus species",
            source_created_at="2026-05-01T17:00:00+00:00",
        )

        self.assertEqual(needs_human_review(obs, target, "me"), (False, False))

    def test_comment_after_provisional_name_triggers_pause(self) -> None:
        obs = self._make_observation(
            [
                StudyComment(
                    comment_id=1,
                    user_login="commenter",
                    created_at="2026-05-01T17:30:00+00:00",
                )
            ]
        )
        target = AgreeTarget(
            observation_id=1,
            taxon_id=1,
            taxon_name="Genus species",
            source_created_at="2026-05-01T17:00:00+00:00",
        )

        self.assertEqual(needs_human_review(obs, target, "me"), (False, True))

    def test_hidden_comment_is_ignored_for_pause_logic(self) -> None:
        obs = self._make_observation(
            [
                StudyComment(
                    comment_id=1,
                    user_login="moderator",
                    created_at="2026-05-01T17:30:00+00:00",
                    hidden=True,
                )
            ]
        )
        target = AgreeTarget(
            observation_id=1,
            taxon_id=1,
            taxon_name="Genus species",
            source_created_at="2026-05-01T17:00:00+00:00",
        )

        self.assertEqual(needs_human_review(obs, target, "me"), (False, False))


if __name__ == "__main__":
    unittest.main()
