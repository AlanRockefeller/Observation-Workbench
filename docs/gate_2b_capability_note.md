# Gate 2B Capability Note — Duplicate Observation Consolidation

Status: **OFFLINE-PROVEN for link-only canonical consolidation and local
supersession. NEEDS-LIVE-PROOF before live acceptance.**

Phase 2B is deliberately non-destructive. It never deletes, hides, withdraws,
or edits a donor observation. Optional donor-data transfers remain disabled
because none satisfies all of Gate 2B's source-pinning, duplicate-detection,
attribution, and unknown-outcome requirements for the donor-to-existing-
canonical shape.

## Capability audit

The audit covered the reconciliation database, link, photo, ITS, observation
creation, specimen-state, matching, coordinator, types, and UI layers; the
iNaturalist and Mushroom Observer clients; the Gate 1E and Gate 2A notes and
manual proof harnesses; `inat.api.txt`; `api-docs.json`; and the local
Mushroom Observer API references.

An existing low-level method was not treated as proof. Gate 1E photo transfer
and Gate 1C ITS transfer are scoped to the two members of an existing confirmed
cross-site pair. Gate 2A creates a new destination. Neither contract safely
means “copy from an arbitrary donor to a different, already-existing
canonical observation.”

### Transfer-capability matrix

| Donor → canonical | Photos | Voucher/collection identifiers | ITS/sequences | Description/notes | Coordinates/date/taxon | Reciprocal links |
|---|---|---|---|---|---|---|
| iNat → iNat | `needs_capability_proof` | `unsupported` | `needs_capability_proof` | `needs_capability_proof` | `unsafe_without_removal` | `not_applicable` |
| MO → MO | `needs_capability_proof` | `unsupported` | `needs_capability_proof` | `needs_capability_proof` | `unsafe_without_removal` | `not_applicable` |
| MO → iNat | `needs_capability_proof` | `unsupported` | `needs_capability_proof` | `needs_capability_proof` | `unsafe_without_removal` | `verified_existing` |
| iNat → MO | `unsupported` | `unsupported` | `needs_capability_proof` | `needs_capability_proof` | `unsafe_without_removal` | `verified_existing` |

Release interpretation:

- **VERIFIED / OFFLINE-PROVEN:** reciprocal-link addition and verification for
  the selected canonical MO/iNaturalist pair.
- **UNSUPPORTED in this release:** every optional transfer row. These appear
  in the preview and immutable ledger as disabled gaps.
- **NEEDS-LIVE-PROOF:** the enabled link-only saga has not performed a live
  remote write during this implementation.
- **DEFERRED-TO-2C:** donor deletion, hiding, withdrawal, donor-link removal,
  and any other donor cleanup.

Same-site reciprocal links are `not_applicable`: the authoritative link model
is specifically MO ↔ iNaturalist.

## Enabled remote writes

Only additive reciprocal-link writes are enabled.

### Mushroom Observer canonical link

- Endpoint: `POST external_links` through the Mushroom Observer API2 client.
- Payload: `api_key`, canonical MO `observation`, the uniquely resolved
  iNaturalist `external_site`, and the public URL of the canonical iNaturalist
  observation.
- Stable destination identity: the API returns a numeric external-link row ID.
- Caller-generated UUID: not accepted.
- Duplicate detection: freshly enumerate the canonical MO observation's
  authoritative external links and normalize the target iNaturalist ID. An
  already-correct row is verified without writing.
- Unknown recovery: reread the authoritative external-link resource and prove
  the desired target. Never resend an ambiguous `POST`.
- Pinning: the complete reviewed canonical/donor record fingerprints and exact
  reviewed link rows are journaled before execution. A stable preflight
  fingerprint excludes only the expected link addition and remote update time.
- Semantics: additive. Existing canonical and donor link rows are retained.
- Donor effect: none.
- Attribution/licensing: not applicable to a URL link.

### iNaturalist canonical link

- Endpoint: `POST /observation_field_values`.
- Payload:

  ```json
  {
    "observation_field_value": {
      "observation_id": "<canonical observation UUID>",
      "observation_field_id": "<verified Mushroom Observer URL field ID>",
      "value": "https://mushroomobserver.org/obs/<canonical MO ID>"
    }
  }
  ```

