"""Shared safety logic for iNaturalist identification write actions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from observation_workbench.api.client import INatClient
from observation_workbench.api.parsers import parse_observation
from observation_workbench.models import StudyIdentification, StudyObservation


@dataclass
class AgreeTarget:
    observation_id: int
    taxon_id: int
    taxon_name: str
    source_login: str = ""
    source_ident_id: Optional[int] = None
    source_created_at: str = ""


@dataclass
class AgreeResult:
    status: str
    message: str
    target: Optional[AgreeTarget] = None
    refreshed_observation: Optional[StudyObservation] = None
    raw_response: Optional[dict] = None


def observation_from_detail_response(raw: dict) -> Optional[StudyObservation]:
    """Parse a full observation response from `/observations/{id}`."""
    if not raw:
        return None
    if isinstance(raw.get("results"), list):
        raw_obs = raw["results"][0] if raw["results"] else None
    else:
        raw_obs = raw
    return parse_observation(raw_obs) if raw_obs else None


def observations_from_detail_response(raw: dict) -> list[StudyObservation]:
    """Parse full observation responses from `/observations/{id1,id2,...}`."""
    if not raw:
        return []
    raw_list = raw.get("results") if isinstance(raw.get("results"), list) else [raw]
    return [
        obs
        for raw_obs in raw_list
        if (obs := parse_observation(raw_obs)) is not None
    ]


def refresh_observation(
    client: INatClient,
    api_token: str,
    observation_id: int,
) -> Optional[StudyObservation]:
    return observation_from_detail_response(
        client.get_observation_by_id(observation_id, api_token=api_token)
    )


def refresh_observations(
    client: INatClient,
    api_token: str,
    observation_ids: Sequence[int],
) -> list[StudyObservation]:
    return observations_from_detail_response(
        client.get_observations_by_ids(observation_ids, api_token=api_token)
    )


def most_recent_non_self_current_identification(
    obs: StudyObservation,
    login: str,
    *,
    provisional_only: bool = False,
) -> Optional[StudyIdentification]:
    login_key = login.casefold()
    candidates = []
    for ident in obs.all_identifications:
        if not ident.current:
            continue
        if ident.user_login.casefold() == login_key:
            continue
        if not ident.taxon or not ident.taxon.taxon_id:
            continue
        if provisional_only and not ident.is_provisional:
            continue
        candidates.append(ident)
    candidates.sort(key=lambda i: (i.created_at or "", i.ident_id), reverse=True)
    return candidates[0] if candidates else None


def current_user_identification(
    obs: StudyObservation,
    login: str,
) -> Optional[StudyIdentification]:
    login_key = login.casefold()
    matches = [
        ident
        for ident in obs.all_identifications
        if ident.current and ident.user_login.casefold() == login_key
    ]
    matches.sort(key=lambda i: (i.created_at or "", i.ident_id), reverse=True)
    return matches[0] if matches else None


def already_current_taxon(obs: StudyObservation, login: str, taxon_id: int) -> bool:
    ident = current_user_identification(obs, login)
    return bool(ident and ident.taxon and ident.taxon.taxon_id == int(taxon_id))


def previously_withdrew_taxon(obs: StudyObservation, login: str, taxon_id: int) -> bool:
    """Return True when this user has a non-current ID for the same taxon."""
    if not login:
        return False
    login_key = login.casefold()
    target_taxon_id = int(taxon_id)
    for ident in obs.all_identifications:
        if ident.current:
            continue
        if ident.user_login.casefold() != login_key:
            continue
        if ident.taxon and ident.taxon.taxon_id == target_taxon_id:
            return True
    return False


def build_agreement_comment(
    obs: StudyObservation,
    login: str,
    taxon_id: int,
) -> str:
    """Return a space-separated @mention string for identifiers who proposed a different taxon."""
    if _agreement_should_suppress_mentions(obs, login, taxon_id):
        return ""

    login_key = login.casefold()
    users_not_to_tag = load_users_not_to_tag()
    mentions: list[str] = []
    seen: set[str] = set()
    for ident in obs.all_identifications:
        if not ident.current:
            continue
        ukey = ident.user_login.casefold()
        if ukey == login_key or ukey in seen:
            continue
        if ukey in users_not_to_tag:
            continue
        if ident.taxon and ident.taxon.taxon_id == int(taxon_id):
            continue  # already proposing our taxon
        seen.add(ukey)
        mentions.append(ident.user_login)
    mentions.sort()
    return " ".join(f"@{u}" for u in mentions)


def _agreement_should_suppress_mentions(
    obs: StudyObservation,
    login: str,
    taxon_id: int,
) -> bool:
    if (obs.quality_grade or "").casefold() == "research":
        return True
    if (obs.quality_grade or "").casefold() != "needs_id":
        return False

    target_taxon_id = int(taxon_id)
    current_target_idents = [
        ident for ident in obs.all_identifications
        if ident.current
        and ident.taxon
        and ident.taxon.taxon_id == target_taxon_id
    ]
    if not current_target_idents:
        return False

    target_rank = (current_target_idents[0].taxon.rank or "").casefold()
    if target_rank not in {"species", "hybrid", "subspecies", "variety", "form"}:
        return False

    login_key = login.casefold()
    non_self_target_count = sum(
        1
        for ident in current_target_idents
        if ident.user_login.casefold() != login_key
    )
    return non_self_target_count >= 1


def load_users_not_to_tag() -> set[str]:
    path = Path.cwd() / "users_not_to_tag.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    users: set[str] = set()
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if "#" in value:
            value = value.split("#", 1)[0].strip()
        value = value.lstrip("@").rstrip("@").strip()
        if value:
            users.add(value.casefold())
    return users


def _load_users_not_to_tag() -> set[str]:
    return load_users_not_to_tag()


def needs_human_review(
    obs: StudyObservation,
    target: AgreeTarget,
    login: str,
) -> tuple[bool, bool]:
    """Return (recent_not_provisional, comments_since_provisional).

    recent_not_provisional: the most recent current non-self identification is
    not provisional (no apostrophe), even though a provisional one exists.

    comments_since_provisional: at least one observation comment was created
    after the provisional name was first proposed.
    """
    login_key = login.casefold()
    prov_date = _parse_iso_datetime(target.source_created_at or "")

    current_non_self = [
        ident for ident in obs.all_identifications
        if ident.current
        and ident.user_login.casefold() != login_key
        and ident.taxon and ident.taxon.taxon_id
    ]
    current_non_self.sort(key=lambda i: (i.created_at or "", i.ident_id), reverse=True)
    recent_not_provisional = bool(
        current_non_self and "'" not in (current_non_self[0].taxon.name or "")
    )

    comments_since = False
    if prov_date:
        for comment in obs.comments:
            if comment.hidden:
                continue
            comment_date = _parse_iso_datetime(comment.created_at or "")
            if comment_date and comment_date > prov_date:
                comments_since = True
                break

    return recent_not_provisional, comments_since


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def make_target_from_ident(obs: StudyObservation, ident: StudyIdentification) -> AgreeTarget:
    return AgreeTarget(
        observation_id=obs.obs_id,
        taxon_id=ident.taxon.taxon_id,
        taxon_name=ident.taxon.name,
        source_login=ident.user_login,
        source_ident_id=ident.ident_id,
        source_created_at=ident.created_at,
    )


def agree_with_most_recent(
    client: INatClient,
    api_token: str,
    login: str,
    observation_id: int,
) -> AgreeResult:
    obs = refresh_observation(client, api_token, observation_id)
    if obs is None:
        return AgreeResult("skipped", "Could not refresh observation before identifying.")
    ident = most_recent_non_self_current_identification(obs, login)
    if ident is None:
        return AgreeResult(
            "skipped",
            "No current non-self identification is available to agree with.",
            refreshed_observation=obs,
        )
    target = make_target_from_ident(obs, ident)
    return post_agreement(client, api_token, login, obs, target)


def agree_with_consensus(
    client: INatClient,
    api_token: str,
    login: str,
    observation_id: int,
) -> AgreeResult:
    obs = refresh_observation(client, api_token, observation_id)
    if obs is None:
        return AgreeResult("skipped", "Could not refresh observation before identifying.")
    if not obs.community_taxon or not obs.community_taxon.taxon_id:
        return AgreeResult(
            "skipped",
            "This observation has no community/consensus taxon to agree with.",
            refreshed_observation=obs,
        )
    target = AgreeTarget(
        observation_id=obs.obs_id,
        taxon_id=obs.community_taxon.taxon_id,
        taxon_name=obs.community_taxon.name,
        source_login="community",
    )
    return post_agreement(client, api_token, login, obs, target)


def post_agreement(
    client: INatClient,
    api_token: str,
    login: str,
    refreshed_obs: StudyObservation,
    target: AgreeTarget,
    body: str = "",
) -> AgreeResult:
    if already_current_taxon(refreshed_obs, login, target.taxon_id):
        return AgreeResult(
            "skipped",
            f"Already currently identified as {target.taxon_name}.",
            target=target,
            refreshed_observation=refreshed_obs,
        )

    response = client.create_identification(
        api_token=api_token,
        observation_id=target.observation_id,
        taxon_id=target.taxon_id,
        body=body,
    )
    try:
        updated = refresh_observation(client, api_token, target.observation_id) or refreshed_obs
        message = f"Added identification: {target.taxon_name}"
    except Exception as exc:
        updated = refreshed_obs
        message = f"Added identification: {target.taxon_name} (refresh failed: {exc})"
    return AgreeResult(
        "posted",
        message,
        target=target,
        refreshed_observation=updated,
        raw_response=response,
    )
