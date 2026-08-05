# Gate 1E-A — Photo Capability Spike Report

Status: **1E-A complete and live-proven; 1E-B backend and application wiring in
progress.** The §12 live proof passed on both sides (2026-07-23), and the
MO→iNat transfer backend now exists — see §16 for exactly what is and is not
built. Coordinator and UI wiring now exist end-to-end (a "Compare photos…"
dialog with per-image thumbnails, a large preview pane, and an explicit
per-upload confirmation), so the feature **is reachable from the running
application**. It has not yet had (a) a visual click-through of the new
thumbnail preview dialog on a real display, or (b) a controlled live
acceptance run of an actual upload through the wired UI — both are required
before this ships as an enabled feature.

Method: iNaturalist findings are drawn from the bundled authoritative v2 schema
(`api-docs.json`) and `inat_api_docs.md`. Mushroom Observer findings are drawn
from the live anonymous `help=1` schema probe of `mushroomobserver.org/api2` and
the existing `mo_client.py`. Every claim is tagged **[VERIFIED]** (established
from a doc or a completed live probe) or **[NEEDS LIVE PROOF]** (cannot be
established without the user's API credentials and a disposable owned record —
see §12).

---

> **Status update 2026-07-23:** the §12 live proof has been run **in full, both
> sides, and every blocking check passed.** MO→iNat and iNat→MO photo transfer
> are both cleared for Gate 1E-B implementation. See §12 for outcomes, §12.1 for
> the iNaturalist findings and §12.2 for the Mushroom Observer ones.
>
> **Both carry-forward obligations are now discharged:** the MO license table is
> confirmed against MO's own fixtures (§12.2b) and `photo_license.py` is
> table-driven (§12.3). Gate 1E-B may begin.

## 1. Supported transfer directions

| Direction | Upload possible | Attach possible | License-preservable | Verdict |
|---|---|---|---|---|
| **MO photo → iNat observation** | yes (POST /photos) | yes (POST /observation_photos) | n/a — account default accepted (§13.4) | **first direction**, gated only on the §12 attach proofs |
| **iNat photo → MO observation** | yes (POST /api2/images, live-proven) | yes — attach-at-create via `observations`, one atomic op | yes — license + copyright_holder round-trip exactly | proven (§12.2), **not implemented** |

Both directions were live-proven on 2026-07-23 (§12). The two constraints
originally identified as binding — **exact license preservation** (§5) and
**orphan-cleanup viability** (§7) — were retired by decisions 4 and 5 in §13.
MO→iNat is implemented as a backend (§16); iNat→MO is proven but unimplemented.

The prior gates' assumption that "MO is read-only" does **not** hold for photos:
`mo_client` already performs MO writes (`external_links`, `sequences`), and
`POST /api2/images` is a real write surface. So iNat→MO is not excluded a
priori — but it is the *less* proven direction and should be sequenced second.

---

## 2. iNaturalist — required API calls [VERIFIED from api-docs.json]

Photo attachment on iNat v2 is a **two-resource** model:

- `POST /photos` — multipart `file` (binary), optional client-supplied
  `uuid`. Returns a `Photo` with a **numeric auto-increment `id`**. This creates
  a bare `LocalPhoto` owned by the authenticated user, **not yet attached** to
  any observation.
- `POST /observation_photos` — two accepted shapes:
  - **JSON**: `{observation_photo: {observation_id: <obs uuid>, photo_id: <int>, position?, uuid?}}` — attaches an *already-uploaded* photo. (separate upload + attach)
  - **multipart**: `file` + `observation_photo[observation_id]` + optional `observation_photo[uuid]` — uploads **and** attaches in one call.
- `PUT /observation_photos/{uuid}` / `DELETE /observation_photos/{uuid}` — reorder / **detach** (removes the join row).
- `PUT /photos/{id}` — update a photo. The body schema in `api-docs.json` is empty (`{}`), but it **does accept `license_code`** (proven live, §12.1b). Unused: uploads keep the account default per decision §13.4.

### 2.1 Atomic vs non-atomic

- The **multipart `POST /observation_photos`** path is the closest thing to an
  atomic upload+attach: one request, no separable orphan window.
- The **two-call** path (`POST /photos` then `POST /observation_photos`) is
  explicitly **non-atomic**. It exists in the gate's step list (§8) because it is
  the only path that lets us record the returned upload ID before attaching — but
  see the orphan finding below, which makes it *more* dangerous on iNat, not less.

### 2.2 iNat orphan cleanup — **GATING NEGATIVE FINDING** [VERIFIED]

The v2 path set for photos is exactly: `POST /photos`, `PUT /photos/{id}`,
`POST /observation_photos`, `PUT|DELETE /observation_photos/{uuid}`.

**There is no `DELETE /photos/{id}`.** Therefore, if `POST /photos` succeeds but
the subsequent `POST /observation_photos` fails, the uploaded photo becomes an
**orphan that this API cannot delete.** The gate's step 7 ("attempt documented
orphan cleanup") is *not satisfiable* for the two-call iNat path.

