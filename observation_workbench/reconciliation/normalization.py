"""Strict normalizers for remote references and non-reversible evidence."""
from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import urlsplit

_MO_HOSTS = frozenset({"mushroomobserver.org", "www.mushroomobserver.org"})
_MO_PATHS = (
    re.compile(r"^/obs/(?P<id>[1-9]\d*)/?$", re.IGNORECASE),
    re.compile(r"^/observations/(?P<id>[1-9]\d*)/?$", re.IGNORECASE),
    re.compile(r"^/observer/show_observation/(?P<id>[1-9]\d*)/?$", re.IGNORECASE),
    re.compile(r"^/(?P<id>[1-9]\d*)/?$"),
)
_INAT_HOSTS = frozenset({"inaturalist.org", "www.inaturalist.org"})
_INAT_PATH = re.compile(r"^/observations/(?P<id>[1-9]\d*)/?$", re.IGNORECASE)
# INSDC nucleotide accessions (GenBank/ENA share a format) are the only values a
# GenBank field may hold. UNITE species hypotheses and BOLD process IDs are real,
# comparable barcode identifiers but belong to different namespaces and must never
# be silently treated as GenBank deposits.
#
# The letter/digit counts below are the DEFINED INSDC accession shapes, not a
# loose range. A permissive `[A-Z]{1,6}_?\d{3,12}` also matched ordinary
# specimen labels — herbarium and personal collection numbers like AR21309 or
# MB0034567 are exactly 1-6 letters followed by 3+ digits. Those were then
# classified as valid GenBank accessions and became eligible to be written to
# Mushroom Observer as a public GenBank DEPOSIT for a record that was never
# deposited. Anything outside these shapes now classifies as "not an accession",
# which fails closed: the ITS gate marks it invalid and blocks the write.
_INSDC_ACCESSION = re.compile(
    r"(?:"
    r"[A-Z]\d{5}"          # 1 + 5   e.g. U12345
    r"|[A-Z]{2}\d{6}"      # 2 + 6   e.g. AF123456, MK000001
    r"|[A-Z]{2}\d{8}"      # 2 + 8   e.g. KY1234567 8-digit series
    r"|[A-Z]{3}\d{5}"      # 3 + 5   protein
    r"|[A-Z]{4}\d{8,10}"   # 4 + 8-10  WGS
    r"|[A-Z]{5}\d{7}"      # 5 + 7   MGA
    r"|[A-Z]{6}\d{9,11}"   # 6 + 9-11  TSA/WGS extended
    r"|[A-Z]{2}_\d{6,9}"   # RefSeq, e.g. NC_000001, NM_000546
    r")"
    r"(?:\.\d+)?"          # optional version suffix
)
_UNITE_ACCESSION = re.compile(r"SH\d{6,9}\.\d{2}FU")
_BOLD_ACCESSION = re.compile(r"[A-Z][A-Z0-9]{1,11}-\d{2,10}")
# Canonical Mushroom Observer archive spellings. MO's Sequence archive parser
# accepts exactly GenBank, ENA, and UNITE (see the MO source archive list). BOLD
# and unknown archives are not accepted by MO and must not be written there,
# though BOLD remains a recognised namespace for read-only comparison/display.
MO_GENBANK_ARCHIVE = "GenBank"
MO_ENA_ARCHIVE = "ENA"
MO_UNITE_ARCHIVE = "UNITE"
MO_BOLD_ARCHIVE = "BOLD"
# Archives MO's Sequence API will accept as a deposit value.
MO_WRITABLE_ARCHIVES = frozenset({MO_GENBANK_ARCHIVE, MO_ENA_ARCHIVE, MO_UNITE_ARCHIVE})