- Stable destination identity: the response supplies the field-value UUID.
- Caller-generated UUID: not accepted for this resource.
- Duplicate detection: freshly enumerate the verified field's values and
  normalize the target MO ID. An already-correct row is verified without
  writing.
- Unknown recovery: reread the canonical iNaturalist observation and prove the
  exact field/target final state. Never resend an ambiguous `POST`.
- Pinning: the same full-member, stable-member, and exact-link-row pinning used
  for the MO action.
- Semantics: additive. No existing observation-field value is removed or
  repaired by Phase 2B.
- Donor effect: none.
- Attribution/licensing: not applicable to a URL field.

Neither endpoint accepts a useful caller-generated idempotency UUID, so safe
recovery depends on journal-before-request, exact destination-state pinning,
duplicate detection, and read-only final-state verification.

## Disabled transfer types

### Photos — UNSUPPORTED in Phase 2B

Gate 1E safely performs MO → iNaturalist photo transfer for one confirmed pair,
including full-byte pinning and duplicate detection. That does not prove an
arbitrary donor → existing canonical destination. Same-site directions have no
proven duplicate prevention, and iNaturalist → MO has no supported executor.
No photo action is minted by consolidation. Consequently no Phase 2B photo can
become `outcome_unknown`; the manual harness asserts that the disabled item
cannot create a photo action.

### Voucher and collection identifiers — UNSUPPORTED

`inat_ofv_add` remains narrowly implemented for reciprocal-link fields. It is
not overloaded for arbitrary identifiers. There is no proven update-existing-
MO-observation path for collection data.

### ITS/sequences — UNSUPPORTED in Phase 2B

The bidirectional ITS service is safe inside an existing confirmed pair, but
its pair identity and destination duplicate snapshot do not cover an arbitrary
donor/canonical plan. `MOClient.sequences()` still has unresolved pagination
and association questions, so Phase 2B does not call it at preview, execution,
or resume time. Sequence/accession values are not specimen-identity evidence
in this workflow; the UI says “Not inspected in Phase 2B.” A set that would
depend on sequence equivalence is blocked. No consolidation sequence action is
minted.

### Description/notes — UNSUPPORTED

There is no proven additive merge mode or update-existing-observation executor.
Phase 2B never concatenates or replaces these fields.

### Coordinates/date/taxon — UNSAFE WITHOUT REPLACEMENT

These are canonical record replacements, not additive transfers. Differences
are displayed as conflicts and remain untransferred. Phase 2B never chooses a
value automatically.

## Supported consolidation shapes

The service supports:

- one MO donor, one MO canonical, and one iNaturalist counterpart;
- one iNaturalist donor, one iNaturalist canonical, and one MO counterpart;
- donors on both sites with an explicit canonical on both sites;
- MO-only duplicate sets;
- iNaturalist-only duplicate sets.

Every participating site requires an explicit canonical choice, even when that
site contributes only one observation. Nothing is preselected for a new stable
identity.

A finalized consolidation is a stable specimen identity, not a one-shot
event. Selecting one of its members with newly discovered observations enters
extension mode. The existing canonical choices are fixed, prior donors are
shown read-only, and only new donors participate in the new attempt. Selecting
members of two different finalized consolidations is blocked; Phase 2B never
automatically merges stable identities.

Extension identity checks use the canonical attempt-member identity
fingerprints from `current_finalized_attempt_id`, never the original stable-
member content fingerprint. Stable identity covers remote site/observation
ID, remote UUID, numeric owner/account ID, and finalized strong canonical
anchors. A login is mutable display identity: it is shown and pinned for the
active attempt but a later attempt may review a renamed login when the numeric
owner ID is unchanged. Mutable taxon, date, description, identifiers, photo
metadata, locality/coordinates, and geoprivacy are tracked separately by safe
component fingerprints.

Mutable canonical changes are listed in the extension preview and do not
permanently block extension. The complete current snapshot is pinned in the
new attempt and must remain exact before every write and finalization.
Identity changes still block. Each successful initial/retry/extension
finalization advances the baseline transactionally. Failed and cancelled
attempts do not; an outcome-unknown attempt advances only after read-only
resolution and successful finalization. Historical attempts remain immutable.