Consequences:
- For MO→iNat we should **prefer the single multipart `POST /observation_photos`**
  so there is no separable orphan. The tradeoff: we do not get an upload ID before
  attach, so success-after-timeout recovery must rely on the client `uuid` (§4).
- If we ever must use the two-call path, "cleanup" degrades to "mark the orphan
  for manual deletion on the website" — it cannot be automated. This must be shown
  in the preview as a real risk.

### 2.3 iNat idempotency / success-after-lost-response [VERIFIED, mechanism]

Both `POST /photos` and the `observation_photo` accept a **client-generated
`uuid`**. `api-docs.json` documents it as: *"New UUID for the photo, helps
prevent duplication in poor network conditions."* This is our idempotency key:

1. Generate `uuid` locally before the write.
2. On a lost/ambiguous response, re-read the destination observation (which the
   reader already expands via `observation_photos:(photo:(id))`) and/or query by
   the `uuid` to determine whether the attachment landed **before** retrying.
3. Only retry when the read proves it did not land.

**Proven live (§12.1a) with an important caveat:** a same-`uuid` re-POST *is*
de-duplicated server-side, but the uuid is **not readable back** — the `Photo`
schema has no uuid field and the create response does not echo it. Recovery
therefore keys off the *observation_photo* uuid, which a destination re-read
does return.

### 2.4 iNat license / ownership representation [VERIFIED, partial]

- `Photo` carries `license_code`, `attribution`, `attribution_name`.
- `POST /photos` (`PhotosCreate`) accepts **only** `file`, `fields`, `uuid` —
  **no license parameter.** So an uploaded photo takes the **uploader account's
  default photo license**, not the source license. Preserving the source license
  therefore *requires* a follow-up `PUT /photos/{id}` with `license_code` — whose
  body schema is empty in the docs. `PUT /photos/{id}` **was proven to accept
  `license_code`** (§12.1b), so exact preservation is available — but decision
  §13.4 keeps the account default, and the implementation never re-licenses a
  photo.
- `native_photo_id` / `native_page_url` describe iNat's own external photo
  subclasses (Flickr, etc.) and are **not writable** for a directly uploaded
  `LocalPhoto`. There is **no server-side field to stamp cross-site provenance**
  (§6).

---

## 3. Mushroom Observer — required API calls

- `GET /api2/images` [VERIFIED live] filters include `license`,
  `copyright_holder_has`, `has_observation`, `observation`, `user`,
  `ok_for_export`, `content_type` (bmp|gif|jpg|png|raw|tiff), `size`
  (thumbnail…huge), `quality`, `date`. This lets us enumerate an observation's
  existing images and read their license/copyright-holder for duplicate and
  eligibility checks **without** authentication.
- `POST /api2/images` [VERIFIED live, §12.2] — create an image. The create
  parameter list required an authenticated **POST** `help` probe (a GET probe
  returns query filters only). It accepts an image payload plus
  `observations` (attach at create — a **single operation**),
  `copyright_holder`, `license`, `notes`, `date`, `vote`, `original`,
  `projects`, `md5sum`, `original_name`, `upload_file`, `upload_url`, `date`,
  `notes`, `vote`. Attach-at-create works and is atomic. **`license` must be a
  numeric id** and the create response returns **no image id** — see §12.2.
- Image bytes are served from `images.mushroomobserver.org` at size variants; the
  original may be gated by `ok_for_export`/license.

### 3.1 MO atomicity & cleanup [VERIFIED live, §12.2]

`POST /api2/images` attaches at create via `observations`, so MO upload+attach is
**one atomic operation** — strictly better than iNaturalist's model.
`DELETE /api2/images` exists and removes a mis-created image (verified by
re-read), so **MO cleanup is genuinely viable**, unlike iNaturalist's
undeletable orphan.

---

## 4. Ambiguous-outcome recovery (both sides)

| Event | Recovery |
|---|---|
| Timeout after iNat upload, before attach | No orphan delete exists; re-read obs by client `uuid`; if unattached, the bare photo is an un-deletable orphan → surface to user, do **not** silently retry. Prefer the single multipart call to avoid this window entirely. |
| Timeout after iNat attach | Re-read destination `observation_photos` for the client `uuid` / matching photo id + fingerprint; mark success only if present. |
| Timeout after MO create-with-attach | Re-query `GET /api2/images?observation=<id>` filtered by copyright_holder/date/fingerprint before any retry. |
| Crash between upload and attach (iNat two-call path) | Journal the returned `photo_id` **before** attach; on reopen, re-read obs, attach if missing, else mark done. (This is why the two-call path is journaled even though it is orphan-unsafe.) |

The controlling rule, mirroring Gate 1D: **a write is only marked succeeded after
a fresh destination re-read proves the intended attachment exists.** Any
ambiguous state is `outcome_unknown`, never an automatic retry.

---

## 5. License mapping — the primary eligibility gate

Eligibility (Gate 1E-B) requires the **source license be preservable exactly at
the destination.** This is the hardest constraint and it fails silently if we map
loosely.

- iNat `license_code` values: `cc0`, `cc-by`, `cc-by-nc`, `cc-by-sa`,
  `cc-by-nd`, `cc-by-nc-sa`, `cc-by-nc-nd`, and *null* = "all rights reserved".
  iNat CC licenses are **version 4.0**.
