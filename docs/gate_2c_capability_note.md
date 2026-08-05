# Gate 2C capability note — verified remote deletion

Date: 2026-07-24

## Closure and live-write boundary

Phase 2B is not formally closed in this repository. Therefore live Phase 2C
acceptance and every production deletion remain blocked. The production
`DeletionService` is constructed with that boundary explicitly false. No
credential, delete request, or live mutation was used for this audit.

Phase 2C is a lossless-deletion workflow only. It has no “accept data loss”
override, no bulk mode outside one consolidation, no automatic donor choice,
no canonical-replacement/merge feature, and no rollback recreation.

## Capability matrix

| Capability | Mushroom Observer | iNaturalist |
| --- | --- | --- |
| Owned-observation delete | `NEEDS-LIVE-PROOF` | `VERIFIED` |
| Stable remote identity | `UNSUPPORTED` | `VERIFIED` |
| Complete donor content read | `AMBIGUOUS` | `AMBIGUOUS` |
| Third-party contribution enumeration | `AMBIGUOUS` | `AMBIGUOUS` |
| Definitive post-delete verification | `UNSUPPORTED` | `NEEDS-LIVE-PROOF` |
| Unknown-outcome recovery | `UNSUPPORTED` | `NEEDS-LIVE-PROOF` |
| Safe for Phase 2C | `UNSUPPORTED` | `NEEDS-LIVE-PROOF` |

No production deletion direction is enabled.

## Mushroom Observer audit

1. The project’s API2 documentation describes
   `DELETE https://mushroomobserver.org/api2/observations` with the same
   filters as GET. An exact operation would use `id=<reviewed id>`.
2. DELETE requires the owning user’s API key. The API documentation says the
   server destroys matching records the key holder has permission to destroy.
   Exact observation ownership/delete rejection still needs controlled live
   proof.
3. The documentation and current client do not establish complete cascade
   semantics for images, collection/herbarium rows, namings/votes, comments,
   sequences, external links, occurrence membership, or newer object types.
4. API2 may return HTTP 200 while reporting fatal errors in a JSON body. The
   current client correctly treats fatal bodies as failures and treats
   transport/invalid-body failures after a write as ambiguous.
5. The response shape, synchronous completion, and stable success signal for
   observation deletion are not live-proven.
6. A GET after deletion can return an empty result, but there is no proven
   authenticated deleted-object feed or tombstone binding absence to the exact
   reviewed identity.
7. Empty results, forbidden results, redirects, and nonexistent records have
   not been proven distinguishable for this purpose.
8. Numeric observation-ID reuse is undocumented. MO exposes no stable
   observation UUID in the reviewed record model.
9. Remote recovery is undocumented.
10. An ambiguous DELETE cannot be recovered identity-safely without a blind
    resend, so unknown-outcome recovery is unsupported.
11. Complete enumeration of third-party activity and reverse external
    references is not proven.
12. The API is intended for individual users/applications, is rate limited,
    and is not for scraping. A small, explicitly reviewed user-owned deletion
    is not documented as prohibited, but this does not cure the missing safety
    capabilities.

Result: MO deletion is disabled.

## iNaturalist audit

1. The bundled OpenAPI 2.2.0 document specifies authenticated
   `DELETE /v2/observations/{uuid}`.
2. JWT authentication is required. The API reference says DELETE ownership
   checks apply; the authenticated user must own the observation.
3. iNaturalist’s deletion help states that deletion is permanent and also
   deletes identifications, comments, annotations, and other community
   contributions. This is why any detected third-party activity blocks.
4. The OpenAPI response is HTTP 200 with no response body. A response alone is
   not the Phase 2C success criterion.
5. The endpoint is described as synchronous, but exact post-return visibility
   and deleted-feed timing still require controlled live proof.
6. `GET /v2/observations/deleted?since=<date>` returns IDs deleted by the
   authenticated user. The bundled schema does not prove whether an exact UUID
   can be recovered from that feed, feed timing, retention, or how it interacts
   with a simultaneous direct authenticated UUID read.
7. Public 404, search omission, unauthenticated absence, forbidden/private
   reads, and timeouts are never accepted as proof.
8. The record UUID is the stable identity; a numeric-ID hit with a different
   UUID must not be treated as the reviewed object.
9. Deleted observations are documented as unrecoverable.
10. An unknown outcome appears potentially recoverable by combining the
    authenticated UUID read and authenticated deleted-ID feed, but this is
    `NEEDS-LIVE-PROOF`. The verifier must never resend.
11. The current deep and activity field sets enumerate comments,
    identifications, favorites, and quality metrics, but do not prove complete
    enumeration of annotations, every annotation vote, project curation,
    subscriptions, sounds, flags, or reverse external references.
12. The recommended API limits are about one request/second and 10,000/day.
    Phase 2C is one reviewed consolidation and sequential deletes, not a bulk
    operation.

Result: iNaturalist deletion is disabled pending complete-enumeration and
controlled post-delete/unknown-recovery proof.

## Lossless parity policy

Every donor content item is inventory-listed and receives a durable typed
parity result.

- Photos require the same stable source-media identity or a full
  original-byte fingerprint. Thumbnail, pixel, or perceptual similarity is
  insufficient. License, copyright holder, attribution, and source identity
  must also match.
- Description, notes, and other user text require exact whitespace-normalized
  equality in the same appropriate field. Substrings and silent concatenation
  do not count.
