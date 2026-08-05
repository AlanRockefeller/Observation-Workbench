"""Gate 1D name proposals.

The two sites use different models, so this module keeps them strictly separate:

* An iNaturalist name change **is an identification**. This service only
  resolves and validates a candidate; the actual write is delegated to the
  existing Identify subsystem (``IdentifyActionManager``), and reconciliation
  stores only the resulting ``identify_action_id``. No identification journaling
  is duplicated here.
* A Mushroom Observer name change **is a proposal** whose effectiveness is a
  separate, later-observed state. Its lifecycle (effective / rejected /
  superseded) is tracked here. Because the MO API exposes no per-observation
  consensus-name proposal endpoint, this gate is deliberately **draft-only**:
  there is no remote submission at all, and recording a draft never marks the
  pair name-synchronized. If a real endpoint is added later, a submission step
  can be built on top of the existing draft records.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatClient

from .db import ReconciliationDB
from .inat_reader import INatReconciliationReader
from .mo_client import MOClient
from .mo_parsing import parse_mo_observation, positive_int
from .normalization import public_fingerprint
from .specimen_state import evaluate_specimen_state
from .types import (
    NameProposalCandidate, NameProposalPreview, NameProposalStatus,
    ReconciliationProfile, RemoteSite,
)

FUNGI_TAXON_ID = "47170"


class NameProposalError(RuntimeError):
    def __init__(self, message: str, code: str = "name_proposal_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NameProposalResult:
    state: str
    message: str
    proposal_id: Optional[int] = None
    identify_action_id: Optional[int] = None


@dataclass(frozen=True)
class INatDelegationParams:
    account_login: str
    observation_id: int
    observation_uuid: str
    taxon_id: int
    proposed_name: str
    source_fingerprint: str


class NameProposalService:
    """Fresh-read eligibility resolution plus MO proposal lifecycle tracking."""

    def __init__(
        self, db: ReconciliationDB, inat_client: INatClient, mo_client: MOClient,
        auth_provider: Callable[[], AuthState], mo_key_provider: Callable[[int], str],
        auth_generation_provider: Callable[[], int],
        mo_key_generation_provider: Callable[[], int],
    ) -> None:
        self.db = db
        self.inat_client = inat_client
        self.mo_client = mo_client
        self.auth_provider = auth_provider
        self.mo_key_provider = mo_key_provider
        self.auth_generation_provider = auth_generation_provider
        self.mo_key_generation_provider = mo_key_generation_provider

    # Preview ----------------------------------------------------------

    def prepare_preview(
        self, profile_id: int, pair_id: int,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> NameProposalPreview:
        pair = self._eligible_pair(profile_id, pair_id)
        profile = self.db.profile(profile_id)
        auth_generation = self.auth_generation_provider()
        mo_key_generation = self.mo_key_generation_provider()
        auth = self.auth_provider()
        token = auth.api_token if auth.is_authenticated else ""
        if not token:
            raise NameProposalError(
                "iNaturalist authentication must match the selected reconciliation account.",
                "inat_auth_mismatch",
            )
        current = _first_result(self.inat_client.get_current_user_v2(token))
        if positive_int(current.get("id") if current else None) != profile.inat_user_id:
            raise NameProposalError(
                "The authenticated iNaturalist account does not match the profile.", "inat_auth_mismatch",
            )

        inat_id = int(pair["inat_observation_id"])
        mo_id = int(pair["mo_observation_id"])
        inat_raw = _first_result(self.inat_client.get_reconciliation_detail(inat_id, token, deep=False))
        if not inat_raw or positive_int(inat_raw.get("id")) != inat_id:
            raise NameProposalError("The iNaturalist observation is unavailable.", "inat_unavailable")
        inat_uuid = str(inat_raw.get("uuid") or "").strip()
        inat_user_raw = inat_raw.get("user")
        inat_user = inat_user_raw if isinstance(inat_user_raw, dict) else {}
        if not inat_uuid or positive_int(inat_user.get("id")) != profile.inat_user_id:
            raise NameProposalError("The iNaturalist record identity or owner changed.", "inat_owner_changed")
        inat_taxon_raw = inat_raw.get("taxon")
        inat_taxon = inat_taxon_raw if isinstance(inat_taxon_raw, dict) else {}
        inat_name = str(inat_taxon.get("name") or "").strip()
        inat_taxon_id = positive_int(inat_taxon.get("id"))
        inat_ancestry = str(inat_taxon.get("ancestry") or "")

        mo_raw = _first_result(self.mo_client.observation(mo_id, cancelled, detail="high"))
        if not mo_raw or positive_int(mo_raw.get("id")) != mo_id:
            raise NameProposalError("The Mushroom Observer observation is unavailable.", "mo_unavailable")
        mo_observation = parse_mo_observation(mo_raw, profile.mo_user_id)
        if mo_observation.owner_id != profile.mo_user_id:
            raise NameProposalError("The Mushroom Observer record owner changed.", "mo_owner_changed")
        mo_name = mo_observation.taxon_name
        mo_name_id = mo_observation.taxon_id
        mo_rank = mo_observation.taxon_rank

        warnings: list[str] = []
        raw_candidates = [
            self._mo_to_inat_candidate(mo_name, mo_rank, inat_name, warnings),
            self._inat_to_mo_candidate(inat_name, inat_taxon.get("rank"), inat_ancestry, mo_name, warnings),
        ]
        candidates = [item for item in raw_candidates if item is not None]
        if not candidates:
            warnings.append("Both sites already agree on the name, or no source name is available.")
        # A name proposal must still prove the two records are the same collection.
        # The name difference under review is not a specimen conflict, so the full
        # specimen check (owner, fungal scope, deletion, date, vouchers,
        # collections, coordinates) applies without any name-specific exclusion.
        reader = INatReconciliationReader(self.inat_client)
        specimen_conflict, _fp, specimen_warnings = evaluate_specimen_state(
            self.db, profile, pair, inat_raw, mo_raw, reader,
            mo_client=self.mo_client, cancelled=cancelled,
            tolerate_unknown_inat_scope=True,
        )
        warnings.extend(specimen_warnings)
        if specimen_conflict:
            warnings.append(
                "Name proposals are blocked while specimen-identity evidence conflicts: "
                + specimen_conflict
            )
            candidates = []
        return NameProposalPreview(
            profile_id=profile_id, pair_id=pair_id,
            auth_generation=auth_generation, mo_key_generation=mo_key_generation,
            source_fingerprint=_pair_fingerprint(pair),
            mo_observation_id=mo_id, inat_observation_id=inat_id,
            inat_observation_uuid=inat_uuid, inat_login=profile.inat_login,
            inat_current_name=inat_name, inat_current_taxon_id=inat_taxon_id,
            mo_current_name=mo_name, mo_current_name_id=mo_name_id,
            candidates=tuple(candidates), warnings=tuple(warnings),
        )

    def _mo_to_inat_candidate(
        self, mo_name: str, mo_rank: str, inat_name: str, warnings: list[str],
    ) -> Optional[NameProposalCandidate]:
        if not mo_name:
            return None
        if inat_name and inat_name.casefold() == mo_name.casefold():
            return None
        # Resolve to a SINGLE UNAMBIGUOUS exact iNaturalist taxon; a fuzzy string
        # match must never become a proposal, and an exact string that maps to
        # several taxa (homonyms/inactive records) must be selected explicitly.
        matches = _autocomplete_results(self.inat_client.get_taxa_autocomplete(mo_name, per_page=30))
        exact, reason = _resolve_exact_taxon(matches, mo_name, expected_rank=mo_rank)
        if reason == "no_exact":
            return NameProposalCandidate(
                target_site=RemoteSite.INAT, source_site=RemoteSite.MO,
                source_name=mo_name, proposed_name=mo_name, proposed_rank=mo_rank,
                current_destination_name=inat_name, string_similarity_only=True,
                enabled=False,
                disabled_reason="No exact iNaturalist taxon matches the Mushroom Observer name.",
            )
        if reason or exact is None:
            disabled_reason = {
                "inactive": (
                    "The only exact iNaturalist match is an inactive taxon; select an active taxon "
                    "explicitly on iNaturalist."
                ),
                "rank_mismatch": (
                    "No active iNaturalist taxon with a rank compatible with the Mushroom Observer name "
                    "matches; select the intended taxon explicitly."
                ),
                "ambiguous": (
                    "Several iNaturalist taxa share this exact name (possible homonyms); select the "
                    "intended taxon explicitly on iNaturalist."
                ),
            }.get(reason, "The iNaturalist taxon could not be resolved unambiguously.")
            return NameProposalCandidate(
                target_site=RemoteSite.INAT, source_site=RemoteSite.MO,
                source_name=mo_name, proposed_name=mo_name, proposed_rank=mo_rank,
                current_destination_name=inat_name, string_similarity_only=False,
                enabled=False, disabled_reason=disabled_reason,
            )
        taxon_id = positive_int(exact.get("id"))
        ancestry = str(exact.get("ancestry") or "")
        kingdom_ok = FUNGI_TAXON_ID in ancestry.split("/") or str(
            exact.get("iconic_taxon_name") or ""
        ).casefold() == "fungi"
        if not kingdom_ok:
            warnings.append("The resolved iNaturalist taxon is outside Fungi; resolve the kingdom conflict first.")
        return NameProposalCandidate(
            target_site=RemoteSite.INAT, source_site=RemoteSite.MO,
            source_name=mo_name, proposed_name=str(exact.get("name") or mo_name),
            proposed_rank=str(exact.get("rank") or mo_rank), proposed_taxon_id=taxon_id,
            current_destination_name=inat_name,
            synonyms=_synonyms(exact), kingdom_compatible=kingdom_ok,
            enabled=bool(taxon_id) and kingdom_ok,
            disabled_reason="" if (taxon_id and kingdom_ok) else "An incompatible-kingdom taxon cannot be proposed.",
        )

    def _inat_to_mo_candidate(
        self, inat_name: str, inat_rank: object, inat_ancestry: str,
        mo_name: str, warnings: list[str],
    ) -> Optional[NameProposalCandidate]:
        if not inat_name:
            return None
        if mo_name and mo_name.casefold() == inat_name.casefold():
            return None
        kingdom_ok = FUNGI_TAXON_ID in inat_ancestry.split("/")
        if not kingdom_ok:
            warnings.append("The iNaturalist source taxon is outside Fungi; a Mushroom Observer proposal is blocked.")
        warnings.append(
            "Mushroom Observer has no per-observation name-proposal endpoint, so this gate is draft-only: "
            "the proposal is tracked locally as pending and never submitted remotely."
        )
        return NameProposalCandidate(
            target_site=RemoteSite.MO, source_site=RemoteSite.INAT,
            source_name=inat_name, proposed_name=inat_name,
            proposed_rank=str(inat_rank or ""), current_destination_name=mo_name,
            kingdom_compatible=kingdom_ok, enabled=kingdom_ok,
            disabled_reason="" if kingdom_ok else "An incompatible-kingdom source cannot be proposed.",
        )

    # iNaturalist delegation (synchronous; no network) -----------------

    def inat_delegation_params(
        self, preview: NameProposalPreview, candidate: NameProposalCandidate,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> INatDelegationParams:
        """Freshly revalidate the candidate and return Identify enqueue params.

        Remote names can change after a preview without changing the locally
        stored pair fingerprint, so this method rereads BOTH observations
        immediately before the enqueue and revalidates every input the write
        depends on: owners, Fungi scope, the reviewed destination baseline, the
        reviewed source name, the exact resolved taxon id, and unrelated
        specimen-identity conflicts. The write itself is performed by the shared
        Identify subsystem; this method never writes to iNaturalist. Because it
        reads the network, it must be called off the UI thread.
        """
        if candidate.target_site is not RemoteSite.INAT:
            raise NameProposalError("This candidate is not an iNaturalist identification.", "invalid_candidate")
        if not candidate.enabled or not candidate.proposed_taxon_id:
            raise NameProposalError("A disabled or unresolved candidate cannot be delegated.", "invalid_candidate")
        if candidate.string_similarity_only:
            raise NameProposalError("A string-similarity-only candidate cannot be proposed.", "string_similarity_only")
        if not candidate.kingdom_compatible:
            raise NameProposalError("An incompatible-kingdom candidate cannot be proposed.", "kingdom_conflict")
        pair = self._eligible_pair(preview.profile_id, preview.pair_id)
        if _pair_fingerprint(pair) != preview.source_fingerprint:
            raise NameProposalError("The confirmed pair changed after preview.", "pair_changed")
        profile = self.db.profile(preview.profile_id)
        inat_raw, mo_raw, inat_name, inat_taxon_id, mo_observation = self._reread_pair(
            profile, pair, cancelled,
        )

        # The destination iNaturalist taxon must still be the reviewed baseline.
        if (
            inat_name.casefold() != preview.inat_current_name.casefold()
            or inat_taxon_id != preview.inat_current_taxon_id
        ):
            raise NameProposalError(
                "The iNaturalist destination taxon changed after preview; refresh the name proposal.",
                "inat_taxon_changed",
            )
        # The Mushroom Observer source name must still match the reviewed candidate
        # by both displayed text AND name ID (a name-ID change with the same text
        # still means a different underlying name record).
        if mo_observation.taxon_name.casefold() != candidate.source_name.casefold():
            raise NameProposalError(
                "The Mushroom Observer source name changed after preview; refresh the name proposal.",
                "mo_name_changed",
            )
        if (
            preview.mo_current_name_id and mo_observation.taxon_id
            and int(mo_observation.taxon_id) != int(preview.mo_current_name_id)
        ):
            raise NameProposalError(
                "The Mushroom Observer source name ID changed after preview; refresh the name proposal.",
                "mo_name_changed",
            )
        # Re-resolve the exact iNaturalist taxon for the source name and confirm it
        # still resolves to the SAME single unambiguous reviewed taxon id.
        matches = _autocomplete_results(
            self.inat_client.get_taxa_autocomplete(candidate.source_name, per_page=30)
        )
        exact, reason = _resolve_exact_taxon(
            matches, candidate.source_name, expected_rank=candidate.proposed_rank,
        )
        if reason == "ambiguous":
            raise NameProposalError(
                "The iNaturalist name now resolves to several taxa; select the intended taxon explicitly.",
                "taxon_resolution_ambiguous",
            )
        if reason == "inactive":
            raise NameProposalError(
                "The iNaturalist taxon for the source name is now inactive; an inactive taxon cannot be proposed.",
                "taxon_inactive",
            )
        if reason == "rank_mismatch":
            raise NameProposalError(
                "No active iNaturalist taxon with a compatible rank matches the source name.",
                "taxon_rank_mismatch",
            )
        resolved_id = positive_int(exact.get("id")) if exact else None
        if not exact or reason == "no_exact" or resolved_id != int(candidate.proposed_taxon_id):
            raise NameProposalError(
                "The iNaturalist taxon resolution for the source name changed after preview.",
                "taxon_resolution_changed",
            )
        ancestry = str(exact.get("ancestry") or "")
        kingdom_ok = FUNGI_TAXON_ID in ancestry.split("/") or str(
            exact.get("iconic_taxon_name") or ""
        ).casefold() == "fungi"
        if not kingdom_ok:
            raise NameProposalError("The resolved iNaturalist taxon is outside Fungi.", "kingdom_conflict")
        # Tolerate the name difference under review, but block on any unrelated
        # specimen-identity conflict.
        reader = INatReconciliationReader(self.inat_client)
        # The destination may be an as-yet unidentified iNaturalist observation —
        # exactly what this identification fixes — so its own (unknowable) fungal
        # scope does not block. ``_reread_pair`` above already refused a record
        # whose taxon is KNOWN to be non-fungal, and the Mushroom Observer side
        # must still prove its scope.
        specimen_conflict, _fp, _warn = evaluate_specimen_state(
            self.db, profile, pair, inat_raw, mo_raw, reader,
            mo_client=self.mo_client, cancelled=cancelled,
            tolerate_unknown_inat_scope=True,
        )
        if specimen_conflict:
            raise NameProposalError(
                "Specimen-identity evidence conflicts; the name proposal is blocked: " + specimen_conflict,
                "specimen_conflict",
            )
        return INatDelegationParams(
            account_login=profile.inat_login,
            observation_id=int(pair["inat_observation_id"]),
            observation_uuid=str(inat_raw.get("uuid") or "").strip(),
            taxon_id=int(candidate.proposed_taxon_id),
            proposed_name=candidate.proposed_name,
            source_fingerprint=preview.source_fingerprint,
        )

    def _reread_pair(
        self, profile: ReconciliationProfile, pair: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> tuple[dict[str, Any], dict[str, Any], str, Optional[int], Any]:
        """Fresh authenticated reread of both observations with owner/identity checks.

        Returns ``(inat_raw, mo_raw, inat_name, inat_taxon_id, mo_observation)``.
        """
        auth = self.auth_provider()
        token = auth.api_token if auth.is_authenticated else ""
        if not token:
            raise NameProposalError(
                "iNaturalist authentication must match the selected reconciliation account.",
                "inat_auth_mismatch",
            )
        current = _first_result(self.inat_client.get_current_user_v2(token))
        if positive_int(current.get("id") if current else None) != profile.inat_user_id:
            raise NameProposalError(
                "The authenticated iNaturalist account does not match the profile.", "inat_auth_mismatch",
            )
        inat_id = int(pair["inat_observation_id"])
        mo_id = int(pair["mo_observation_id"])
        inat_raw = _first_result(self.inat_client.get_reconciliation_detail(inat_id, token, deep=False))
        if not inat_raw or positive_int(inat_raw.get("id")) != inat_id:
            raise NameProposalError("The iNaturalist observation is unavailable.", "inat_unavailable")
        inat_user_raw = inat_raw.get("user")
        inat_user = inat_user_raw if isinstance(inat_user_raw, dict) else {}
        if not str(inat_raw.get("uuid") or "").strip() or positive_int(inat_user.get("id")) != profile.inat_user_id:
            raise NameProposalError("The iNaturalist record identity or owner changed.", "inat_owner_changed")
        if _fungi_status(inat_raw) == "nonfungal":
            raise NameProposalError("The iNaturalist observation is now outside Fungi.", "inat_out_of_scope")
        inat_taxon_raw = inat_raw.get("taxon")
        inat_taxon = inat_taxon_raw if isinstance(inat_taxon_raw, dict) else {}
        inat_name = str(inat_taxon.get("name") or "").strip()
        inat_taxon_id = positive_int(inat_taxon.get("id"))

        mo_raw = _first_result(self.mo_client.observation(mo_id, cancelled, detail="high"))
        if not mo_raw or positive_int(mo_raw.get("id")) != mo_id:
            raise NameProposalError("The Mushroom Observer observation is unavailable.", "mo_unavailable")
        mo_observation = parse_mo_observation(mo_raw, profile.mo_user_id)
        if mo_observation.owner_id != profile.mo_user_id:
            raise NameProposalError("The Mushroom Observer record owner changed.", "mo_owner_changed")
        if mo_observation.fungi_status == "nonfungal":
            raise NameProposalError("The Mushroom Observer observation is now outside Fungi.", "mo_out_of_scope")
        return inat_raw, mo_raw, inat_name, inat_taxon_id, mo_observation

    # Mushroom Observer proposal lifecycle -----------------------------

    def record_mo_proposal_draft(
        self, preview: NameProposalPreview, candidate: NameProposalCandidate,
        cancelled: Callable[[], bool],
    ) -> NameProposalResult:
        """Record a local Mushroom Observer name-proposal draft (pending).

        Mushroom Observer exposes no per-observation consensus-name proposal
        endpoint, so this records local intent (draft only): it never submits
        anything remotely, never sets ``proposal_submitted``/``submitted_at``/a
        remote id, and never marks the pair name-synchronized.

        Like the iNaturalist delegation path, it rereads both observations
        immediately beforehand and performs equivalent stale-source checks
        (owners, Fungi scope, the reviewed source name, specimen-identity
        conflicts) so a draft is never recorded from stale name information.
        """
        if candidate.target_site is not RemoteSite.MO:
            raise NameProposalError("This candidate is not a Mushroom Observer proposal.", "invalid_candidate")
        if not candidate.enabled or not candidate.proposed_name:
            raise NameProposalError("A disabled or empty candidate cannot be proposed.", "invalid_candidate")
        if not candidate.kingdom_compatible:
            raise NameProposalError("An incompatible-kingdom candidate cannot be proposed.", "kingdom_conflict")
        pair = self._eligible_pair(preview.profile_id, preview.pair_id)
        if _pair_fingerprint(pair) != preview.source_fingerprint:
            raise NameProposalError("The confirmed pair changed after preview.", "pair_changed")
        profile = self.db.profile(preview.profile_id)
        inat_raw, mo_raw, inat_name, _inat_taxon_id, mo_observation = self._reread_pair(
            profile, pair, cancelled,
        )
        # The source of an MO draft is the iNaturalist name; it must still match
        # both the reviewed candidate and the reviewed baseline.
        if (
            inat_name.casefold() != candidate.source_name.casefold()
            or inat_name.casefold() != preview.inat_current_name.casefold()
        ):
            raise NameProposalError(
                "The iNaturalist source name changed after preview; refresh the name proposal.",
                "inat_name_changed",
            )
        # The destination is the Mushroom Observer consensus; require both its name
        # TEXT and its name ID to still equal the reviewed baseline (the same
        # baseline the opposite direction checks), so a stale draft is never
        # recorded after the MO consensus moves.
        mo_current = mo_observation.taxon_name
        mo_current_id = mo_observation.taxon_id
        if mo_current.casefold() != preview.mo_current_name.casefold() or (
            candidate.current_destination_name
            and mo_current.casefold() != candidate.current_destination_name.casefold()
        ):
            raise NameProposalError(
                "The Mushroom Observer consensus name changed after preview; refresh the name proposal.",
                "mo_name_changed",
            )
        if (
            preview.mo_current_name_id and mo_current_id
            and int(mo_current_id) != int(preview.mo_current_name_id)
        ):
            raise NameProposalError(
                "The Mushroom Observer consensus name ID changed after preview; refresh the name proposal.",
                "mo_name_changed",
            )
        # If the consensus already equals the proposed name, report it as already
        # effective instead of recording a redundant pending draft.
        if mo_current.casefold() == candidate.proposed_name.casefold():
            raise NameProposalError(
                "The Mushroom Observer consensus already matches the proposed name; no draft is needed.",
                "already_effective",
            )
        reader = INatReconciliationReader(self.inat_client)
        # No scope tolerance here: an MO draft is only offered when the
        # iNaturalist ancestry positively proves Fungi (``_inat_to_mo_candidate``
        # gates ``enabled`` on exactly that), so an unknown scope on this side is
        # a genuine change since preview and must block.
        specimen_conflict, _fp, _warn = evaluate_specimen_state(
            self.db, profile, pair, inat_raw, mo_raw, reader,
            mo_client=self.mo_client, cancelled=cancelled,
        )
        if specimen_conflict:
            raise NameProposalError(
                "Specimen-identity evidence conflicts; the name proposal is blocked: " + specimen_conflict,
                "specimen_conflict",
            )
        mo_id = int(pair["mo_observation_id"])
        current_effective = mo_observation.taxon_name
        proposal_id = self.db.record_mo_proposal(
            preview.profile_id, preview.pair_id, mo_id, candidate.proposed_name,
            candidate.proposed_name_id, current_effective,
        )
        return NameProposalResult(
            "recorded_draft",
            "The name proposal was recorded as a local draft (pending). It does not change the "
            "Mushroom Observer consensus name; Mushroom Observer has no per-observation submission "
            "endpoint, so this gate is draft-only.",
            proposal_id=proposal_id,
        )

    def refresh_proposal_effectiveness(
        self, profile_id: int, pair_id: int, cancelled: Callable[[], bool],
    ) -> NameProposalResult:
        """Re-read the MO consensus name and update the tracked proposal status."""
        proposal = self.db.mo_proposal_for_pair(profile_id, pair_id)
        if not proposal:
            raise NameProposalError("No Mushroom Observer proposal is tracked for this pair.", "no_proposal")
        proposal_id = int(proposal["proposal_id"])
        mo_id = int(proposal["mo_observation_id"])
        mo_raw = _first_result(self.mo_client.observation(mo_id, cancelled, detail="high"))
        if not mo_raw or positive_int(mo_raw.get("id")) != mo_id:
            raise NameProposalError("The Mushroom Observer observation is unavailable.", "mo_unavailable")
        effective_name = parse_mo_observation(mo_raw, 0).taxon_name
        proposed = str(proposal["proposed_name"])
        now = _utc_now()
        if effective_name and effective_name.casefold() == proposed.casefold():
            self.db.update_mo_proposal_status(
                profile_id, proposal_id, NameProposalStatus.EFFECTIVE.value,
                current_effective_name=effective_name, became_effective_at=now,
            )
            return NameProposalResult("effective", "The proposed name is now the Mushroom Observer consensus.", proposal_id=proposal_id)
        # Draft-only: nothing is submitted remotely, so a draft is simply "pending"
        # until the consensus happens to match it. Supersession is only a
        # meaningful outcome once a real submission exists, so it is not asserted
        # here (that logic is intentionally absent, not dead-gated on a submitted
        # flag that is never set).
        self.db.update_mo_proposal_status(
            profile_id, proposal_id, NameProposalStatus.PENDING.value,
            current_effective_name=effective_name,
        )
        return NameProposalResult("pending", "The proposal is still pending; the consensus has not changed to it.", proposal_id=proposal_id)

    # Eligibility ------------------------------------------------------

    def _eligible_pair(self, profile_id: int, pair_id: int) -> dict[str, Any]:
        pair = self.db.pair_detail(profile_id, pair_id)
        if not pair or pair.get("review_state") != "confirmed" or pair.get("excluded"):
            raise NameProposalError("Only a currently confirmed, non-excluded pair can produce name proposals.")
        if self.db.confirmed_pair_conflict(
            profile_id, int(pair["mo_observation_id"]), int(pair["inat_observation_id"]),
        ):
            raise NameProposalError("Another confirmed one-to-one pairing conflicts with this pair.", "one_to_one_conflict")
        return pair


def _is_fungi_taxon(taxon: dict[str, Any]) -> bool:
    ancestry = str(taxon.get("ancestry") or "")
    return FUNGI_TAXON_ID in ancestry.split("/") or str(
        taxon.get("iconic_taxon_name") or ""
    ).casefold() == "fungi"


def _resolve_exact_taxon(
    matches: list[dict[str, Any]], name: str, *, expected_rank: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """Resolve one unambiguous, active, rank-compatible exact-name taxon.

    Returns ``(taxon, reason)`` where ``reason`` is ``""`` for a single confident
    resolution or one of: ``"no_exact"`` (nothing matches the name exactly),
    ``"inactive"`` (exact matches exist but none is active), ``"rank_mismatch"``
    (a meaningful expected rank is supplied and no active match carries a
    compatible rank), or ``"ambiguous"`` (more than one candidate survives).

    For an automated identification an exact string is not enough: an inactive or
    wrong-rank exact string must never become an ID, so active status and
    (when the source supplies a rank and the candidates expose one) rank
    compatibility are REQUIRED, not merely preferred.
    """
    folded = name.casefold()
    exact = [item for item in matches if str(item.get("name") or "").casefold() == folded]
    if not exact:
        return None, "no_exact"
    # Require an active taxon (never resolve to an inactive record).
    active = [item for item in exact if item.get("is_active", True) is not False]
    if not active:
        return None, "inactive"
    pool = active
    # Filter to Fungi when any active exact match is fungal; otherwise keep the
    # pool so the caller's own kingdom check still fires on a non-fungal result.
    fungi = [item for item in pool if _is_fungi_taxon(item)]
    if fungi:
        pool = fungi
    # Require a compatible rank when a meaningful expected rank is supplied AND at
    # least one candidate exposes a rank we can compare against.
    if expected_rank:
        with_rank = [item for item in pool if str(item.get("rank") or "").strip()]
        if with_rank:
            ranked = [item for item in with_rank if str(item.get("rank") or "").casefold() == expected_rank.casefold()]
            if not ranked:
                return None, "rank_mismatch"
            pool = ranked
    ids = {positive_int(item.get("id")) for item in pool if positive_int(item.get("id"))}
    if len(ids) == 1 and pool:
        # A single distinct taxon id (even if duplicated across framework rows).
        chosen = next(item for item in pool if positive_int(item.get("id")) in ids)
        return chosen, ""
    return None, "ambiguous"


def _autocomplete_results(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _synonyms(taxon: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for key in ("synonyms", "taxon_names", "names"):
        value = taxon.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    text = str(item.get("name") or item.get("text_name") or "").strip()
                elif isinstance(item, str):
                    text = item.strip()
                else:
                    text = ""
                if text:
                    names.append(text)
    return tuple(dict.fromkeys(names))


def _first_result(payload: object) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("results")
    if isinstance(rows, list):
        return next((item for item in rows if isinstance(item, dict)), None)
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload if payload.get("id") is not None else None


def _pair_fingerprint(pair: dict[str, Any]) -> str:
    return public_fingerprint(
        "pair", pair.get("pair_id"), pair.get("updated_at"), pair.get("review_state"),
        pair.get("link_state"), pair.get("confirmed_by"),
    )


def _fungi_status(raw: dict[str, Any]) -> str:
    taxon = raw.get("taxon") if isinstance(raw.get("taxon"), dict) else {}
    ancestry = str(taxon.get("ancestry") or "")
    iconic = str(taxon.get("iconic_taxon_name") or "").casefold()
    if iconic == "fungi" or FUNGI_TAXON_ID in ancestry.split("/"):
        return "fungi"
    return "nonfungal" if taxon else "unknown"


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