- MO licenses are Creative Commons **version 3.0** (and historically 2.5), plus
  "Public Domain"/CC0 and "All Rights Reserved".

**Version drift means most CC licenses are NOT exactly preservable** across the
two platforms (CC BY-NC-SA 3.0 ≠ CC BY-NC-SA 4.0). Under a strict reading of
"preserved exactly," only the licenses that coincide exactly qualify:
- `CC0 / Public Domain` ↔ `cc0` — exact.
- "All Rights Reserved" ↔ null — exact (but such a photo usually fails the
  "authorized to transfer" test unless self-owned; still no CC downgrade).
- Every versioned CC license is a **version mismatch** → **not exactly
  preservable → ineligible** under the strict rule.

Decision needed from the user (§13): treat "same CC letters, different version"
as *preservable* (pragmatic) or *not preservable* (strict). The strict reading
makes the initial eligible set very small (essentially CC0-only), which is the
safe way to ship the first version.

The explicit license map, unsupported-license list, and the chosen version policy
must be a reviewed constant, not inferred at runtime.

---

## 6. Provenance, duplicate detection, and idempotency keys

- **iNat lets us write no cross-site provenance identifier**; `native_photo_id`
  is read-only for local uploads. Provenance for MO→iNat is **client-side state
  only.**
  > **Corrected 2026-07-23 for the other direction:** MO's create parameters
  > include `original_name` ("original file name or other private identifier")
  > and `md5sum`, both writable. For **iNat→MO**, provenance and a dedup key
  > *can* live server-side. See §12.2d and §12.4.
- The domain model is already right: `MediaIdentity(site, photo_id, source_site,
  source_photo_id)` and `provenance_key` (types.py:60), and `matching.py` already
  keys media by `(site, photo_id)` and by provenance. **Same numeric id on both
  sites is not the same image** — the completion criteria explicitly test this, so
  identity must always be the `(site, id)` tuple, never a bare int.
- Duplicate detection therefore relies on **three** signals, in priority order:
  1. A locally persisted `(source_site, source_photo_id) → (dest_site, dest_photo_id)`
     ledger of prior transfers (authoritative for *our own* prior transfers).
  2. A **displayed byte/pixel fingerprint** (e.g. SHA-256 of normalized bytes, or a
     perceptual hash) shown in the preview for human confirmation — the only
     signal that catches images transferred by *other* means.
  3. Destination enumeration (`GET /api2/images?observation=` / iNat
     `observation_photos`) matched against 1 and 2.
- **Idempotency keys**: iNat client `uuid` (per §2.3); MO has no documented
  idempotency token → MO relies on the local ledger + destination enumeration.
- **Persistence rule** (from the gate): store only remote photo IDs, source
  provenance IDs, safe fingerprints, license code, action state, and verification
  results. **Never** persist photo bytes or credential-bearing/signed URLs.

---

## 7. Failure taxonomy

| Class | iNat | MO |
|---|---|---|
| Cleanly recoverable | attach fails, photo id known → journaled, resumable (two-call path) | create fails → nothing created, retryable |
| **Irrecoverable via API** | **orphaned bare photo** (no `DELETE /photos/{id}`) | mis-created image if `DELETE /api2/images` absent [NEEDS PROOF] |
| Ambiguous | timeout around either call → `outcome_unknown`, re-read before retry | timeout around create → enumerate before retry |

The irrecoverable iNat orphan is the single most important design driver: it is
why the plan defaults to the **single multipart attach** for MO→iNat.

---

## 8. Multi-step execution model (for the non-atomic iNat two-call path only)

Mirrors the gate's step list, retained only if we ever need per-step upload IDs:
1. Refresh source + destination photo state (fresh reads).
2. Download selected source bytes (memory / short-lived temp only).
3. `POST /photos` with client `uuid` → record returned numeric photo id.
4. (If preserving license) `PUT /photos/{id}` license_code [NEEDS PROOF].
5. `POST /observation_photos` (photo_id + obs uuid).
6. Verify by destination re-read (uuid / id + fingerprint).
7. On attach failure: **cannot delete** the orphan → mark and surface (no silent
   automation).
8. On unknown outcome: enumerate destination by provenance/fingerprint before any
   retry.
9. Mark success only after verified attachment.

For the **preferred single multipart path**, steps 3–5 collapse to one call and
step 7's orphan risk disappears.

---

## 9. What the preview must show (from the gate, mapped to available data)

Source/destination observations; selected photos; copyright holder (iNat
`attribution_name` / MO `copyright_holder`); source license → destination license
(with an explicit "**exact / version-mismatch / unsupported**" verdict from §5);
attribution string to be written; existing destination photos (enumerated live);
displayed byte/pixel fingerprint evidence; any unsupported metadata; and — for
the iNat two-call path — an explicit **"a temporary upload could become an
un-deletable orphan"** warning.

---

## 10. Unsupported / excluded up front

- Any photo whose copyright holder does not match the configured user via the
  explicit alias set.
