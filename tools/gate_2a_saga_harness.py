#!/usr/bin/env python3
"""Gate 2A M8 — live saga harness. Run this yourself, against a disposable record.

M0's harness (``gate_2a_proof_harness.py``) only proved the raw create/delete/
marker-search HTTP primitives. This harness drives the ACTUAL saga service
code path — ``observation_workbench.reconciliation.observation_creation
.ObservationCreationService`` — end to end, through a real (but temporary,
throwaway) ``ReconciliationDB``, exactly the way the coordinator drives it in
the real app. It never touches the real app's database or QSettings.

It performs REAL WRITES: it creates a genuinely new destination observation
from a source observation you name explicitly. Guard rails, same as the other
two Gate 1E/2A harnesses:

* Dry-run is the default. Writes require ``--run --i-own-these-records``.
* The source observation is named explicitly on the command line — nothing is
  discovered or enumerated.
* Credentials come from the environment (``INAT_JWT``, ``MO_API_KEY``) or a
  hidden prompt.
* A throwaway sqlite file (deleted afterward unless ``--keep-db``) is used —
  never the real app database.
* Cleanup deletes the created destination observation via the app's own
  ``delete_observation_v2`` (iNat) — Mushroom Observer's created observation is
  reported for manual deletion if MO ever proves harder to delete
  programmatically than the M0 proof already showed it to be.

Usage
-----
    export INAT_JWT='...'
    export MO_API_KEY='...'
    python tools/gate_2a_saga_harness.py --source-site mo --source-id 656464 \
        --run --i-own-these-records

    # Prove the outcome_unknown recovery path (never re-sends the create):
    python tools/gate_2a_saga_harness.py --source-site mo --source-id 656464 \
        --run --i-own-these-records --simulate-lost-response

    # Also exercise ONE selected photo population item (MO->iNat only):
    python tools/gate_2a_saga_harness.py --source-site mo --source-id 656464 \
        --run --i-own-these-records --with-photo-item
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observation_workbench.api.auth import AuthState  # noqa: E402
from observation_workbench.api.client import INatAPIError, INatClient  # noqa: E402
from observation_workbench.reconciliation.actions import LinkRepairService  # noqa: E402
from observation_workbench.reconciliation.db import ReconciliationDB  # noqa: E402
from observation_workbench.reconciliation.inat_reader import (
    INatReconciliationReader,
)  # noqa: E402
from observation_workbench.reconciliation.mo_client import (
    MOAPIError,
    MOClient,
)  # noqa: E402
from observation_workbench.reconciliation.mo_parsing import (
    parse_mo_observation,
)  # noqa: E402
from observation_workbench.reconciliation.observation_creation import (  # noqa: E402
    ObservationCreationError,
    ObservationCreationService,
)
from observation_workbench.reconciliation.photos import PhotoSyncService  # noqa: E402


def _first(payload: object) -> dict:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            return results[0] if isinstance(results[0], dict) else {}
    return {}


class LostResponseINat(INatClient):
    """Wraps create_observation_v2 to send the real request, then discard the
    response and raise as if it were lost — proving the outcome_unknown/
    verify_unknown recovery path never re-sends the create."""

    def create_observation_v2(self, *args, **kwargs):  # type: ignore[override]
        super().create_observation_v2(*args, **kwargs)  # real write happens
        raise INatAPIError(
            "Simulated lost response (--simulate-lost-response).",
            endpoint="/observations",
            request_phase="unsafe_write",
            method="POST",
        )


class LostResponseMO(MOClient):
    def create_observation(self, *args, **kwargs):  # type: ignore[override]
        super().create_observation(*args, **kwargs)  # real write happens
        raise MOAPIError(
            "observations",
            None,
            "Simulated lost response (--simulate-lost-response).",
            response_received=False,
            outcome_unknown=True,
            error_code="simulated_lost_response",
        )


def run(args: argparse.Namespace) -> int:
    inat_jwt = os.environ.get("INAT_JWT", "") or getpass.getpass(
        "iNaturalist JWT (hidden): "
    )
    if not inat_jwt.strip():
        print("INAT_JWT is required.", file=sys.stderr)
        return 2

    # MO_API_KEY is only needed pre-`--run` when MO is the source (its owner
    # verification requires it); once `--run` is set it is always needed, for
    # both directions, to actually execute the saga. A dry run with iNat as
    # the source can print the plan without ever prompting for it.
    mo_key = ""
    if args.source_site == "mo" or args.run:
        mo_key = os.environ.get("MO_API_KEY", "") or getpass.getpass(
            "Mushroom Observer API key (hidden): "
        )
        if not mo_key.strip():
            print("MO_API_KEY is required.", file=sys.stderr)
            return 2

    inat_client_cls = (
        LostResponseINat
        if args.simulate_lost_response and args.source_site == "mo"
        else INatClient
    )
    mo_client_cls = (
        LostResponseMO
        if args.simulate_lost_response and args.source_site == "inat"
        else MOClient
    )
    inat_client = inat_client_cls()
    mo_client = mo_client_cls()

    who_payload = (
        inat_client._request_v2_auth(  # noqa: SLF001 - harness-only introspection
            "GET",
            "/users/me",
            inat_jwt,
            params={"fields": "(id:!t,login:!t)"},
        )
    )
    inat_me = _first(who_payload)
    if not inat_me:
        print("Could not authenticate to iNaturalist.", file=sys.stderr)
        return 2
    inat_user_id, inat_login = int(inat_me["id"]), str(inat_me["login"])

    cancelled = lambda: False  # noqa: E731

    # --- read the named source observation FIRST. MO has no "whoami" endpoint
    # (GET /api2/users with just an api_key is NOT a current-user lookup — it
    # returns an unrelated public user record); the real app never derives
    # mo_user_id that way either (authenticated_user_id VERIFIES a key against
    # an ALREADY-KNOWN profile.mo_user_id, it does not discover it). When the
    # source is MO, its owner IS the account this harness's profile
    # represents. When the destination is MO (source is iNat), --mo-login is
    # required and resolved via the real resolve_user lookup. ---------------
    if args.source_site == "inat":
        raw = _first(
            inat_client.get_reconciliation_detail(args.source_id, inat_jwt, deep=True)
        )
        if not raw:
            print(
                f"iNaturalist observation {args.source_id} is not readable.",
                file=sys.stderr,
            )
            return 1
        source_owner_id = int((raw.get("user") or {}).get("id") or 0)
        if source_owner_id != inat_user_id:
            print(
                f"iNaturalist observation {args.source_id} is owned by user {source_owner_id}, "
                f"not the authenticated user {inat_user_id}. Refusing to use someone else's observation.",
                file=sys.stderr,
            )
            return 1
        inventory = INatReconciliationReader(inat_client).parse_inventory(
            raw, inat_user_id, None, None
        )
        if not args.mo_login:
            print(
                "--mo-login is required when the destination is Mushroom Observer.",
                file=sys.stderr,
            )
            return 2
        mo_user = mo_client.resolve_user(args.mo_login, cancelled)
        if not mo_user:
            print(
                f"Could not resolve Mushroom Observer login {args.mo_login!r}.",
                file=sys.stderr,
            )
            return 2
        mo_user_id = int(mo_user["id"])
        mo_login = str(
            mo_user.get("login_name") or mo_user.get("login") or args.mo_login
        )
    else:
        raw = _first(mo_client.observation(args.source_id, cancelled, detail="high"))
        if not raw:
            print(
                f"Mushroom Observer observation {args.source_id} is not readable.",
                file=sys.stderr,
            )
            return 1
        owner = raw.get("owner") or {}
        mo_user_id = int(owner.get("id") or 0)
        mo_login = str(owner.get("login_name") or "")
        if not mo_user_id:
            print(
                f"Mushroom Observer observation {args.source_id} has no readable owner id.",
                file=sys.stderr,
            )
            return 1
        # Confirm the supplied key actually belongs to that account, using the
        # real app's own verification method (never a discovery mechanism).
        verified = mo_client.authenticated_user_id(mo_key, mo_user_id, cancelled)
        if verified != mo_user_id:
            print(
                f"MO_API_KEY does not match observation {args.source_id}'s owner (id {mo_user_id}). "
                "Refusing to use someone else's observation.",
                file=sys.stderr,
            )
            return 1
        inventory = parse_mo_observation(raw, mo_user_id)

    print(
        f"Authenticated: iNat {inat_login!r} (id {inat_user_id}); MO {mo_login!r} (id {mo_user_id})"
    )

    if not args.run:
        print(
            "\nDRY RUN — nothing was sent. Re-run with --run --i-own-these-records to execute."
        )
        print(
            f"Would create a destination observation from {args.source_site}:{args.source_id}, "
        )
        print(
            "run the full saga (create -> per-item rows -> reciprocal links -> pair_finalize),"
        )
        if args.with_photo_item:
            print("also select and upload one real photo population item,")
        print(
            "then delete the created observation as cleanup"
            + (
                ", and prove outcome_unknown recovery without a second create."
                if args.simulate_lost_response
                else "."
            )
        )
        return 0
    if not args.i_own_these_records:
        print(
            "Refusing to write. --run requires --i-own-these-records.", file=sys.stderr
        )
        return 2

    if args.db_path:
        db_path = args.db_path
        db_owned = False
    else:
        fd, tmp_name = tempfile.mkstemp(suffix=".gate2a-saga.sqlite3")
        os.close(fd)
        db_path = Path(tmp_name)
        db_owned = True
    print(f"Using throwaway database: {db_path}")
    db = ReconciliationDB(str(db_path))
    try:
        return _run_saga_with_db(
            args,
            db,
            inat_client,
            mo_client,
            inat_jwt,
            mo_key,
            inat_user_id,
            inat_login,
            mo_user_id,
            mo_login,
            inventory,
            cancelled,
        )
    finally:
        db.close_thread_connection()
        # Never unlink an operator-supplied --db-path, regardless of --keep-db.
        if db_owned and not args.keep_db:
            try:
                db_path.unlink(missing_ok=True)
            except OSError:
                pass
        elif db_owned:
            print(f"\n--keep-db: throwaway database left at {db_path}")


def _run_saga_with_db(
    args: argparse.Namespace,
    db: ReconciliationDB,
    inat_client: INatClient,
    mo_client: MOClient,
    inat_jwt: str,
    mo_key: str,
    inat_user_id: int,
    inat_login: str,
    mo_user_id: int,
    mo_login: str,
    inventory,
    cancelled,
) -> int:
    profile = db.save_profile(inat_user_id, inat_login, mo_user_id, mo_login)
    print(f"Profile {profile.profile_id} ready.")

    # The reciprocal-link step (reused unmodified from Gate 1B) requires the
    # "Mushroom Observer URL" iNat custom field binding to already be verified
    # for this profile — a real profile gets this from the app's "Verify field
    # bindings" button (ui/reconciliation.py:_field_candidates_loaded). This
    # harness must do the same one-time setup, or every mo_external_link_add/
    # inat_ofv_add follow-up row fails with "field binding unverified".
    mo_field_defs = INatReconciliationReader(inat_client).resolve_field_definitions(
        "Mushroom Observer URL"
    )
    if len(mo_field_defs) != 1:
        print(
            f"Found {len(mo_field_defs)} exact 'Mushroom Observer URL' text field(s) on iNaturalist "
            "(expected exactly 1) — cannot auto-bind. Run the real app's 'Verify field bindings' "
            "button once against this account, or extend this harness to disambiguate.",
            file=sys.stderr,
        )
        return 2
    db.save_field_binding(
        profile.profile_id,
        "mo_url",
        int(mo_field_defs[0]["id"]),
        "Mushroom Observer URL",
        "text",
        "verified",
    )
    print("Mushroom Observer URL field binding verified for this profile.")

    destination_site = "mo" if args.source_site == "inat" else "inat"
    db.upsert_records(profile.profile_id, [inventory])
    expected_state = (
        "confirmed_missing_on_mo"
        if destination_site == "mo"
        else "confirmed_missing_on_inat"
    )
    db.set_confirmed_missing(profile.profile_id, args.source_site, args.source_id, True)
    record = db.record_detail(profile.profile_id, args.source_site, args.source_id)
    if not record or str(record.get("unpaired_state")) != expected_state:
        print("Failed to mark the source record confirmed-missing.", file=sys.stderr)
        return 1
    print(f"Source record seeded and marked {expected_state}.")

    # --- build the real service graph, exactly like the coordinator does ----
    auth_provider = lambda: AuthState(
        api_token=inat_jwt, login=inat_login
    )  # noqa: E731
    mo_key_provider = lambda _profile_id: mo_key  # noqa: E731
    gen_provider = lambda: 1  # noqa: E731
    photo_service = PhotoSyncService(
        db,
        inat_client,
        mo_client,
        auth_provider,
        mo_key_provider,
        gen_provider,
        gen_provider,
    )
    link_service = LinkRepairService(
        db,
        inat_client,
        mo_client,
        auth_provider,
        mo_key_provider,
        gen_provider,
        gen_provider,
    )
    service = ObservationCreationService(
        db,
        inat_client,
        mo_client,
        auth_provider,
        mo_key_provider,
        gen_provider,
        gen_provider,
        photo_service,
        link_service,
    )

    try:
        preview = service.prepare_preview(
            profile.profile_id, args.source_site, args.source_id, cancelled=cancelled
        )
    except ObservationCreationError as exc:
        print(f"prepare_preview FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        f"Preview built: destination {preview.destination_site.value}, "
        f"{len(preview.items)} selectable item(s), gaps={list(preview.approved_field_gaps)}"
    )
    for warning in preview.warnings:
        print(f"  warning: {warning}")

    # Optionally exercise ONE selected population item, exactly the way the
    # coordinator selects and journals it (round-3 smaller issue: this
    # checked-in harness previously always passed item_specs=[], so no
    # checked-in live path ever exercised a real photo upload through the
    # saga -- only the disposable, non-checked-in scratchpad smoke tests
    # did). Only photo items are enabled at all (Gate 2A section 9).
    item_specs: list[dict] = []
    if args.with_photo_item:
        candidate = next(
            (
                item
                for item in preview.items
                if item.item_type == "photo" and item.enabled
            ),
            None,
        )
        if not candidate:
            print(
                "--with-photo-item requested, but no enabled photo item is available in this "
                "preview (no MO photos owned by this account, or all are disabled/duplicates).",
                file=sys.stderr,
            )
            return 1
        item_specs = [
            {
                "item_type": candidate.item_type,
                "source_identity": candidate.source_identity,
                "metadata_fingerprint": candidate.metadata_fingerprint,
                "reviewed_byte_fingerprint": candidate.reviewed_byte_fingerprint,
            }
        ]
        print(
            f"Selected photo item {candidate.source_identity!r} for population (--with-photo-item)."
        )

    import uuid as uuidlib

    marker = (
        str(uuidlib.uuid4())
        if preview.destination_site.value == "inat"
        else f"[gate2a-saga-harness:{uuidlib.uuid4()}]"
    )
    marker_location = (
        "client_uuid_field"
        if preview.destination_site.value == "inat"
        else "public_notes"
    )
    group_id, create_action_id, attempt_id = db.journal_observation_creation_actions(
        profile.profile_id,
        source_site=preview.source_site.value,
        source_observation_id=preview.source_observation_id,
        destination_site=preview.destination_site.value,
        source_fingerprint=preview.source_fingerprint,
        correlation_marker=marker,
        marker_location=marker_location,
        approved_field_gaps=list(preview.approved_field_gaps),
        item_specs=item_specs,  # [] by default -- this harness then proves creation/link/finalize
        # ONLY, never population; see the RESULT line below. Pass
        # --with-photo-item to also exercise one real photo upload.
        reviewed_destination_taxon_id=preview.taxon_id,
        reviewed_destination_taxon_name=preview.resolved_taxon_name,
        reviewed_source_taxon_name=preview.taxon_name,
        reviewed_source_taxon_rank=preview.taxon_rank,
        resolution_mode=preview.resolution_mode,
        taxon_resolution_fingerprint=preview.taxon_resolution_fingerprint,
        reviewed_payload_fingerprint=preview.reviewed_payload_fingerprint,
    )
    print(
        f"Journaled group {group_id}, create action {create_action_id}, creation attempt {attempt_id}."
    )

    def progress(message: str) -> None:
        print(f"  progress: {message}")

    results = service.execute_group(profile.profile_id, group_id, cancelled, progress)
    for result in results:
        print(
            f"  result: action {result.action_id} -> {result.state}: {result.message}"
        )

    create_row = db.action(profile.profile_id, create_action_id)
    destination_id: Optional[int] = None
    if create_row and str(create_row["state"]) == "outcome_unknown":
        if args.simulate_lost_response:
            print(
                "\nSimulated lost response landed as outcome_unknown, as expected. Verifying recovery..."
            )
            recovery = service.verify_unknown(
                profile.profile_id, create_action_id, cancelled
            )
            print(f"  verify_unknown: {recovery.state}: {recovery.message}")
            if recovery.state != "succeeded":
                print(
                    "RECOVERY FAILED — the create may be an orphan. Check manually.",
                    file=sys.stderr,
                )
                return 1
            create_row = db.action(profile.profile_id, create_action_id)
        else:
            print("Create ended in outcome_unknown unexpectedly.", file=sys.stderr)
    if create_row:
        destination_id = (
            int(create_row["mo_observation_id"] or 0)
            if destination_site == "mo"
            else int(create_row["inat_observation_id"] or 0)
        )
        destination_id = destination_id or None

    final_group = db.action_group_rows(profile.profile_id, group_id)
    all_succeeded = all(str(row["state"]) == "succeeded" for row in final_group)
    print(
        f"\nFinal saga state: {[(row['ordinal'], row['action_type'], row['state']) for row in final_group]}"
    )
    population_note = (
        "One selected photo population item was ALSO exercised end to end (--with-photo-item)."
        if item_specs
        else "NOTE: item_specs=[] means NO population item (photo) was exercised by this run; do not "
        "describe this as a population-item proof. Re-run with --with-photo-item, or see "
        "the Gate 2A capability note for the population-item status."
    )
    print(
        "RESULT: "
        + (
            f"CLEARED — creation + both reciprocal links + pair_finalize succeeded end to end. {population_note}"
            if all_succeeded
            else "INCOMPLETE — see rows above."
        )
    )

    if not args.no_cleanup and destination_id:
        print(
            f"\n--- cleanup: deleting destination observation {destination_id} on {destination_site} ---"
        )
        if destination_site == "inat":
            uuid_row = create_row
            obs_uuid = (
                str(uuid_row["inat_observation_uuid"])
                if uuid_row and uuid_row["inat_observation_uuid"]
                else ""
            )
            if not obs_uuid:
                detail = _first(
                    inat_client.get_reconciliation_detail(
                        destination_id, inat_jwt, deep=True
                    )
                )
                obs_uuid = str(detail.get("uuid") or "")
            if obs_uuid:
                deleted = inat_client.delete_observation_v2(inat_jwt, obs_uuid)
                print(
                    f"  DELETE /observations/{obs_uuid}: HTTP {deleted.metadata.status_code}"
                )
            else:
                print(
                    f"  Could not resolve a uuid for observation {destination_id}; delete manually."
                )
        else:
            try:
                mo_client._write(  # noqa: SLF001 - harness-only cleanup; no public delete exists (deliberately, per the saga's never-auto-delete design)
                    "DELETE",
                    "observations",
                    {"api_key": mo_key, "id": destination_id},
                    cancelled,
                )
                print(f"  DELETE MO observation {destination_id}: ok")
            except MOAPIError as exc:
                print(
                    f"  DELETE MO observation {destination_id} FAILED: {exc}. Delete manually."
                )
    elif destination_id:
        print(
            f"\n--no-cleanup: destination observation {destination_id} on {destination_site} left in place."
        )

    return 0 if all_succeeded else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gate 2A M8 live saga harness (performs REAL writes when --run is given).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source-site", choices=("inat", "mo"), required=True)
    parser.add_argument(
        "--source-id", type=int, required=True, help="A DISPOSABLE observation you own"
    )
    parser.add_argument(
        "--mo-login",
        default="",
        help="Your Mushroom Observer login. Required when --source-site is inat "
        "(MO is the destination, so its account identity can't be read from the source).",
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--i-own-these-records", action="store_true")
    parser.add_argument(
        "--simulate-lost-response",
        action="store_true",
        help="Send the real create, then force outcome_unknown and prove verify_unknown recovers it",
    )
    parser.add_argument(
        "--with-photo-item",
        action="store_true",
        help="Also select and upload one real photo population item (MO->iNat only)",
    )
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--db-path", type=Path, default=None)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