A proposed donor is attempt-scoped until successful finalization. A
definitively failed or cancelled attempt leaves the donor active, visible in
ordinary workflows, and free to participate in a later attempt with the same
or a different donor set (or a different stable consolidation). The immutable
failed/cancelled attempt still shows “Proposed but not admitted” in history.
Pending and outcome-unknown proposals remain reserved and block a new attempt
until safely resumed or resolved; ambiguity is never treated as release.

After a failed/cancelled initial attempt, different canonical choices are
allowed only when one transaction proves that no action in any attempt for the
draft identity has a `write_started_at`, succeeded, or became outcome-unknown.
That transaction marks the old identity cancelled, releases its stable
canonical memberships, retains all attempt snapshots/history, and creates the
replacement identity. Once any write may have started, canonical choices stay
locked; Phase 2B never removes the possible remote link.

Cross-site shapes add or verify the two canonical reciprocal links, freshly
verify the canonical specimen, and confirm the local canonical pair. Same-site-
only shapes perform no remote writes and finalize only the reviewed local
canonical/superseded decision.

## Eligibility and conflicts

A preview begins only from IDs explicitly entered or confirmed by the user.
Every record is freshly read and must:

- belong to the profile and be owned by its exact remote account;
- be available, in scope, and fungal;
- expose required specimen-identity evidence;
- either have no stable membership or be a fixed canonical context member of
  the single finalized consolidation explicitly being extended;
- have no excluded correspondence or external confirmed one-to-one conflict.

At least one site must contribute two records. Eligibility is an explicit
undirected evidence graph with one node per `(site, observation_id)`. Evidence
is deliberately split into identity anchors and supporting facts.

Strong identity evidence:

- an authoritative MO or iNaturalist cross-site link field targeting the other
  reviewed member (`strong`);
- exact normalized voucher-to-voucher identity (`exact_voucher`, `strong`);
- exact normalized collection-number-to-collection-number identity
  (`exact_collection_number`, `strong`);
- exact source-qualified native-media identity (`strong`);

Supporting evidence:

- an exact observation date together with two explicit comparable coordinate
  points no more than 100 m apart (`exact_date_close_coordinates`,
  `corroborating`).

The irreversible validator uses a closed type-to-strength registry. Only
`authoritative_link_mo_to_inat`, `authoritative_link_inat_to_mo`,
`exact_voucher`, `exact_collection_number`, and `native_media_identity` may be
`strong`; only `exact_date_close_coordinates` may be `corroborating`. Unknown
types and known types carrying the wrong strength are rejected before
journaling, writing, or finalization.

Coordinate/date support is created only when both dates and points exist; all
latitude, longitude, accuracy, and distance values are finite; both accuracy
values are explicit, positive, and no greater than 100 m; the distance is no
greater than 100 m; and coordinate privacy/source semantics permit comparison.
An authenticated owned private point may be compared in memory. An unknown,
zero, negative, malformed, non-finite, or overly broad accuracy, an unreadable
private point, or a named-location centroid creates no edge. This is
“unavailable or insufficient evidence,” not a coordinate conflict.

The coordinate proof fingerprint includes both dates, both points, both
accuracy values, computed distance, evidence type/strength, threshold, and
policy version. Any accuracy change invalidates the reviewed edge.

Taxon similarity, date alone, general locality similarity, visual photo
similarity, absence of a conflict, accession text, and sequence equivalence
never create an edge. They may be displayed, but they are not positive
Phase 2B identity proof.

The complete graph must be one connected component and contain strong identity
evidence. A corroborating-only graph always fails. After selection, the
canonical MO/iNaturalist records (when both participate) must have a direct
`strong` edge. Every donor must reach a canonical record through a path whose
donor-to-canonical portion contains at least one strong edge. Reaching a
canonical using support alone and then traversing the strong canonical-pair
edge does not prove the donor. Corroborating edges may extend a donor path only
when that path has its own strong anchor. The preview labels identity versus
supporting edges, shows every hop, and names each donor path's required strong
anchor.

Voucher and collection-number sets are never combined. A voucher on one
record cannot match a collection number on another. Taxon, date alone,
locality alone, absence of conflict, and corroboration are never promoted to
strong evidence. Disconnected strong clusters, unsupported extra members,
weak canonical bridges, and donors without their own strong-anchored route all
fail closed. Pairwise specimen-state conflicts still fail closed.

