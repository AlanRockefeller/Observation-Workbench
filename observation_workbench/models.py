"""
Core data models for Observation Workbench.
All fields are defensive (Optional where API may omit them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar, List, Optional

# ---------------------------------------------------------------------------
# Photo size constants (iNaturalist-defined sizes)
# ---------------------------------------------------------------------------
PHOTO_SIZES = ["original", "large", "medium", "small", "thumb", "square"]

# The size token in an iNaturalist photo URL, e.g. ".../photos/12345/square.jpg".
# A URL that does not match this cannot have its size substituted at all, which
# StudyPhoto.candidate_size_urls must detect rather than silently mislabel.
_SIZE_TOKEN_RE = re.compile(
    r"/(square|thumb|small|medium|large|original)\.(jpe?g|png|gif|webp)",
    re.IGNORECASE,
)


@dataclass
class StudyTaxon:
    # Class-level flag: controlled by AppSettings.show_common_names at startup
    # and whenever the settings dialog is accepted. False = scientific names only.
    show_common_names: ClassVar[bool] = False

    taxon_id: int
    name: str
    common_name: str = ""
    rank: str = ""
    iconic_taxon_name: str = ""
    ancestry: str = ""  # e.g. "48460/1/2/355675"

    @property
    def display_name(self) -> str:
        if StudyTaxon.show_common_names and self.common_name:
            return f"{self.common_name} ({self.name})"
        return self.name

    @property
    def short_display(self) -> str:
        if StudyTaxon.show_common_names:
            return self.common_name or self.name
        return self.name


@dataclass
class StudyPhoto:
    photo_id: int
    url_square: str  # canonical square URL; derive other sizes from this
    attribution: str = ""
    license_code: str = ""

    def url_for_size(self, size: str = "original") -> str:
        """Derive URL for a given size by replacing the size token.

        iNat photo URLs look like:
          https://inaturalist-open-data.s3.amazonaws.com/photos/12345/square.jpg
          https://static.inaturalist.org/photos/12345/square.jpeg

        NOTE: 'original' may still be capped at ~2048px on iNat's side.
        Flickr-hosted legacy photos use a different URL scheme and may not
        support this substitution — we attempt it and fall back gracefully.
        """
        if not self.url_square:
            return ""
        return _SIZE_TOKEN_RE.sub(rf"/{size}.\2", self.url_square)

    @property
    def url_original(self) -> str:
        return self.url_for_size("original")

    @property
    def url_large(self) -> str:
        return self.url_for_size("large")

    @property
    def url_medium(self) -> str:
        return self.url_for_size("medium")

    def candidate_size_urls(self) -> List[tuple[str, str]]:
        """Return unique ``(size, url)`` candidates in descending quality order.

        When ``url_square`` carries no substitutable size token — Flickr-hosted
        legacy photos, per :meth:`url_for_size` — every size derives the SAME
        url, and de-duplication collapses them to one candidate. Labelling that
        candidate ``"original"`` would be a lie the rest of the stack believes:
        the prefetcher caches the thumbnail bytes under size ``original``,
        records ``loaded_size = "original"``, clears its failure state, and then
        never attempts an upgrade or offers a manual retry again. Report it
        honestly at the lowest tier instead, so it still displays but is never
        mistaken for full quality.
        """
        if not self.url_square:
            return []
        if not _SIZE_TOKEN_RE.search(self.url_square):
            return [("square", self.url_square)]
        seen: set[str] = set()
        result = []
        for size in ("original", "large", "medium", "small"):
            u = self.url_for_size(size)
            if u and u not in seen:
                seen.add(u)
                result.append((size, u))
        return result

    def candidate_urls(self) -> List[str]:
        """Backward-compatible URL-only view of :meth:`candidate_size_urls`."""
        return [url for _, url in self.candidate_size_urls()]


@dataclass
class StudyIdentification:
    ident_id: int
    taxon: StudyTaxon
    user_login: str
    user_id: Optional[int] = None
    body: str = ""
    is_leading: Optional[bool] = None  # None = field absent from API payload
    category: str = ""
    disagreement: Optional[bool] = None  # None = unknown, True = disagrees
    created_at: str = ""
    current: bool = True
    own_observation: Optional[bool] = None

    @property
    def is_disagreement(self) -> bool:
        return bool(self.disagreement)

    @property
    def is_provisional(self) -> bool:
        return "'" in (self.taxon.name or "")


@dataclass
class StudyComment:
    comment_id: int
    user_login: str
    body: str = ""
    created_at: str = ""
    hidden: bool = False


@dataclass
class StudyVote:
    vote_id: int
    user_login: str
    vote_scope: str = ""
    vote_flag: Optional[bool] = None


@dataclass
class StudyObservation:
    obs_id: int
    observer_login: str
    uuid: str = ""
    observed_on: str = ""
    place_guess: str = ""
    taxon: Optional[StudyTaxon] = None  # observation's own taxon (obs taxon)
    community_taxon: Optional[StudyTaxon] = None  # community ID taxon
    photos: List[StudyPhoto] = field(default_factory=list)
    target_identification: Optional[StudyIdentification] = None  # ID by queried user
    all_identifications: List[StudyIdentification] = field(default_factory=list)
    comments: List[StudyComment] = field(default_factory=list)
    votes: List[StudyVote] = field(default_factory=list)
    provisional_species_name: str = ""
    species_name_override: str = ""
    dna_barcode_its: str = ""
    quality_grade: str = ""
    obscured: bool = False
    num_identification_agreements: int = 0
    num_identification_disagreements: int = 0
    created_at: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    positional_accuracy: Optional[float] = None
    description: str = ""
    captive: Optional[bool] = None
    reviewed_by: List[int] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.inaturalist.org/observations/{self.obs_id}"

    @property
    def display_taxon(self) -> Optional[StudyTaxon]:
        """Best taxon to display: first of target ID / community / obs taxon with a usable name."""

        def _usable(t: Optional["StudyTaxon"]) -> bool:
            return t is not None and bool(t.name) and t.name != "Unknown"

        candidates = [
            self.target_identification.taxon if self.target_identification else None,
            self.community_taxon,
            self.taxon,
        ]
        for t in candidates:
            if _usable(t):
                return t

        # Nothing usable — fall back to original priority order
        if self.target_identification:
            return self.target_identification.taxon
        return self.community_taxon or self.taxon

    @property
    def display_date(self) -> str:
        return self.observed_on[:10] if self.observed_on else "Unknown date"


@dataclass
class TaxonCount:
    taxon: StudyTaxon
    count: int


@dataclass
class TaxonSummary:
    counts: List[TaxonCount] = field(default_factory=list)
    total: int = 0
