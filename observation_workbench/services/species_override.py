"""Helpers for updating species-name observation field values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from observation_workbench.api.client import INatClient
from observation_workbench.models import StudyPhoto

PROVISIONAL_SPECIES_FIELD_NAME = "Provisional Species Name"
SPECIES_NAME_OVERRIDE_FIELD_NAME = "Species Name Override"
PER_PAGE = 200
MAX_SEARCH_RESULTS = 999_999
SUPPORTED_TARGET_FIELD_NAMES = frozenset(
    {PROVISIONAL_SPECIES_FIELD_NAME, SPECIES_NAME_OVERRIDE_FIELD_NAME}
)


@dataclass
class ObservationFieldValueRef:
    value_id: int | str | None = None
    field_id: Optional[int] = None
    value: str = ""


@dataclass
class SpeciesOverridePlanRow:
    observation_id: int
    observation_url: str
    observer_login: str
    consensus_name: str
    provisional_name: str
    override_value: str = ""
    override_value_id: int | str | None = None
    override_field_id: Optional[int] = None
    override_present: bool = False
    update_allowed: bool = True
    skip_reason: str = ""
    photos: list[StudyPhoto] = field(default_factory=list)


@dataclass
class SpeciesOverridePlan:
    provisional_name: str
    override_name: str
    total_observations: int
    override_field_id: Optional[int] = None
    rows: list[SpeciesOverridePlanRow] = field(default_factory=list)
    missing_provisional_fields: int = 0
    source_mode: str = "provisional"
    genus_filter: str = ""
    requested_observation_ids: list[int] = field(default_factory=list)
    missing_observation_ids: list[int] = field(default_factory=list)
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME

    @property
    def updatable_row_count(self) -> int:
        return sum(1 for row in self.rows if row.update_allowed)

    @property
    def skipped_row_count(self) -> int:
        return sum(1 for row in self.rows if not row.update_allowed)

    def source_label(self) -> str:
        if self.source_mode == "observations":
            return f"{len(self.requested_observation_ids)} pasted observation(s)"
        return f"Provisional Species Name = {self.provisional_name}"


@dataclass
class SpeciesOverrideResult:
    provisional_name: str
    override_name: str
    selected: int
    source_label: str = ""
    genus_filter: str = ""
    updated_ids: list[int] = field(default_factory=list)
    created_ids: list[int] = field(default_factory=list)
    unchanged_ids: list[int] = field(default_factory=list)
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME

    @property
    def changed(self) -> int:
        return len(self.updated_ids) + len(self.created_ids)

    @property
    def unchanged(self) -> int:
        return len(self.unchanged_ids)

    @property
    def applied_ids(self) -> list[int]:
        return self.updated_ids + self.created_ids + self.unchanged_ids


def plan_species_override_update(
    client: INatClient,
    provisional_name: str,
    override_name: str,
    *,
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    genus_filter: str = "",
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> SpeciesOverridePlan:
    """Find observations by provisional name and plan target-field writes."""
    provisional_name = provisional_name.strip()
    override_name = override_name.strip()
    if not provisional_name:
        raise ValueError("Enter a Provisional Species Name to search for.")
    target_field_name = _normalise_target_field_name(target_field_name)
    if not override_name:
        raise ValueError(f"Enter the {target_field_name} value to set.")
    genus_filter = _normalise_genus_filter(genus_filter)
    genus_taxon_id = _resolve_genus_taxon_id(client, genus_filter)

    query = {
        f"field:{PROVISIONAL_SPECIES_FIELD_NAME}": provisional_name,
        "verifiable": "any",
        "order_by": "id",
        "order": "asc",
    }
    page = 1
    seen = 0
    total = 0
    rows: list[SpeciesOverridePlanRow] = []
    missing_provisional_fields = 0

    while True:
        if is_cancelled and is_cancelled():
            break
        raw = client.get_observations(query, page=page, per_page=PER_PAGE)
        total = int(raw.get("total_results", 0) or 0)
        if total > MAX_SEARCH_RESULTS:
            raise RuntimeError(
                f"iNaturalist returned {total} matches. This workflow is limited "
                f"to {MAX_SEARCH_RESULTS} matches because the API does not support "
                "paging farther through one search."
            )

        results = raw.get("results") or []
        seen += len(results)
        page_rows, missing_ids = _rows_from_observations(
            results,
            provisional_name,
            target_field_name=target_field_name,
            genus_filter=genus_filter,
            genus_taxon_id=genus_taxon_id,
        )
        rows.extend(page_rows)
        if missing_ids:
            detail_raw = client.get_observations_by_ids(missing_ids)
            detail_results = detail_raw.get("results") or []
            detail_rows, still_missing = _rows_from_observations(
                detail_results,
                provisional_name,
                target_field_name=target_field_name,
                genus_filter=genus_filter,
                genus_taxon_id=genus_taxon_id,
            )
            rows.extend(detail_rows)
            returned_detail_ids = {
                int(raw_obs.get("id") or 0)
                for raw_obs in detail_results
                if isinstance(raw_obs, dict)
            }
            missing_provisional_fields += len(still_missing) + len(
                set(missing_ids) - returned_detail_ids
            )
        if progress:
            progress(seen, total)
        if total <= 0 or page * PER_PAGE >= total:
            break
        page += 1

    override_field_id = _resolve_override_field_id(
        client,
        rows,
        target_field_name=target_field_name,
    )
    return SpeciesOverridePlan(
        provisional_name=provisional_name,
        override_name=override_name,
        total_observations=total,
        target_field_name=target_field_name,
        override_field_id=override_field_id,
        rows=rows,
        missing_provisional_fields=missing_provisional_fields,
        source_mode="provisional",
        genus_filter=genus_filter,
    )


def plan_species_override_update_for_observations(
    client: INatClient,
    observation_ids: list[int],
    override_name: str,
    *,
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    genus_filter: str = "",
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> SpeciesOverridePlan:
    """Plan target-field writes for an explicit observation list."""
    override_name = override_name.strip()
    target_field_name = _normalise_target_field_name(target_field_name)
    if not override_name:
        raise ValueError(f"Enter the {target_field_name} value to set.")

    ids: list[int] = []
    seen_ids: set[int] = set()
    for raw_id in observation_ids:
        obs_id = int(raw_id)
        if obs_id <= 0 or obs_id in seen_ids:
            continue
        seen_ids.add(obs_id)
        ids.append(obs_id)
    if not ids:
        raise ValueError("Enter at least one observation ID or URL.")

    genus_filter = _normalise_genus_filter(genus_filter)
    genus_taxon_id = _resolve_genus_taxon_id(client, genus_filter)
    if is_cancelled and is_cancelled():
        return SpeciesOverridePlan(
            provisional_name="",
            override_name=override_name,
            total_observations=len(ids),
            target_field_name=target_field_name,
            source_mode="observations",
            genus_filter=genus_filter,
            requested_observation_ids=ids,
        )

    raw = client.get_observations_by_ids(ids)
    results = raw.get("results") or []
    raw_by_id = {
        obs_id: raw_obs
        for raw_obs in results
        if isinstance(raw_obs, dict)
        if (obs_id := _int_or_none(raw_obs.get("id"))) is not None
    }

    rows: list[SpeciesOverridePlanRow] = []
    missing_ids: list[int] = []
    for obs_id in ids:
        raw_obs = raw_by_id.get(obs_id)
        if raw_obs is None:
            missing_ids.append(obs_id)
            rows.append(
                SpeciesOverridePlanRow(
                    observation_id=obs_id,
                    observation_url=f"https://www.inaturalist.org/observations/{obs_id}",
                    observer_login="",
                    consensus_name="Unknown",
                    provisional_name="",
                    override_value="",
                    update_allowed=False,
                    skip_reason="Observation was not returned by iNaturalist.",
                )
            )
            continue
        row, _missing_id = _row_from_observation(
            raw_obs,
            provisional_name="",
            require_provisional=False,
            target_field_name=target_field_name,
            genus_filter=genus_filter,
            genus_taxon_id=genus_taxon_id,
        )
        if row is not None:
            rows.append(row)

    if progress:
        progress(len(rows), len(ids))

    override_field_id = _resolve_override_field_id(
        client,
        rows,
        target_field_name=target_field_name,
    )
    return SpeciesOverridePlan(
        provisional_name="",
        override_name=override_name,
        total_observations=len(ids),
        target_field_name=target_field_name,
        override_field_id=override_field_id,
        rows=rows,
        source_mode="observations",
        genus_filter=genus_filter,
        requested_observation_ids=ids,
        missing_observation_ids=missing_ids,
    )


def update_species_overrides(
    client: INatClient,
    api_token: str,
    plan: SpeciesOverridePlan,
    selected_observation_ids: list[int],
    *,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, int, int, int, str], None]] = None,
) -> SpeciesOverrideResult:
    """Apply a planned species-name field update to selected observations."""
    selected_ids = set(int(obs_id) for obs_id in selected_observation_ids)
    selected_rows = [
        row
        for row in plan.rows
        if row.update_allowed and row.observation_id in selected_ids
    ]
    result = SpeciesOverrideResult(
        provisional_name=plan.provisional_name,
        override_name=plan.override_name,
        selected=len(selected_rows),
        target_field_name=plan.target_field_name,
        source_label=plan.source_label(),
        genus_filter=plan.genus_filter,
        skipped=(len(plan.rows) - len(selected_rows)) + plan.missing_provisional_fields,
    )
    total = len(selected_rows)

    for index, row in enumerate(selected_rows, start=1):
        if is_cancelled and is_cancelled():
            result.skipped += total - index + 1
            break

        if row.override_value == plan.override_name:
            result.unchanged_ids.append(row.observation_id)
            message = (
                f"Skipped observation {row.observation_id} "
                f"({plan.target_field_name} already matches)."
            )
            if progress:
                progress(
                    index, total, result.changed, result.skipped, result.failed, message
                )
            continue

        try:
            if row.override_present:
                if row.override_value_id is None:
                    result.skipped += 1
                    message = (
                        f"Skipped observation {row.observation_id} "
                        f"(existing {plan.target_field_name} has no editable "
                        "field value ID)."
                    )
                else:
                    client.update_observation_field_value(
                        api_token,
                        row.override_value_id,
                        plan.override_name,
                        observation_id=row.observation_id,
                        observation_field_id=(
                            row.override_field_id or plan.override_field_id
                        ),
                    )
                    result.updated_ids.append(row.observation_id)
                    message = f"Updated observation {row.observation_id}."
            else:
                field_id = row.override_field_id or plan.override_field_id
                if field_id is None:
                    result.skipped += 1
                    message = (
                        f"Skipped observation {row.observation_id} "
                        f"(could not resolve {plan.target_field_name} field ID)."
                    )
                else:
                    client.create_observation_field_value(
                        api_token,
                        row.observation_id,
                        field_id,
                        plan.override_name,
                    )
                    result.created_ids.append(row.observation_id)
                    message = (
                        f"Created {plan.target_field_name} on observation "
                        f"{row.observation_id}."
                    )
        except Exception as exc:
            result.failed += 1
            message = f"Observation {row.observation_id}: {exc}"
            result.errors.append(message)

        if progress:
            progress(
                index, total, result.changed, result.skipped, result.failed, message
            )

    return result


def _rows_from_observations(
    raw_observations: list,
    provisional_name: str,
    *,
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    genus_filter: str = "",
    genus_taxon_id: Optional[int] = None,
) -> tuple[list[SpeciesOverridePlanRow], list[int]]:
    rows: list[SpeciesOverridePlanRow] = []
    missing_ids: list[int] = []
    for raw_obs in raw_observations:
        row, missing_id = _row_from_observation(
            raw_obs,
            provisional_name=provisional_name,
            require_provisional=True,
            target_field_name=target_field_name,
            genus_filter=genus_filter,
            genus_taxon_id=genus_taxon_id,
        )
        if row is not None:
            rows.append(row)
        if missing_id is not None:
            missing_ids.append(missing_id)
    return rows, missing_ids


def _row_from_observation(
    raw_obs: object,
    *,
    provisional_name: str,
    require_provisional: bool,
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
    genus_filter: str = "",
    genus_taxon_id: Optional[int] = None,
) -> tuple[Optional[SpeciesOverridePlanRow], Optional[int]]:
    if not isinstance(raw_obs, dict):
        return None, None
    obs_id = _int_or_none(raw_obs.get("id"))
    if obs_id is None:
        return None, None

    provisional_ref = _extract_field_value(
        raw_obs,
        PROVISIONAL_SPECIES_FIELD_NAME,
        expected_value=provisional_name if require_provisional else "",
    )
    if require_provisional and provisional_ref is None:
        return None, obs_id

    override_ref = _extract_field_value(raw_obs, target_field_name)
    observer = raw_obs.get("user") or {}
    consensus_name = _consensus_name(raw_obs)
    update_allowed = True
    skip_reason = ""
    if genus_filter and not _observation_matches_genus(
        raw_obs, genus_filter, genus_taxon_id
    ):
        update_allowed = False
        skip_reason = f"Genus mismatch: current consensus is {consensus_name}."

    return (
        SpeciesOverridePlanRow(
            observation_id=obs_id,
            observation_url=f"https://www.inaturalist.org/observations/{obs_id}",
            observer_login=observer.get("login", "") or "",
            consensus_name=consensus_name,
            provisional_name=provisional_ref.value if provisional_ref else "",
            override_value=override_ref.value if override_ref else "",
            override_value_id=override_ref.value_id if override_ref else None,
            override_field_id=override_ref.field_id if override_ref else None,
            override_present=override_ref is not None,
            update_allowed=update_allowed,
            skip_reason=skip_reason,
            photos=_photos_from_observation(raw_obs),
        ),
        None,
    )


def _photos_from_observation(raw_obs: dict) -> list[StudyPhoto]:
    photos: list[StudyPhoto] = []
    for raw_photo in raw_obs.get("photos") or []:
        if not isinstance(raw_photo, dict):
            continue
        photo_id = _int_or_none(raw_photo.get("id"))
        url = str(raw_photo.get("url") or "")
        if photo_id is None or not url:
            continue
        photos.append(
            StudyPhoto(
                photo_id=photo_id,
                url_square=url,
                attribution=str(raw_photo.get("attribution") or ""),
                license_code=str(raw_photo.get("license_code") or ""),
            )
        )
    return photos


def _extract_field_value(
    raw_obs: dict,
    field_name: str,
    *,
    expected_value: str = "",
) -> Optional[ObservationFieldValueRef]:
    wanted_field = field_name.casefold()
    wanted_value = expected_value.strip().casefold()
    raw_values = (
        raw_obs.get("ofvs")
        or raw_obs.get("observation_field_values")
        or raw_obs.get("observation_fields")
        or []
    )
    for item in raw_values:
        if not isinstance(item, dict):
            continue
        obs_field = item.get("observation_field") or item.get("field") or {}
        item_field_name = (
            item.get("name") or item.get("field_name") or obs_field.get("name") or ""
        )
        if item_field_name.casefold() != wanted_field:
            continue

        value = _field_item_value(item)
        if wanted_value and value.strip().casefold() != wanted_value:
            continue
        return ObservationFieldValueRef(
            value_id=item.get("id"),
            field_id=_int_or_none(
                # ``field_id`` is the key the observation serializer actually
                # returns; the other two are only present on other payloads.
                item.get("field_id")
                or item.get("observation_field_id")
                or obs_field.get("id")
            ),
            value=value,
        )
    return None


def _field_item_value(item: dict) -> str:
    for key in ("value", "display_value", "value_text"):
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def _resolve_override_field_id(
    client: INatClient,
    rows: list[SpeciesOverridePlanRow],
    *,
    target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
) -> Optional[int]:
    for row in rows:
        if row.override_field_id is not None:
            return row.override_field_id
    needs_create = any(row.update_allowed and not row.override_present for row in rows)
    if not needs_create:
        return None
    field_id = client.find_observation_field_id(target_field_name)
    if field_id is None:
        raise RuntimeError(
            f"Could not resolve the {target_field_name!r} "
            "observation field ID from iNaturalist."
        )
    return field_id


def _normalise_target_field_name(value: str) -> str:
    target_field_name = value.strip()
    if target_field_name not in SUPPORTED_TARGET_FIELD_NAMES:
        supported = ", ".join(sorted(SUPPORTED_TARGET_FIELD_NAMES))
        raise ValueError(
            f"Unsupported target observation field. Expected one of: {supported}."
        )
    return target_field_name


def _consensus_name(raw_obs: dict) -> str:
    community_name = _taxon_name(raw_obs.get("community_taxon"))
    if community_name:
        return community_name
    observation_name = _taxon_name(raw_obs.get("taxon"))
    if observation_name:
        return observation_name
    return "Unknown"


def _taxon_name(raw_taxon) -> str:
    if not isinstance(raw_taxon, dict):
        return ""
    return str(raw_taxon.get("name") or "").strip()


def _observation_matches_genus(
    raw_obs: dict,
    genus_filter: str,
    genus_taxon_id: Optional[int] = None,
) -> bool:
    genus = _normalise_genus_filter(genus_filter)
    if not genus:
        return True
    taxon = raw_obs.get("community_taxon") or raw_obs.get("taxon")
    return _taxon_matches_genus(taxon, genus, genus_taxon_id)


def _taxon_matches_genus(
    raw_taxon,
    genus: str,
    genus_taxon_id: Optional[int] = None,
) -> bool:
    """Test whether a taxon is the genus itself or sits below it.

    Ancestry is checked by taxon ID when the genus name could be resolved: the
    observation serializer returns ``ancestor_ids`` but no ``ancestors`` list,
    so a name-only test cannot see through intermediate ranks whose names do
    not start with the genus (sections, subgenera). The name comparisons remain
    as the fallback for when the genus could not be resolved to an ID.
    """
    if not isinstance(raw_taxon, dict):
        return False
    if genus_taxon_id is not None:
        if _int_or_none(raw_taxon.get("id")) == genus_taxon_id:
            return True
        for ancestor_id in raw_taxon.get("ancestor_ids") or []:
            if _int_or_none(ancestor_id) == genus_taxon_id:
                return True
    wanted = genus.casefold()
    name = _taxon_name(raw_taxon)
    if name.casefold() == wanted:
        return True
    if name.casefold().startswith(wanted + " "):
        return True
    for item in raw_taxon.get("ancestors") or []:
        if not isinstance(item, dict):
            continue
        if _taxon_name(item).casefold() == wanted:
            return True
    return False


def _resolve_genus_taxon_id(client: INatClient, genus_filter: str) -> Optional[int]:
    """Resolve a genus name to its iNaturalist taxon ID, or None if unavailable.

    Returning None is not an error: the genus gate then falls back to matching
    on taxon names alone.
    """
    genus = _normalise_genus_filter(genus_filter)
    if not genus:
        return None
    try:
        raw = client.get_taxa_autocomplete(genus, per_page=10)
    except Exception:
        return None
    wanted = genus.casefold()
    for item in raw.get("results") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("rank") or "").strip().casefold() != "genus":
            continue
        if _taxon_name(item).casefold() != wanted:
            continue
        return _int_or_none(item.get("id"))
    return None


def _normalise_genus_filter(value: str) -> str:
    return value.strip().split()[0] if value.strip() else ""


def _int_or_none(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
