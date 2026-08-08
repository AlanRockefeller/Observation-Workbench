"""Gate 1E photo licensing and provenance primitives (network-free).

This module holds the deterministic pieces of the photo-transfer gate that do
not depend on any unproven remote behaviour (see ``docs/gate_1e_capability_report.md``):

* the license map between Mushroom Observer licenses and iNaturalist
  ``license_code`` values, and
* content fingerprints over raw photo bytes.

Nothing here uploads, attaches, or reads the network. No photo bytes are
persisted by anything in this module; the fingerprints are one-way digests.

Why this is a table and not a parser
------------------------------------
An earlier version inferred Creative Commons clauses from the license *text*.
The 2026-07-23 live proof (report §12.3) showed that is wrong for three of the
eight licenses Mushroom Observer actually uses: MO names ids 1-3 without the
word "Attribution" ("Creative Commons Non-commercial v3.0") and names id 3 with
no clause at all ("Creative Commons Wikipedia Compatible v3.0"), so a
clause-scanning parser marked all three unmappable. Id 3 alone covers roughly
58,000 images — the most common license on the site.

MO also requires a **numeric License id** on write and returns only the license
**name** on read, and exposes no ``/api2/licenses`` endpoint, so both directions
of the mapping have to live here as a reviewed constant.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

# iNaturalist ``license_code`` values (the CC family is version 4.0). ``None`` /
# absent means "all rights reserved" on iNaturalist. iNaturalist is inconsistent
# about case -- reads return ``cc-by``, a ``PUT /photos/{id}`` response returns
# ``CC-BY-NC`` -- so every comparison against a live value must be casefolded
# (report §12.1b). Use :func:`normalise_inat_license_code`.
INAT_CC0 = "cc0"
INAT_LICENSE_CODES = frozenset(
    {"cc0", "cc-by", "cc-by-nc", "cc-by-sa", "cc-by-nd", "cc-by-nc-sa", "cc-by-nc-nd"}
)


class LicensePreservation:
    """How exactly a source license can be reproduced at the destination."""

    EXACT = "exact"  # same license, same version
    VERSION_SHIFTED = "version_shifted"  # same CC clauses, different version
    UNSUPPORTED = "unsupported"  # all-rights-reserved, unknown, unmappable


@dataclass(frozen=True)
class MOLicense:
    """One Mushroom Observer License row, as verified live on 2026-07-23.

    ``license_id`` and ``mo_name`` are **verified** against the live API.
    ``inat_license_code`` is *inferred from the name* for the rows where
    ``clauses_confirmed`` is False -- see the class docstring warning below.
    """

    license_id: int
    mo_name: str
    inat_license_code: Optional[str]
    preservation: str
    # False where MO's name does not state the CC clauses outright AND no
    # authoritative source confirms them, so the mapping is a guess. Such a
    # license may be *displayed*, but must not silently drive a write to
    # Mushroom Observer: writing the wrong id mis-states a copyright term.
    # See :func:`inat_code_to_mo_license_id`.
    #
    # NO ROW BELOW CURRENTLY SETS THIS FALSE, and that is correct, not an
    # oversight: ids 1-6 are confirmed against MO's own licenses.yml fixture
    # (which pairs each display_name with its canonical creativecommons.org
    # URL) and ids 7-8 state every clause in their names. An earlier draft
    # inferred the clauses from the names alone and marked ids 1-3 unconfirmed;
    # the fixture settled them, so the flag is now a live mechanism with no
    # current members rather than a description of this table. A future MO
    # license whose clauses cannot be established must set it False.
    clauses_confirmed: bool = True


# The eight MO licenses. Ids and display names verified live against the API
# (report §12.2b); the CC equivalents confirmed 2026-07-23 against Mushroom
# Observer's own fixtures, which pair each display_name with its canonical
# creativecommons.org URL:
#
#     test/fixtures/licenses.yml, MushroomObserver/mushroom-observer
#
# That source matters. MO's "Creative Commons Non-commercial vN" names do NOT
# mean CC BY-NC -- they map to ``licenses/by-nc-sa/N``, i.e. they carry a
# **ShareAlike** clause the display name never mentions. Reading those names as
# CC BY-NC (as an earlier draft of this table did) silently drops SA and
# mis-states the license. Do not "simplify" this table from the names.
MO_LICENSES: dict[int, MOLicense] = {
    # by-nc-sa/2.5 -- ShareAlike, despite the name
    1: MOLicense(
        1,
        "Creative Commons Non-commercial v2.5",
        "cc-by-nc-sa",
        LicensePreservation.VERSION_SHIFTED,
    ),
    # by-nc-sa/3.0 -- ShareAlike, despite the name
    2: MOLicense(
        2,
        "Creative Commons Non-commercial v3.0",
        "cc-by-nc-sa",
        LicensePreservation.VERSION_SHIFTED,
    ),
    # by-sa/3.0
    3: MOLicense(
        3,
        "Creative Commons Wikipedia Compatible v3.0",
        "cc-by-sa",
        LicensePreservation.VERSION_SHIFTED,
    ),
    # public-domain/cc0
    4: MOLicense(
        4, "Public Domain (Wikipedia compatible)", INAT_CC0, LicensePreservation.EXACT
    ),
    # by/4.0
    5: MOLicense(
        5,
        "Creative Commons Attribution v4.0 (Wikipedia compatible)",
        "cc-by",
        LicensePreservation.EXACT,
    ),
    # by-nc/4.0 -- note this one really is plain NC, unlike ids 1-2
    6: MOLicense(
        6,
        "Creative Commons Attribution Non-commercial v4.0",
        "cc-by-nc",
        LicensePreservation.EXACT,
    ),
    # Ids 7-8 postdate the fixtures, but their names state every clause.
    7: MOLicense(
        7,
        "Creative Commons Attribution Non-commercial NoDerivs v.4.0",
        "cc-by-nc-nd",
        LicensePreservation.EXACT,
    ),
    8: MOLicense(
        8,
        "Creative Commons Attribution Non-commercial ShareAlike v4.0",
        "cc-by-nc-sa",
        LicensePreservation.EXACT,
    ),
}

# Exact-match name lookup. Deliberately not fuzzy: an unrecognised name must
# fail closed rather than be guessed at.
_MO_LICENSE_BY_NAME: dict[str, MOLicense] = {
    _normalised: entry
    for entry in MO_LICENSES.values()
    for _normalised in (" ".join(entry.mo_name.split()).casefold(),)
}


@dataclass(frozen=True)
class LicenseVerdict:
    """The result of mapping one source license toward iNaturalist."""

    source_label: str
    inat_license_code: Optional[str]
    preservation: str
    # ``eligible`` means the license can be represented at the destination:
    # EXACT and VERSION_SHIFTED are eligible, UNSUPPORTED is not. The
    # distinction is surfaced to the user, never silently collapsed.
    eligible: bool
    mo_license_id: Optional[int] = None
    # False when the CC clauses were inferred from MO's license name rather
    # than stated by it. Display-safe; not write-safe.
    clauses_confirmed: bool = True

    @property
    def version_shift_notice(self) -> str:
        if self.preservation == LicensePreservation.VERSION_SHIFTED:
            return (
                f"'{self.source_label}' will be recorded on iNaturalist as "
                f"'{self.inat_license_code}' (same terms, different Creative "
                "Commons version); it is not byte-identical to the source."
            )
        return ""

    @property
    def confirmation_notice(self) -> str:
        if not self.clauses_confirmed:
            return (
                f"'{self.source_label}' does not state its Creative Commons "
                f"clauses; '{self.inat_license_code}' is inferred from the "
                "license name and has not been confirmed with Mushroom Observer."
            )
        return ""


_UNSUPPORTED = LicensePreservation.UNSUPPORTED


def _unsupported(label: str) -> LicenseVerdict:
    return LicenseVerdict(label, None, _UNSUPPORTED, False)


def normalise_inat_license_code(value: object) -> str:
    """Casefold an iNaturalist ``license_code`` for comparison.

    iNaturalist returns ``cc-by`` from reads but ``CC-BY-NC`` from a
    ``PUT /photos/{id}`` response (report §12.1b), so raw string equality
    against a live value is unreliable.
    """
    return " ".join(str(value or "").split()).casefold()


def lookup_mo_license(source: object) -> Optional[MOLicense]:
    """Resolve a MO license id (int or digit string) or exact name to a row."""
    if isinstance(source, bool):
        return None
    if isinstance(source, int):
        return MO_LICENSES.get(source)
    text = " ".join(str(source or "").split())
    if not text:
        return None
    if text.isdigit():
        return MO_LICENSES.get(int(text))
    return _MO_LICENSE_BY_NAME.get(text.casefold())


def map_mo_license_to_inat(source_label: object) -> LicenseVerdict:
    """Map a Mushroom Observer license toward an iNaturalist ``license_code``.

    Accepts a numeric License id (what MO requires on write) or the exact
    license name (what MO returns on read). Anything else -- an unknown name,
    "All Rights Reserved", empty -- is UNSUPPORTED and ineligible; the mapping
    fails closed rather than guessing at a copyright term.
    """
    text = " ".join(str(source_label or "").split())
    entry = lookup_mo_license(source_label)
    if entry is None:
        return _unsupported(text)
    # An id is not a label a user should ever be shown; resolve it to the name
    # so the notices read as license terms rather than as a bare number.
    return LicenseVerdict(
        source_label=entry.mo_name if (not text or text.isdigit()) else text,
        inat_license_code=entry.inat_license_code,
        preservation=entry.preservation,
        eligible=entry.preservation != _UNSUPPORTED,
        mo_license_id=entry.license_id,
        clauses_confirmed=entry.clauses_confirmed,
    )


def inat_code_to_mo_license_id(
    license_code: object, *, allow_unconfirmed: bool = False
) -> Optional[int]:
    """Map an iNaturalist ``license_code`` to a MO License id, for iNat->MO writes.

    Returns ``None`` when no MO license represents the code, which callers must
    treat as "not transferable", never as a default.

    A row whose clauses could not be established (``clauses_confirmed`` False)
    is refused unless ``allow_unconfirmed`` is set, because writing the wrong id
    mis-states a copyright term. **No row in the current table is unconfirmed**
    (see :class:`MOLicense` — MO's own fixture settled ids 1-6 and ids 7-8 state
    their clauses), so today this parameter changes nothing; it is the gate a
    future unestablished license would pass through, not a description of the
    table as it stands.

    Prefer an exact-version match when several ids share a code: ``cc-by-nc-sa``
    maps to id 8 (4.0), not id 1 (2.5) or id 2 (3.0). Note that a code with only
    a VERSION_SHIFTED row still resolves — ``cc-by-sa`` returns id 3, MO's 3.0 —
    so a caller that must not silently change the CC version has to check
    :attr:`LicenseVerdict.preservation` / ``version_shift_notice`` from
    :func:`map_mo_license_to_inat` as well; this function reports the id only.
    """
    code = normalise_inat_license_code(license_code)
    if not code:
        return None
    candidates = [
        entry
        for entry in MO_LICENSES.values()
        if normalise_inat_license_code(entry.inat_license_code) == code
        and (entry.clauses_confirmed or allow_unconfirmed)
    ]
    if not candidates:
        return None
    # Exact-version rows first, then lowest id for determinism.
    candidates.sort(
        key=lambda entry: (
            entry.preservation != LicensePreservation.EXACT,
            entry.license_id,
        )
    )
    return candidates[0].license_id


def photo_byte_fingerprint(data: bytes) -> str:
    """One-way SHA-256 digest of raw photo bytes, for duplicate *display*.

    This is deliberately a plain content hash: it detects byte-identical images
    (the same file transferred twice) but not re-encoded copies. It is safe to
    persist because it reveals nothing about the image content and cannot be
    reversed. Photo bytes themselves are never stored.
    """
    if not data:
        return ""
    return hashlib.sha256(data).hexdigest()


def normalized_pixel_fingerprint(data: bytes) -> str:
    """Re-encode-tolerant content digest, for comparing against a REMOTE copy.

    :func:`photo_byte_fingerprint` only matches byte-identical files, which is
    useless against a destination image: iNaturalist re-encodes and rescales
    everything it stores, so the same photograph never hashes the same there.
    This computes a difference hash (dHash) instead -- luminance of a 9x8
    downscale, comparing each pixel with its right-hand neighbour.

    **This is a best-effort similarity signal, not proof of identity or of
    difference.** It is designed for, and expected to handle, the ordinary
    iNaturalist path of JPEG re-encoding and rescaling. It is not robust against
    a crop, a rotation, a flip or a substantial tonal edit -- no simple
    perceptual hash is -- so a meaningfully edited copy of the same photograph
    can still evade it, and a match is evidence rather than certainty.

    Because of that, **similarity is only ever allowed to BLOCK a transfer,
    never to trigger one** (report §10). A false positive costs a refused upload
    the user can override by other means; a false negative costs a duplicate
    photo that cannot be deleted. It is therefore used fail-closed, and it is
    one of three duplicate signals rather than the only one.

    Returns "" when the bytes cannot be decoded as an image.
    """
    if not data:
        return ""
    # Imported lazily: this keeps the module importable (and unit-testable)
    # without Qt, and QImage needs no display, unlike QPixmap.
    try:
        from PySide6.QtCore import QBuffer, QByteArray, Qt
        from PySide6.QtGui import QImage, QImageReader
    except ImportError:  # pragma: no cover - Qt is a hard runtime dependency
        return ""
    # Read through QImageReader with autoTransform so an EXIF orientation tag is
    # APPLIED. QImage.loadFromData ignores it, which would fingerprint a rotated
    # camera JPEG differently from the upright copy iNaturalist serves back --
    # a false negative, and a false negative here means a duplicate upload.
    # ``payload`` MUST be held in a local: QBuffer does not take ownership, and
    # a temporary QByteArray is collected before the reader touches it, which
    # fails as "Unsupported image format" rather than anything obvious.
    payload = QByteArray(data)
    buffer = QBuffer(payload)
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    image = reader.read()
    buffer.close()
    if image.isNull():
        return ""
    scaled = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        9,
        8,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    if scaled.isNull() or scaled.width() < 9 or scaled.height() < 8:
        return ""
    bits = 0
    index = 0
    for y in range(8):
        for x in range(8):
            if scaled.pixelColor(x, y).value() > scaled.pixelColor(x + 1, y).value():
                bits |= 1 << index
            index += 1
    return f"dhash8:{bits:016x}"


def pixel_fingerprint_distance(left: str, right: str) -> Optional[int]:
    """Return the 64-bit dHash Hamming distance, or ``None`` if invalid."""
    if (
        not left
        or not right
        or not left.startswith("dhash8:")
        or not right.startswith("dhash8:")
    ):
        return None
    try:
        a = int(left.split(":", 1)[1], 16)
        b = int(right.split(":", 1)[1], 16)
    except ValueError:
        return None
    return (a ^ b).bit_count()


def pixel_fingerprints_match(left: str, right: str, *, max_distance: int = 4) -> bool:
    """Compare two :func:`normalized_pixel_fingerprint` values.

    A small Hamming distance is tolerated because re-encoding perturbs a few
    low-contrast comparisons. The threshold is deliberately loose: this only
    ever blocks a transfer, so over-matching is the safe direction of error.
    """
    distance = pixel_fingerprint_distance(left, right)
    return distance is not None and distance <= max_distance


def photo_md5(data: bytes) -> str:
    """MD5 digest of raw photo bytes, as Mushroom Observer's ``md5sum`` field.

    ``POST /api2/images`` accepts ``md5sum`` (report §12.2d), which makes this a
    genuine *server-side* duplicate key for the iNat->MO direction -- unlike
    :func:`photo_byte_fingerprint`, which only ever informs the local ledger and
    the preview. MD5 is fixed by MO's API contract; it is used here purely as a
    content fingerprint, never for integrity against an adversary.
    """
    if not data:
        return ""
    return hashlib.md5(data).hexdigest()
