"""Canonical, persistence-safe reconciliation domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, IntEnum
from typing import Optional


class RemoteSite(str, Enum):
    INAT = "inat"
    MO = "mo"


class LinkActionType(str, Enum):
    INAT_OFV_ADD = "inat_ofv_add"
    INAT_OFV_REPAIR = "inat_ofv_repair"
    INAT_OFV_REMOVE = "inat_ofv_remove"
    MO_EXTERNAL_LINK_ADD = "mo_external_link_add"
    MO_EXTERNAL_LINK_REPAIR = "mo_external_link_repair"
    MO_EXTERNAL_LINK_REMOVE = "mo_external_link_remove"


class ITSActionType(str, Enum):
    INAT_ITS_ADD = "inat_its_add"
    INAT_ITS_REPAIR = "inat_its_repair"
    INAT_ITS_REMOVE = "inat_its_remove"
    MO_SEQUENCE_ADD = "mo_sequence_add"
    MO_SEQUENCE_REPAIR = "mo_sequence_repair"


class SyncActionState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class LinkActionPhase(str, Enum):
    PREVIEW = "preview"
    ACCOUNT_PREFLIGHT = "account_preflight"
    RESOURCE_PREFLIGHT = "resource_preflight"
    UNSAFE_WRITE = "unsafe_write"
    VERIFICATION = "verification"
    LOCAL_REFRESH = "local_refresh"


@dataclass(frozen=True, order=True)
class RemoteRecordKey:
    site: RemoteSite
    observation_id: int

    def __post_init__(self) -> None:
        if self.observation_id <= 0:
            raise ValueError("Remote observation IDs must be positive")


@dataclass(frozen=True)
class MediaIdentity:
    site: RemoteSite
    photo_id: str
    rendition: str = "display"
    source_site: Optional[RemoteSite] = None
    source_photo_id: str = ""

    def __post_init__(self) -> None:
        if not self.photo_id.strip():
            raise ValueError("A media identity requires a photo ID")

    @property
    def provenance_key(self) -> Optional[tuple[RemoteSite, str]]:
        if self.source_site is not None and self.source_photo_id.strip():
            return self.source_site, self.source_photo_id
        return None


@dataclass(frozen=True)
class AuthoritativeLinkRow:
    row_id: str
    external_site_id: Optional[int]
    target_site: RemoteSite
    target_observation_id: Optional[int]
    parse_state: str
    fingerprint: str


@dataclass(frozen=True)
class AuthoritativeLinkSnapshot:
    site: RemoteSite
    observation_id: int
    row_id: str
    row_uuid: str = ""
    binding_id: Optional[int] = None
    target_observation_id: Optional[int] = None
    parse_state: str = "valid"
    row_fingerprint: str = ""
    added_by_user_id: Optional[int] = None
    display_value: str = ""


@dataclass(frozen=True)
class LinkRepairOption:
    action_type: LinkActionType
    site: RemoteSite
    description: str
    destructive: bool
    mo_observation_id: int
    inat_observation_id: int
    remote_row_id: str = ""
    remote_row_uuid: str = ""
    binding_id: Optional[int] = None
    current_target_id: Optional[int] = None
    desired_target_id: Optional[int] = None
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class LinkRepairPreview:
    profile_id: int
    source_kind: str
    review_intent: str
    auth_generation: int
    mo_key_generation: int
    pair_id: Optional[int]
    issue_id: Optional[int]
    source_fingerprint: str
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_field_id: int
    mo_external_site_id: int
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    inat_links_fingerprint: str
    mo_links_fingerprint: str
    inat_rows: tuple[AuthoritativeLinkSnapshot, ...]
    mo_rows: tuple[AuthoritativeLinkSnapshot, ...]
    options: tuple[LinkRepairOption, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ITSRecordSnapshot:
    """One memory-only ITS value; raw sequence bodies must never be persisted."""

    site: RemoteSite
    observation_id: int
    remote_id: str
    remote_uuid: str = ""
    binding_id: Optional[int] = None
    value_kind: str = "empty"
    raw_sequence: str = field(default="", repr=False, compare=False)
    raw_value: str = field(default="", repr=False, compare=False)
    normalized_sequence: str = field(default="", repr=False, compare=False)
    sequence_fingerprint: str = ""
    normalized_accession: str = ""
    archive: str = ""
    label: str = ""
    public_metadata: tuple[tuple[str, str], ...] = ()
    metadata_fingerprint: str = ""
    validation_state: str = "valid"
    added_by_user_id: Optional[int] = None


@dataclass(frozen=True)
class MOSequenceRecord:
    """One complete Mushroom Observer ``Sequence`` row, kept together.

    Bases, archive, and accession all belong to the same MO record, so they are
    modelled as one composite value. Raw and normalized bases are memory-only and
    must never be persisted. Writes always operate on this whole row.
    """

    observation_id: int
    sequence_id: int
    locus: str = ""
    raw_bases: str = field(default="", repr=False, compare=False)
    normalized_sequence: str = field(default="", repr=False, compare=False)
    sequence_fingerprint: str = ""
    archive: str = ""
    normalized_accession: str = ""
    raw_accession: str = field(default="", repr=False, compare=False)
    creator_user_id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""
    notes_fingerprint: str = ""
    sequence_validation: str = "empty"
    accession_validation: str = "empty"
    record_fingerprint: str = ""
    public_metadata: tuple[tuple[str, str], ...] = ()

    @property
    def has_bases(self) -> bool:
        return bool(self.sequence_fingerprint) and self.sequence_validation == "valid"

    @property
    def has_deposit(self) -> bool:
        return (
            bool(self.normalized_accession and self.archive)
            and self.accession_validation == "valid"
        )

    @property
    def accession_identity(self) -> Optional[tuple[str, str]]:
        """Archive-qualified accession identity, never the bare string."""
        return (self.archive, self.normalized_accession) if self.has_deposit else None

    @property
    def sequence_nonempty_invalid(self) -> bool:
        return self.sequence_validation not in ("valid", "empty")

    @property
    def deposit_nonempty_invalid(self) -> bool:
        return self.accession_validation not in ("valid", "empty")

    @property
    def is_valid_row(self) -> bool:
        """The MO invariant: bases and/or a complete deposit, and no invalid component."""
        return (
            not self.sequence_nonempty_invalid
            and not self.deposit_nonempty_invalid
            and (self.has_bases or self.has_deposit)
        )


@dataclass(frozen=True)
class ITSActionOption:
    action_type: ITSActionType
    destination_site: RemoteSite
    description: str
    destructive: bool
    source_site: RemoteSite
    source_record_id: int
    destination_record_id: int
    source_remote_id: str = ""
    destination_remote_id: str = ""
    destination_remote_uuid: str = ""
    destination_binding_id: Optional[int] = None
    sequence_fingerprint: str = ""
    normalized_accession: str = ""
    archive: str = ""
    source_metadata_fingerprint: str = ""
    destination_preflight_fingerprint: str = ""
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class ITSComparisonPreview:
    profile_id: int
    pair_id: int
    auth_generation: int
    mo_key_generation: int
    source_fingerprint: str
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_field_id: int
    inat_accession_field_id: Optional[int]
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    specimen_state_fingerprint: str
    states: tuple[str, ...]
    inat_records: tuple[ITSRecordSnapshot, ...]
    mo_records: tuple[ITSRecordSnapshot, ...]
    options: tuple[ITSActionOption, ...]
    warnings: tuple[str, ...] = ()


class CoordinateActionType(str, Enum):
    """Gate 1D coordinate writes. MO observations are a read-only source, so the
    only destination is iNaturalist."""

    INAT_COORDINATE_SET = "inat_coordinate_set"
    INAT_COORDINATE_REPLACE = "inat_coordinate_replace"


class CoordinatePrivacyState(str, Enum):
    PUBLIC = "public"
    OBSCURED = "obscured"
    PRIVATE = "private"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CoordinateRecordSnapshot:
    """One memory-only coordinate value.

    Raw latitude/longitude and any exact distance must never be persisted,
    logged, put in payloads/URLs, or sent to a third-party map. The raw values
    live only in the ``repr=False, compare=False`` fields below, populated by a
    fresh remote read and dropped before the snapshot leaves the worker.

    No coordinate-derived fingerprint is stored: an unkeyed hash of a
    low-entropy point is offline-enumerable, so coordinate freshness and
    equality are established from the sites' own record/version fingerprints
    (which change with ``updated_at``) and from in-memory point comparison —
    never from a durable point-derived value.
    """

    site: RemoteSite
    observation_id: int
    coordinates_available: bool = False
    # ``privacy_state`` is the EFFECTIVE public visibility (most restrictive of the
    # observation-level and taxon-level settings). The two contributing settings
    # are kept separately so verification can check the observation-level value we
    # actually control while tolerating extra taxon-driven restriction.
    privacy_state: str = CoordinatePrivacyState.UNKNOWN.value
    # Observation-level geoprivacy we can directly write/verify: "open",
    # "obscured", "private", or "unknown" for an unrecognized nonempty value.
    observation_geoprivacy: str = "open"
    # Taxon-level geoprivacy that can independently clamp effective visibility.
    taxon_geoprivacy: str = "open"
    accuracy_m: Optional[float] = None
    # True when a coordinate almost certainly exists on the destination but its
    # exact point could not be read here (obscured/private without an authorized
    # private point). Distinct from ``coordinates_available``: an obscured record
    # may expose a public, imprecise point while its exact point stays unreadable.
    # Automated replacement is blocked whenever this is set, so a hidden exact
    # coordinate is never overwritten blindly.
    exact_point_unreadable: bool = False
    latitude: Optional[float] = field(default=None, repr=False, compare=False)
    longitude: Optional[float] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CoordinateActionOption:
    action_type: CoordinateActionType
    destination_site: RemoteSite
    source_site: RemoteSite
    source_record_id: int
    destination_record_id: int
    source_privacy_state: str
    proposed_privacy_state: str
    accuracy_m: Optional[float]
    replaces_data: bool
    # ``sends_nonpublic_source`` is true when the Mushroom Observer source point
    # is private/obscured, so copying discloses that exact point to iNaturalist.
    # ``broadens_visibility`` is a distinct concept: it is true only when the copy
    # makes the *existing* iNaturalist coordinate more publicly visible (a
    # transition to a less restrictive geoprivacy). The two are independent and
    # each warrant their own confirmation.
    sends_nonpublic_source: bool
    broadens_visibility: bool
    large_discrepancy: bool
    description: str = ""
    destructive: bool = False
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class CoordinateComparisonPreview:
    profile_id: int
    pair_id: int
    auth_generation: int
    mo_key_generation: int
    source_fingerprint: str
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    source: CoordinateRecordSnapshot
    destination: CoordinateRecordSnapshot
    options: tuple[CoordinateActionOption, ...]
    warnings: tuple[str, ...] = ()


class PhotoActionType(str, Enum):
    """Gate 1E/2A photo writes.

    ``INAT_PHOTO_ATTACH`` is the original Gate 1E MO->iNat direction: a single
    multipart ``POST /observation_photos`` call that uploads and attaches at
    once, never a separate upload+attach pair, because a photo that uploads but
    fails to attach cannot be deleted through the iNaturalist API.

    ``MO_PHOTO_ATTACH`` is the iNat->MO direction. It is NOT IMPLEMENTED:
    kept only as a historical/schema-compatibility action_type value so a
    v9-era database still loads. It is never minted by Gate 2A's preview
    (``observation_creation.py``'s ``_photo_items`` only ever generates the
    proven MO->iNat direction) and is explicitly rejected — before any
    network call — by every executor that could otherwise reach it
    (``PhotoSyncService._execute``, which unconditionally uploads to iNat via
    ``create_observation_photo_v2`` and would misdirect an MO-destination
    write; and ``ObservationCreationService._execute_followup_row``'s own
    independent guard). ``MOClient.create_image`` exists as a raw client
    method but is not wired into any reviewed, journaled, or verified write
    path.
    """

    INAT_PHOTO_ATTACH = "inat_photo_attach"
    MO_PHOTO_ATTACH = "mo_photo_attach"


@dataclass(frozen=True)
class PhotoRecordSnapshot:
    """One remote photo as read fresh from its site.

    ``byte_fingerprint``/``md5`` are populated only for a photo whose bytes have
    actually been fetched; they are safe to persist (one-way digests of image
    content) and are the only duplicate signal that survives across sites.
    Photo bytes themselves are never stored.
    """

    site: RemoteSite
    photo_id: str
    observation_id: int
    license_label: str = ""
    copyright_holder: str = ""
    # Remote account that owns the photo at its own site. Used to keep a
    # co-observer's image out of the transferable set; 0 when unknown.
    owner_id: int = 0
    # Populated for MO sources; the URL is a public CDN address, never signed.
    source_url: str = ""
    byte_fingerprint: str = ""
    md5: str = ""
    # iNaturalist only: the observation_photo join-row uuid, which is what a
    # destination re-read returns and therefore what verification keys off.
    observation_photo_uuid: str = ""

    @property
    def media_identity(self) -> MediaIdentity:
        return MediaIdentity(site=self.site, photo_id=self.photo_id)


@dataclass(frozen=True)
class PhotoIdentityPreview:
    """Fresh, read-only photo sets used to decide whether a pair is identical."""

    profile_id: int
    pair_id: int
    review_state: str
    mo_observation_id: int
    inat_observation_id: int
    mo_photos: tuple[PhotoRecordSnapshot, ...] = ()
    inat_photos: tuple[PhotoRecordSnapshot, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhotoActionOption:
    """One reviewable photo transfer. Never a batch."""

    action_type: PhotoActionType
    source_site: RemoteSite
    destination_site: RemoteSite
    source_photo_id: str
    source_record_id: int
    destination_record_id: int
    source_license_label: str = ""
    # What the photo will actually be licensed as at the destination: the
    # uploading account's default. iNaturalist accepts no license on upload.
    destination_license_note: str = ""
    source_copyright_holder: str = ""
    # True whenever the MO copyright holder is not POSITIVELY confirmed to be
    # the transferring account: a different holder, a missing holder, or a
    # profile with no verified Mushroom Observer login all set it. Authorship is
    # evaluated fail-closed, so "unproven" and "different" are the same answer.
    holder_differs: bool = False
    # SHA-256 of the image bytes as downloaded during the explicit preview. This
    # is what the user reviewed, and the write refuses to proceed if the source
    # no longer hashes to it.
    byte_fingerprint: str = ""
    description: str = ""
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class PhotoComparisonPreview:
    profile_id: int
    pair_id: int
    auth_generation: int
    mo_key_generation: int
    source_fingerprint: str
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    source_photos: tuple[PhotoRecordSnapshot, ...] = ()
    destination_photos: tuple[PhotoRecordSnapshot, ...] = ()
    options: tuple[PhotoActionOption, ...] = ()
    warnings: tuple[str, ...] = ()


class NameProposalStatus(str, Enum):
    PENDING = "pending"
    EFFECTIVE = "effective"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class NameProposalCandidate:
    """One reviewed direction for a name proposal.

    For an iNaturalist target the change is delegated to the Identify subsystem
    and requires a resolved ``proposed_taxon_id``. For a Mushroom Observer target
    the change is a tracked proposal keyed on ``proposed_name`` (submission is not
    yet wired, so a resolved ``proposed_name_id`` is best-effort).
    """

    target_site: RemoteSite
    source_site: RemoteSite
    source_name: str
    proposed_name: str
    proposed_rank: str = ""
    proposed_taxon_id: Optional[int] = None
    proposed_name_id: Optional[int] = None
    current_destination_name: str = ""
    synonyms: tuple[str, ...] = ()
    kingdom_compatible: bool = True
    string_similarity_only: bool = False
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class NameProposalPreview:
    profile_id: int
    pair_id: int
    auth_generation: int
    mo_key_generation: int
    source_fingerprint: str
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_login: str
    inat_current_name: str
    inat_current_taxon_id: Optional[int]
    mo_current_name: str
    mo_current_name_id: Optional[int]
    candidates: tuple[NameProposalCandidate, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NameDelegationRecord:
    """Link from a confirmed pair to a delegated iNaturalist identification.

    Reconciliation stores only the ``identify_action_id`` produced by the shared
    Identify subsystem; the proposed taxon, name, and intent live in the Identify
    journal and are deliberately not duplicated here.
    """

    profile_id: int
    pair_id: int
    identify_action_id: int
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MOProposalRecord:
    """A Mushroom Observer name proposal tracked separately from consensus.

    This gate is **draft-only**: Mushroom Observer exposes no per-observation
    consensus-name submission endpoint, so nothing is ever submitted remotely and
    a draft never marks the pair name-synchronized. Effectiveness (the consensus
    later matching the drafted name) is a distinct, observed state.

    ``proposal_submitted``, ``proposal_remote_id``, ``submitted_at`` and the
    ``rejected``/``superseded`` statuses are **reserved schema fields** for a
    future real submission path; no current code sets them.
    """

    profile_id: int
    pair_id: int
    mo_observation_id: int
    proposed_name: str
    proposed_name_id: Optional[int] = None
    current_effective_name: str = ""
    # Reserved for a future submission path (never set by the draft-only gate).
    proposal_submitted: bool = False
    proposal_remote_id: str = ""
    status: str = NameProposalStatus.PENDING.value
    submitted_at: str = ""
    became_effective_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReconciliationProfile:
    profile_id: int
    inat_user_id: int
    inat_login: str
    mo_user_id: int
    mo_login: str
    created_at: str = ""
    last_used_at: str = ""


@dataclass(frozen=True)
class InventoryObservation:
    key: RemoteRecordKey
    account_id: int
    owner_id: Optional[int]
    owner_login: str
    observed_on: Optional[date]
    taxon_id: Optional[int]
    taxon_name: str
    taxon_rank: str
    public_locality: str
    fungi_status: str
    updated_at: Optional[datetime]
    deleted: bool = False
    scope_state: str = "in_scope"
    availability_state: str = "available"
    content_fingerprint: str = ""
    authoritative_targets: tuple[int, ...] = ()
    link_malformed: bool = False
    authoritative_links: tuple[AuthoritativeLinkRow, ...] = ()
    identifiers: tuple[tuple[str, str], ...] = ()
    inventory_identifiers: Optional[tuple[tuple[str, str], ...]] = None
    sequence_hashes: tuple[str, ...] = ()
    inventory_sequence_hashes: Optional[tuple[str, ...]] = None
    media: tuple[MediaIdentity, ...] = ()


@dataclass(frozen=True)
class HydratedObservation:
    inventory: InventoryObservation
    voucher_identifiers: tuple[str, ...] = ()
    collection_identifiers: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    sequence_hashes: tuple[str, ...] = ()
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    coordinates_available: bool = False
    # Evidence-only provenance. Coordinate/date corroboration is fail-closed
    # unless the point is an explicit public point or an authenticated private
    # point owned by the active profile. Named-area centroids are not specimen
    # identity evidence.
    coordinate_privacy_state: str = "unknown"
    coordinate_source: str = ""
    required_values_available: bool = True
    photo_urls: tuple[tuple[MediaIdentity, str], ...] = ()
    # Gate 2A: surfaced only for a missing-observation creation preview. Never
    # used by any existing matching/specimen-identity/write path.
    description: str = ""
    attribution_name: str = ""
    specimen_available: Optional[bool] = None


@dataclass(frozen=True)
class ExternalLinkRecord:
    row_id: str
    source: RemoteRecordKey
    target: RemoteRecordKey
    authoritative: bool = True


class EvidenceTier(IntEnum):
    INVENTORY = 1
    METADATA = 2
    DEEP = 3


class EvidenceFamily(str, Enum):
    LINK = "link"
    SPECIMEN = "specimen"
    BARCODE = "barcode"
    MEDIA = "media"
    TEMPORAL = "temporal"
    SPATIAL = "spatial"
    TAXON = "taxon"
    TEXT = "text"


FAMILY_CAPS: dict[EvidenceFamily, int] = {
    EvidenceFamily.LINK: 65,
    EvidenceFamily.SPECIMEN: 50,
    EvidenceFamily.BARCODE: 50,
    EvidenceFamily.MEDIA: 60,
    EvidenceFamily.TEMPORAL: 15,
    EvidenceFamily.SPATIAL: 15,
    EvidenceFamily.TAXON: 10,
    EvidenceFamily.TEXT: 15,
}


@dataclass(frozen=True)
class MatchEvidence:
    evidence_type: str
    family: EvidenceFamily
    score: int
    explanation: str
    tier: EvidenceTier = EvidenceTier.INVENTORY


@dataclass(frozen=True)
class CandidateScore:
    total: int
    family_scores: dict[EvidenceFamily, int]
    evidence: tuple[MatchEvidence, ...]
    classification: str


@dataclass(frozen=True)
class ObservationPair:
    mo_observation_id: int
    inat_observation_id: int
    state: str
    score: int = 0
    confirmed_by: str = ""
    evidence: tuple[MatchEvidence, ...] = ()


@dataclass(frozen=True)
class SyncIssue:
    issue_type: str
    severity: str
    title: str
    detail: str
    fingerprint: str
    records: tuple[RemoteRecordKey, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReconciliationPlan:
    pairs: tuple[ObservationPair, ...]
    issues: tuple[SyncIssue, ...]
    active_pairs: tuple[tuple[int, int], ...]
    auto_confirm_pairs: tuple[tuple[int, int], ...] = ()


# --------------------------------------------------------------------------
# Gate 2A: missing-observation creation.
#
# A creation saga is a dynamically-sized sequence of ``sync_actions`` rows —
# one action_type per remote write, never an aggregate "populate" row (see
# the ``saga-architecture-rules`` project memory). ``ObservationCreationItem``
# is the reviewable unit for exactly one such write (one photo, one
# identifier, one sequence); ``ObservationCreationPreview`` is the complete
# reviewed proposal the UI renders and the user approves before ANY of those
# rows are journaled.
# --------------------------------------------------------------------------


class ObservationCreationActionType(str, Enum):
    """The two action types this feature owns end-to-end (create, finalize).

    Per-item rows (identifiers, sequences, photos, reciprocal links) reuse the
    EXISTING action types/services unchanged (``LinkActionType``,
    ``ITSActionType``, ``PhotoActionType``) — they are not redefined here.
    """

    INAT_OBSERVATION_CREATE = "inat_observation_create"
    MO_OBSERVATION_CREATE = "mo_observation_create"
    PAIR_FINALIZE = "pair_finalize"


@dataclass(frozen=True)
class ObservationCreationItem:
    """One individually reviewable remote write that will run after the
    destination observation exists (an identifier, a sequence, or a photo).

    Never a bundle: the UI selects/deselects these one at a time, mirroring
    ``PhotoActionOption``'s "never a batch" rule. ``metadata_fingerprint`` is
    what ``sync_creation_items.reviewed_metadata_fingerprint`` stores — a
    digest of the reviewed license/holder/value, never raw bytes or text.
    """

    item_type: str  # "identifier" | "photo"
    source_site: RemoteSite
    source_identity: str  # e.g. an MO photo id, or "voucher:ABC123"
    description: str
    metadata_fingerprint: str = ""
    enabled: bool = True
    disabled_reason: str = ""
    selected_by_default: bool = False  # always False in practice; kept explicit
    # Photo items only: populated so the UI can show a REAL thumbnail rather
    # than approving a photo based on an id/text label alone (section 7).
    source_url: str = ""
    license_label: str = ""
    copyright_holder: str = ""
    # Photo items only (round-3 finding 4): the byte-content fingerprint of
    # the image actually downloaded and shown during preview -- pinned HERE,
    # not at a later item-mint step, so "the reviewed image" always means
    # what the user's thumbnail actually displayed.
    reviewed_byte_fingerprint: str = ""


@dataclass(frozen=True)
class ObservationCreationPreview:
    """The complete reviewed creation proposal for one currently-unpaired
    source record. Every field a Phase 2A create request could carry is
    listed here as either transferable (with its resolved value) or as an
    entry in ``approved_field_gaps`` — never silently dropped."""

    profile_id: int
    source_site: RemoteSite
    source_observation_id: int
    destination_site: RemoteSite
    auth_generation: int
    mo_key_generation: int
    source_fingerprint: str
    destination_account_login: str
    observed_on_string: str
    taxon_name: str
    taxon_id: Optional[int]
    place_guess: str
    description: str
    # The source observer's login/display name. The spec requires the
    # creation preview to show attribution. This app never claims another
    # observer's identity: this is shown for review only, never sent as the
    # destination account (destination_account_login is the account that
    # will actually own the new observation).
    source_attribution_name: str = ""
    # The ACTUAL resolved iNaturalist taxon name the user is confirming --
    # never just ``taxon_name`` (the raw source string) again. Section 10:
    # the user must confirm the real destination taxon, which can differ from
    # the source string (an approximated base name with the MO group/clade/
    # complex suffix stripped, or empty when nothing resolved at all).
    resolved_taxon_name: str = ""
    # Source-side rank as shown during preview (section 5's persisted
    # provenance requirement) and the resolution mode that produced
    # resolved_taxon_name -- 'exact' | 'provisional_exact' |
    # 'disclosed_rank_approximation' | '' (nothing resolved).
    taxon_rank: str = ""
    resolution_mode: str = ""
    # A drift-detection fingerprint over (source name, taxon id, resolved
    # name, resolution mode). Pinned at journal time; recomputed fresh
    # immediately before the write and compared -- a mismatch blocks the
    # write rather than silently using a newly recomputed decision.
    taxon_resolution_fingerprint: str = ""
    # A drift-detection fingerprint over the COMPLETE derived creation
    # payload (date, locality, coordinates, accuracy, description,
    # attribution, sorted gap codes) -- source_fingerprint alone only covers
    # the local inventory record's (site, id, unpaired_state, local update
    # timestamp), which a remote-only change would not bump.
    reviewed_payload_fingerprint: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    positional_accuracy: Optional[float] = None
    geoprivacy: str = ""
    # Fields the direction-specific policy matrix (observation_creation.py)
    # determined cannot be transferred to this destination at all.
    approved_field_gaps: tuple[str, ...] = ()
    items: tuple[ObservationCreationItem, ...] = ()
    # Set only when the destination is MO and the correlation marker must be
    # embedded in the observation's public notes field — the UI must disclose
    # this explicitly before the user may confirm.
    marker_in_public_notes: bool = False
    # Free-text disclosures that do NOT correspond to a specific untransferred
    # field (unlike approved_field_gaps, which is a fixed set of machine-readable
    # codes _execute_pair_finalize matches exactly): a taxon approximation note,
    # an item type with no wired executor, a photo excluded at review time.
    # prepare_preview always populates this and ObservationCreationPreviewDialog
    # always renders it, so it is part of the reviewed proposal, not an optional
    # extra — every caller that builds this dataclass must pass it.
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Gate 2B M2-M3: duplicate observation consolidation.
#
# Phase 2B v1 scope is LINK-ONLY canonical-pair consolidation (per the M0
# capability audit, docs/gate_2b_capability_note.md): the only
# verified_existing write is the MO<->iNat reciprocal link, reused as-is from
# LinkRepairService once a canonical pair exists. Every optional transfer
# item type (photo, ITS/sequence, voucher/collection identifier,
# description/notes, coordinates/date/taxon) is disabled and disclosed here,
# never selectable, until a separate capability-proof spike lands.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsolidationMemberSnapshot:
    """One freshly-hydrated candidate record shown in the M3 comparison view."""

    site: RemoteSite
    observation_id: int
    remote_uuid: str = ""
    owner_login: str = ""
    owner_id: Optional[int] = None
    account_id: Optional[int] = None
    taxon_id: Optional[int] = None
    taxon_name: str = ""
    taxon_rank: str = ""
    observed_on_string: str = ""
    locality: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    geoprivacy: str = ""
    description: str = ""
    voucher_identifiers: tuple[str, ...] = ()
    collection_identifiers: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    sequence_summaries: tuple[str, ...] = ()
    photos: tuple[PhotoRecordSnapshot, ...] = ()
    reciprocal_links: tuple[AuthoritativeLinkSnapshot, ...] = ()
    photo_metadata_fingerprint: str = ""
    sequence_metadata_fingerprint: str = ""
    reciprocal_link_state: str = ""
    current_pair_partner_site: Optional[RemoteSite] = None
    current_pair_partner_id: Optional[int] = None
    current_pair_review_state: str = ""
    remote_updated_at: str = ""
    # One-way hash over the normalized fields above, never raw sensitive
    # values beyond what is already shown for review (matches
    # its._specimen_evidence_fingerprint's non-reversible-hash convention).
    record_fingerprint: str = ""
    # Same reviewed record with reciprocal-link rows omitted. Core link writes
    # intentionally change those rows, so later saga ordinals compare this
    # stable fingerprint while each link action pins its own exact link state.
    preflight_fingerprint: str = ""
    # Narrow durable identity: remote site/id/UUID and reviewed owner/account.
    # Mutable observation content is deliberately excluded.
    identity_fingerprint: str = ""
    # Safe one-way component fingerprints used to explain mutable canonical
    # changes between successful baselines.
    mutable_component_fingerprints: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ConsolidationItemDisclosure:
    """One optional transfer item type, disclosed but not selectable in v1."""

    item_type: str
    description: str
    enabled: bool = False
    disabled_reason: str = ""


@dataclass(frozen=True)
class ConsolidationConflict:
    """One M6 data conflict requiring an explicit decision, never auto-resolved."""

    conflict_type: str
    description: str
    blocking: bool = False


@dataclass(frozen=True)
class ConsolidationEligibility:
    """M2 duplicate-set eligibility result. A non-empty ``blocking_reasons``
    means the candidate set may not proceed to a consolidation preview at
    all — identity uncertainty is fail-closed, never a soft warning."""

    eligible: bool
    blocking_reasons: tuple[str, ...] = ()
    supporting_evidence: tuple[str, ...] = ()
    evidence_unavailable: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsolidationEvidenceEdge:
    """One positive, pairwise specimen-identity fact reviewed for an attempt.

    ``reviewed_evidence_fingerprint`` hashes the normalized proof inputs.  The
    display summary is deliberately non-sensitive and is the only metadata
    persisted alongside the hash.
    """

    left_site: RemoteSite
    left_observation_id: int
    right_site: RemoteSite
    right_observation_id: int
    evidence_type: str
    evidence_strength: str
    reviewed_evidence_fingerprint: str
    display_summary: str


@dataclass(frozen=True)
class ConsolidationEvidencePath:
    """Auditable path from one proposed donor to the canonical specimen."""

    donor_site: RemoteSite
    donor_observation_id: int
    steps: tuple[str, ...]
    strong_anchor_step: str = ""


@dataclass(frozen=True)
class PriorConsolidationMember:
    """Read-only member from a stable consolidation being extended."""

    site: RemoteSite
    observation_id: int
    role: str
    local_state: str
    added_by_attempt_id: Optional[int] = None
    superseded_by_attempt_id: Optional[int] = None
    superseded_at: str = ""


@dataclass(frozen=True)
class ConsolidationPreview:
    """The complete M3 reviewed consolidation proposal. The canonical
    observation(s) are UNSET by default — never preselected."""

    profile_id: int
    consolidation_id: Optional[int]
    members: tuple[ConsolidationMemberSnapshot, ...]
    eligibility: ConsolidationEligibility
    auth_generation: int = 0
    mo_key_generation: int = 0
    canonical_mo_observation_id: Optional[int] = None
    canonical_inat_observation_id: Optional[int] = None
    unsupported_items: tuple[ConsolidationItemDisclosure, ...] = ()
    conflicts: tuple[ConsolidationConflict, ...] = ()
    local_changes_preview: tuple[str, ...] = ()
    evidence_edges: tuple[ConsolidationEvidenceEdge, ...] = ()
    donor_evidence_paths: tuple[ConsolidationEvidencePath, ...] = ()
    is_extension: bool = False
    previous_members: tuple[PriorConsolidationMember, ...] = ()
    donor_retention_notice: str = (
        "Donor observations will remain online. This phase does not delete or hide them."
    )
    deferred_deletion_notice: str = (
        "Deleting, hiding, or withdrawing a donor observation is out of scope for Phase "
        "2B and requires a separately designed and reviewed Phase 2C."
    )

    @property
    def donor_members(self) -> tuple[ConsolidationMemberSnapshot, ...]:
        canonical_ids = {
            (RemoteSite.MO, self.canonical_mo_observation_id),
            (RemoteSite.INAT, self.canonical_inat_observation_id),
        }
        return tuple(
            member
            for member in self.members
            if (member.site, member.observation_id) not in canonical_ids
        )

    @property
    def canonical_members(self) -> tuple[ConsolidationMemberSnapshot, ...]:
        canonical_ids = {
            (RemoteSite.MO, self.canonical_mo_observation_id),
            (RemoteSite.INAT, self.canonical_inat_observation_id),
        }
        return tuple(
            member
            for member in self.members
            if (member.site, member.observation_id) in canonical_ids
        )

    warnings: tuple[str, ...] = ()
