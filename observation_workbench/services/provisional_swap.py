"""Helpers for swapping Provisional Species Name observation field values."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from observation_workbench.api.client import INatClient

PROVISIONAL_SPECIES_FIELD_NAME = "Provisional Species Name"
PER_PAGE = 200
MAX_SEARCH_RESULTS = 999_999


@dataclass
class ProvisionalFieldValueRef:
    observation_id: int
    observation_url: str
    observer_login: str
    value_id: int | str
    field_id: Optional[int] = None
    value: str = ""


@dataclass
class ProvisionalSwapPlan:
    source_name: str
    total_observations: int
    field_values: list[ProvisionalFieldValueRef] = field(default_factory=list)
    missing_field_value_ids: int = 0


@dataclass
class ProvisionalSwapResult:
    source_name: str
    destination_name: str
    total_observations: int
    updated_ids: list[int] = field(default_factory=list)
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def updated(self) -> int:
        return len(self.updated_ids)


def plan_provisional_name_swap(
    client: INatClient,
    source_name: str,
    *,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ProvisionalSwapPlan:
    """Find observations whose Provisional Species Name field equals source_name."""
    source_name = source_name.strip()
    if not source_name:
        raise ValueError("Enter a provisional name to search for.")

    query = {
        f"field:{PROVISIONAL_SPECIES_FIELD_NAME}": source_name,
        "verifiable": "any",
        "order_by": "id",
        "order": "asc",
    }
    page = 1
    seen = 0
    total = 0
    refs: list[ProvisionalFieldValueRef] = []
    missing_field_value_ids = 0

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
        page_refs, missing_ids = _field_refs_from_observations(results, source_name)
        refs.extend(page_refs)
        if missing_ids:
            detail_raw = client.get_observations_by_ids(missing_ids)
            detail_results = detail_raw.get("results") or []
            detail_refs, still_missing = _field_refs_from_observations(
                detail_results,
                source_name,
            )
            refs.extend(detail_refs)
            returned_detail_ids = {
                int(raw_obs.get("id") or 0)
                for raw_obs in detail_results
                if isinstance(raw_obs, dict)
            }
            missing_field_value_ids += len(still_missing) + len(
                set(missing_ids) - returned_detail_ids
            )
        if progress:
            progress(seen, total)
        if total <= 0 or page * PER_PAGE >= total:
            break
        page += 1

    return ProvisionalSwapPlan(
        source_name=source_name,
        total_observations=total,
        field_values=refs,
        missing_field_value_ids=missing_field_value_ids,
    )


def swap_provisional_name(
    client: INatClient,
    api_token: str,
    plan: ProvisionalSwapPlan,
    destination_name: str,
    *,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, int, int, int, str], None]] = None,
) -> ProvisionalSwapResult:
    """Update all plannable field values in plan to destination_name."""
    destination_name = destination_name.strip()
    if not destination_name:
        raise ValueError("Enter a destination name.")
    if destination_name == plan.source_name:
        raise ValueError("Destination name is the same as the provisional name.")

    result = ProvisionalSwapResult(
        source_name=plan.source_name,
        destination_name=destination_name,
        total_observations=plan.total_observations,
        skipped=plan.missing_field_value_ids,
    )
    total = len(plan.field_values)
    for index, ref in enumerate(plan.field_values, start=1):
        if is_cancelled and is_cancelled():
            result.skipped += total - index + 1
            break
        if ref.field_id is None:
            result.skipped += 1
            message = f"Skipped observation {ref.observation_id} (no editable field ID)."
            if progress:
                progress(index, total, result.updated, result.skipped, result.failed, message)
            continue
        try:
            client.update_observation_field_value(
                api_token,
                ref.value_id,
                destination_name,
                observation_id=ref.observation_id,
                observation_field_id=ref.field_id,
            )
            result.updated_ids.append(ref.observation_id)
            message = f"Updated observation {ref.observation_id}."
        except Exception as exc:
            result.failed += 1
            message = f"Observation {ref.observation_id}: {exc}"
            result.errors.append(message)
        if progress:
            progress(index, total, result.updated, result.skipped, result.failed, message)
    return result


def _field_refs_from_observations(
    raw_observations: list,
    source_name: str,
) -> tuple[list[ProvisionalFieldValueRef], list[int]]:
    refs: list[ProvisionalFieldValueRef] = []
    missing_ids: list[int] = []
    for raw_obs in raw_observations:
        if not isinstance(raw_obs, dict):
            continue
        obs_id = int(raw_obs.get("id") or 0)
        if not obs_id:
            continue
        ref = _extract_provisional_field_value(raw_obs, source_name)
        if ref is None:
            missing_ids.append(obs_id)
            continue
        refs.append(ref)
    return refs, missing_ids


def _extract_provisional_field_value(
    raw_obs: dict,
    source_name: str,
) -> Optional[ProvisionalFieldValueRef]:
    wanted_field = PROVISIONAL_SPECIES_FIELD_NAME.casefold()
    raw_values = (
        raw_obs.get("ofvs")
        or raw_obs.get("observation_field_values")
        or raw_obs.get("observation_fields")
        or []
    )
    wanted_value = source_name.strip().casefold()
    for item in raw_values:
        if not isinstance(item, dict):
            continue
        obs_field = item.get("observation_field") or item.get("field") or {}
        field_name = item.get("name") or item.get("field_name") or obs_field.get("name") or ""
        if field_name.casefold() != wanted_field:
            continue
        value = item.get("value") or item.get("display_value") or item.get("value_text") or ""
        # Match case-insensitively to mirror iNaturalist's field-value search;
        # an exact-case compare would push valid matches into the missing-ids
        # re-fetch and ultimately drop them.
        if str(value).strip().casefold() != wanted_value:
            continue
        # The www update endpoint is /observation_field_values/<numeric id>.json,
        # so only a numeric id is usable; a bare uuid would 404. Treat a value
        # without a numeric id as missing so it is reported, not silently failed.
        value_id = item.get("id")
        if not value_id:
            return None
        field_id_raw = item.get("observation_field_id") or obs_field.get("id")
        field_id = int(field_id_raw) if field_id_raw else None
        obs_id = int(raw_obs.get("id") or 0)
        observer = raw_obs.get("user") or {}
        return ProvisionalFieldValueRef(
            observation_id=obs_id,
            observation_url=f"https://www.inaturalist.org/observations/{obs_id}",
            observer_login=observer.get("login", "") or "",
            value_id=value_id,
            field_id=field_id,
            value=str(value),
        )
    return None
