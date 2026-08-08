# Gate 2A Capability Note

## Round-7 review pass (follow-up review — 4 newly-found defects)

A follow-up code review found 4 new defects on top of the Round-6 pass, all
confirmed as real (not already-safe) after investigation. Proven by the
existing offline/offscreen harnesses, extended in place (no check removed or
weakened) rather than replaced: `tools/gate_2a_review_fixes_harness.py`
(41 checks, was 22), `tools/gate_2a_thumbnail_ui_harness.py` (13 offscreen Qt
checks, `QT_QPA_PLATFORM=offscreen`, was 9), and
`tools/gate_2a_duplicate_search_harness.py` (12 checks, unchanged) — 66/66
passing.

1. **A photo could be approved before its thumbnail finished loading
   (confirmed defect, release blocker, fixed)**. In
   `observation_workbench/ui/reconciliation.py`,
   `ObservationCreationPreviewDialog` created every photo checkbox with
   `box.setEnabled(item.enabled)` immediately at dialog construction —
   before the async `_PhotoThumbnailWorker` had returned. `setChecked()` is
   a programmatic call that succeeds even on a disabled `QCheckBox`
   (disabled only blocks *user* interaction), so a user who checked and
   confirmed quickly, or a script driving the dialog, could select an item
   whose pinned image the app never actually verified was displayable.
   Fixed: photo checkboxes now start **disabled**, with a visible
   `[loading image…]` reason appended to their label, until
   `_thumbnail_loaded()` proves all three of: a valid decoded `QImage`, a
   matching pinned byte fingerprint (when one was required), and a callback
   that resolves to a still-known key/item this dialog itself created (a new
   `self._item_by_key` map backs this — an unrecognized key is treated as
   "no update", never a green light). Only then does `_enable_item()`
   populate a new `self._visually_verified_photo_keys` set and re-enable the
   checkbox. `selected_items()` — the single method the dialog's caller
   actually consumes (`observation_workbench/ui/reconciliation.py`'s
   `_show_create_missing_dialog`-equivalent call site) — independently
   filters every photo item against that verified set, never trusting
   `box.isChecked()`/`isEnabled()` alone. This closes the race regardless of
   UI-thread timing. Every previously-passing failure-path check (download
   error, empty bytes, decode failure, fingerprint mismatch, stale-callback
   protection, safe close-while-workers-running, no off-thread `QPixmap`)
   remains intact and passing.
2. **`_fail_item_durably()` had a non-atomic check-then-act race that could
   downgrade an ambiguous write (confirmed defect, fixed)**. In
   `observation_workbench/reconciliation/observation_creation.py`, the loop checked an
   existing action's state, then separately called
   `mint_creation_item_action()` (which *silently returns* an
   already-existing action id rather than raising) followed by an
   unconditional `finish_action(..., "failed")` — two separate
   statements/transactions with a gap in which a concurrent worker could
   claim/start/complete that exact action, only to have it overwritten as
   `'failed'` regardless. Fixed: a new
   `ReconciliationDB.fail_creation_item_preflight()` runs the whole
   operation under one `BEGIN IMMEDIATE` transaction (the same
   `self.transaction()` pattern used throughout `db.py`, e.g.
   `claim_action`/`settle_pair_finalize_success`). If no action is linked
   yet, it mints one and fails it atomically in the same transaction. If one
   is already linked, it transitions it to `'failed'` **only** via a
   conditional `UPDATE ... WHERE state='pending' AND write_started_at IS
   NULL`, requiring exactly one affected row (mirrors `claim_action`'s
   `cursor.rowcount == 1` idiom). If that conditional update affects zero
   rows, nothing is downgraded — the method reads back and returns the
   ACTUAL current state (`{"action_id", "downgraded": False, "state": ...}`)
   instead, and `_fail_item_durably()` reports that real state to its
   caller, which already stops the tail identically either way (every call
   site does `results.append(_fail_item_durably(...)); return results`).
   *Judgment call*: reused the existing action types
   (`inat_photo_attach`/`inat_ofv_add`) rather than adding a dedicated
   `creation_item_preflight` action type — the new method's atomicity comes
   from the transaction and the conditional `UPDATE`, not from the action
   type, and every other part of the saga (resume, verify_unknown routing,
   display) already keys off action type in ways a new type would need to
   be taught about for no added safety.
3. **Cancellation during the full-image re-download could still be ignored
   (confirmed defect, fixed)**. In the same file, `cancelled` was checked
   before `_refresh()` (a prior fix) but not around the subsequent
   `image_bytes = self.photo_service._download(source)` call — a
   potentially lengthy download that, if cancelled mid-flight, could still
   run to completion, get fingerprinted, minted, and proceed toward upload.
   Fixed: `cancelled()` is now checked at four points — immediately before
   starting the download, immediately after `_download()` returns,
   immediately before minting the action, and immediately before invoking
   the photo service's own upload (`_execute_followup_row`) after mint. The
   first three are strictly pre-mint, so `_fail_item_durably()` can only
   ever mint-and-fail a fresh, never-attempted action there. The fourth is
   post-mint but pre-write (the action exists, still `'pending'`, no
   `write_started_at`) — it reuses the exact same
   `fail_creation_item_preflight()` atomic transaction from finding 2, whose
   conditional `WHERE state='pending' AND write_started_at IS NULL` guard
   means that if a write genuinely raced ahead of the cancellation check in
   between, nothing is downgraded: the item's real ambiguous/in-flight state
   is preserved and reported instead of a fabricated cancellation failure.