- Any versioned CC license, **if** the strict "exact preservation" policy is
  chosen (§13).
- iNat→MO until §12 live proof of the MO create/attach/delete contract passes.
- Bulk/routine transfer — the gate forbids it until the spike passes and even
  then the preview is per-photo or per-bounded-reviewed-group.
- "Looks similar" matching — never a transfer trigger.

---

## 11. Reused vs new infrastructure

- **Reuse**: `MediaIdentity`/`provenance_key` (types.py), the Gate 1D state
  machine shape (fresh-read preview → journal one reviewed action → preflight →
  single verified write → mandatory re-read), `mo_client._write` (already supports
  POST/PATCH/DELETE + outcome-unknown semantics), `INatClient` v2 auth request
  path, `download_image` (needs a size/credential-safe wrapper for MO hosts).
- **New**: multipart upload helpers on both clients; a per-transfer ledger table
  (IDs + provenance + fingerprint + license + state only); the license map
  constant; a fingerprint function over normalized bytes; the copyright-holder
  alias-set config.

---

## 12. Live-proof checklist — **requires the user's credentials + disposable owned records**

I cannot perform authenticated uploads. These must be run against a *small number
of explicitly selected, user-owned, disposable* observations, ideally on a test
account. Each row must pass before Gate 1E-B implementation begins.

Run these with `tools/gate_1e_proof_harness.py` (see §15). Rows marked
*informational* no longer block the gate — see decisions 4 and 5 in §13.

**iNaturalist — RUN 2026-07-23, account `alan_rockefeller` (25945), observation
384114909 / `37e4497c-4948-4dc1-9c7f-96745c635851`. ALL BLOCKING ROWS PASSED.**

- [x] **PASS** JWT authenticates against v2 (bare `Authorization`, not `Bearer`).
- [x] **PASS** Target observation readable; owner matches authenticated account.
- [x] **PASS** Same-`uuid` re-POST **is** de-duplicated — the second
      `POST /photos` returned the *same* id (703107702), not a new photo.
- [x] **PASS** `POST /observation_photos` (JSON, `photo_id`) attaches an
      existing upload; confirmed by destination re-read.
- [x] **PASS** `POST /observation_photos` (multipart) uploads **and** attaches in
      one call; confirmed by re-read. *This is the path the design uses.*
- [x] **PASS** An attachment is re-findable by the client `uuid` we set on the
      **observation_photo** — lost-response recovery works.
- [x] **PASS** `DELETE /observation_photos/{uuid}` detaches; confirmed by re-read.
- [x] *(informational)* Uploads landed with `cc-by` — the account default, as
      expected under decision 4.
- [x] *(informational)* **`PUT /photos/{id}` DOES accept `license_code`** —
      HTTP 200, license became `CC-BY-NC`, despite the empty body schema in
      `api-docs.json`. See §12.1.
- [x] *(informational)* **No `DELETE /photos/{id}`** — HTTP 404, confirming the
      §2.2 finding. Accepted under decision 5.

### 12.1 Two findings from the run that change the design

**(a) The client `uuid` is a write idempotency key but is NOT readable back.**
`POST /photos` does not echo the `uuid` we send, and the `Photo` schema has no
`uuid` field — yet the same-uuid re-POST returned the same photo id. So iNat
honours the uuid server-side for de-duplication, but a **bare uploaded photo
cannot be located by uuid afterwards**. Consequences:
- Retry-after-timeout on `POST /photos` is *safe* (it de-duplicates) but not
  *verifiable* — we cannot read back to confirm.
- Verification must key off the **observation_photo** uuid, which we do control
  and which the destination re-read does return. This is another reason the
  single multipart attach is the primary path.

**(b) Exact license preservation is available after all.** `PUT /photos/{id}`
accepts `license_code`. Decision 4 (account default is fine) still stands and
remains the shipping behaviour, but §5's map is no longer display-only — it
could drive a real license write if ever wanted.
**Case asymmetry, important:** reads return the code lower-cased (`cc-by`) while
the `PUT` response returns it upper-cased (`CC-BY-NC`). **Every license
comparison must casefold.** `photo_license.py` emits lower-case codes, so any
code comparing its output against a live iNat value must normalise first.

**Mushroom Observer — RUN 2026-07-23, observation 656464, user `Alan
Rockefeller` (123). ALL BLOCKING ROWS PASSED.**

- [x] **PASS** `POST /api2/images` create parameters (probed `help` *with key*,
      **via POST** — see §12.2a): `copyright_holder`, `date`, `license`,
      `md5sum`, `notes`, `observations`, `original_name`, `projects`, `upload`,
      `upload_file`, `upload_url`, `vote`.
- [x] **PASS** Attach-at-create via `observations` works — **one atomic
      operation**, strictly better than iNaturalist's model.
- [x] **PASS** `license` + `copyright_holder` round-trip **exactly**: sent
      license id 2, read back `Creative Commons Non-commercial v3.0`;
      copyright_holder returned verbatim.
- [x] **PASS** `DELETE /api2/images` removes a created image (verified by
      re-read). **MO cleanup IS viable — unlike iNaturalist.**
