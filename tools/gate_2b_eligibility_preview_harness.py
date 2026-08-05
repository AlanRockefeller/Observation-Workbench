#!/usr/bin/env python3
"""Gate 2B M2/M3 offline smoke harness — no network, no live writes.

Exercises ``observation_workbench.reconciliation.consolidation`` (duplicate-set
eligibility + canonical-selection preview) and the new ``ReconciliationDB``
consolidation accessors against a disposable in-memory-shaped sqlite file.
Fresh reads are simulated by constructing raw payload dicts directly (the
same shape ``INatReconciliationReader.parse_inventory``/
``mo_parsing.parse_mo_observation`` accept) rather than making real HTTP
calls — this module never performs the fetch itself (that's the
coordinator's job), so it is fully testable this way.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observation_workbench.reconciliation.consolidation import (  # noqa: E402
    ConsolidationError, _evidence_unavailable_notices, _hydrate_candidate,
    _supporting_evidence, check_duplicate_set_eligibility, prepare_preview,
    select_canonical,
)
from observation_workbench.reconciliation.db import ReconciliationDB  # noqa: E402
from observation_workbench.reconciliation.inat_reader import INatReconciliationReader  # noqa: E402
from observation_workbench.reconciliation.types import ReconciliationProfile, RemoteSite  # noqa: E402

PROFILE_ID = 1
INAT_USER_ID = 500
MO_USER_ID = 900


def _profile() -> ReconciliationProfile:
    return ReconciliationProfile(
        profile_id=PROFILE_ID, inat_user_id=INAT_USER_ID, inat_login="inat_user",
        mo_user_id=MO_USER_ID, mo_login="mo_user", created_at="", last_used_at="",
    )


def _inat_raw(obs_id: int, *, voucher: str = "", collection: str = "",
              photo_id: str = "", taxon_id: int = 47000,
              taxon_name: str = "Amanita muscaria", observed_on: str = "2026-06-01",
              owner_id: int = INAT_USER_ID, latitude: float | None = None,
              longitude: float | None = None, accuracy: float | None = None) -> dict:
    ofvs = []
    if voucher:
        ofvs.append({"observation_field": {"name": "Voucher Number"}, "value": voucher})
    if collection:
        ofvs.append({"observation_field": {"name": "Collection Number"}, "value": collection})
    photos = []
    if photo_id:
        photos.append({"photo": {"id": photo_id, "url": f"https://x/{photo_id}.jpg"}})
    return {
        "id": obs_id, "uuid": f"uuid-inat-{obs_id}",
        "user": {"id": owner_id, "login": "inat_user"},
        "taxon": {"id": taxon_id, "name": taxon_name, "rank": "species", "iconic_taxon_name": "Fungi"},
        "observed_on": observed_on, "updated_at": "2026-07-01T00:00:00Z",
        "place_guess": "Some Forest", "ofvs": ofvs, "observation_photos": photos,
        "geoprivacy": "open",
        "geojson": (
            {"type": "Point", "coordinates": [longitude, latitude]}
            if latitude is not None and longitude is not None else None
        ),
        "positional_accuracy": accuracy,
    }


def _mo_raw(obs_id: int, *, voucher: str = "", photo_id: str = "", taxon_name: str = "Amanita muscaria",
            observed_on: str = "2026-06-01", owner_id: int = MO_USER_ID,
            collection: str = "", latitude: float | None = None,
            longitude: float | None = None, accuracy: float | None = None) -> dict:
    collection_numbers = [{"number": collection}] if collection else []
    herbarium_records = [{"accession_number": voucher}] if voucher else []
    images = [{"id": photo_id, "url": f"https://y/{photo_id}.jpg"}] if photo_id else []
    return {
        "id": obs_id, "owner_id": owner_id, "owner": {"login": "mo_user"},
        "consensus": {"id": 111, "text_name": taxon_name, "rank": "Species", "classification": {"kingdom": "Fungi"}},
        "location": {"id": 5, "name": "Some Forest"}, "date": observed_on,
        "updated_at": "2026-07-01T00:00:00Z", "collection_numbers": collection_numbers,
        "herbarium_records": herbarium_records, "images": images,
        "latitude": latitude, "longitude": longitude, "gps_accuracy": accuracy,
    }


def _pair(inat_raw: dict, mo_raw: dict, reader: INatReconciliationReader, profile: ReconciliationProfile):
    from observation_workbench.reconciliation.consolidation import _hydrate_candidate
    return _hydrate_candidate(RemoteSite.INAT, inat_raw["id"], inat_raw, profile, reader), \
        _hydrate_candidate(RemoteSite.MO, mo_raw["id"], mo_raw, profile, reader)


def _eligibility(db, profile, hydrated):
    return check_duplicate_set_eligibility(
        db, profile, hydrated,
        evidence_edges=_supporting_evidence(hydrated),
    )


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    db = ReconciliationDB(path=db_path)
    profile = _profile()
    reader = INatReconciliationReader(client=None)  # parse_inventory never touches self.client

    db.connection().execute(
        "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,mo_user_id,mo_login,"
        "created_at,last_used_at) VALUES(?,?,?,?,?,?,?)",
        (PROFILE_ID, INAT_USER_ID, "inat_user", MO_USER_ID, "mo_user", "", ""),
    )

    # Scenario 1: strong supporting evidence (matching voucher) -> eligible.
    inat1 = _inat_raw(101, voucher="AR-001")
    mo1 = _mo_raw(201, voucher="AR-001")
    mo1_donor = _mo_raw(202, voucher="AR-001")
    (inv_i1, hyd_i1), (inv_m1, hyd_m1) = _pair(inat1, mo1, reader, profile)
    inv_m1d, hyd_m1d = _hydrate_candidate(
        RemoteSite.MO, 202, mo1_donor, profile, reader
    )
    elig1 = _eligibility(
        db, profile, [(inv_i1, hyd_i1), (inv_m1, hyd_m1), (inv_m1d, hyd_m1d)]
    )
    assert elig1.eligible, elig1.blocking_reasons
    assert elig1.supporting_evidence
    preview1 = prepare_preview(db, profile, [
        (RemoteSite.INAT, 101, inat1), (RemoteSite.MO, 201, mo1),
        (RemoteSite.MO, 202, mo1_donor),
    ], reader)
    assert preview1.canonical_mo_observation_id is None
    assert preview1.canonical_inat_observation_id is None
    assert all(not item.enabled for item in preview1.unsupported_items)
    print("scenario 1 (strong evidence, eligible): PASS")

    # Scenario 2: only taxon similarity -> blocked.
    inat2 = _inat_raw(102, taxon_name="Amanita muscaria", observed_on="2026-06-01")
    mo2 = _mo_raw(203, taxon_name="Amanita muscaria", observed_on="2026-06-01")
    mo2_donor = _mo_raw(204, taxon_name="Amanita muscaria", observed_on="2026-06-01")
    (inv_i2, hyd_i2), (inv_m2, hyd_m2) = _pair(inat2, mo2, reader, profile)
    inv_m2d, hyd_m2d = _hydrate_candidate(
        RemoteSite.MO, 204, mo2_donor, profile, reader
    )
    elig2 = _eligibility(
        db, profile, [(inv_i2, hyd_i2), (inv_m2, hyd_m2), (inv_m2d, hyd_m2d)]
    )
    assert not elig2.eligible
    assert any(
        "no strong identity evidence" in r
        for r in elig2.blocking_reasons
    )
    print("scenario 2 (taxon-only, blocked): PASS")

    # Graph negatives: a strong pair never admits an unsupported third node,
    # and two internally strong components never become one specimen.
    strong_pair_plus_extra = [
        _hydrate_candidate(
            RemoteSite.INAT, 110, _inat_raw(110, voucher="GRAPH-A"),
            profile, reader,
        ),
        _hydrate_candidate(
            RemoteSite.MO, 210, _mo_raw(210, voucher="GRAPH-A"),
            profile, reader,
        ),
        _hydrate_candidate(
            RemoteSite.MO, 211, _mo_raw(211),
            profile, reader,
        ),
    ]
    unsupported = _eligibility(db, profile, strong_pair_plus_extra)
    assert not unsupported.eligible
    assert any(
        "connected specimen-evidence graph" in reason
        for reason in unsupported.blocking_reasons
    )

    disconnected = [
        _hydrate_candidate(
            RemoteSite.INAT, 120, _inat_raw(120, voucher="CLUSTER-A"),
            profile, reader,
        ),
        _hydrate_candidate(
            RemoteSite.MO, 220, _mo_raw(220, voucher="CLUSTER-A"),
            profile, reader,
        ),
        _hydrate_candidate(
            RemoteSite.INAT, 121, _inat_raw(121, voucher="CLUSTER-B"),
            profile, reader,
        ),
        _hydrate_candidate(
            RemoteSite.MO, 221, _mo_raw(221, voucher="CLUSTER-B"),
            profile, reader,
        ),
    ]
    assert not _eligibility(db, profile, disconnected).eligible

    # These display similarities never produce graph edges.
    taxon_left = _mo_raw(230, taxon_name="Same taxon", observed_on="2026-06-01")
    taxon_right = _mo_raw(231, taxon_name="Same taxon", observed_on="2026-06-04")
    taxon_right["location"]["name"] = "Different Forest"
    date_left = _mo_raw(235, taxon_name="Taxon one", observed_on="2026-06-01")
    date_right = _mo_raw(236, taxon_name="Taxon two", observed_on="2026-06-01")
    date_right["location"]["name"] = "Different Forest"
    locality_left = _mo_raw(237, taxon_name="Taxon one", observed_on="2026-06-01")
    locality_right = _mo_raw(238, taxon_name="Taxon two", observed_on="2026-06-04")
    similarity_cases = {
        "taxon": (taxon_left, taxon_right),
        "date": (date_left, date_right),
        "broad locality": (locality_left, locality_right),
        "no-conflict similarity": (_mo_raw(239), _mo_raw(243)),
    }
    for label, raws in similarity_cases.items():
        hydrated_case = [
            _hydrate_candidate(
                RemoteSite.MO, raw["id"], raw, profile, reader,
            )
            for raw in raws
        ]
        assert not _supporting_evidence(hydrated_case), f"{label} created an edge"
    malformed_base = _supporting_evidence(strong_pair_plus_extra[:2])[0]
    malformed = replace(malformed_base, reviewed_evidence_fingerprint="")
    malformed_result = check_duplicate_set_eligibility(
        db, profile, strong_pair_plus_extra[:2],
        evidence_edges=(malformed,),
    )
    assert not malformed_result.eligible
    assert any("incomplete evidence" in reason for reason in malformed_result.blocking_reasons)
    forged_type = replace(
        malformed_base,
        evidence_type="made_up_proof",
        evidence_strength="strong",
    )
    forged_result = check_duplicate_set_eligibility(
        db, profile, strong_pair_plus_extra[:2],
        evidence_edges=(forged_type,),
    )
    assert not forged_result.eligible
    assert any(
        "unsupported evidence type" in reason
        for reason in forged_result.blocking_reasons
    )
    wrong_strength = replace(
        malformed_base,
        evidence_strength="corroborating",
    )
    wrong_strength_result = check_duplicate_set_eligibility(
        db, profile, strong_pair_plus_extra[:2],
        evidence_edges=(wrong_strength,),
    )
    assert not wrong_strength_result.eligible
    assert any(
        "must have strength" in reason
        for reason in wrong_strength_result.blocking_reasons
    )
    print(
        "graph negatives (unsupported member / clusters / similarity-only / "
        "malformed / forged type / wrong strength): PASS"
    )

    # Multi-hop positive: member 232 shares a voucher with 233; 233 shares an
    # exact native photo identity with 234. There is no direct 232↔234 edge.
    mo232 = _mo_raw(232, voucher="PATH-A")
    mo233 = _mo_raw(233, voucher="PATH-A", photo_id=999)
    mo234 = _mo_raw(234, photo_id=999)
    path_preview = prepare_preview(
        db, profile,
        [
            (RemoteSite.MO, 232, mo232),
            (RemoteSite.MO, 233, mo233),
            (RemoteSite.MO, 234, mo234),
        ],
        reader,
    )
    path_selected = select_canonical(path_preview, 232, None)
    donor_234 = next(
        path for path in path_selected.donor_evidence_paths
        if path.donor_observation_id == 234
    )
    assert len(donor_234.steps) == 2

    # The full graph can be connected while a selected cross-site canonical
    # pair lacks a direct evidence edge; that choice must be rejected.
    mo240 = _mo_raw(240, voucher="CANON-A")
    mo241 = _mo_raw(241, voucher="CANON-B")
    mo242 = _mo_raw(242)
    mo242["herbarium_records"] = [
        {"accession_number": "CANON-A"}, {"accession_number": "CANON-B"},
    ]
    canonical_preview = prepare_preview(
        db, profile,
        [
            (RemoteSite.MO, 240, mo240),
            (RemoteSite.MO, 241, mo241),
            (RemoteSite.MO, 242, mo242),
            (RemoteSite.INAT, 140, _inat_raw(140, voucher="CANON-A")),
        ],
        reader,
    )
    bad_choice = select_canonical(canonical_preview, 241, 140)
    assert not bad_choice.eligibility.eligible
    assert any(
        "direct reviewed specimen-identity edge" in reason
        for reason in bad_choice.eligibility.blocking_reasons
    )
    print("graph positives (connected voucher/media path) and canonical-dependent block: PASS")

    # Scenario 3: owner mismatch -> blocked.
    inat3 = _inat_raw(103, voucher="AR-003", owner_id=999999)
    mo3 = _mo_raw(205, voucher="AR-003")
    mo3_donor = _mo_raw(206, voucher="AR-003")
    (inv_i3, hyd_i3), (inv_m3, hyd_m3) = _pair(inat3, mo3, reader, profile)
    inv_m3d, hyd_m3d = _hydrate_candidate(
        RemoteSite.MO, 206, mo3_donor, profile, reader
    )
    elig3 = _eligibility(
        db, profile, [(inv_i3, hyd_i3), (inv_m3, hyd_m3), (inv_m3d, hyd_m3d)]
    )
    assert not elig3.eligible
    assert any("not owned by the profile" in r for r in elig3.blocking_reasons)
    print("scenario 3 (owner mismatch, blocked): PASS")

    # Scenario 4/5: select_canonical validation + donor recomputation.
    try:
        select_canonical(
            preview1, canonical_mo_observation_id=999999,
            canonical_inat_observation_id=101,
        )
        raise AssertionError("expected ConsolidationError for out-of-set canonical id")
    except ConsolidationError as exc:
        assert exc.code == "canonical_not_in_set"
    print("scenario 4 (canonical not in set, raises): PASS")

    confirmed = select_canonical(preview1, canonical_mo_observation_id=201, canonical_inat_observation_id=101)
    assert confirmed.canonical_mo_observation_id == 201
    assert confirmed.canonical_inat_observation_id == 101
    assert len(confirmed.donor_members) == 1
    assert len(confirmed.canonical_members) == 2
    assert any("become the canonical pair" in c for c in confirmed.local_changes_preview)
    print("scenario 5 (canonical selected, donor list recomputed): PASS")

    # Scenario 6: a candidate already belongs to another unresolved consolidation.
    consolidation_id = db.create_consolidation_with_canonical(
        PROFILE_ID, [("inat", 101), ("mo", 201), ("mo", 202)],
        canonical_mo_observation_id=201, canonical_inat_observation_id=101,
    )
    assert consolidation_id > 0
    membership = db.consolidation_membership_for_observation(PROFILE_ID, "inat", 101)
    assert membership is not None and int(membership["consolidation_id"]) == consolidation_id

    inat4 = _inat_raw(101, voucher="AR-001")  # same #101, now already a member
    mo4 = _mo_raw(207, voucher="AR-001")
    mo4_donor = _mo_raw(208, voucher="AR-001")
    (inv_i4, hyd_i4), (inv_m4, hyd_m4) = _pair(inat4, mo4, reader, profile)
    inv_m4d, hyd_m4d = _hydrate_candidate(
        RemoteSite.MO, 208, mo4_donor, profile, reader
    )
    elig4 = _eligibility(
        db, profile, [(inv_i4, hyd_i4), (inv_m4, hyd_m4), (inv_m4d, hyd_m4d)]
    )
    assert not elig4.eligible
    assert any("already belongs to consolidation" in r for r in elig4.blocking_reasons)
    print("scenario 6 (already in unresolved consolidation, blocked pre-DB-constraint): PASS")

    # Schema-level backstop still fires when a pre-check is bypassed.
    try:
        db.create_consolidation_with_canonical(
            PROFILE_ID, [("inat", 101), ("mo", 207), ("mo", 208)],
            canonical_mo_observation_id=207, canonical_inat_observation_id=101,
        )
        raise AssertionError("expected sqlite3.IntegrityError for duplicate membership")
    except sqlite3.IntegrityError:
        pass
    print("schema backstop (duplicate membership -> IntegrityError): PASS")

    # Scenario 7: a same-site set whose canonical carries a real cross-site
    # authoritative link. Every other case here hydrates with the default
    # inat_mo_field_id=None, so no AuthoritativeLinkRow is ever produced and
    # the reciprocal-link evidence branch is never entered at all.
    inat_linked = _inat_raw(301, voucher="AR-700")
    inat_linked["ofvs"].append({
        "id": 5001, "uuid": "ofv-5001", "field_id": 77,
        "value": "https://mushroomobserver.org/obs/900",
    })
    inat_dupe = _inat_raw(302, voucher="AR-700")
    inv_l, hyd_l = _hydrate_candidate(
        RemoteSite.INAT, 301, inat_linked, profile, reader, inat_mo_field_id=77,
    )
    inv_d, hyd_d = _hydrate_candidate(
        RemoteSite.INAT, 302, inat_dupe, profile, reader, inat_mo_field_id=77,
    )
    same_site = [(inv_l, hyd_l), (inv_d, hyd_d)]
    # Reaching this at all requires the inventory-link fallback to expose the
    # snapshot shape (fingerprint vs row_fingerprint).
    same_site_edges = _supporting_evidence(same_site)
    assert any(e.evidence_type == "exact_voucher" for e in same_site_edges), same_site_edges

    same_site_preview = prepare_preview(
        db, profile,
        [(RemoteSite.INAT, 301, inat_linked), (RemoteSite.INAT, 302, inat_dupe)],
        reader, inat_mo_field_id=77,
    )
    assert same_site_preview.eligibility.eligible, (
        same_site_preview.eligibility.blocking_reasons
    )
    # The linked record stays canonical: nothing is overwritten, and no link is
    # written at all for a same-site set.
    canonical_linked = select_canonical(same_site_preview, None, 301)
    assert canonical_linked.eligibility.eligible, (
        canonical_linked.eligibility.blocking_reasons
    )
    print("scenario 7 (same-site set with linked canonical, allowed): PASS")

    # Scenario 8: the same record may NOT become a donor while it is still
    # confirmed-paired to an observation outside the set.
    paired_preview = replace(
        same_site_preview,
        members=tuple(
            replace(
                member,
                current_pair_partner_site=RemoteSite.MO,
                current_pair_partner_id=900,
                current_pair_review_state="confirmed",
            ) if member.observation_id == 301 else member
            for member in same_site_preview.members
        ),
    )
    demoted = select_canonical(paired_preview, None, 302)
    assert not demoted.eligibility.eligible
    assert any(
        "still confirmed-paired" in r for r in demoted.eligibility.blocking_reasons
    ), demoted.eligibility.blocking_reasons
    # ...but staying canonical remains fine.
    kept = select_canonical(paired_preview, None, 301)
    assert kept.eligibility.eligible, kept.eligibility.blocking_reasons
    print("scenario 8 (confirmed-paired record may not be demoted to donor): PASS")

    # Scenario 9: every coordinate/date rejection explains itself.
    near = _inat_raw(
        401, taxon_name="Amanita muscaria", latitude=47.1, longitude=-122.1, accuracy=10,
    )
    far = _inat_raw(
        402, taxon_name="Amanita muscaria", latitude=47.1045, longitude=-122.1, accuracy=10,
    )
    inv_n, hyd_n = _hydrate_candidate(RemoteSite.INAT, 401, near, profile, reader)
    inv_f, hyd_f = _hydrate_candidate(RemoteSite.INAT, 402, far, profile, reader)
    distant = [(inv_n, hyd_n), (inv_f, hyd_f)]
    assert not any(
        e.evidence_type == "exact_date_close_coordinates"
        for e in _supporting_evidence(distant)
    )
    notices = _evidence_unavailable_notices(distant)
    assert any("beyond the 100 m corroboration threshold" in n for n in notices), notices
    print("scenario 9 (distance rejection is explained, not silent): PASS")

    integrity = db.connection().execute("PRAGMA integrity_check").fetchone()[0]
    fk = db.connection().execute("PRAGMA foreign_key_check").fetchall()
    assert integrity == "ok" and not fk, (integrity, fk)

    print("ALL GATE 2B-M2/M3 SMOKE CHECKS PASSED")
    db.close_thread_connection()
    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