Different dates, coordinates, taxa, descriptions, and identifiers are shown.
They are never merged or overwritten. Explicit canonical choice selects the
record to retain; it does not authorize copying conflicting values.

## Durable data model and migration

Schema version 11 introduced:

- `sync_consolidations`: stable identity and chosen canonical IDs;
- `sync_consolidation_members`: canonical/donor roles, remote UUID where
  available, reviewed and stable preflight fingerprints, and local
  active/canonical/superseded state;
- `sync_consolidation_attempts`: one immutable approved plan, its action group,
  canonical/donor/pair fingerprints, account identities, disabled-gap
  decisions, and explicit supersession chain;
- `sync_consolidation_items`: one reviewed optional item per donor/destination;
  all current rows are durably `disabled`;
- `consolidation_finalize`: a local-only journal action permitted by the shared
  one-action/one-write journal.

Schema version 12 added the original extension/evidence provenance without rewriting
consolidation or action parents:

- `added_by_attempt_id`, `superseded_by_attempt_id`, and `superseded_at` on
  stable member rows;
- `sync_consolidation_attempt_members`, which records the canonical context and
  new donors plus their immutable attempt-specific fingerprints;
- `sync_consolidation_evidence`, with normalized member ordering, evidence type
  and strength, a one-way fingerprint of the normalized proof, a safe display
  summary, and composite foreign keys proving that both endpoints participated
  in the same attempt;
- an attempt-level evidence-graph fingerprint, checked against the edge rows
  before every possible write and again inside transactional finalization;
- canonical uniqueness indexes that continue to reserve finalized canonical
  observations for their stable identity;
- a lossless rebuild of `sync_action_snapshot_rows` to repair a stale temporary
  parent-table FK name left by the older action-group migration.

Schema version 13 corrects admission and baseline semantics:

- `sync_consolidations.current_finalized_attempt_id` points to the latest
  successfully finalized attempt;
- `sync_consolidation_attempts.base_finalized_attempt_id` pins the baseline an
  extension reviewed, providing a compare-and-set guard against conflicting
  finalizations;
- attempt members have their own immutable identity, site/observation, remote
  UUID, reviewed full/preflight fingerprints, reviewed account snapshot,
  participation role, and proposal state;
- evidence endpoints reference two attempt-member IDs from the same attempt,
  so proposed donor evidence never requires stable membership;
- only canonical identity rows exist stably before initial success; proposed
  donors consume no global stable-membership uniqueness;
- successful finalization inserts all proposed donors as stable superseded
  members or none of them;
- pending/outcome-unknown proposals are reserved through an unresolved-
  proposal view, while failed/cancelled proposals are released.

The v12→v13 migration preserves every attempt/evidence snapshot before
classifying legacy active donors. Successfully finalized donors remain stable.
Failed/cancelled proposals are removed from stable membership after their
attempt-only history is preserved. Pending/outcome-unknown proposals are
removed from falsely admitted stable membership but remain globally reserved
by their unresolved attempt. A finalized donor must be traceable to a
succeeded attempt with a succeeded local finalization action; ambiguous legacy
classification aborts the migration instead of guessing. Original
proposal provenance is retained and version 14 copies it into
`originally_proposed_by_attempt_id`; `added_by_attempt_id` then identifies the
attempt that actually admitted a stable donor.

Schema version 14 adds irreversible-boundary and provenance hardening:

- one pure structured graph validator is used by canonical selection, durable
  journaling, every pre-write revalidation, and transactional finalization;
- journaling reconstructs donor paths from persisted evidence edges and never
  trusts rendered `strong_anchor_step` text;
- attempt members persist a narrow stable-identity fingerprint and safe
  mutable-component fingerprints;
- an extension also requires every strong canonical-pair anchor from the
  current finalized baseline to remain present; newly added strong anchors may
  reinforce the pair, but cannot silently replace a finalized anchor;
- `originally_proposed_by_attempt_id` preserves legacy proposal provenance;
- `admitted_from_attempt_member_id` links every stable donor directly to the
  exact immutable `new_donor` snapshot that admitted it;
- database triggers require admission attempt, profile, consolidation,
  site/observation, and role to match;
- attempt-member content remains immutable while its optional stable-member
  association may become `NULL` when a proven no-write initial plan is
  abandoned.