4. **Finalization didn't verify `sync_created_observations.pair_id` matches
   the supplied pair (confirmed defect, fixed)**. `settle_pair_finalize_success()` in `db.py` already verified the action's group/type/state,
   the action's own pair/mo/inat ids, creation-attempt ownership, and the
   stable-identity source/destination ids — but never selected or checked
   `sync_created_observations.pair_id` itself, a column that exists
   specifically to carry this direct provenance link (set once by
   `finish_observation_creation`). The existing id checks made an accidental
   mismatch unlikely on their own; this adds defense in depth. Fixed: the
   attempt-row query now also selects `co.pair_id AS creation_pair_id`, and
   the method raises `RuntimeError` (rolling back the whole transaction,
   including any pair-promotion write already made in it) if it is `NULL`
   or does not exactly equal the `pair_id` argument — per the reviewer's
   explicit instruction to fail via raise rather than the quieter
   "return `False`, nothing written yet" path used by the other pre-write
   checks in this same method (the id checks above it still return `False`
   uniformly, since they run strictly before any write; this one was
   specified as a raise).

**Residual risk**: none identified beyond what is already documented in the
Round-6 section below (this pass's fixes close specific gaps within that
existing model, they do not change its scope). The Round-6 section's
"controlled acceptance — simulated only, not live" caveat still applies
identically here: nothing in this pass performed or required a live remote
write.

---

## Round-6 review pass (z(17).diff review — 8 numbered concerns)

A sixth independent review pass ("z(17).diff") checked 8 specific safety
concerns against the actual working tree. 4 were confirmed defects and
fixed below; the rest were verified already handled safely and are noted as
such, not re-fixed. Proven by three new disposable, offline (no network,
no live writes) smoke harnesses: `tools/gate_2a_review_fixes_harness.py`
(22 checks), `tools/gate_2a_duplicate_search_harness.py` (12 checks),
`tools/gate_2a_thumbnail_ui_harness.py` (9 offscreen Qt checks, `QT_QPA_PLATFORM=offscreen`) — 43/43 passing.