def parse_mo_observation_url(value: object) -> Optional[int]:
    """Return a canonical MO ID only when the entire value is an allowed URL."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"} or parts.hostname not in _MO_HOSTS:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.username or parts.password or port not in (None, 80, 443):
        return None
    for pattern in _MO_PATHS:
        match = pattern.fullmatch(parts.path)
        if match:
            return int(match.group("id"))
    return None


def parse_inat_observation_url(value: object) -> Optional[int]:
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"} or parts.hostname not in _INAT_HOSTS:
        return None
    match = _INAT_PATH.fullmatch(parts.path)
    return int(match.group("id")) if match else None


def normalize_identifier(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _accession_text(value: object) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def accession_namespace(value: object) -> str:
    """Classify the archive namespace of an accession-shaped value by format.

    Returns ``UNITE`` or ``BOLD`` for their distinctive formats, ``GenBank`` for
    the shared INSDC (GenBank/ENA) nucleotide format — which the format alone
    cannot disambiguate from ENA — or an empty string when the value is not a
    recognised accession. A returned namespace never proves cross-namespace
    equivalence: identity is always the ``(archive, accession)`` pair, and the
    authoritative archive for a real record comes from its source, not this guess.
    """
    text = _accession_text(value)
    if not text:
        return ""
    if _UNITE_ACCESSION.fullmatch(text):
        return MO_UNITE_ARCHIVE
    if _BOLD_ACCESSION.fullmatch(text):
        return MO_BOLD_ARCHIVE
    if _INSDC_ACCESSION.fullmatch(text):
        return MO_GENBANK_ARCHIVE
    return ""


def normalize_accession(value: object) -> str:
    """Return the normalized accession string when it is valid in any namespace."""
    text = _accession_text(value)
    return text if accession_namespace(text) else ""


def is_genbank_accession(value: object) -> bool:
    """True only for values whose format is a valid INSDC (GenBank/ENA) accession."""
    return accession_namespace(value) == MO_GENBANK_ARCHIVE


def normalize_archive(value: object, accession: object = None) -> str:
    """Map an archive label to a canonical MO archive spelling.

    MO recognises GenBank, ENA, and UNITE as distinct archives; ENA is not a
    spelling of GenBank. An unrecognised explicit label is preserved verbatim (it
    will not be writable to MO). When no archive label is present we fall back to
    the namespace implied by the accession, preserving archive information without
    inferring GenBank for every accession-shaped value.
    """
    text = " ".join(str(value or "").strip().casefold().split())
    known = {
        "genbank": MO_GENBANK_ARCHIVE, "ncbi": MO_GENBANK_ARCHIVE,
        "ena": MO_ENA_ARCHIVE, "unite": MO_UNITE_ARCHIVE,
    }
    if text in known:
        return known[text]
    if text:
        # Preserve an unknown but explicit MO archive label verbatim rather than
        # discarding or reclassifying it. It will fail the writable-archive gate.
        return str(value).strip()
    return accession_namespace(accession) if accession is not None else ""


def is_mo_writable_archive(archive: object) -> bool:
    """True only for archives MO's Sequence API accepts as a deposit value."""
    return str(archive or "").strip() in MO_WRITABLE_ARCHIVES


def normalize_sequence(value: object) -> str:
    """Normalize IUPAC DNA characters; reject prose and unusably short values."""
    lines = [
        line for line in str(value or "").splitlines()
        if not line.lstrip().startswith(">")
    ]
    text = re.sub(r"[\s\d.-]+", "", "\n".join(lines)).upper()
    if len(text) < 20 or re.search(r"[^ACGTRYSWKMBDHVN]", text):
        return ""
    return text


def reverse_complement(sequence: str) -> str:
    """Return the IUPAC reverse complement of an already normalized sequence."""
    return sequence.translate(str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN"))[::-1]


def sequence_digest(value: object) -> str:
    """Hash the canonical orientation so reverse complements compare equal."""
    sequence = normalize_sequence(value)
    if not sequence:
        return ""
    reverse = reverse_complement(sequence)
    canonical = min(sequence, reverse)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def public_fingerprint(*values: object) -> str:
    canonical = "\x1f".join(str(value or "").strip() for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