- [x] **PASS** `GET /api2/images?observation=` enumerates existing images, and
      is the *only* way to learn a new image's id (§12.2c).

### 12.2 Mushroom Observer findings that change the design

**(a) `help` is method-specific.** `GET /api2/images?help=1` returns *query
filters*; only `POST /api2/images?help=1` returns the *create* parameters. The
first run of this harness probed GET and learned nothing about creation.

**(b) `license` must be a numeric License id.** MO rejects the human-readable
name outright: `BadParameterValue: Invalid or unknown license, "...", accept
only numerical id.` There is **no `/api2/licenses` endpoint** (404), and image
records return only the license *name*, never its id — so the id↔name map had to
be recovered by querying images per license id, and must live as a reviewed
constant:

| id | MO name (verified live) | MO's own CC URL | iNat code |
|---|---|---|---|
| 1 | Creative Commons Non-commercial v2.5 | `by-nc-sa/2.5` | `cc-by-nc-sa` |
| 2 | Creative Commons Non-commercial v3.0 | `by-nc-sa/3.0` | `cc-by-nc-sa` |
| 3 | Creative Commons Wikipedia Compatible v3.0 | `by-sa/3.0` | `cc-by-sa` |
| 4 | Public Domain (Wikipedia compatible) | `public-domain/cc0` | `cc0` |
| 5 | Creative Commons Attribution v4.0 (Wikipedia compatible) | `by/4.0` | `cc-by` |
| 6 | Creative Commons Attribution Non-commercial v4.0 | `by-nc/4.0` | `cc-by-nc` |
| 7 | Creative Commons Attribution Non-commercial NoDerivs v.4.0 | — | `cc-by-nc-nd` |
| 8 | Creative Commons Attribution Non-commercial ShareAlike v4.0 | — | `cc-by-nc-sa` |

**CONFIRMED 2026-07-23** against Mushroom Observer's own
`test/fixtures/licenses.yml`, which pairs each `display_name` with its canonical
`creativecommons.org` URL. Ids 7–8 postdate those fixtures but their names state
every clause explicitly.

> **The confirmation caught a real error.** This table's first draft inferred
> ids 1 and 2 from their names as **CC BY-NC**. They are actually
> **CC BY-NC-SA** — MO's "Non-commercial vN" names map to `by-nc-**sa**/N` and
> carry a **ShareAlike clause the display name never mentions**. Shipping the
> inferred value would have silently dropped SA and mis-stated the license on
> every transferred photo under ids 1–2. Note also that id 6, which *does* say
> "Attribution Non-commercial", really is plain `by-nc` — so the names cannot be
> pattern-matched even loosely. **Never regenerate this table from the display
> names.**

**(c) The create response does NOT return the new image id.** A successful
create returns no usable identifier; the id is recoverable only by diffing
`GET /api2/images?observation=` before and after. This makes MO creation
**only enumeration-verifiable**, so the ambiguous-outcome procedure in §4 is
mandatory rather than optional. `md5sum` (below) is what makes that diff
trustworthy.

**(d) `md5sum` is a native MO create parameter.** §6 assumed cross-site
fingerprints were client-side only; that is wrong for MO. MO accepts an MD5 of
the image bytes at create, giving a **server-side idempotency/duplicate key for
the iNat→MO direction** — much stronger than the display-only fingerprint
`photo_license.py` provides. Note it is **MD5**, not the SHA-256 that
`photo_byte_fingerprint` computes, so the ledger needs both.

**(e) MO returns HTTP 200 with fatal errors in the body.** The status code is
not a verdict; `errors[]` must be read on every write. The first harness run
reported "HTTP 200" for a create that had entirely failed.

### 12.3 `photo_license.py` is wrong for MO's real license names — must be fixed

`map_mo_license_to_inat` infers CC clauses from free text. Run against the eight
**actual** MO license names (§12.2b), three of eight are mis-mapped:

| id | MO name | Expected | `photo_license.py` gives |
|---|---|---|---|
| 1 | Creative Commons Non-commercial v2.5 | `cc-by-nc` | **`None`, ineligible** |
| 2 | Creative Commons Non-commercial v3.0 | `cc-by-nc` | **`None`, ineligible** |
| 3 | Creative Commons Wikipedia Compatible v3.0 | `cc-by-sa` | **`None`, ineligible** |
| 4–8 | (the rest) | — | correct |

Cause: the parser requires a literal "attribution" token to emit a `by` clause,
but MO's names for ids 1–3 never say "Attribution", and id 3 states no clause at
all. The failure is **fail-closed** — those photos are refused, not mislicensed —
so it is not a copyright hazard, but it silently blocks transfer of the single
most common license on the site (id 3 covers ~58k images; id 2 ~353 for the test
account).

**Fix:** replace the free-text heuristic with the id-keyed constant from
§12.2b. MO exposes the license *name* on reads and requires the *id* on writes,
so the table needs both directions and the name→id lookup must be exact-match on
the eight known strings, not fuzzy. This is exactly what §5 asked for: "a
reviewed constant, not inferred at runtime."