1. **Duplicate discovery is now account-complete, not date/taxon-filtered
   (confirmed defect, fixed)**. `_live_search_inat_destination` previously
   ran ONLY the bounded date-window search when the source had an observed
   date, with no fallback — a destination counterpart with no date, a date
   more than 30 days off, no taxon, or a misidentification outside Fungi
   could be invisible. It now runs the efficient date-window search (when a
   source date exists) as a scoring-only optimization, THEN **always**
   (regardless of the window search's outcome or whether it ran at all)
   performs an authenticated, cursor-paginated, unfiltered full-account
   scan as an unconditional completeness backstop, before creation is ever
   allowed. Date and taxon remain scoring evidence only (`score_candidate`'s
   `EvidenceFamily` caps — LINK 65, SPECIMEN 50, BARCODE 50 vs TAXON 10 —
   are unchanged); neither can act as a hard inclusion filter. The MO
   reciprocal-link field binding is now MANDATORY: a DB read failure, a
   missing binding, or a binding that is not `verified` all block creation
   outright (`duplicate_search_unavailable`) instead of silently degrading
   to "no link evidence". The ITS/accession binding remains genuinely
   optional (documented below) — a read failure still fails closed; a
   simply-absent binding is safe because nothing else in the app requires
   it to exist for a profile to be usable.
   - *Not implemented*: a dedicated "search by exact field value" API call
     (an efficiency optimization named in the review as one option). It was
     not added because the API surface for it is not confirmed/documented
     in this app's condensed reference, and — critically — the unconditional
     full-account fallback scan already guarantees every candidate
     (including one that would only match by voucher/collection/reciprocal
     link/coordinates) is read and scored regardless. Omitting it is an
     efficiency-only gap, not a completeness gap.
2. **An existing ambiguous/succeeded photo action can no longer be
   rewritten as failed by a pair-drift preflight check (confirmed
   defect, fixed)**. `_execute_population_items`'s "pair could not be
   re-verified" loop picked the first item whose LOCAL `sync_creation_items.state` was not `succeeded`/`failed` and unconditionally routed it
   through `_fail_item_durably`, which called `finish_action(...,
   "failed")` on whatever action id was already linked — but that local
   state column stays `'pending'` while the item's actual linked action is
   `running` or `outcome_unknown` (it is only updated once the action
   reaches a terminal-ish outcome). A pair excluded/de-confirmed after an
   ambiguous upload could therefore have that upload silently downgraded to
   a definitive `failed`, in direct violation of the no-blind-retry rule.
   Fixed: the loop now resolves each item's OWN linked action's state
   first. `succeeded` → left untouched. `outcome_unknown`/`running` → fully
   preserved, tail stopped, reported back to the caller for an explicit
   resume/verify (never auto-failed). `failed`/`cancelled` → preserved as
   terminal history. Only a `pending` local state with NO linked action (or
   a `pending` action with no `write_started_at`) — cases where no remote
   write could possibly have started — is safe for the existing durable
   pre-mint failure path to close out. `mint_creation_photo_item_action`'s
   own existing-transfer-row block (round-3 finding) and `mint_creation_item_action`'s existing-action short-circuit were already correct; the
   defect was entirely in the CALLER not checking action state before
   invoking the failure helper.
   - Durable pre-mint failures continue to reuse `inat_photo_attach`/
     `inat_ofv_add` as a *local-only* row (never `write_started_at` set,
     never a `sync_photo_transfers` claim of a remote write) rather than a
     dedicated `creation_item_preflight` type. This was not changed in this
     pass — see the risk note in "Remaining risks" below.
3. **A photo whose pinned image cannot be displayed is now always disabled
   (confirmed defect, fixed)**. `ObservationCreationPreviewDialog.
   _thumbnail_loaded` disabled+unchecked an item on a byte-fingerprint
   mismatch, but a download failure, empty response, or decode failure
   (`image is None or image.isNull()`) only set placeholder label text —
   the checkbox stayed enabled and selectable. Fixed via a shared
   `_disable_item` helper invoked for ANY failure to actually display the
   pinned image (download exception, empty bytes, decode failure, null/
   invalid `QImage`, or a defensively-checked non-`QImage` callback
   payload), showing "Image could not be displayed and reviewed; this
   photo cannot be selected." The confirm button (`_confirm`) already reads
   live checkbox state via `selected_items()` at click time, so a
   just-disabled item is automatically excluded with no separate wiring
   needed. Worker-thread discipline (no `QPixmap` construction off-thread,
   full image bytes never retained beyond the worker call, `self._closed`
   checked before/after every callback, stale-callback safety) was already
   correct and unchanged.
4. **Pair finalization is now transactionally tied to the exact creation
   attempt, and a partial finalization can no longer be committed
   (confirmed defect, fixed)**. `settle_pair_finalize_success` already
   validated the action (exists/group/type/state/pair/mo/inat ids) and the
   pair (identity/provisional/excluded/conflict) — but validated nothing
   about `sync_creation_attempts` at all, and, critically, `return False`
   after the pair-promotion `UPDATE` had already executed inside the open
   `with self.transaction()` block would still COMMIT that promotion (a
   normal `return` — not an exception — makes the context manager commit).
   A finalize-action update racing to 0 affected rows after a successful
   pair promotion could therefore leave a confirmed pair with its
   `pair_finalize` action stuck non-succeeded: a genuine partial
   finalization silently committed. Fixed: now additionally requires
   exactly one `sync_creation_attempts` row owning the action group,
   belonging to the same profile, whose identity's source/destination ids
   agree with the mo/inat ids being finalized, and whose state is
   `'succeeded'` (the only state a successfully-written attempt reaches —
   this schema has no separate "finalized" attempt state). Once the pair
   promotion `UPDATE` has actually affected a row, every subsequent step
   either succeeds with exactly one affected row or **raises** (never
   returns `False`), so the whole transaction — including the pair
   promotion — rolls back rather than committing a partial state.
5. **Malformed duplicate-search results fail closed (confirmed defect,
   fixed — same code as #1)**. Previously a non-dict page/response or a
   non-dict candidate item was silently `continue`d/skipped as though the
   account were still fully searched. Now: a non-dict response, a missing
   or non-list `results` field, any non-dict candidate entry, a candidate
   with no usable numeric id (pagination-breaking), or a candidate that
   `INatReconciliationReader.parse_inventory` cannot parse safely, all
   raise `duplicate_search_unavailable` immediately. A genuinely valid page
   with zero observations is unaffected and still counts as a clean,
   complete search step.
6. **Cancellation is honored during population-item photo refresh
   (confirmed defect, fixed)**. `_execute_population_items` called
   `self.photo_service._refresh(profile, pair, lambda: False)` — ignoring
   the real `cancelled` callback passed into the method — for the
   pre-write source/destination photo re-read that runs strictly BEFORE
   that item's own mint/execute. Fixed to pass the real `cancelled`
   callback through; because this refresh runs before any write for that
   item, honoring cancellation here can only ever leave the item `pending`
   (durable, resumable) or raise `ReconciliationCancelled`, never touch an
   in-flight or ambiguous write. `_live_search_inat_destination`,
   `_photo_items`, and the reciprocal-link/finalize path already checked
   the real `cancelled` callback correctly and needed no change.
7. **The one-photo-at-a-time saga design is preserved (already handled
   safely — no change needed)**. `_execute_population_items` still resumes
   an existing action's OWN state (via `_execute_followup_row`, which
   dispatches `outcome_unknown` to the owning service's `verify_unknown`)
   before ever considering minting a new item, and still mints+executes
   exactly one item at a time, stopping the tail on anything but success.
   The section-2 fix above only tightens the pair-drift preflight branch;
   it does not change this per-item resume/mint/execute loop's shape.
8. **Controlled acceptance — simulated only, not live (see harness list
   below)**. No live write was performed or authorized this session. The
   lost-response/`outcome_unknown` recovery property (an accepted write
   whose response is discarded must never be resent, and the next item
   must not be minted until it is resolved) is proven here by
   `gate_2a_review_fixes_harness.py`'s ambiguous-preservation scenario
   against a real `ReconciliationDB` with a `outcome_unknown` action seeded
   directly (simulating exactly what a lost-response create/attach would
   leave behind) — not by driving a stubbed `INatClient`/`MOClient` boundary
   through an actual HTTP-shaped call. `tools/gate_2a_saga_harness.py`'s
   existing `--simulate-lost-response` flag (drives the real client
   boundary) requires live credentials and `--run --i-own-these-records`
   and was NOT run this session.

---

## Round-5 review pass (additional hardening on top of the sections below)

A fifth independent review pass found and fixed 4 numbered issues plus 2
smaller ones — the most significant being a genuine functional break, not
just a hardening gap. All are proven by the accumulated disposable smoke
suite (18 scripts, zero live writes — no live-write authorization was
available this session).

- **Two or more selected photos were guaranteed to fail (a real bug, not
  just a hardening gap)**: the previous design minted every selected photo
  action up front — all against the SAME pre-any-upload destination
  snapshot — and only then executed them one at a time. The moment the
  first photo actually uploaded, the destination's photo set changed, so
  every subsequent item's stale mint-time preview fingerprint was
  guaranteed to fail `PhotoSyncService._require_unchanged_context`'s
  freshness check — deterministically, every time, for any session with
  two or more selected photos. Fixed by minting AND executing each item
  ONE AT A TIME (`_execute_population_items`, replacing
  `_mint_item_followups`) — mirroring the reciprocal links' existing
  one-at-a-time discipline. Proven directly against the REAL
  `PhotoSyncService` (not a stub) with two genuinely distinct photos, both
  succeeding in sequence.
- **A dedicated, authenticated duplicate-search reader** replaces the
  reuse of `INatReconciliationReader.inventory_page` (Gate 1A's public
  inventory scanner), which silently made the "live" search from round 4
  unsuitable for its purpose in three ways: it always injected
  `taxon_id=47170`, excluding exactly the taxon-less/misidentified
  duplicates that most need catching; it called the deliberately
  unauthenticated public endpoint, missing hidden/private-coordinate
  observations; and it never resolved the ITS/accession field binding, so
  identifier/sequence evidence could never match despite the docstring
  claiming otherwise. `INatClient.get_creation_duplicate_search` is
  authenticated, never taxon-filtered, and searches a bounded date window
  around the source's observed date first (falling back to a capped full
  scan only when no date is available) — cheaper and more complete than
  the previous exhaustive scan.
- **A pre-mint preparation failure now stops the population tail and
  leaves a durable, real action row**: previously, a failed item (source
  unreadable, changed content, changed metadata) got only a display-only
  negative pseudo id and the loop continued to the NEXT item regardless —
  a later item could still be written after an earlier one failed
  preflight, and the failed item was invisible to the generic
  action-resume/verify UI (no real `sync_actions` row). Now the first
  preparation failure mints a real, immediately-failed action row (so the
  existing action UI can represent and eventually supersede it) and stops
  the tail outright — no later item is ever minted or written.
- **`settle_pair_finalize_success` validates its own complete contract**:
  beyond the pair, it now also requires the action to exist, belong to the
  expected action group, be a `pair_finalize` row, be currently `running`,
  and reference the exact same pair/mo/inat ids passed in — any mismatch
  refuses cleanly, exactly like a pair mismatch already did, and exactly
  one action row must be updated.
- **Smaller issues**: the Gate 2A creation dialog's thumbnail loader now
  hashes its own separately-downloaded bytes and compares them against
  `reviewed_byte_fingerprint`, disabling and unchecking the item if they
  differ — closing the gap between what `prepare_preview` pinned as
  reviewed and what the dialog actually shows the user; the live-search
  docstring's claims about included evidence now match what it actually
  resolves (ITS/accession binding included).

---

## Round-4 review pass (additional hardening on top of the sections below)

A fourth independent review pass found and fixed 8 numbered issues plus 4
smaller ones. All are proven by the accumulated disposable smoke suite (16
scripts, zero live writes — no live-write authorization was available this
session). Two of the round-4 findings (payload pinning, photo-byte pinning)
turned out to already be correctly fixed by round 3 and needed no further
code change; verified against the current code rather than assumed.

- **Duplicate search is now LIVE, not the local cache**: `_search_for_
  existing_match` (for the one reachable direction, MO->iNat) now pages
  through the destination account's ENTIRE current iNaturalist Fungi
  inventory directly via the API (`_live_search_inat_destination`, reusing
  `INatReconciliationReader`'s proven cursor-based full-scan pagination),
  scored with the same `score_candidate` used everywhere else. A record
  created after the last local scan, never loaded into the cache, or
  dropped by incomplete pagination is now visible. Fails closed on any
  read/pagination failure or an account too large to exhaustively page
  through (250-page cap).
- **Unexpected exceptions in `_execute_create` never leave the action
  stuck**: a final broad `except Exception` re-reads the row and settles
  `outcome_unknown` (if a write may have started) or `failed` (if not) —
  a parsing failure, missing field, type error, or DB exception after an
  already-successful remote write can no longer leave the journal
  permanently `running`.
- **Pair promotion and finalize success are now atomic**: a new
  `ReconciliationDB.settle_pair_finalize_success` promotes the provisional
  pair and marks `pair_finalize` succeeded in ONE transaction, rechecking
  identity/exclusion/one-to-one-conflict immediately beforehand. A crash
  between two separate statements used to be able to leave the pair
  confirmed while the finalize action stayed `running` forever.
- **Photo mint-time metadata is now compared, not just re-used**: before
  minting a photo action, `_mint_item_followups` reconstructs the exact
  metadata fingerprint (`photo_id`, `license_label`, `copyright_holder`)
  `_photo_items` computed at preview time and requires an exact match — a
  license or copyright-holder change since review now blocks minting even
  when the image bytes themselves are unchanged.
- **A photo item that fails to mint is never silently dropped from
  results**: `_mint_item_followups` now returns an explicit failed
  `ObservationCreationResult` for every item it could not mint an action
  for (missing/excluded pair, unreadable source, changed content, changed
  metadata, unsupported type). `execute_group` folds these into its
  returned results, so a selected photo can no longer vanish from the
  saga's outcome while the rest reports success.
- **The preview now runs Gate 1E's license/copyright-holder eligibility
  policy**: a photo `PhotoSyncService._options` would refuse (unsupported
  license, no verified MO login, no/mismatched copyright holder) is now
  shown disabled in the Gate 2A creation dialog too, with the same reason
  — never selectable only to fail after the destination observation and
  pair already exist. The destination-dependent half of Gate 1E's pipeline
  (duplicate-on-destination, transfer ledger, a complete destination scan)
  still cannot run before the destination exists and stays correctly
  deferred to mint time.
- **`species_guess` now agrees with the pinned `taxon_id`**: the create
  request's `species_guess` uses the PINNED resolved destination taxon
  name (`reviewed_destination_taxon_name`), never the raw, possibly
  unresolved/approximated source name string — the two could previously
  assert two different taxonomic identities in the same request for a
  disclosed-rank-approximation case (e.g. source "X group" -> resolved
  "X").
- **`_mint_item_followups` uses the identity's own pinned, confirmed
  pair**: replaced a `create_provisional_pair` re-derivation (which
  performed no confirmed/excluded/one-to-one-conflict check of its own)
  with an explicit re-verification of the identity's stored `pair_id` —
  exists, ids match, `review_state=='confirmed'`, not excluded, no
  conflicting confirmed pair. A pair that lost its confirmation or became
  excluded between finalize and item-minting is now caught rather than
  silently proceeding.
- **`_require_unambiguous_mo_match` no longer fails open**: the previous
  `owned or rows` fallback accepted a single search result even when NO
  row's ownership could be confirmed. Now requires exactly one row with a
  confirmed matching owner id AND (when a marker is supplied) whose notes
  actually contain the exact correlation marker searched for. This path
  is unreached (MO-destination creation remains unsupported) but is fixed
  as scaffolding.
- **Smaller issues**: the module docstring's ordinal list no longer
  implies `inat_ofv_add` is used for identifier population (it never is —
  only for the reciprocal link); confirmed (not re-changed) that the
  full-payload fingerprint (`reviewed_payload_fingerprint`) and the
  preview-time photo byte fingerprint were both already correctly pinned
  by round 3's work, contrary to the round-4 critique's premise on those
  two points — verified against the current code rather than assumed
  correct or re-fixed unnecessarily.

---

## Round-3 review pass (additional hardening on top of the section below)

A third independent review pass found and fixed eight further issues, all
now proven by the accumulated disposable smoke suite (15 scripts, zero live
writes — no live-write authorization was available this session):

- **v9->v10 migration on a `running` create action**: the old migration
  classified any non-terminal action as `attempt_state='pending'`, but
  `sync_creation_attempts.state`'s CHECK constraint has no `'running'`
  value, so migrating a database with an in-flight create crashed. Now
  classified conservatively: no `write_started_at` -> reset to `pending`
  (safe to retry); `write_started_at` set -> `outcome_unknown` (must go
  through `verify_unknown` recovery, never silently retried). The
  underlying `sync_actions` row is normalized to match in the same
  migration step so it is never left permanently unclaimable.
- **Full reviewed-payload pinning, not just the taxon**: the date,
  locality, coordinates, accuracy, description, attribution, and approved
  field-gap decisions are now folded into a one-way
  `reviewed_payload_fingerprint` (`sync_creation_attempts.
  reviewed_payload_fingerprint`), computed at `prepare_preview` time and
  re-checked immediately before the write. A remote-only change (e.g. the
  source's locality edited after preview, with no corresponding local
  timestamp bump) is now caught even though `source_fingerprint` alone
  would miss it. Raw notes/coordinates are never stored — only the hash.
- **Photo bytes pinned at preview time, not mint time**: `prepare_preview`
  now downloads and fingerprints each candidate MO photo immediately
  (`reviewed_byte_fingerprint`, journaled on `sync_creation_items` before
  any write). Minting re-downloads and requires an exact match; a photo
  whose content changed between review and mint is refused, never
  silently re-baselined as "reviewed."
- **Photo-transfer conflict handling never overwrites**: replaced an `ON
  CONFLICT ... DO UPDATE` with Gate 1E's proven explicit pattern — an
  existing `sync_photo_transfers` row that isn't `'failed'` blocks
  outright; only a `'failed'` row is reset and reused. A schema-level
  `UNIQUE(attempt_id,item_type,source_item_identity)` backs this.
- **Creation-item methods scoped to profile and group**: `creation_items`,
  `mint_creation_item_action`, and `mint_creation_photo_item_action` all
  now join through `sync_creation_attempts` and require the caller's
  `profile_id`/`group_id` to actually match — a wrong profile or a
  mismatched group is rejected with a `ValueError`, not silently allowed.
- **Coordinator validates selected items against the immutable preview**:
  `execute_observation_creation_action` now rejects any selected item that
  isn't an exact, enabled, unique member of `preview.items` (matched by
  `(item_type, source_identity)` *and* `metadata_fingerprint`) before any
  journal write — closes a forged/disabled/duplicated-selection gap at the
  irreversible-write boundary.
- **`PhotoSyncService`'s creation-saga exemption tightened**: beyond the
  existing pair/group id agreement, `_require_current_source` now also
  requires the action to be `inat_photo_attach`, linked to a `sync_
  creation_items` row of `item_type='photo'`, belonging to the ledger's
  own `attempt_id`, matching the identity's recorded destination, and
  matching its own `sync_photo_transfers` row's `action_id` exactly. A
  manually malformed `inat_photo_attach` row inside an otherwise-genuine
  creation group can no longer ride this exemption.
- **Smaller issues**: the preview/confirmation dialogs now show the pinned
  iNaturalist taxon id alongside its name, not the name alone;
  `settle_creation_write_success()` now requires an identity's
  `destination_observation_id` to be NULL-or-equal before writing it,
  rather than allowing an unconditional overwrite; `sync_creation_attempts`
  `-> sync_action_groups` is now `ON DELETE RESTRICT` (was `CASCADE`) so a
  future journal-cleanup path can never silently erase supposedly-immutable
  attempt provenance; `tools/gate_2a_saga_harness.py` gained
  `--with-photo-item`, so the checked-in *live* harness (not only the
  disposable scratchpad smoke tests) can exercise a real selected photo
  population item end to end.

---

## Current orchestration status (this review pass)

This section is the authoritative, current statement of what Gate 2A
actually does. The "Gate 2A-M0 Capability Note" below it is the original
raw-HTTP-primitive proof (still accurate for what it tested) — it predates,
and must not be read as equivalent to, saga-level orchestration completeness.

### Supported creation direction

**Mushroom Observer -> iNaturalist only.** A source record confirmed
missing on iNaturalist (`unpaired_state == 'confirmed_missing_on_inat'`) is
the only case the "Create missing observation…" button enables, and the
only direction `ObservationCreationService.prepare_preview` /
`journal_observation_creation_actions` will accept — both raise explicitly
(`unsupported_creation_direction`) for the opposite direction, independent
of the UI gate.

iNaturalist -> Mushroom Observer creation is **not enabled**, even though
the M0 note below shows the raw MO create/marker-search primitives were
live-proven for that direction too. What was never built is the
*orchestration*: duplicate prevention, taxon/name mapping, privacy mapping,
and final verification for an MO-destination saga. Do not re-enable this
direction without independently building and proving each of those, to the
same standard as the MO->iNat direction below.

### Supported population items

- **Identifier (voucher/collection number): NOT SUPPORTED.** Originally
  wired to `inat_ofv_add`, but `LinkRepairService._write`'s `INAT_OFV_ADD`
  branch is hardcoded to send the MO observation's own URL as the field
  value (its only real purpose is the Gate 1B reciprocal link) — there is no
  code path for writing an arbitrary identifier value. Reusing it would
  silently write the wrong value. Disabled at `prepare_preview` (never
  generates identifier items) and disclosed as a warning instead.
- **Photo, MO -> iNaturalist (`inat_photo_attach`): SUPPORTED.** Reuses
  Gate 1E's proven upload primitive. Requires the pair to be CONFIRMED
  (`PhotoSyncService._eligible_pair`), which is guaranteed by running
  population strictly after `pair_finalize` in the saga. Both
  `PhotoSyncService._require_current_source` and the item-mint step
  (`ObservationCreationService._mint_item_followups`) needed creation-saga-
  specific fixes this pass — see "Fixes this pass" below — without which
  every Gate 2A photo item failed unconditionally.
- **Photo, iNaturalist -> MO (`mo_photo_attach`): NOT SUPPORTED.**
  `PhotoSyncService._write` unconditionally uploads to iNaturalist
  regardless of the row's direction; an MO-destination photo row would
  misdirect the write. Never minted; rejected explicitly (before any
  network call) by two independent layers if a legacy/malformed row ever
  reaches an executor: `PhotoSyncService._execute` and
  `ObservationCreationService._execute_followup_row`.
- **ITS/sequence: NOT SUPPORTED.** Would route to `LinkRepairService`, which
  cannot construct a `LinkActionType` from an ITS action type and fails
  every time. `MOClient.sequences()` also remains structurally unverified
  (its pagination behavior was never live-tested the way images/names/
  external_links were) — out of scope for Gate 2A regardless, since the
  item type itself is disabled.

### Exact taxon-pinning policy

The destination taxon is resolved once, during `prepare_preview`, and
**pinned** on the creation attempt (`sync_creation_attempts.
reviewed_destination_taxon_id` etc) — never recomputed and substituted
later:

- Exact active Fungi matches preferred; inactive/synonym matches and a
  kingdom-rank match (the source resolving to "Fungi" itself) are excluded.
- A quoted MO provisional name that resolves exactly is `resolution_mode=
  'provisional_exact'`.
- An MO-only rank suffix (group/clade/complex) is stripped and the base
  taxon is used only as a disclosed `resolution_mode=
  'disclosed_rank_approximation'`, never silently.
- **An unresolved destination taxon blocks creation entirely** — it is not
  an approved "cannot be transferred" gap. `prepare_preview` and
  `_execute_create` both raise before any write.
- `_execute_create` re-resolves fresh immediately before the write and
  compares against the pinned value (source name, resolved id, resolution
  mode, and a drift fingerprint) — any mismatch (source name changed, the
  taxon became inactive) blocks the write and requires a fresh preview. The
  create payload uses the PINNED id, never the freshly recomputed one.
- `pair_finalize` requires the created observation's actual taxon id to
  equal the pinned `reviewed_destination_taxon_id` exactly.

### Creation-attempt provenance (durable, multi-attempt)

`sync_created_observations` (the stable source->destination identity) and
`sync_creation_attempts` (one immutable row per reviewed/approved attempt)
are separate tables (schema v10). Retrying never overwrites, repoints, or
deletes a prior attempt's correlation marker, reviewed taxon pin, or item
plan — a new attempt is inserted and links to its predecessor via
`supersedes_attempt_id`. An attempt still `pending` or `outcome_unknown`
blocks a new attempt outright (an unknown outcome is never silently
superseded); an identity with a recorded destination id blocks any further
attempt (duplicate-creation prevention).

### Unknown-outcome behavior

Both a lost iNat create response and MO's structurally-ambiguous create
response (see "Critical asymmetry" below) land the create action in
`outcome_unknown` and are recovered ONLY by re-reading the destination
(`verify_unknown`, by uuid on iNat / marker search on MO) — never by
resending the create. `MOClient.find_observation_by_marker` is a plain read
(`_get`, not `_write`): a search failure never counts as an ambiguous
second write, and the original create action is left exactly where it was.

### Live-proven vs. stubbed/offline (this pass)

- **Live-proven earlier (2026-07-23, unchanged by this pass):** the raw
  create/marker-search/delete HTTP primitives on both sites (see the M0
  note below), and the full MO->iNat creation+link+finalize saga with
  `item_specs=[]` (`tools/gate_2a_saga_harness.py`) — creation, reciprocal
  links, and `pair_finalize` only, explicitly NOT any population item.
- **Verified this pass, stubbed HTTP boundary, no live writes (no live-write
  authorization was available in this session):** the reordered saga
  (population strictly after `pair_finalize` confirms the pair);
  `mint_creation_item_action`'s atomicity under 8 concurrent callers on the
  same item; the full creation-attempt retry/supersession state machine
  (failed-before-write, failed-after-rejection, cancelled, outcome_unknown,
  successful, superseded); the tightened creation-saga link exemption's 7
  negative cases plus the 2 positive cases; and — critically — one REAL
  Gate 2A photo population item executing through the actual
  `PhotoSyncService._execute` (not a test double) against a confirmed pair,
  uploading exactly once, with a safe no-op resume.
- **Round-3, stubbed HTTP boundary, no live writes:** the v9->v10 running-
  action migration classification (both the no-write-started and
  write-started cases); full-payload drift detection (a remote-only
  locality change blocking the write with zero `create_observation_v2`
  calls); photo-byte drift detection (content changed between preview and
  mint refused, no action minted, no transfer row created); creation-item
  profile/group scoping (cross-profile and mismatched-group access
  rejected, correctly-scoped access still succeeds); the coordinator's
  item-selection validation (legitimate item allowed, disabled/forged/
  absent/duplicated selections rejected); the tightened photo exemption's
  five new checks individually (wrong action_type, item not linked,
  attempt mismatch, transfer-ledger mismatch, plus a positive control);
  and the `ON DELETE RESTRICT` provenance guard (deleting an action group
  with attempt history is blocked, not silently cascaded).
- **Round-4, stubbed HTTP boundary, no live writes:** the live destination
  search finding a duplicate that is NEVER present in the local cache
  (only reachable via `get_reconciliation_observations`), scored via a
  genuine reciprocal-link (LINK-family) match; an unexpected
  (non-API/domain) exception after `mark_action_write_started` settling
  `outcome_unknown`; `settle_pair_finalize_success`'s happy path (atomic
  promote + settle) and its refusal on an excluded pair (both pair and
  action left untouched); a photo's copyright holder changing since
  preview blocking minting despite unchanged bytes; `species_guess`
  carrying the pinned resolved taxon name, not the raw source string, in
  a disclosed-rank-approximation case; `_mint_item_followups` refusing
  to mint against an excluded/unconfirmed pair; and
  `_require_unambiguous_mo_match` refusing both a no-owner-info row and a
  row whose notes lack the searched marker, while still accepting a
  genuinely matching row.
