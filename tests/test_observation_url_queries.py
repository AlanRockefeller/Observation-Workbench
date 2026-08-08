import unittest

from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    extract_optional_single_taxon_id_from_observation_query,
    extract_provisional_species_name_from_observation_query,
    parse_observations_url,
)
from observation_workbench.services.study_loader import LoadFilters, StudyLoader


class ObservationURLParserTests(unittest.TestCase):
    def test_observations_url_keeps_blank_field_filter_and_removes_paging(self) -> None:
        query = parse_observations_url(
            "https://www.inaturalist.org/observations?"
            "place_id=14&taxon_id=63421&field:DNA%20Barcode%20ITS=&page=3&per_page=30"
        )

        self.assertIsNotNone(query)
        assert query is not None
        self.assertEqual(
            query.params,
            [
                ("place_id", "14"),
                ("taxon_id", "63421"),
                ("field:DNA Barcode ITS", ""),
            ],
        )
        self.assertNotIn("page=", query.source_key)
        self.assertNotIn("per_page=", query.source_key)

    def test_non_observations_url_is_rejected(self) -> None:
        with self.assertRaises(ObservationURLParseError):
            parse_observations_url("https://www.inaturalist.org/taxa/63421")

    def test_optional_taxon_id_returns_none_when_url_has_no_taxon(self) -> None:
        query = parse_observations_url(
            "https://www.inaturalist.org/observations?"
            "verifiable=any&place_id=any&field:Provisional%20Species%20Name=Clavaria%20sp."
        )
        assert query is not None
        self.assertIsNone(
            extract_optional_single_taxon_id_from_observation_query(query)
        )

    def test_optional_taxon_id_returns_single_value(self) -> None:
        query = parse_observations_url(
            "https://www.inaturalist.org/observations?taxon_id=63421"
        )
        assert query is not None
        self.assertEqual(
            extract_optional_single_taxon_id_from_observation_query(query), 63421
        )

    def test_optional_taxon_id_rejects_multiple_taxa(self) -> None:
        query = parse_observations_url(
            "https://www.inaturalist.org/observations?taxon_id=1&taxon_id=2"
        )
        assert query is not None
        with self.assertRaises(ValueError):
            extract_optional_single_taxon_id_from_observation_query(query)

    def test_extracts_provisional_species_name_from_field_filter(self) -> None:
        query = parse_observations_url(
            "https://www.inaturalist.org/observations?verifiable=any&place_id=any&"
            "field:Provisional%20Species%20Name=Hygrocybe%20sp.%20%27flavescens-PNW06%27"
        )
        assert query is not None
        self.assertEqual(
            extract_provisional_species_name_from_observation_query(query),
            "Hygrocybe sp. 'flavescens-PNW06'",
        )

    def test_provisional_species_name_absent_returns_none(self) -> None:
        query = parse_observations_url(
            "https://www.inaturalist.org/observations?taxon_id=63421"
        )
        assert query is not None
        self.assertIsNone(
            extract_provisional_species_name_from_observation_query(query)
        )


class _FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def get_observations(self, query_params, page, per_page):
        self.calls.append((query_params, page, per_page))
        return {
            "total_results": 1,
            "results": [
                {
                    "id": 42,
                    "user": {"login": "observer"},
                    "observed_on": "2024-02-03",
                    "place_guess": "San Diego County, CA",
                    "taxon": {"id": 1, "name": "Agaricus", "rank": "genus"},
                    "community_taxon": {
                        "id": 2,
                        "name": "Agaricus campestris",
                        "rank": "species",
                    },
                    "photos": [
                        {
                            "id": 99,
                            "url": "https://static.inaturalist.org/photos/99/square.jpg",
                        }
                    ],
                    "quality_grade": "research",
                }
            ],
        }


class _FakeDB:
    def __init__(self) -> None:
        self.cached = None

    def make_observation_query_key(self, source_key, page, per_page):
        return f"{source_key}:{page}:{per_page}"

    def get_query_cache(self, cache_key):
        return None

    def set_query_cache(self, cache_key, results, total):
        self.cached = (cache_key, results, total)


class StudyLoaderObservationURLTests(unittest.TestCase):
    def test_loader_fetches_observations_for_url_mode(self) -> None:
        client = _FakeClient()
        db = _FakeDB()
        filters = LoadFilters(
            "https://www.inaturalist.org/observations?"
            "place_id=14&taxon_id=63421&field:DNA%20Barcode%20ITS=",
            place_id=999,
            taxon_id=888,
            leading_only=True,
            rank_level=10,
        )

        observations, total = StudyLoader(client, db).load_page(
            filters,
            page=1,
            per_page=30,
        )

        self.assertEqual(total, 1)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].obs_id, 42)
        self.assertIsNone(observations[0].target_identification)
        self.assertEqual(observations[0].display_taxon.name, "Agaricus campestris")
        self.assertEqual(
            client.calls,
            [
                (
                    [
                        ("place_id", "14"),
                        ("field:DNA Barcode ITS", ""),
                        ("taxon_id", "888"),
                    ],
                    1,
                    30,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
