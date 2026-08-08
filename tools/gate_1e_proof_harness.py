#!/usr/bin/env python3
"""Gate 1E-A live-proof harness — run this yourself, against disposable records.

This script exists to settle the **[NEEDS LIVE PROOF]** rows in
``docs/gate_1e_capability_report.md`` §12. It is deliberately kept *outside* the
``observation_workbench`` package so that proving photo-transfer behaviour does not wire a
single write path into the real application. Nothing here imports the app,
reads QSettings, or touches the user's saved token.

It performs REAL WRITES to REAL accounts. Guard rails:

* Dry-run is the default. Writes require ``--run --i-own-these-records``.
* Every target record is named explicitly on the command line. Nothing is
  discovered, enumerated-then-written, or looped over.
* Credentials come from the environment (``INAT_JWT``, ``MO_API_KEY``) or a
  prompt. They are never logged, echoed, or written to disk.
* Cleanup detaches every observation-photo the run created. Bare uploaded
  photos CANNOT be deleted through the iNaturalist API (a firm finding, not a
  bug in this script) — the run reports their IDs so you can delete them on the
  website if you care to.

Usage
-----
    export INAT_JWT='...'          # from https://www.inaturalist.org/users/api_token
    python tools/gate_1e_proof_harness.py \
        --inat-obs-uuid <uuid-of-a-disposable-observation-you-own> \
        --image /path/to/small-test.jpg

    # add --run --i-own-these-records to actually execute

    export MO_API_KEY='...'
    python tools/gate_1e_proof_harness.py --mo-obs-id 123456 --image ... \
        --mo-write --run --i-own-these-records
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import mimetypes
import os
import sys
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing message
    sys.exit("httpx is required: ./.venv/bin/python -m pip install httpx")


INAT_V2 = "https://api.inaturalist.org/v2"
MO_API2 = "https://mushroomobserver.org/api2"
USER_AGENT = "iNat-Study-Gate1E-ProofHarness/1.0"

# v2 returns almost nothing unless fields are requested. These RISON field
# expressions ask for exactly the identity/licence data each proof inspects.
# Mushroom Observer License ids, discovered live 2026-07-23 (there is NO
# /api2/licenses endpoint — these were mapped by querying images per license id).
# ``license`` on POST /api2/images MUST be one of these numeric ids; MO rejects
# the human-readable name outright.
MO_LICENSE_NAMES = {
    1: "Creative Commons Non-commercial v2.5",
    2: "Creative Commons Non-commercial v3.0",
    3: "Creative Commons Wikipedia Compatible v3.0",
    4: "Public Domain (Wikipedia compatible)",
    5: "Creative Commons Attribution v4.0 (Wikipedia compatible)",
    6: "Creative Commons Attribution Non-commercial v4.0",
    7: "Creative Commons Attribution Non-commercial NoDerivs v.4.0",
    8: "Creative Commons Attribution Non-commercial ShareAlike v4.0",
}
MO_LICENSE_CC_BY_NC_SA_3 = 2

PHOTO_FIELDS = "(id:!t,uuid:!t,license_code:!t,attribution:!t,attribution_name:!t,original_filename:!t)"
OBS_PHOTO_FIELDS = f"(id:!t,uuid:!t,position:!t,photo:{PHOTO_FIELDS})"
OBS_FIELDS = (
    f"(id:!t,uuid:!t,user:(id:!t,login:!t),observation_photos:{OBS_PHOTO_FIELDS})"
)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

PASS, FAIL, UNKNOWN, SKIP = "PASS", "FAIL", "UNKNOWN", "SKIP"


@dataclass
class Result:
    check_id: str
    title: str
    status: str = SKIP
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    blocking: bool = True


@dataclass
class Ledger:
    """Everything this run created, so cleanup and orphan reporting are honest."""

    attached_obs_photo_uuids: list[str] = field(default_factory=list)
    uploaded_photo_ids: list[int] = field(default_factory=list)
    mo_image_ids: list[int] = field(default_factory=list)
    orphaned_photo_ids: list[int] = field(default_factory=list)


class Ambiguous(RuntimeError):
    """A write whose outcome could not be determined (timeout / transport loss)."""


# ---------------------------------------------------------------------------
# iNaturalist v2 proof client
# ---------------------------------------------------------------------------


class INatProof:
    """Minimal v2 client. Mirrors the app's auth convention: the bare JWT is the
    ``Authorization`` header value — NOT ``Bearer <jwt>``, which iNaturalist
    treats as unauthenticated."""

    def __init__(self, jwt: str, *, timeout: float = 60.0, verbose: bool = False) -> None:
        self._jwt = _normalise_jwt(jwt)
        self._verbose = verbose
        self._last = 0.0
        self._client = httpx.Client(
            base_url=INAT_V2,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _wait(self) -> None:
        remaining = 1.0 - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> tuple[int, Any]:
        self._wait()
        if self._verbose:
            print(f"    → {method} {path}")
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers={"Authorization": self._jwt},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Ambiguous(f"{method} {path}: {type(exc).__name__}") from exc
        self._last = time.monotonic()
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"_raw": response.text[:400]}
        if self._verbose:
            print(f"    ← {response.status_code} {json.dumps(payload)[:300]}")
        return response.status_code, payload

    # -- reads -------------------------------------------------------------

    def whoami(self) -> tuple[int, Any]:
        return self._request("GET", "/users/me", params={"fields": "(id:!t,login:!t)"})

    def observation(self, obs_uuid: str) -> tuple[int, Any]:
        return self._request(
            "GET", f"/observations/{obs_uuid}", params={"fields": OBS_FIELDS}
        )

    def resolve_observation(self, value: str) -> tuple[str, dict]:
        """Accept a numeric observation ID *or* a UUID; return (uuid, observation).

        The v2 photo endpoints key off the observation UUID
        (``observation_photo[observation_id]`` is documented as a UUID), but the
        ID is what appears in an observation's web URL, so both are accepted.
        """
        value = value.strip()
        if value.isdigit():
            status, payload = self._request(
                "GET",
                "/observations",
                params={"id": value, "fields": OBS_FIELDS},
            )
            if status >= 400:
                raise RuntimeError(f"lookup of observation {value} failed: HTTP {status}")
            results = payload.get("results") or []
            if not results:
                raise RuntimeError(f"no observation found with id {value}")
            obs = results[0]
            resolved = str(obs.get("uuid") or "")
            if not resolved:
                raise RuntimeError(f"observation {value} returned no uuid")
            return resolved, obs
        status, payload = self.observation(value)
        if status >= 400:
            raise RuntimeError(f"lookup of observation {value} failed: HTTP {status}")
        results = payload.get("results") or []
        if not results:
            raise RuntimeError(f"no observation found with uuid {value}")
        return value, results[0]

    def observation_photos(self, obs_uuid: str) -> list[dict]:
        status, payload = self.observation(obs_uuid)
        if status >= 400:
            raise RuntimeError(f"observation re-read failed: HTTP {status}")
        results = payload.get("results") or []
        if not results:
            raise RuntimeError("observation re-read returned no results")
        return list(results[0].get("observation_photos") or [])

    # -- writes ------------------------------------------------------------

    def upload_photo(self, image: Path, client_uuid: str) -> tuple[int, Any]:
        """POST /photos — bare upload, NOT attached to any observation."""
        with image.open("rb") as handle:
            return self._request(
                "POST",
                "/photos",
                params={"fields": PHOTO_FIELDS},
                data={"uuid": client_uuid},
                files={"file": (image.name, handle, _content_type(image))},
            )

    def attach_multipart(
        self, image: Path, obs_uuid: str, client_uuid: str
    ) -> tuple[int, Any]:
        """POST /observation_photos (multipart) — upload AND attach in one call."""
        with image.open("rb") as handle:
            return self._request(
                "POST",
                "/observation_photos",
                params={"fields": OBS_PHOTO_FIELDS},
                data={
                    "observation_photo[observation_id]": obs_uuid,
                    "observation_photo[uuid]": client_uuid,
                },
                files={"file": (image.name, handle, _content_type(image))},
            )

    def attach_json(
        self, obs_uuid: str, photo_id: int, client_uuid: str
    ) -> tuple[int, Any]:
        """POST /observation_photos (JSON) — attach an already-uploaded photo."""
        return self._request(
            "POST",
            "/observation_photos",
            params={"fields": OBS_PHOTO_FIELDS},
            json_body={
                "observation_photo": {
                    "observation_id": obs_uuid,
                    "photo_id": int(photo_id),
                    "uuid": client_uuid,
                }
            },
        )

    def detach(self, obs_photo_uuid: str) -> tuple[int, Any]:
        return self._request("DELETE", f"/observation_photos/{obs_photo_uuid}")

    def put_photo_license(self, photo_id: int, license_code: str) -> tuple[int, Any]:
        """PUT /photos/{id} — the docs give an EMPTY body schema, so this probes
        whether license_code is accepted at all."""
        return self._request(
            "PUT",
            f"/photos/{int(photo_id)}",
            params={"fields": PHOTO_FIELDS},
            json_body={"photo": {"license_code": license_code}},
        )

    def try_delete_photo(self, photo_id: int) -> tuple[int, Any]:
        """Confirm DELETE /photos/{id} really does not exist (expect 404/405)."""
        return self._request("DELETE", f"/photos/{int(photo_id)}")


# ---------------------------------------------------------------------------
# Mushroom Observer API2 proof client
# ---------------------------------------------------------------------------


class MOProof:
    """MO spaces requests conservatively (>=5s, or the server's reported runtime)."""

    def __init__(self, api_key: str = "", *, timeout: float = 60.0, verbose: bool = False) -> None:
        self._key = api_key.strip()
        self._verbose = verbose
        self._last_finished = 0.0
        self._last_runtime = 0.0
        self._client = httpx.Client(
            base_url=MO_API2,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._client.close()

    def _wait(self) -> None:
        minimum = max(5.0, self._last_runtime)
        remaining = minimum - (time.monotonic() - self._last_finished)
        while remaining > 0:
            time.sleep(min(0.25, remaining))
            remaining = minimum - (time.monotonic() - self._last_finished)

    def _request(
        self, method: str, endpoint: str, payload: dict, *, files: Optional[dict] = None
    ) -> tuple[int, Any]:
        self._wait()
        if self._verbose:
            print(f"    → MO {method} /{endpoint} {sorted(payload)}")
        body = {"format": "json", **payload}
        try:
            if method == "GET":
                response = self._client.get(f"/{endpoint}", params=body)
            else:
                response = self._client.request(
                    method, f"/{endpoint}", data=body, files=files
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._last_finished = time.monotonic()
            raise Ambiguous(f"MO {method} /{endpoint}: {type(exc).__name__}") from exc
        self._last_finished = time.monotonic()
        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {"_raw": response.text[:400]}
        if isinstance(parsed, dict):
            try:
                self._last_runtime = float(parsed.get("run_time") or 0.0)
            except (TypeError, ValueError):
                self._last_runtime = 0.0
        if self._verbose:
            print(f"    ← {response.status_code} {json.dumps(parsed)[:300]}")
        return response.status_code, parsed

    def help_images(self, method: str = "POST") -> tuple[int, Any]:
        """Probe the parameter list. NOTE: the help text is method-specific —
        GET returns query filters, POST returns the *create* parameters."""
        payload: dict[str, Any] = {"help": 1}
        if self._key:
            payload["api_key"] = self._key
        return self._request(method, "images", payload)

    def images_for_observation(self, observation_id: int) -> tuple[int, Any]:
        return self._request(
            "GET", "images", {"observation": int(observation_id), "detail": "high"}
        )

    def create_image(
        self,
        image: Path,
        observation_id: int,
        *,
        license_id: int,
        copyright_holder: str,
        md5sum: str = "",
    ) -> tuple[int, Any]:
        """POST /api2/images. ``license`` MUST be a numeric License id — MO
        rejects the human-readable name outright. ``md5sum`` is MO's own
        server-side integrity/fingerprint field."""
        payload: dict[str, Any] = {
            "api_key": self._key,
            "observations": int(observation_id),
            "license": int(license_id),
            "copyright_holder": copyright_holder,
        }
        if md5sum:
            payload["md5sum"] = md5sum
        with image.open("rb") as handle:
            return self._request(
                "POST",
                "images",
                payload,
                files={"upload": (image.name, handle, _content_type(image))},
            )

    def delete_image(self, image_id: int) -> tuple[int, Any]:
        return self._request(
            "DELETE", "images", {"api_key": self._key, "id": int(image_id)}
        )


# ---------------------------------------------------------------------------
# Checks — iNaturalist (§12, MO→iNat direction, the locked first direction)
# ---------------------------------------------------------------------------


def run_inat_checks(
    inat: INatProof, obs_uuid: str, image: Path, ledger: Ledger
) -> list[Result]:
    results: list[Result] = []

    def record(res: Result) -> Result:
        results.append(res)
        _print_result(res)
        return res

    # --- identity + baseline -------------------------------------------------
    status, payload = inat.whoami()
    if status >= 400:
        record(
            Result(
                "inat.auth",
                "JWT authenticates against v2",
                FAIL,
                f"HTTP {status} from /users/me — the JWT is missing, expired, or "
                "was sent as 'Bearer <jwt>' (iNaturalist wants the bare token).",
            )
        )
        return results
    me = (payload.get("results") or [{}])[0]
    record(
        Result(
            "inat.auth",
            "JWT authenticates against v2",
            PASS,
            f"authenticated as {me.get('login')} (id {me.get('id')})",
            {"user": me},
        )
    )

    # --- resolve the target (accepts an ID or a UUID) and confirm ownership --
    try:
        obs_uuid, observation = inat.resolve_observation(obs_uuid)
    except RuntimeError as exc:
        record(Result("inat.baseline", "Target observation is readable", FAIL, str(exc)))
        return results

    owner = (observation.get("user") or {}).get("login")
    if owner and me.get("login") and owner != me.get("login"):
        record(
            Result(
                "inat.baseline",
                "Target observation is readable and owned by you",
                FAIL,
                f"observation {observation.get('id')} belongs to {owner!r}, but you "
                f"are authenticated as {me.get('login')!r}. Refusing to write to a "
                "record you do not own.",
                {"owner": owner},
            )
        )
        return results

    baseline = list(observation.get("observation_photos") or [])
    baseline_ids = {p.get("uuid") for p in baseline}
    record(
        Result(
            "inat.baseline",
            "Target observation is readable and owned by you",
            PASS,
            f"observation id={observation.get('id')} uuid={obs_uuid} "
            f"owner={owner}; {len(baseline)} photo(s) already attached",
            {
                "observation_id": observation.get("id"),
                "uuid": obs_uuid,
                "existing_observation_photo_uuids": sorted(x for x in baseline_ids if x),
            },
        )
    )

    # --- 1. POST /photos returns a numeric id, and does it echo the uuid? ----
    upload_uuid = str(uuidlib.uuid4())
    try:
        status, payload = inat.upload_photo(image, upload_uuid)
    except Ambiguous as exc:
        record(
            Result(
                "inat.upload",
                "POST /photos returns a numeric id + echoes client uuid",
                UNKNOWN,
                f"outcome unknown: {exc}",
            )
        )
        return results
    photo = _first(payload)
    photo_id = _int_or_none(photo.get("id"))
    if status >= 400 or photo_id is None:
        record(
            Result(
                "inat.upload",
                "POST /photos returns a numeric id + echoes client uuid",
                FAIL,
                f"HTTP {status}; no numeric photo id in response",
                {"response": payload},
            )
        )
        return results
    ledger.uploaded_photo_ids.append(photo_id)
    echoed = str(photo.get("uuid") or "")
    record(
        Result(
            "inat.upload",
            "POST /photos returns a numeric id + echoes client uuid",
            PASS if echoed == upload_uuid else UNKNOWN,
            f"photo id={photo_id}; response uuid="
            + (
                f"{echoed!r} (matches)"
                if echoed == upload_uuid
                else f"{echoed!r} — does NOT match the uuid we sent; the Photo "
                "schema has no uuid field, so a bare upload is not re-findable "
                "by client uuid. Check inat.dedup: if that passed, the uuid is "
                "still honoured server-side as a write idempotency key, it just "
                "cannot be read back"
            ),
            {"photo_id": photo_id, "sent_uuid": upload_uuid, "returned_uuid": echoed},
            blocking=False,
        )
    )

    # --- 2. same-uuid re-POST is de-duplicated ------------------------------
    try:
        status, payload = inat.upload_photo(image, upload_uuid)
        dup = _first(payload)
        dup_id = _int_or_none(dup.get("id"))
        if dup_id is not None and dup_id != photo_id:
            ledger.uploaded_photo_ids.append(dup_id)
        if status >= 400:
            verdict, detail = UNKNOWN, f"re-POST rejected with HTTP {status}"
        elif dup_id == photo_id:
            verdict, detail = PASS, f"same uuid returned the same photo id {dup_id} — de-duplicated"
        else:
            verdict, detail = (
                FAIL,
                f"same uuid created a SECOND photo (id {dup_id} != {photo_id}); the "
                "client uuid is NOT an idempotency key for POST /photos, so "
                "retry-after-timeout must re-read instead of re-posting",
            )
        record(
            Result(
                "inat.dedup",
                "Same-uuid re-POST is de-duplicated",
                verdict,
                detail,
                {"first_id": photo_id, "second_id": dup_id},
            )
        )
    except Ambiguous as exc:
        record(Result("inat.dedup", "Same-uuid re-POST is de-duplicated", UNKNOWN, str(exc)))

    # --- 3. JSON attach of an already-uploaded photo -------------------------
    json_attach_uuid = str(uuidlib.uuid4())
    try:
        status, payload = inat.attach_json(obs_uuid, photo_id, json_attach_uuid)
        op = _first(payload)
        op_uuid = str(op.get("uuid") or "")
        if status < 400 and op_uuid:
            ledger.attached_obs_photo_uuids.append(op_uuid)
        landed = _attached(inat, obs_uuid, baseline_ids)
        ok = status < 400 and bool(landed)
        record(
            Result(
                "inat.attach_json",
                "POST /observation_photos (JSON, photo_id) attaches an existing upload",
                PASS if ok else FAIL,
                f"HTTP {status}; verified by re-read: {len(landed)} new observation_photo(s)",
                {"response": op, "new_after_reread": landed},
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.attach_json",
                "POST /observation_photos (JSON, photo_id) attaches an existing upload",
                UNKNOWN,
                str(exc),
            )
        )

    # --- 4. re-find by client uuid after a "lost" response -------------------
    try:
        current = inat.observation_photos(obs_uuid)
        match = [p for p in current if str(p.get("uuid") or "") == json_attach_uuid]
        record(
            Result(
                "inat.refind",
                "An attachment is re-findable by client uuid (lost-response recovery)",
                PASS if match else FAIL,
                (
                    f"observation_photo uuid {json_attach_uuid} found on re-read — "
                    "ambiguous writes can be resolved by re-reading the observation"
                )
                if match
                else (
                    f"client uuid {json_attach_uuid} did NOT appear as an "
                    "observation_photo uuid; recovery must fall back to photo_id "
                    "+ byte fingerprint matching"
                ),
                {"observation_photo_uuids": [p.get("uuid") for p in current]},
            )
        )
    except RuntimeError as exc:
        record(
            Result(
                "inat.refind",
                "An attachment is re-findable by client uuid (lost-response recovery)",
                UNKNOWN,
                str(exc),
            )
        )

    # --- 5. multipart single-call upload+attach (the PREFERRED path) ---------
    before = {p.get("uuid") for p in _safe_photos(inat, obs_uuid)}
    mp_uuid = str(uuidlib.uuid4())
    try:
        status, payload = inat.attach_multipart(image, obs_uuid, mp_uuid)
        op = _first(payload)
        op_uuid = str(op.get("uuid") or "")
        if status < 400 and op_uuid:
            ledger.attached_obs_photo_uuids.append(op_uuid)
        mp_photo_id = _int_or_none((op.get("photo") or {}).get("id"))
        if mp_photo_id:
            ledger.uploaded_photo_ids.append(mp_photo_id)
        landed = _attached(inat, obs_uuid, before)
        ok = status < 400 and bool(landed)
        record(
            Result(
                "inat.attach_multipart",
                "POST /observation_photos (multipart) uploads AND attaches in one call",
                PASS if ok else FAIL,
                f"HTTP {status}; verified by re-read: {len(landed)} new observation_photo(s). "
                "This is the path the design prefers — it has no separable orphan window.",
                {"response": op, "new_after_reread": landed},
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.attach_multipart",
                "POST /observation_photos (multipart) uploads AND attaches in one call",
                UNKNOWN,
                str(exc),
            )
        )

    # --- 6. what license did an upload actually land with? -------------------
    try:
        current = _safe_photos(inat, obs_uuid)
        ours = [
            p
            for p in current
            if _int_or_none((p.get("photo") or {}).get("id")) in set(ledger.uploaded_photo_ids)
        ]
        licenses = {
            _int_or_none((p.get("photo") or {}).get("id")): (p.get("photo") or {}).get("license_code")
            for p in ours
        }
        record(
            Result(
                "inat.default_license",
                "Uploaded photo lands with the account default license",
                PASS if licenses else UNKNOWN,
                f"observed license_code per uploaded photo: {licenses}. "
                "Per the locked decision, the account default is acceptable — "
                "this row is informational, recording what the destination shows.",
                {"licenses": licenses},
                blocking=False,
            )
        )
    except Exception as exc:  # noqa: BLE001 - informational row only
        record(
            Result(
                "inat.default_license",
                "Uploaded photo lands with the account default license",
                UNKNOWN,
                str(exc),
                blocking=False,
            )
        )

    # --- 7. INFORMATIONAL: can PUT /photos/{id} set license_code? -----------
    try:
        status, payload = inat.put_photo_license(photo_id, "cc-by-nc")
        updated = _first(payload)
        got = str(updated.get("license_code") or "")
        # iNaturalist echoes the code back upper-cased ("CC-BY-NC") even though
        # reads report it lower-cased ("cc-by"). Compare casefolded, and say so
        # loudly — any license comparison in the app must do the same.
        matched = got.casefold() == "cc-by-nc"
        record(
            Result(
                "inat.put_license",
                "PUT /photos/{id} accepts license_code (informational)",
                PASS if status < 400 and matched else UNKNOWN,
                f"HTTP {status}; license_code now {got!r}. "
                + (
                    "Accepted despite the empty body schema in api-docs.json, so "
                    "exact license preservation IS available if ever wanted. "
                    "NOTE the case asymmetry: reads return lower-case, this "
                    "response is upper-case — compare casefolded."
                    if matched
                    else "Did not take effect."
                )
                + " Not a blocker — the locked decision accepts the account default.",
                {"response": updated, "returned_license_code": got},
                blocking=False,
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.put_license",
                "PUT /photos/{id} accepts license_code (informational)",
                UNKNOWN,
                str(exc),
                blocking=False,
            )
        )

    # --- 8. INFORMATIONAL: confirm no DELETE /photos/{id} --------------------
    try:
        status, payload = inat.try_delete_photo(photo_id)
        absent = status in (404, 405, 501)
        record(
            Result(
                "inat.no_photo_delete",
                "There is no DELETE /photos/{id} (informational)",
                PASS if absent else UNKNOWN,
                f"HTTP {status} — "
                + (
                    "confirms bare uploads cannot be deleted via the API, as the "
                    "report states. Out of scope per the locked decision."
                    if absent
                    else "unexpected; a delete surface may exist after all."
                ),
                {"status": status, "response": payload},
                blocking=False,
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.no_photo_delete",
                "There is no DELETE /photos/{id} (informational)",
                UNKNOWN,
                str(exc),
                blocking=False,
            )
        )

    # --- 9. DELETE /observation_photos/{uuid} detaches ----------------------
    if ledger.attached_obs_photo_uuids:
        target = ledger.attached_obs_photo_uuids[0]
        before_detach = {p.get("uuid") for p in _safe_photos(inat, obs_uuid)}
        try:
            status, _ = inat.detach(target)
            after = {p.get("uuid") for p in _safe_photos(inat, obs_uuid)}
            gone = target in before_detach and target not in after
            if gone:
                ledger.attached_obs_photo_uuids.remove(target)
            record(
                Result(
                    "inat.detach",
                    "DELETE /observation_photos/{uuid} detaches",
                    PASS if status < 400 and gone else FAIL,
                    f"HTTP {status}; observation_photo {target} "
                    + ("removed on re-read" if gone else "still present on re-read"),
                    {"detached": target},
                )
            )
        except Ambiguous as exc:
            record(
                Result("inat.detach", "DELETE /observation_photos/{uuid} detaches", UNKNOWN, str(exc))
            )

    return results


# ---------------------------------------------------------------------------
# Checks — Mushroom Observer (iNat→MO, the second direction)
# ---------------------------------------------------------------------------


def run_mo_read_checks(mo: MOProof, observation_id: Optional[int]) -> list[Result]:
    results: list[Result] = []

    def record(res: Result) -> Result:
        results.append(res)
        _print_result(res)
        return res

    # The help text is method-specific: GET lists query filters, POST lists the
    # create parameters. Only the POST list settles the create contract.
    status, payload = mo.help_images("POST")
    params = _mo_help_params(payload)
    required = {"upload", "observations", "license", "copyright_holder"}
    missing = required - params
    record(
        Result(
            "mo.help_create",
            "POST /api2/images create parameter names (probe help WITH key)",
            PASS if params and not missing else UNKNOWN,
            f"HTTP {status}; create parameters: {sorted(params) or 'none parsed'}"
            + (f"; MISSING expected {sorted(missing)}" if missing else "")
            + (
                ". 'observations' confirms attach-at-create; 'md5sum' is MO's own "
                "server-side fingerprint field."
                if not missing
                else ""
            ),
            {"parameters": sorted(params), "missing": sorted(missing)},
        )
    )

    if observation_id:
        status, payload = mo.images_for_observation(observation_id)
        rows = _mo_results(payload)
        record(
            Result(
                "mo.enumerate",
                "GET /api2/images?observation= enumerates existing images",
                PASS if status < 400 else FAIL,
                f"HTTP {status}; {len(rows)} image(s) on MO observation {observation_id}. "
                "This is the destination-enumeration signal for duplicate detection.",
                {
                    "images": [
                        {
                            "id": row.get("id"),
                            "license": row.get("license"),
                            "copyright_holder": row.get("copyright_holder"),
                        }
                        for row in rows
                    ]
                },
            )
        )
    return results


def run_mo_write_checks(
    mo: MOProof,
    observation_id: int,
    image: Path,
    copyright_holder: str,
    ledger: Ledger,
    *,
    license_id: int = MO_LICENSE_CC_BY_NC_SA_3,
) -> list[Result]:
    results: list[Result] = []

    def record(res: Result) -> Result:
        results.append(res)
        _print_result(res)
        return res

    digest = hashlib.md5(image.read_bytes(), usedforsecurity=False).hexdigest()  # noqa: S324 — MO's md5sum contract requires MD5, not a security use

    try:
        before = {row.get("id") for row in _mo_results(mo.images_for_observation(observation_id)[1])}
    except Ambiguous as exc:
        record(
            Result(
                "mo.create_attach",
                "POST /api2/images creates AND attaches in one operation",
                UNKNOWN,
                f"{exc} — could not enumerate before create, aborting to avoid an unsafe write",
            )
        )
        return results

    try:
        status, payload = mo.create_image(
            image,
            observation_id,
            license_id=license_id,
            copyright_holder=copyright_holder,
            md5sum=digest,
        )
    except Ambiguous as exc:
        record(
            Result(
                "mo.create_attach",
                "POST /api2/images creates AND attaches in one operation",
                UNKNOWN,
                f"{exc} — re-enumerate before any retry",
            )
        )
        return results

    rows = _mo_results(payload)
    returned_id = _int_or_none(rows[0].get("id")) if rows else None
    error_text = _mo_error_text(payload)

    # Record whatever id the create response carried immediately, before doing
    # anything that can raise — otherwise a created-but-unenumerated image
    # would be untracked and could not be cleaned up.
    if returned_id:
        ledger.mo_image_ids.append(returned_id)

    try:
        after_rows = _mo_results(mo.images_for_observation(observation_id)[1])
    except Ambiguous as exc:
        record(
            Result(
                "mo.create_attach",
                "POST /api2/images creates AND attaches in one operation",
                UNKNOWN,
                f"HTTP {status}; create response carried image id={returned_id}; "
                f"{exc} — could not enumerate after create to confirm attachment",
            )
        )
        return results
    after = {row.get("id") for row in after_rows}
    landed = after - before

    # MO's create response does NOT carry the new image id, so the id has to be
    # recovered by diffing the destination enumeration. Track whichever we get,
    # otherwise the image cannot be cleaned up.
    landed_ids = sorted(v for v in (_int_or_none(x) for x in landed) if v is not None)
    new_id = returned_id or (landed_ids[0] if landed_ids else None)
    # returned_id was already recorded above, right after creation; only the
    # diff-recovered fallback id needs adding here.
    if new_id and new_id != returned_id:
        ledger.mo_image_ids.append(new_id)
    record(
        Result(
            "mo.create_attach",
            "POST /api2/images creates AND attaches in one operation",
            PASS if landed and not error_text else FAIL,
            f"HTTP {status}; create response carried image id={returned_id} "
            f"(MO does NOT return it — recovered id {new_id} by enumeration diff); "
            f"enumeration shows {len(landed)} new image(s) on the observation"
            + (
                " — create-with-attach is a single atomic operation."
                if landed and not error_text
                else f" — attachment NOT confirmed. MO error: {error_text or 'none reported'}"
            ),
            {
                "returned_id": new_id,
                "mo_error": error_text,
                "new_ids": sorted(x for x in landed if x),
            },
        )
    )

    created = next((row for row in after_rows if row.get("id") in landed), {})
    if created:
        got_license = str(created.get("license") or "")
        got_holder = str(created.get("copyright_holder") or "")
        holder_ok = got_holder.strip() == copyright_holder.strip()
        license_ok = got_license == MO_LICENSE_NAMES.get(license_id, "")
        record(
            Result(
                "mo.license_roundtrip",
                "license + copyright_holder round-trip exactly",
                PASS if holder_ok and license_ok else UNKNOWN,
                f"sent license id {license_id} → got {got_license!r} "
                + ("(exact match)" if license_ok else f"(expected {MO_LICENSE_NAMES.get(license_id)!r})")
                + f"; copyright_holder {got_holder!r} (sent {copyright_holder!r})"
                + ("" if holder_ok else " — MISMATCH"),
                {
                    "license": got_license,
                    "license_id_sent": license_id,
                    "copyright_holder": got_holder,
                    "md5sum_sent": digest,
                },
            )
        )

    if not new_id:
        record(
            Result(
                "mo.delete",
                "DELETE /api2/images removes a mis-created image",
                FAIL,
                "could not determine the created image id (create returned none and "
                "the enumeration diff was empty), so deletion could not be proven "
                "and any created image was NOT cleaned up",
            )
        )
    else:
        try:
            status, payload = mo.delete_image(new_id)
            del_error = _mo_error_text(payload)
            remaining = {row.get("id") for row in _mo_results(mo.images_for_observation(observation_id)[1])}
            gone = new_id not in remaining
            if gone and new_id in ledger.mo_image_ids:
                ledger.mo_image_ids.remove(new_id)
            record(
                Result(
                    "mo.delete",
                    "DELETE /api2/images removes a mis-created image",
                    PASS if gone and not del_error else FAIL,
                    f"HTTP {status}; image {new_id} "
                    + (
                        "removed on re-read — MO cleanup IS viable, unlike iNat"
                        if gone
                        else f"still present. MO error: {del_error or 'none reported'}"
                    ),
                    {"mo_error": del_error},
                )
            )
        except Ambiguous as exc:
            record(
                Result("mo.delete", "DELETE /api2/images removes a mis-created image", UNKNOWN, str(exc))
            )

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_jwt(token: str) -> str:
    value = (token or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value.split(None, 1)[1].strip()
    return value


def _content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _first(payload: Any) -> dict:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            return results[0] if isinstance(results[0], dict) else {}
        if isinstance(results, dict):
            return results
        return payload
    return {}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_photos(inat: INatProof, obs_uuid: str) -> list[dict]:
    try:
        return inat.observation_photos(obs_uuid)
    except RuntimeError:
        return []


def _attached(inat: INatProof, obs_uuid: str, before: set) -> list[str]:
    """Re-read the observation and return observation_photo uuids that are new.

    This is the controlling rule from Gate 1D, carried forward: a write counts as
    succeeded only when a fresh destination read proves the attachment exists.
    """
    current = _safe_photos(inat, obs_uuid)
    return [str(p.get("uuid")) for p in current if p.get("uuid") not in before]


def _mo_results(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("results", "images"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _mo_help_params(payload: Any) -> set[str]:
    """Pull parameter names out of MO's help payload.

    MO does not return structured help. It raises ``API2::HelpMessage`` and puts
    a usage string in ``errors[].details``::

        Usage: copyright_holder: string (limit=255 chars); date: date; ...

    so the names are the ``;``-separated leading tokens before each colon.
    """
    found: set[str] = set()
    for error in _mo_errors(payload):
        details = str(error.get("details") or "")
        if "Usage:" not in details:
            continue
        usage = details.split("Usage:", 1)[1]
        for chunk in usage.split(";"):
            name = chunk.split(":", 1)[0].strip()
            if name and " " not in name and name.replace("_", "").isalnum():
                found.add(name)
    return found


def _mo_errors(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _mo_error_text(payload: Any) -> str:
    """MO returns HTTP 200 with a fatal error in the body, so failures must be
    read out of ``errors[]`` rather than the status code."""
    parts = []
    for error in _mo_errors(payload):
        code = str(error.get("code") or "")
        details = str(error.get("details") or "").split("\n")[0]
        if "HelpMessage" in code:
            continue
        parts.append(f"{code}: {details}" if code else details)
    return "; ".join(parts)[:300]


_ICON = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "????", SKIP: "SKIP"}


def _print_result(res: Result) -> None:
    tag = _ICON[res.status]
    scope = "" if res.blocking else "  (informational)"
    print(f"  [{tag}] {res.check_id}{scope}\n         {res.title}\n         {res.detail}")


# ---------------------------------------------------------------------------
# Cleanup + reporting
# ---------------------------------------------------------------------------


def cleanup(inat: Optional[INatProof], mo: Optional[MOProof], ledger: Ledger) -> None:
    print("\n--- cleanup ---")
    if inat:
        for op_uuid in list(ledger.attached_obs_photo_uuids):
            try:
                status, _ = inat.detach(op_uuid)
                print(f"  detached observation_photo {op_uuid} (HTTP {status})")
                ledger.attached_obs_photo_uuids.remove(op_uuid)
            except Ambiguous as exc:
                print(f"  !! detach of {op_uuid} outcome UNKNOWN: {exc}")
    if mo:
        for image_id in list(ledger.mo_image_ids):
            try:
                status, _ = mo.delete_image(image_id)
                print(f"  deleted MO image {image_id} (HTTP {status})")
                ledger.mo_image_ids.remove(image_id)
            except Ambiguous as exc:
                print(f"  !! MO delete of {image_id} outcome UNKNOWN: {exc}")

    if ledger.uploaded_photo_ids:
        print(
            "\n  NOTE: iNaturalist has no DELETE /photos/{id}. These uploaded photo\n"
            "  IDs are now unattached and cannot be removed via the API. Delete them\n"
            "  on the website if you want to; the design treats this as out of scope."
        )
        for photo_id in ledger.uploaded_photo_ids:
            print(f"    orphaned photo id: {photo_id}")


def summarise(results: list[Result]) -> int:
    print("\n" + "=" * 72)
    print("GATE 1E-A LIVE PROOF SUMMARY")
    print("=" * 72)
    blocking = [r for r in results if r.blocking]
    info = [r for r in results if not r.blocking]

    for label, group in (("BLOCKING", blocking), ("INFORMATIONAL", info)):
        if not group:
            continue
        print(f"\n{label}:")
        for res in group:
            print(f"  {_ICON[res.status]:<5} {res.check_id:<26} {res.title}")

    failed = [r for r in blocking if r.status == FAIL]
    unknown = [r for r in blocking if r.status == UNKNOWN]
    print("\n" + "-" * 72)
    if failed:
        print(f"RESULT: NOT CLEARED — {len(failed)} blocking check(s) failed.")
        print("Gate 1E-B implementation must not begin. Failing checks:")
        for res in failed:
            print(f"  - {res.check_id}: {res.detail}")
        return 1
    if unknown:
        print(f"RESULT: INCONCLUSIVE — {len(unknown)} blocking check(s) unknown.")
        print("Re-run against a fresh disposable record before deciding.")
        return 2
    if not blocking:
        print("RESULT: nothing ran. Supply --inat-obs-uuid / --mo-obs-id.")
        return 2
    print("RESULT: CLEARED — every blocking check passed.")
    print("Update docs/gate_1e_capability_report.md §12 with these outcomes,")
    print("then Gate 1E-B implementation may begin.")
    return 0


def write_report(results: list[Result], path: Path) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": [
            {
                "id": r.check_id,
                "title": r.title,
                "status": r.status,
                "blocking": r.blocking,
                "detail": r.detail,
                "evidence": r.evidence,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nMachine-readable results written to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gate 1E-A live-proof harness (performs REAL writes when --run is given).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--inat-obs",
        "--inat-obs-uuid",
        dest="inat_obs_uuid",
        default="",
        metavar="ID_OR_UUID",
        help="A DISPOSABLE iNaturalist observation you own — numeric ID or UUID",
    )
    parser.add_argument("--mo-obs-id", type=int, default=0, help="ID of a DISPOSABLE MO observation you own")
    parser.add_argument("--image", type=Path, help="Small test image to upload")
    parser.add_argument("--mo-write", action="store_true", help="Also run MO create/delete write proofs")
    parser.add_argument("--copyright-holder", default="", help="copyright_holder to send to MO")
    parser.add_argument("--run", action="store_true", help="Actually execute (default is dry-run)")
    parser.add_argument(
        "--i-own-these-records",
        action="store_true",
        help="Required with --run. Asserts every named record is yours and disposable.",
    )
    parser.add_argument("--no-cleanup", action="store_true", help="Leave created records in place")
    parser.add_argument("--json-out", type=Path, help="Write machine-readable results here")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log every request/response")
    return parser


def plan(args: argparse.Namespace) -> None:
    print("DRY RUN — nothing was sent. This run would:\n")
    if args.inat_obs_uuid:
        print(f"  iNaturalist, on observation {args.inat_obs_uuid}:")
        print("    - upload a photo twice with the same client uuid (dedup probe)")
        print("    - attach an uploaded photo via the JSON shape, then re-read to verify")
        print("    - upload+attach via the multipart shape, then re-read to verify")
        print("    - probe PUT /photos/{id} license_code   (informational)")
        print("    - probe DELETE /photos/{id} absence     (informational)")
        print("    - detach an observation_photo and verify by re-read")
        print("    ! leaves 2-3 UNDELETABLE orphan photos on your account")
    if args.mo_obs_id:
        print(f"\n  Mushroom Observer, on observation {args.mo_obs_id}:")
        print("    - probe GET /api2/images?help=1 WITH your key (read-only)")
        print("    - enumerate existing images (read-only)")
        if args.mo_write:
            print("    - create an image attached to that observation, then DELETE it")
        else:
            print("    (no MO writes; pass --mo-write to include them)")
    print("\nRe-run with --run --i-own-these-records to execute.")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.inat_obs_uuid and not args.mo_obs_id:
        print("Nothing to do: pass --inat-obs-uuid and/or --mo-obs-id.", file=sys.stderr)
        return 2

    needs_image = bool(args.inat_obs_uuid) or args.mo_write
    if needs_image:
        if not args.image:
            print("--image is required for upload proofs.", file=sys.stderr)
            return 2
        if not args.image.is_file():
            print(f"--image not found: {args.image}", file=sys.stderr)
            return 2

    if not args.run:
        plan(args)
        return 0

    if not args.i_own_these_records:
        print(
            "Refusing to write. --run requires --i-own-these-records, asserting that\n"
            "every record named above is yours and disposable.",
            file=sys.stderr,
        )
        return 2

    ledger = Ledger()
    results: list[Result] = []
    inat: Optional[INatProof] = None
    mo: Optional[MOProof] = None

    try:
        if args.inat_obs_uuid:
            jwt = os.environ.get("INAT_JWT", "") or getpass.getpass("iNaturalist JWT (hidden): ")
            if not jwt.strip():
                print("No iNaturalist JWT supplied.", file=sys.stderr)
                return 2
            inat = INatProof(jwt, verbose=args.verbose)
            print("\n--- iNaturalist proofs ---")
            results += run_inat_checks(inat, args.inat_obs_uuid, args.image, ledger)

        if args.mo_obs_id:
            key = os.environ.get("MO_API_KEY", "") or getpass.getpass("Mushroom Observer API key (hidden): ")
            mo = MOProof(key, verbose=args.verbose)
            print("\n--- Mushroom Observer proofs ---")
            results += run_mo_read_checks(mo, args.mo_obs_id)
            if args.mo_write:
                holder = args.copyright_holder.strip()
                if not holder:
                    print(
                        "  [SKIP] mo.create_attach — --copyright-holder is required for MO writes",
                        file=sys.stderr,
                    )
                    results.append(
                        Result(
                            "mo.create_attach",
                            "MO create/attach write proof",
                            status=SKIP,
                            detail="--copyright-holder is required for MO writes",
                            blocking=False,
                        )
                    )
                else:
                    results += run_mo_write_checks(mo, args.mo_obs_id, args.image, holder, ledger)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    finally:
        if not args.no_cleanup:
            cleanup(inat, mo, ledger)
        elif ledger.attached_obs_photo_uuids or ledger.mo_image_ids:
            print("\n--no-cleanup: left these in place:")
            print(f"  observation_photo uuids: {ledger.attached_obs_photo_uuids}")
            print(f"  MO image ids: {ledger.mo_image_ids}")
        if inat:
            inat.close()
        if mo:
            mo.close()

    if args.json_out:
        write_report(results, args.json_out)
    return summarise(results)


if __name__ == "__main__":
    raise SystemExit(main())
