"""Persistence-safe domain objects for DNA observation linking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


ALGORITHM_VERSION = "dna-link-v2"
DNA_FIELD_NAME = "DNA Barcode ITS"
FUNGI_TAXON_ID = 47170


@dataclass(frozen=True)
class ScanConfig:
    source_url: str
    source_params: tuple[tuple[str, str], ...]
    candidate_login: str = ""
    radius_m: float = 100.0
    window_minutes: float = 15.0
    chunk_size: int = 100


@dataclass(frozen=True)
class ObservationSnapshot:
    observation_id: int
    uuid: str
    observer: str
    observed_at: str
    latitude: float
    longitude: float
    positional_accuracy: Optional[float]
    taxon_id: int
    taxon_name: str
    taxon_rank: str
    family_id: Optional[int]
    family_name: str
    photo_urls: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CandidatePair:
    source: ObservationSnapshot
    candidate: ObservationSnapshot
    distance_m: float
    time_difference_seconds: float
    distance_score: float
    time_score: float
    family_score: float
    score: int


@dataclass(frozen=True)
class DiscoveryProgress:
    message: str
    calls_made: int
    calls_estimated: int
    source_rows: int
    valid_sources: int
    pairs: int


@dataclass(frozen=True)
class FieldRow:
    row_id: str
    value: str


@dataclass(frozen=True)
class DestinationState:
    observation_id: int
    observation_uuid: str
    rows: tuple[FieldRow, ...]
    fingerprint: str