**Status: FIXED 2026-07-23.** `photo_license.py` is now table-driven
(`MO_LICENSES`), maps id *or* exact name in both directions, and fails closed on
anything unrecognised. All eight rows are now **confirmed** against MO's
fixtures (§12.2b), so `clauses_confirmed` is True throughout and
`inat_code_to_mo_license_id()` resolves every code MO can represent —
preferring the exact-version row, e.g. `cc-by-nc-sa` → id 8 (4.0) rather than id
1 (2.5) or id 2 (3.0). `cc-by-nd` correctly returns `None`: MO has no BY-ND
license. The module also gained `photo_md5()` for §12.2d and
`normalise_inat_license_code()` for §12.1b.

### 12.4 Original filename: not in the API, but reachable — and MO can store it

`original_filename` **is** declared on the v2 `Photo` schema, and the harness
explicitly requested it — iNaturalist **never returned it**, on any photo, in
any of the runs. Treat the field as documented-but-not-served.

It *is* visible in the web UI at `https://www.inaturalist.org/photos/{id}`, in a
table row headed "Filename", but only to the photo's owner and only with an
`_inaturalist_session` cookie. A working scraper exists at
`/home/alan/alison7.py.bak` (API for observation → photo ids, then BeautifulSoup
over the photo page; falls back to a `data-original-filename` attribute).

**Why this matters to Gate 1E.** MO's create parameters include
`original_name` — "original file name **or other private identifier**". That
partly overturns §6: for the **iNat→MO** direction there *is* a writable,
server-side field that can carry cross-site provenance, so provenance need not
be purely client-side ledger state in that direction. Combined with `md5sum`
(§12.2d), MO gives us two native dedup/provenance keys that iNaturalist does not.

**Costs, which is why this is not adopted here.** The scrape needs an
`_inaturalist_session` cookie — a *different and far more powerful credential*
than the API token the app stores today. It authenticates as the whole account
with no scoping and no per-app revocation, and the API guidelines direct
applications to OAuth for acting as a user. Storing it beside the existing token
is a real security downgrade and must be an explicit, separate decision — not a
side effect of wanting a filename. The scrape is also HTML-shaped and will break
without notice, and it costs one extra web request per photo against the ~10k/day
budget.

**Recommendation:** keep filename capture **out of Gate 1E**. If it is wanted
later, treat it as optional enrichment that degrades silently when absent, never
as a transfer precondition or a duplicate-detection key. `md5sum` already
provides the reliable server-side dedup signal without a new credential.

**Cross-cutting:**
- [ ] Destination-already-has-photo detection via enumeration + fingerprint.
- [ ] Same numeric id / different source identity is treated as distinct.
- [ ] Copyright-holder alias match **and** mismatch behave correctly.
- [ ] No long-lived photo bytes / signed URLs are persisted or logged.

---

## 13. Decisions (locked)

1. **License version policy** (§5): **PRAGMATIC** — "same CC letters" counts as
   preservable, so CC BY-NC-SA 3.0 → cc-by-nc-sa (4.0) is allowed. The preview
   must still label these transfers as *version-shifted* (not byte-identical) so
   the user sees the drift. "All Rights Reserved" and unrecognized licenses stay
   ineligible.
2. **First direction**: **MO→iNat first.** Uses the single multipart
   `POST /observation_photos` to avoid the un-deletable-orphan window (§2.2).
   License preservation on the iNat side still depends on the `PUT /photos/{id}`
   live proof (§12); until that passes, MO→iNat can only *attach* (photo lands
   with the account default license) — the preview must disclose this.
3. **Live proof** (§12): user will run it later against disposable owned records.
   Until then, **no network write code is wired**; only deterministic, proof-
   independent foundation is built (license map §5, byte fingerprint §6).
4. **Destination license**: the iNaturalist **account default license is
   acceptable** for transferred photos. `POST /photos` takes no license
   parameter (§2.4), and we will not require a follow-up `PUT /photos/{id}`.
   This **removes the §5 exact-preservation constraint as a blocker** for
   MO→iNat. The preview must still *disclose* the landing license so the drift
   is visible, and the §5 map is retained for that display.
5. **Photo deletion is out of scope.** This tool syncs across platforms; it has
   no reason to delete photos. The absence of `DELETE /photos/{id}` (§2.2) and
   the resulting un-deletable orphan are therefore **accepted**, not blocking.
   The single multipart attach remains preferred simply because it is one call
   with no separable failure window — not because orphans must be prevented.
   (Deleting duplicate *observations* remains in scope and is a separate gate.)

---

## 14. Recommendation

Decisions 4 and 5 cleared both original blockers; the 2026-07-23 run then cleared
the iNaturalist attach contract (§12).

- **MO→iNat** (first direction) is **CLEARED.** Both attach shapes, detach, and
  lost-response re-find verified live.
- **iNat→MO** (second direction) is **CLEARED.** Create-with-attach is a single
  atomic operation, license and copyright_holder round-trip exactly, and
  `DELETE /api2/images` gives real cleanup.

MO turns out to be the *better-behaved* side: attach-at-create is atomic, delete
works, and `md5sum` is a native server-side dedup key. iNaturalist is the side
with the irrecoverable orphan and the unreadable photo uuid.

