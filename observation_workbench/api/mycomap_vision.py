"""Synchronous MycoMap Vision API client for background workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://vision.mycomap.org/api/v1"
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


@dataclass(frozen=True)
class MycomapVisionError(RuntimeError):
    """A credential-free, UI-safe Vision request failure."""

    message: str
    status_code: int | None = None
    code: str = ""
    retry_after_seconds: int | None = None

    def __str__(self) -> str:
        return self.message


class MycomapVisionClient:
    """Small API boundary; methods are intended to run in ``QRunnable``s."""

    def identify_inat(
        self,
        observation_id: int,
        token: str,
        *,
        top_k: int = 5,
        rerun: bool = False,
    ) -> dict[str, Any]:
        observation_id = int(observation_id)
        if observation_id <= 0:
            raise ValueError("A positive iNaturalist observation ID is required")
        credential = str(token or "").strip()
        if not credential:
            raise MycomapVisionError("No MycoMap Vision token is configured.")

        payload: dict[str, Any] = {"top_k": max(1, min(10, int(top_k)))}
        if rerun:
            payload["rerun"] = True
        try:
            response = httpx.post(
                f"{BASE_URL}/identify/inat/{observation_id}",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=DEFAULT_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise MycomapVisionError(
                "MycoMap Vision did not answer before the request timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise MycomapVisionError(
                "MycoMap Vision could not be reached. Check the network and try again."
            ) from exc

        if not response.is_success:
            raise _response_error(response)
        try:
            result = response.json()
        except ValueError as exc:
            raise MycomapVisionError(
                "MycoMap Vision returned an unreadable response.",
                status_code=response.status_code,
            ) from exc
        if not isinstance(result, dict):
            raise MycomapVisionError(
                "MycoMap Vision returned an unexpected response.",
                status_code=response.status_code,
            )
        return result


def _response_error(response: httpx.Response) -> MycomapVisionError:
    code = ""
    detail = ""
    retry_after: int | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("code") or "").strip()
        detail = str(body.get("detail") or "").strip()
        try:
            retry_after = int(body.get("retry_after_seconds"))
        except (TypeError, ValueError):
            retry_after = None
    if retry_after is None:
        try:
            retry_after = int(response.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            retry_after = None

    if response.status_code == 403 and code == "key_inactive":
        message = (
            "This MycoMap Vision token is pending approval, disabled, or revoked."
        )
    elif response.status_code == 401:
        message = "MycoMap Vision rejected the saved token."
    elif response.status_code == 429 and code == "quota_exceeded":
        message = "The MycoMap Vision monthly identification allowance is used up."
    elif response.status_code in {429, 503}:
        message = detail or "MycoMap Vision is busy. Try again later."
    elif response.status_code == 404 and code == "observation_not_found":
        message = "MycoMap Vision could not find this iNaturalist observation."
    elif response.status_code == 422 and code == "no_usable_photos":
        message = "This observation has no photos MycoMap Vision can use."
    else:
        message = detail or (
            f"MycoMap Vision refused the request (HTTP {response.status_code})."
        )
    if retry_after is not None and retry_after > 0:
        message = f"{message} Try again in about {retry_after} seconds."
    return MycomapVisionError(
        message,
        status_code=response.status_code,
        code=code,
        retry_after_seconds=retry_after,
    )