- Voucher, collection, accession, and configured observation-field values
  preserve their types and field identities.
- Date, coordinates, positional accuracy, locality, geoprivacy, and taxon are
  exact typed comparisons. Any difference blocks.
- Sequence rows require the same typed accession identity and exact sequence
  fingerprint. An unproven sequence enumeration blocks.
- Licenses, copyright/attribution, external URLs, project associations, sounds,
  and any unknown remotely stored item are typed content and must match.
- Owner-authored comments, identifications, annotations, votes, and other
  interactions are user-owned content and require exact typed parity.
- Parity consumes canonical items one-to-one; two donor objects cannot both
  claim one canonical object as preservation proof.
- Missing authorship and malformed collection/object rows fail closed as an
  incomplete inventory and are never silently skipped.

Phase 2C performs no content-transfer writes.

## Third-party contributions

Identifications, comments, votes, favorites, annotations, project curation,
and any other externally authored contribution block deletion. The preview
shows a safe summary and contributor account ID. Comment bodies are not stored
in the deletion ledger. Phase 2C never copies or recreates another user’s work.
Incomplete enumeration is itself a blocker.

## External dependencies

Known reciprocal links, configured observation-field links, local pair rows,
sequence links, cached/local identities, and consolidation identities are
reviewed as dependencies. A dependency blocks unless it is explicitly proven
to point to the canonical record. Reverse-reference search completeness is
shown separately; an incomplete search blocks the production workflow.

## Immutable review and confirmation

Schema version 16 introduced the Phase 2C ledger. Schema version 17 hardens it
with consolidation-level serialization, cross-phase database guards, and exact
tombstone-provenance triggers. The ledger includes:

- `sync_deletion_attempts`;
- `sync_deletion_items`;
- `sync_deletion_parity_items`;
- a deletion-only `sync_deletion_actions` journal;
- stable-member remote tombstone columns.

Deletion review fields and parity rows are protected by immutable triggers.
The selected donor set, canonical stable/mutable fingerprints, all parity and
activity/dependency fingerprints, and account-generation values are pinned.
A partial unique index permits only one unresolved (`pending`, `partial`, or
`outcome_unknown`) deletion attempt per profile/consolidation. Database
triggers mutually exclude unresolved Phase 2B and Phase 2C work and prevent
the finalized baseline from advancing during deletion. A fresh retry review
atomically supersedes a settled retryable partial attempt before replacement.

Nothing starts selected. Blocked checkboxes are disabled, and result collection
independently filters for enabled eligible rows. There is no “select all.”
Confirmation has two stages:

1. a permanent-deletion summary whose default is No;
2. an exact typed phrase (`DELETE INAT 123`, `DELETE MO 123`, or
   `DELETE N DONORS`).

Selection, canonical, content, parity, dependency, activity, or authentication
drift invalidates the plan.

## Journal, execution, unknown outcomes, and concurrency

The service contract is `prepare_preview`, `execute_group`, and
`verify_unknown`.

For each donor, the service freshly reconstructs readiness, compares every
reviewed fingerprint, revalidates owner/noncanonical/superseded state, then
journals exactly one site-specific action. The journal transaction commits
before `write_started_at` and before dispatch. Actions are claimed with a
conditional state transition so concurrent workers cannot send the same action
twice. The next donor is not journaled until the current donor is verified and
finalized.

Any exception after `write_started_at`, including cancellation or verifier
failure, becomes `outcome_unknown`; the tail stops. A pre-write refresh
exception atomically returns the claim to a safe pre-write state. Resume
normalizes a discovered `running` row from its durable write marker without
requiring restart, then routes unknown work only to `verify_unknown`. A
verified-still-present result becomes
`retry_required` and requires a new explicit review. Ambiguity remains unknown.
No automatic retry occurs.

Cancellation before `write_started_at` sends nothing. Cancellation after the
write boundary is ambiguous and routes to verification.

## Partial completion and tombstones

If one donor succeeds and a later donor fails, the first remains deleted and
tombstoned, the later donor remains online, untouched tail items are explicitly
cancelled, and the attempt is `partial`. Success followed by cancellation or a
required fresh retry is also `partial`. There is no rollback and no recreation.

Verified finalization atomically checks action, item, attempt, profile,
consolidation, finalized baseline, stable donor, remote ID/UUID, donor role,
superseded state, noncanonical status, exact site-specific action type,
write-start marker, verified-deleted state, and recomputed stable identity.
Database triggers bind the deleting attempt/item provenance to the exact stable
member and make action/tombstone identities immutable. Exact affected-row
counts are required. It then records:

- remote state `deleted`;
- deletion timestamp;
- deleting attempt and item;
- last reviewed remote fingerprint;
- canonical MO/iNaturalist destination IDs.

The donor remains a stable consolidation member. Phase 2B attempts, evidence,
actions, provenance, UUID, former ID, and historical URL remain visible. The
UI provides no Undo button.

## Proof status and remaining disabled features

Offline implementation and proof may precede Phase 2B live closure, but it
cannot authorize a live delete. The production boundary currently disables
both sites. Controlled live acceptance must use newly created disposable
owned observations and must separately prove exact deletion, authenticated
absence, deleted-feed behavior, cascade/content inventory, and lost-response
recovery. No valuable observation may be used.

Canonical replacement, consolidation merging, general bulk deletion,
data-loss override, automatic donor selection, rollback recreation, and live
deletion remain disabled.