Implementation must honour:
- §12.1 — verify via the **observation_photo** uuid (the photo uuid is not
  readable back); casefold every license comparison.
- §12.2c/e — after any MO create, learn the id by **enumeration diff** and read
  `errors[]`; never trust the HTTP status.
- §12.3 — use `photo_license.py`'s table for every license decision; never
  re-derive a license from MO's display name (§12.2b).

The design guidance is unchanged: prefer the single multipart
`POST /observation_photos`, and mark a write succeeded only after a fresh
destination re-read proves the attachment exists.

---

## 15. Live-proof harness

`tools/gate_1e_proof_harness.py` executes §12 mechanically. It sits **outside**
the `observation_workbench` package so that proving photo behaviour wires no write path into
the application, and it never reads QSettings or the app's saved token.

```bash
export INAT_JWT='...'        # https://www.inaturalist.org/users/api_token (expires ~24h)
./.venv/bin/python tools/gate_1e_proof_harness.py \
    --inat-obs <disposable-observation-id-or-uuid-you-own> \
    --image /path/to/small-test.jpg
# add --run --i-own-these-records to execute; add --json-out results.json to record
```

`--inat-obs` accepts either the numeric observation ID (what appears in the web
URL) or the UUID; numeric IDs are resolved to a UUID first, because the v2 photo
endpoints key off the UUID. Before any write the harness confirms the
observation's owner matches the authenticated account and aborts if it does not.

Guard rails: dry-run by default; writes require both `--run` and
`--i-own-these-records`; every target record is named explicitly on the command
line (nothing is discovered then written); credentials come from the environment
or a hidden prompt and are never logged. Cleanup detaches every
observation-photo the run created and deletes every MO image it created; the
orphaned iNat photo IDs it cannot delete are reported explicitly.

Exit codes: `0` cleared, `1` a blocking check failed, `2` inconclusive or
misconfigured.

---

## 16. Gate 1E-B implementation status (MO→iNat)

**Built and verified offline; now wired into the coordinator and UI.**
`ReconciliationCoordinator` exposes `prepare_photo_comparison`/
`execute_photo_action` (mirroring the Gate 1D coordinate-copy pattern), and
`ui/reconciliation.py` adds a "Compare photos…" action with a
`PhotoComparisonDialog` that shows a real thumbnail for every source option, a
comparable strip of destination thumbnails, and a larger preview pane synced
to the selected/clicked image; the confirm button stays disabled until an
enabled option is selected, and outcome-unknown results route through the
existing generic recover/verify machinery. It has **not** yet been visually
exercised on a real display or exercised in a controlled live acceptance run
(only import-time sanity checks were possible so far) — see the top-of-file
status line.

### 16.1 What exists

| Layer | Added |
|---|---|
| `api/client.py` | multipart support on the v2 path; `create_observation_photo_v2` (single upload+attach), `get_observation_photos_v2` |
| `types.py` | `PhotoActionType`, `PhotoRecordSnapshot`, `PhotoActionOption`, `PhotoComparisonPreview` |
| `db.py` | schema **v8**: `inat_photo_attach`, `source_photo_id`, `planned_observation_photo_uuid`, `reviewed_byte_fingerprint`, `sync_photo_transfers` ledger, `journal_photo_actions`, `transferred_source_photo_ids`, `transferred_photo_digests`, `finish_photo_transfer` |
| `photos.py` | `PhotoSyncService` |

### 16.2 Safety properties, each verified offline

- **Specimen identity.** Preview and execution both run the shared
  `evaluate_specimen_state`, with **no tolerated field** (unlike Gate 1D, which
  tolerates the coordinate it is reconciling). Any conflict blocks the transfer.
- **Both owners proven fresh.** The iNaturalist observation owner, the Mushroom
  Observer observation owner, **and** the individual image owner must all match
  the profile. Ownership **fails closed**: an image whose owner is missing or
  unparsable is not transferable.
- **The token is proven to be the profile's account** via the current-user
  endpoint during preview, in addition to the destination-owner check.
- **Content is pinned to what was reviewed.** Preview downloads each candidate,
  fingerprints it (byte digest + pixel digest), and journals the byte digest;
  the write aborts if the source no longer hashes to it. Recovery paths never
  blank a stored digest — `finish_photo_transfer` preserves any field it is not
  explicitly given, so duplicate evidence survives an unknown-outcome recovery. The source fingerprint also covers license, copyright
  holder, owner and URL, so a changed attribute invalidates the preview.
- **All three duplicate signals** from §6 run *before* upload, and again in a
  final pre-write check: (1) the local ledger by source id; (2) content digests
  of prior transfers **to the same destination observation** — a profile-wide
  match is advisory only, since one image may legitimately belong to two
  confirmed pairs; (3) **destination enumeration**, which compares **every** photo
  already on the observation against the source — there is no cap, because an
  unchecked destination photo may already *be* the image about to be uploaded.
  Signal 3 cannot use a byte digest, since iNaturalist re-encodes everything it
  stores, so it uses a normalized pixel fingerprint (dHash) — verified to
  survive JPEG re-encoding, rescaling and **EXIF orientation** while still
  rejecting a different image. dHash is a **best-effort** re-encoding-resistant
  signal, not proof: it covers the ordinary iNaturalist rescaling path well, but
  a crop, rotation or substantial edit can evade it, as it can evade any simple
  perceptual hash. That is why it is one of three signals and never the only
  one, and why, per §10, similarity is only ever allowed to **block** a
  transfer, never to trigger one.