- **Round-5, stubbed HTTP boundary, no live writes:** TWO real photo
  population items, selected together, both executing successfully
  through the ACTUAL `PhotoSyncService` (not a stub) in sequence — the
  fix for the guaranteed-second-item-failure bug; a pre-mint preparation
  failure on the first of two items stopping the tail before the second
  is ever minted or written, with a durable real (non-pseudo) failed
  action row left behind; and `settle_pair_finalize_success` refusing on
  a not-yet-running action, a wrong action group, a wrong pair id, a
  wrong mo/inat id, and a nonexistent action id — five independent
  negative cases, each leaving the pair provisional and untouched — while
  the genuinely correct call still succeeds.
- **Not exercised this pass:** a live selected-item acceptance (no
  authorization available this session — `tools/gate_2a_saga_harness.py
  --with-photo-item` is wired up and ready for the next session with
  authorization); `MOClient.sequences()`'s actual pagination behavior
  against the live API.

### Fixes this pass (see the accompanying review report for full detail)

`mo_photo_attach` hard-rejected at every executor; creation direction
gated to MO->iNat only, in the service, the journal function, and the UI;
exact taxon pinning + drift detection + hard block on unresolved taxa;
immutable multi-attempt provenance (schema v10) replacing the old
mutate-in-place retry; the creation-saga link exemption tightened to check
every condition explicitly (excluded pair, one-to-one conflicts, ledger/
group/pair id agreement, population-requires-confirmed) instead of a broad
early return; `mint_creation_item_action` given a schema-level partial
unique index as a backstop; identifier population disabled (was silently
going to write the wrong field value); and two previously-undetected bugs
that made every real photo population item fail unconditionally
(`PhotoSyncService._require_current_source` compared against the wrong
fingerprint reference for a creation-saga group; item mint never captured
the live preview fingerprints `_require_unchanged_context` requires).

