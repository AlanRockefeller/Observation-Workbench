"""Read-only iNaturalist inventory parsing and field-link validation."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from observation_workbench.api.client import INatClient

from .normalization import normalize_accession, parse_mo_observation_url, public_fingerprint, sequence_digest
from .types import (
    AuthoritativeLinkRow, InventoryObservation, MediaIdentity, RemoteRecordKey,
    RemoteSite,
)

MO_FIELD_NAME = "Mushroom Observer URL"
ITS_FIELD_NAME = "DNA Barcode ITS"
ACCESSION_FIELD_NAME = "Genbank Accession Number"


class INatReconciliationReader:
    def __init__(self, client: INatClient) -> None:
        self.client = client

    def resolve_user(self, login: str) -> Optional[dict[str, Any]]:
        result = self.client.get_user(login.strip())
        if result and str(result.get("login") or "").casefold() == login.strip().casefold():
            return result
        return None

    def resolve_field_definitions(
        self, exact_name: str, datatypes: tuple[str, ...] = ("text",),
    ) -> list[dict[str, Any]]:
        raw = self.client.get_observation_fields_autocomplete(exact_name)
        allowed = {value.casefold() for value in datatypes}
        return [
            item for item in raw.get("results") or []
            if isinstance(item, dict)
            and str(item.get("name") or "") == exact_name
            and str(item.get("datatype") or "").casefold() in allowed
        ]

    def inventory_page(
        self, user_id: int, page: int, *, id_above: Optional[int] = None, updated_since: str = "",
    ) -> dict[str, Any]:
        """Read one inventory page in one of two DELIBERATELY different modes.

        The two branches are not interchangeable, and the taxon filter is the
        difference that matters:

        * ``id_above`` — the full baseline scan. Cursor-paginated by ascending
          id and filtered to Fungi (``taxon_id=47170``, which iNaturalist
          expands to descendants), because a full account scan is otherwise
          unbounded.
        * ``updated_since`` — the incremental delta. Deliberately NOT filtered
          by taxon. A record re-identified out of Fungi (say a fungus
          corrected to a slime mould or a plant) would be INVISIBLE to a
          taxon-filtered delta, so the stored record would keep its stale
          ``fungi_status`` forever. Unfiltered, ``parse_inventory`` sees it and
          assigns ``scope_state="out_of_scope"``, which is how leaving scope
          gets recorded at all.

        Consequence the caller must expect: a delta page can legitimately
        contain non-fungal records that the baseline scan never returned.
        """
        params: dict[str, Any] = {
            "user_id": int(user_id), "per_page": 200,
            "order_by": "id" if id_above is not None else "updated_at", "order": "asc",
        }
        if id_above is not None:
            params.update({"id_above": int(id_above), "taxon_id": 47170})
        elif updated_since:
            params["updated_since"] = updated_since
        return self.client.get_reconciliation_observations(params, page=page)

    def deleted_observations(self, token: str, deleted_since: str = "") -> dict[str, Any]:
        return self.client.get_reconciliation_deleted(
            token, deleted_since=deleted_since[:10]
        )

    def detail(self, observation_id: int, token: str = "") -> dict[str, Any]:
        return self.client.get_reconciliation_detail(observation_id, token)

    def parse_inventory(
        self, raw: dict[str, Any], account_id: int, mo_field_id: Optional[int], its_field_id: Optional[int] = None,
    ) -> InventoryObservation:
        observation_id = _positive_int(raw.get("id"))
        if observation_id is None:
            raise ValueError("iNaturalist observation has no positive ID")
        user = _mapping(raw.get("user"))
        taxon = _mapping(raw.get("taxon"))
        field_rows = [
            item for item in (raw.get("ofvs") or raw.get("observation_field_values") or [])
            if isinstance(item, dict)
        ]
        targets: list[int] = []
        link_rows: list[AuthoritativeLinkRow] = []
        malformed = False
        matching_rows = 0
        identifiers: list[tuple[str, str]] = []
        sequence_hashes: list[str] = []
        for row in field_rows:
            field = _mapping(row.get("observation_field"))
            field_id = _positive_int(
                row.get("field_id") or row.get("observation_field_id") or field.get("id")
            )
            if mo_field_id and field_id == mo_field_id:
                matching_rows += 1
                target = parse_mo_observation_url(row.get("value"))
                row_id = str(row.get("id") or f"ofv:{observation_id}:{matching_rows}")
                if target is None:
                    malformed = True
                    link_rows.append(AuthoritativeLinkRow(
                        row_id, None, RemoteSite.MO, None, "malformed",
                        public_fingerprint(row_id, "malformed"),
                    ))
                else:
                    targets.append(target)
                    link_rows.append(AuthoritativeLinkRow(
                        row_id, None, RemoteSite.MO, target, "valid",
                        public_fingerprint(row_id, target, "valid"),
                    ))
            if its_field_id and field_id == its_field_id:
                accession = normalize_accession(row.get("value"))
                if accession:
                    identifiers.append(("accession", accession))
                digest = sequence_digest(row.get("value"))
                if digest:
                    sequence_hashes.append(digest)
        unique_targets = tuple(sorted(set(targets)))
        if matching_rows > 1 or len(unique_targets) > 1:
            malformed = True
            parse_state = "conflicting" if len(unique_targets) > 1 else "duplicate"
            link_rows = [
                AuthoritativeLinkRow(
                    item.row_id, item.external_site_id, item.target_site,
                    item.target_observation_id,
                    parse_state if item.parse_state == "valid" else item.parse_state,
                    public_fingerprint(
                        item.row_id, item.target_observation_id,
                        parse_state if item.parse_state == "valid" else item.parse_state,
                    ),
                ) for item in link_rows
            ]
        fungi = inat_fungi_status(taxon)
        observed = _parse_date(raw.get("observed_on"))
        updated = _parse_datetime(raw.get("updated_at"))
        locality = str(raw.get("place_guess") or "").strip()
        media: list[MediaIdentity] = []
        for photo_row in raw.get("observation_photos") or []:
            if not isinstance(photo_row, dict):
                continue
            photo = _mapping(photo_row.get("photo")) or photo_row
            photo_id = photo.get("id")
            if photo_id is not None:
                media.append(MediaIdentity(
                    RemoteSite.INAT, str(photo_id), "display",
                    RemoteSite.INAT, str(photo_id),
                ))
        fingerprint = public_fingerprint(
            observation_id, raw.get("updated_at"), observed, taxon.get("id"), taxon.get("name"),
            locality, fungi, unique_targets, malformed,
        )
        return InventoryObservation(
            key=RemoteRecordKey(RemoteSite.INAT, observation_id), account_id=account_id,
            owner_id=_positive_int(user.get("id")), owner_login=str(user.get("login") or ""),
            observed_on=observed, taxon_id=_positive_int(taxon.get("id")),
            taxon_name=str(taxon.get("name") or raw.get("species_guess") or ""),
            taxon_rank=str(taxon.get("rank") or ""), public_locality=locality,
            fungi_status=fungi, updated_at=updated,
            scope_state="in_scope" if fungi == "fungi" else "out_of_scope",
            content_fingerprint=fingerprint,
            authoritative_targets=unique_targets, link_malformed=malformed,
            authoritative_links=tuple(link_rows),
            identifiers=tuple(identifiers), inventory_identifiers=tuple(identifiers),
            sequence_hashes=tuple(sorted(set(sequence_hashes))),
            inventory_sequence_hashes=tuple(sorted(set(sequence_hashes))),
            media=tuple(media),
        )


def _mapping(value: object) -> dict[str, Any]:
    """Narrow an untrusted payload member to a dict in ONE evaluation.

    The `x.get(k) if isinstance(x.get(k), dict) else {}` idiom calls .get
    twice, so no type checker can narrow the first call from the second --
    it reports a possible None member access on every downstream use. That
    noise is what a genuine unnarrowed-Optional crash hides in.
    """
    return value if isinstance(value, dict) else {}


def inat_fungi_status(taxon: dict[str, Any]) -> str:
    """Classify one iNaturalist taxon as fungi / nonfungal / unknown.

    This is the definition that stamps ``fungi_status`` on every scanned
    record, so every scope check elsewhere must call it rather than re-deriving
    the rule. Two properties matter and were missing from re-derived copies:
    the Fungi kingdom taxon (47170) is itself in scope, and a taxon carrying
    neither an iconic name nor an ancestry (a coarse ID such as "State of
    Matter Life") is UNKNOWN, not nonfungal — treating it as nonfungal would
    hard-block writes for a pair the scan considers perfectly in scope.
    """
    if not taxon:
        return "unknown"
    iconic = str(taxon.get("iconic_taxon_name") or "").casefold()
    if iconic == "fungi":
        return "fungi"
    ancestry = {int(item) for item in str(taxon.get("ancestry") or "").split("/") if item.isdigit()}
    if _positive_int(taxon.get("id")) == 47170 or 47170 in ancestry:
        return "fungi"
    return "nonfungal" if iconic or ancestry else "unknown"


def _positive_int(value: object) -> Optional[int]:
    try:
        parsed = int(value)  # type: ignore[arg-type]
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: object) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _parse_datetime(value: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