- **The destination scan is fail-closed.** If any existing photo cannot be
  compared — no size-token URL (e.g. a legacy Flickr-hosted photo), a failed
  download, or an undecodable rendition — the scan is incomplete and **no
  transfer is offered or submitted at all**. An unverifiable destination is
  treated as a possible duplicate, not as a warning to click past.
- **The duplicate scan's own network time is closed off.** Fingerprinting the
  destination costs one download per photo, which is itself a window in which
  state can change. After the scan, the service re-reads once more and requires
  the pair, specimen evidence, and *both* record fingerprints — including the
  destination photo-set fingerprint whose photos were just examined — to be
  exactly what was checked. A photo attached mid-scan aborts the transfer.
  The destination photo-set fingerprint covers each `observation_photo` uuid
  **plus the photo's own identity** — photo id, license, attribution and file
  URL (whose cache-busting query is iNaturalist's version marker; the v2 `Photo`
  schema exposes no `updated_at`) — so content changing behind a stable
  attachment uuid also invalidates the scan, not just attachment and removal.
- **Copyright holder must match**, compared exactly against the profile's
  Mushroom Observer login. Authorship is evaluated **fail-closed** in three
  steps, each of which disables the transfer on its own: the profile has no
  verified Mushroom Observer login to compare against; the image records no
  copyright holder; or the holder is not the transferring account. Write
  authorization never rests on the assumption that the UI supplied a login. A
  source credited to someone else is **disabled**, not merely warned about:
  uploading it would attribute the photo to this iNaturalist account and drop
  that credit, which the source license may not permit. The exact-match rule is
  deliberately strict and will refuse a holder
  recorded as a real name rather than a login; relaxing that requires
  explicitly configured, verified holder aliases, not a looser string match. A
  future permission/attribution workflow could relax it; a generic confirmation
  warning is not sufficient.
- **No race between review and write.** Downloading a full-size image takes real
  time, so after the download the service re-checks cancellation, re-reads the
  pair and both records, re-runs specimen validation and the fingerprint
  comparisons, re-runs every duplicate signal against the fresh destination, and
  re-proves the credential — all *before* `write_started_at` is set.
- **Credentials are pinned across the read.** The auth generation and token
  marker **and** the Mushroom Observer key generation and key marker are
  captured *before* the remote reads and required to be unchanged after, so an
  A→B→A transition cannot end with the original marker and the newest
  generation. The same four-part context is re-proven at the final write
  boundary, immediately before the upload: the write is iNaturalist-only, but
  the images, licensing and ownership under review were read under a Mushroom
  Observer credential, so a MO credential transition between the last source
  read and the upload aborts rather than being waved through. Only one-way
  markers of either credential are ever held, compared or journalled.
- **Unsupported licenses are refused.** All-rights-reserved or unrecognised
  disables the action: the destination account's default license is not
  permission to republish. Version-shifted CC licenses stay available with
  explicit disclosure (decision §13.4).
- **Ambiguity never becomes a retryable failure.** `failed` is reached *only*
  when iNaturalist positively rejected the request (a client-error status with a
  response in hand). Lost transport, 5xx, and "not visible yet" all stay
  `outcome_unknown`, because a retried upload is a duplicate that cannot be
  deleted. Verification polls a bounded window before any verdict.
- **Recovery does not depend on current eligibility.** `verify_unknown` builds
  its pair view from immutable journaled IDs, so a pair reopened or excluded
  after an ambiguous upload can still be resolved.
- **A definitively failed transfer is retryable.** The ledger's UNIQUE key is
  one durable transfer identity; a `failed` row is reset and reused rather than
  blocking a second attempt.

### 16.3 Migration validation

The v8 migration rebuilds `sync_actions` (SQLite cannot ALTER a CHECK). A seeded
v7→v8 smoke migration covering **all 13 prior action types × all 6 states**, with
supersession references, unknown-outcome flags, `write_started_at` and HTTP
statuses populated, was run: 78/78 rows preserved with every column intact, new
columns defaulted empty, all three indexes rebuilt (including the partial unique
index), `PRAGMA integrity_check` = ok, `PRAGMA foreign_key_check` = 0 violations.

### 16.4 Not built

- Visual UI validation of the new thumbnail preview dialog, and a controlled
  live acceptance run of an upload through the wired UI — both required before
  this ships as an enabled feature (no display was available while wiring it).
- The iNat→MO direction, though §12 proved it viable.
- The copyright-holder attribution note. MO's `copyright_holder` is captured in
  the preview, the option (`holder_differs`) and the ledger, but writing it into
  the iNaturalist description is a **second, non-atomic write** to a user-visible
  free-text field, so it must be its own explicitly reviewed step rather than a
  side effect of the attach.