---

# Gate 2A-M0 Capability Note

Status: **CLEARED for both sites, 2026-07-23.** M1 (client methods) may begin.
Harness: `tools/gate_2a_proof_harness.py`. Live proof run against disposable
observations on account alan_rockefeller — iNat observation 384179444
(deleted), MO observation 656469 (recovered by explicit id, marker-verified,
deleted).

## iNaturalist — CLEARED

All blocking checks passed:

- `POST /observations` creates a real observation from a minimal payload
  (`species_guess`, `observed_on_string`, `place_guess`, `description`,
  `geoprivacy`). Body wraps as `{"observation": {...}}` with a sibling
  `fields` selector — confirmed directly against `api-docs.json`'s
  `ObservationsCreate` schema (`additionalProperties: false` at both levels),
  not assumed.
- **The client-supplied `uuid` field becomes the observation's own uuid
  exactly** — sent `b754c37b-...`, returned observation uuid was byte-for-byte
  identical. This is the M4 idempotency anchor: strictly better than a
  fuzzy window search.
- **Same-uuid re-POST is de-duplicated** — a second `POST /observations` with
  the identical `client_uuid` returned the *same* observation, not a second
  one. Confirmed live, not just assumed by analogy to `POST /photos`' proven
  uuid behaviour.
- `GET /observations/{uuid}` recovers a "lost" create response by exact
  lookup — this is `verify_unknown`'s mechanism for the iNat-destination case.