Schema version 15 adds finalized-baseline and ownership hardening without
amending version 14:

- stable identity uses the numeric remote owner ID; the login is stored
  separately as a reviewed display snapshot;
- baseline-pointer triggers require the current attempt to belong to the same
  profile/consolidation, be succeeded, and have a succeeded
  `consolidation_finalize` action; a new attempt base must equal the
  consolidation baseline at insertion and must precede the new attempt;
- finalization independently joins the baseline attempt, canonical stable
  members, numeric owners, remote UUIDs, and strong-anchor signatures before
  advancing the compare-and-set pointer;
- migration normalizes legacy `numeric-id:login` identities and fails rather
  than guessing when the numeric owner cannot be parsed;
- current and referenced baseline attempts and their successful finalization
  actions cannot later be rewritten into an invalid state.

Unknown remote IDs remain `NULL`; `0` is never used as an unknown identity.
Membership, composite foreign keys, attempt/group restrictions, conditional
action identity checks, and partial unique indexes retain existing invariants.
Deleting a local pair cannot erase consolidation attempts or action history.

The migration harness starts from a real v10 schema seeded with:

- a nonempty action journal and an `outcome_unknown` photo action;
- a photo-transfer ledger row;
- a Gate 2A creation identity, immutable creation attempt, creation item, and
  unknown create/photo rows.

It upgrades through v11 to v15, verifies those rows survive, and separately
checks populated v12 finalized, pending, outcome-unknown, failed, and cancelled
consolidation
states. It asserts member/attempt/action/evidence provenance and row counts,
exercises uniqueness failures and transactional rollback, and finishes with:

```sql
PRAGMA integrity_check;     -- ok
PRAGMA foreign_key_check;   -- no rows
```

## Immutable review and saga

Preview reads and fingerprints every member, including numeric owner/account
identity and the current display login, remote
identity, taxon/date, privacy-safe location state, notes hash, identifiers,
photo metadata/license/holder, remote update time, and exact
reciprocal-link rows. Long/sensitive values are represented by one-way hashes;
photo bytes are not stored. Sequence metadata is explicitly not read by this
workflow. Because photos are not selectable, no photo byte payload is approved
or journaled.

Every accepted evidence edge and its explicit strength are stored for the attempt.
Immediately before
each possible canonical-link write and again before local finalization, all
attempt members are freshly read, the current graph is rebuilt, and every
reviewed edge must still exist with the exact reviewed fingerprint. A changed
edge blocks even if another route would still connect the graph. The reviewed
graph itself must retain the direct strong canonical edge and a strong-
anchored path for every new donor. An edge that disappears, changes type, or
changes strength blocks.

The same pure validator runs before preview acceptance, again over
`preview.evidence_edges` at the journal boundary, before each possible remote
write, and from the persisted attempt/evidence rows inside finalization.
Display strings and UI path objects are never authority.

Journal creation writes the stable canonical identity, immutable attempt-
member snapshots (including proposed donors), evidence, reviewed link
snapshot, disabled item gaps, and an empty dynamically sized action group in
one transaction. It does not admit donors or mint all future actions.

Execution order is:

1. Create or locate the provisional local canonical pair.
2. Freshly reread every canonical and donor.
3. Mint and execute at most one MO canonical-link action.
4. Repeat the full preflight.
5. Mint and execute at most one iNaturalist canonical-link action.
6. Freshly reread the pair and donors and verify specimen identity and both
   canonical links.
7. Mint one `consolidation_finalize` local action.
8. In one database transaction, recheck action/attempt/group/member/pair,
   baseline and evidence provenance; verify every proposed donor is still
   unadmitted elsewhere; insert all new stable donors as superseded; reject
   internal donor pairs; confirm the canonical pair; succeed the finalization
   action and attempt; and advance `current_finalized_attempt_id`. Every
   required update has an exact row-count check and any failure rolls back the
   entire transaction.

One `sync_actions` row represents one atomic remote write. Actions are
journaled before their request. Later ordinals do not exist until the prior
ordinal succeeds.