- `DELETE /observations/{uuid}` works for the owning user (v2 exposes this,
  confirmed both by the API spec listing `delete` under `/observations/{uuid}`
  and by a live delete + re-read showing the record gone). Unlike photos
  (Gate 1E: no delete surface at all), a wrongly-created iNat observation
  **can** be removed — though Phase 2A's saga design still does not do this
  automatically on partial failure; this is recorded for completeness, not as
  a plan change.

## Mushroom Observer — CLEARED

All blocking checks passed, including the hard-outcome question:

- `POST /api2/observations` create parameters (via `?help=1`, method-specific
  per the Gate 1E finding): `accession_number, altitude, code,
  collection_number, collectors_name, date, gps_hidden, has_specimen,
  herbarium, images, initial_det, is_collection_location, latitude, location,
  log, longitude, name, notes, projects, reason_1..4, source, species_lists,
  thumbnail, vote`.
- `notes` is a genuine free-text field and accepts an opaque marker.
- **`notes` is independently searchable via `GET /api2/observations?notes_has=<marker>`**
  (optionally combined with `&user=<id>`) and reliably resolves back to the
  created observation. **This is the HARD-OUTCOME finding: Mushroom Observer
  MAY proceed as a Phase 2A creation destination.** Confirmed live: created
  observation 656469 with `notes="[inat-study-gate2a-proof:63708e1f-...]"`,
  then `notes_has` search for that exact string returned observation 656469
  and no other record.
- `DELETE /api2/observations?api_key=...&id=<id>` works and is confirmed by
  re-read.

### Critical asymmetry vs. iNat — read before implementing M5

**MO's create response carries no usable id, even on an ordinary, fully
successful, synchronous create (HTTP 200, no `errors[]`).** The harness's
first live create attempt returned `HTTP 200` with no parseable id in the
response body at all — this was not a parsing bug reacting to a partial/lost
response, it is what a normal successful MO create returns. The id had to be
recovered by finding the observation on the website and reading it back via
`GET /api2/observations?id=<id>`.

**Implication for M5:** unlike iNat (where the create response itself carries
the id — no ambiguity, no immediate follow-up needed) or MO's own photo
create (which requires an *enumeration diff* against the destination per
Gate 1E, since concurrent images exist), **every MO observation creation —
not only an `outcome_unknown` recovery — must immediately follow the create
with a marker search** (`notes_has=<marker>&user=<profile's MO id>`) to
discover `destination_observation_id`, even when the create's own HTTP
response was unambiguously successful. This is a normal, expected step of
ordinal 0's `_execute` on the MO-destination path, not a fallback path. The
marker-embedded-in-notes mechanism (M4) is therefore load-bearing for the
*happy path* on MO, not just for recovery.

Two follow-on considerations for M5, not yet resolved here:
- If the marker search itself returns zero results immediately after a
  successful create (e.g. read-replica lag), the row must stay in a
  "created, id not yet located" state and retry the *search* (never the
  create) — this is a new sub-state alongside the existing
  `pending/running/succeeded/failed/cancelled/outcome_unknown` enum, or can
  be modeled as `outcome_unknown` with `verification_state` distinguishing
  "write confirmed, id not yet found" from "write itself ambiguous." Decide
  during M5 implementation.
- The `notes` marker is genuinely public (it is the observation's visible
  notes field) — M4's disclosure requirement to the user applies to every MO
  creation, not just the outcome-unknown edge case, since it is now known to
  be part of MO's normal happy path, not a rare fallback.

### Double-create idempotency — not directly tested this run

The harness's `mo.create_idempotency` check (does a second create with the
same marker dedupe or silently duplicate?) was not exercised in this run,
since the first create's id had to be recovered manually via
`--mo-recover-id` rather than through the normal `run_mo_checks` path. Given
MO's create response returns no id even for a single successful create, a
second create is exceedingly unlikely to be deduplicated by MO itself
(there is no id for it to key off of) — **assume MO does NOT dedupe
creates**, and Phase 2A's MO recovery path must always search-before-retry
and never rely on MO rejecting a duplicate. This should be stated as a
design assumption, not re-verified, unless a future run finds otherwise.

## Outcome for the rest of the plan

Both `inat_observation_create` and `mo_observation_create` may proceed as
designed in the approved plan (`sorted-wibbling-meadow.md`), with M5 updated
per the asymmetry above: MO's ordinal-0 `_execute` always performs a
post-create marker search regardless of whether the create response itself
was ambiguous.