An `outcome_unknown` attempt blocks later actions and new attempts. Resume
routes to the link service's read-only `verify_unknown`; it does not resend.
After restart, a recovered running action whose request might have started is
also classified unknown. Definitively failed/cancelled attempts may be
superseded by a new immutable attempt with a different proposed donor set.
They never become the finalized baseline. An outcome-unknown attempt becomes
the baseline only after read-only resolution and successful finalization.

For an extension, an `outcome_unknown` canonical-link repair blocks donor
supersession and any concurrent/new extension attempt. Resume uses the owning
link verifier and never resends automatically. If both canonical links are
already correct, the two journal rows settle as already satisfied and make
zero remote requests. One missing link produces exactly one request; neither
link produces two sequential requests. A canonical link targeting a different
record blocks and is never overwritten.

## Local supersession and history

Only successful canonical verification permits finalization. Donors become
locally `superseded` in the same transaction that confirms the canonical pair.
They are excluded from inventory-driven matching, candidate pairs, ordinary
unpaired queues, and issue rows. They remain in the dedicated Consolidation
history view with:

- “Superseded by MO … / iNat …” provenance;
- canonical and donor IDs;
- the attempt that admitted and superseded each donor;
- remote page URLs and an explicit “Open consolidation member…” action;
- every attempt, evidence edge, disabled item, and journal action.

History also identifies the original consolidation attempt, every later
successful extension, failed/cancelled attempts, the current finalized
baseline attempt, and the canonical fingerprints established by each
successful attempt. Failed/cancelled donors are shown only under their attempt
as “Proposed but not admitted”; only admitted donors receive “Superseded by …”.

“Superseded” never means deleted. Donors remain remotely intact and may retain
old links until separately reviewed Phase 2C work.

## Offline proof and live boundary

The disposable manual harnesses cover:

- all three requested cross-site duplicate shapes and both same-site-only
  shapes;
- preview cancellation and missing canonical selection;
- mutable canonical review, stable canonical identity drift, donor drift, and
  login rename/review, active-attempt login pinning, and a new pair conflict
  before write;
- first link success followed by second-link preflight failure;
- ambiguous link response, restart, verification, and no duplicate upload;
- restart between every dynamically minted ordinal;
- two concurrent resumes producing one write per canonical link;
- finalization invariant failure with complete transaction rollback;
- disabled optional items, no photo/sequence action minting, no delete action;
- MO previews and revalidation completing without `MOClient.sequences()`;
- connected, disconnected, unsupported-member, taxon/date/locality-only,
  multi-hop, canonical-dependent, disappearing-edge, and malformed-edge cases;
- stable-identity extensions with donors on either/both sites, zero-write
  canonical no-ops, one-link repair, unknown recovery, concurrent-attempt
  rejection, cross-consolidation conflicts, and prior-history preservation;
- donor remote snapshots unchanged;
- superseded filtering plus retained history and remote links;
- fabricated display-path rejection at the durable journal boundary;
- no-write initial canonical abandonment and post-write canonical locking;
- schema-enforced admitted-donor snapshot provenance;
- cross-consolidation/cross-profile baseline-pointer rejection and independent
  finalization revalidation after a deliberately corrupted disposable ledger;
- seeded v10→v15 and populated v12→v15 migration integrity, including release
  of failed proposals and unresolved-proposal reservation;
- explicit rejection of an unparseable legacy account identity;
- offscreen UI canonical defaults, disabled/failed photo presentation, account
  and ID display, prominent donor retention, and second confirmation default
  No.

No repository test suite was run. No live remote write was attempted. No remote
record was modified or deleted during this implementation.

Live acceptance remains **NEEDS-LIVE-PROOF** and requires explicit
authorization plus owned disposable duplicates. It must confirm each
canonical link is added once, donors are untouched, resume makes no duplicate
write, local supersession follows canonical verification only, the canonical
records contain exactly the reviewed additions, and a later new donor reuses
the stable identity with zero link writes when the links remain correct.

## Phase 2C boundary

Phase 2B still does not call any observation delete, hide, withdraw, link
removal, photo removal, field removal, sequence removal, identification
removal, or comment removal path. Phase 2C now has a separate capability
audit, preview, journal, unknown-recovery contract, and confirmation UI in
`docs/gate_2c_capability_note.md`; both production deletion directions remain
disabled and Phase 2B is not formally closed. The Phase 2C service consumes
the immutable Phase 2B baseline as evidence but never rewrites it.
