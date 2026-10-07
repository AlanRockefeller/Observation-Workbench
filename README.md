# Observation Workbench

# By Alan Rockefeller - May 2, 2026

Desktop app for learning from iNaturalist expert identifications. Can also be used as an identify interface which shows full resolution images quickly - or to automatically identify observations with provisional species names, with human oversight.

Use `./start.sh` from the repo root to launch it. That is the recommended entry point because it picks the local virtual environment when available and applies the Qt flags this app expects.

## What It Does

- Study a specific identifier's work, or load an iNaturalist observations URL directly
- Browse results with keyboard navigation, image prefetching, and disk caching
- Open the current observation or image in the browser
- Inspect a live taxon summary panel for the current query
- Authenticate to iNaturalist and post agreements from inside the app
- Supervise bulk provisional-name agreements with pause, skip, and review controls
- Work through an iNaturalist Identify query in a dedicated window with large photos
- Bulk-disagree to a coarser taxon, or apply MycoMap's autovalidated sequence IDs
- Edit the `Provisional Species Name` and `Species Name Override` observation fields in bulk
- Discover and durably record DNA-barcode links between observations
- Find DNA-barcoded observations whose identification votes deserve a second look
- Reconcile your Mushroom Observer and iNaturalist records (beta)

### Requirements

- Python 3.11+
- A display (Linux: X11 or Wayland + Qt6 platform plugins)

## Setup

### Prebuilt executables

Tagged releases carry standalone builds for Windows x64, macOS x64, macOS arm64, and
Linux x64, published to the GitHub Release for that tag by
`.github/workflows/build-executables.yml`. Download the zip for your platform, unpack it,
and run `ObservationWorkbench` (`ObservationWorkbench.app` on macOS). No Python
installation is needed for those builds.

Maintainers cut a release with `./build-release.sh`, which lints with ruff and then
creates and pushes the `v*` tag that triggers the build.

### From source

```bash
cd /path/to/observation-workbench
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
pip install -r requirements.txt
./start.sh
```

Or install as a package:

```bash
pip install -e .
observation-workbench
```

## Working With It

- Enter an identifier username, such as `deniszabin`, in the Identifier / URL field.
- Or paste an iNaturalist `/observations` URL. The app uses that query as the base search.
- For username studies, the Place and Taxon fields autocomplete from the iNat API.
- Press `Load` or `R` to fetch results.
- Double-click a taxon in the Taxon Summary panel to filter by it.

### Filter options

| Field            | Description                                                                                  |
| ---------------- | -------------------------------------------------------------------------------------------- |
| Identifier / URL | iNaturalist username of the person whose IDs to study, or an iNaturalist `/observations` URL |
| Place            | Filter to a geographic place (autocomplete)                                                  |
| Taxon            | Filter to a taxon and all descendants (autocomplete)                                         |
| Leading IDs only | Only show identifications that are currently leading the community ID                        |
| Provisional Name | Client-side filter for taxon names containing an apostrophe                                  |
| Minimum rank     | Limit identifications to a chosen taxonomic rank                                             |
| Exact rank       | Show only identifications at the chosen rank                                                 |
| From / To        | Optional date range filter                                                                   |

When you use an observations URL, the URL query becomes the base query. Taxon can still narrow it further. Username-only controls like Leading IDs only and rank filtering are ignored in URL mode.

Example:
`https://www.inaturalist.org/observations?place_id=14&taxon_id=63421&field:DNA%20Barcode%20ITS=`

## Keyboard Shortcuts (Main Window)

| Key                 | Action                              |
| ------------------- | ----------------------------------- |
| `→` / `Space`       | Next observation                    |
| `←` / `Shift+Space` | Previous observation                |
| `↓` / `]`           | Next photo within observation       |
| `↑` / `[`           | Previous photo within observation   |
| `G`                 | Go to a specific result number      |
| `a`                 | Agree with most recent non-self ID  |
| `A`                 | Agree with consensus/community ID   |
| `O`                 | Open current observation in browser |
| `I`                 | Open current image in browser       |
| `L`                 | Toggle fit-to-window / 1:1 zoom     |
| `R`                 | Reload current filters              |
| `F`                 | Focus the Identifier / URL field    |

Navigation shortcuts work globally, except when a text input field is active.

## The Identify Window

Use **Action -> Identify observations...** to open a separate window modelled on
iNaturalist's Identify page, but showing full-resolution photos with the same prefetching
and disk caching as the main window.

The setup dialog takes an iNaturalist observations or Identify URL and breaks it into a
table of **Parameter / API value / Resolved meaning / Source**, so you can see what the
query will actually ask for. Taxon and place IDs are resolved to names. You can edit values
in place, **Add advanced parameter** for anything the URL did not carry, and **Remove
selected** to drop one. **Parse / reload** re-reads the URL and shows the result count
before you commit.

- **Session limit** caps how many observations the session will hold.
- **Prefetch radius** is how many neighbouring observations to preload images for.
- Queries using `reviewed` (or any other viewer-scoped filter) require authentication,
  because unauthenticated they would quietly return different results. The dialog says so
  and pairs `reviewed` with the `viewer_id` the API requires.

**Execute** builds a fixed-order session; the order never changes underneath you while you
work through it. **Show reviewed** toggles whether already-reviewed observations stay
visible.

The right-hand pane has an **Info** tab with the observation's identifications, comments,
and fields. The **Suggestions**, **Annotations**, and **Data Quality** tabs are placeholders
and are not implemented yet. The refresh button in the tab corner re-reads the current
observation from iNaturalist, which picks up changes you made in a web browser.

The toolbar posts **Add ID**, **Agree**, **Comment**, **Reviewed**, and **Favorite**, and a
**Captive/Cultivated** button below the photo casts that Data Quality vote.

### Identify write safety

Identify writes go through a durable journal. Intent is written to SQLite before the request
leaves the machine, requests are serialized, and each one needs an explicit authorization
for the current account. An identification you post appears immediately in the header as an
optimistic placeholder. Real data replaces it once a confirmed refresh comes back, and it is
reverted with a notice if the write definitely failed or came back ambiguous.

Two menu items appear only when they are actually needed:

- **Pending Identify actions...** lists unresolved journal rows. Opening it never resumes them.
- **Retry safe Identify refresh** retries a failed *read* of an observation whose write was
  already confirmed. It never resends the write.

The status bar shows network health, a **Details...** button for recent failures, and a
**Retry details** button when a detail read has failed.

### Identify keyboard shortcuts

| Key                        | Action                                                                       |
| -------------------------- | ---------------------------------------------------------------------------- |
| `←` / `→`                  | Previous / next observation                                                  |
| `↑` / `↓`                  | Previous / next photo                                                        |
| `Alt`/`Cmd` + `←` / `→`    | Previous / next photo                                                        |
| `Shift` + `←` / `→`        | Previous / next tab                                                          |
| `I`                        | Add an identification                                                        |
| `A`                        | Agree with the observation taxon                                             |
| `C`                        | Add a comment                                                                |
| `R`                        | Mark reviewed; advances when reviewed observations are hidden                |
| `F`                        | Add or remove favorite                                                       |
| `X`                        | Change your Captive/Cultivated Data Quality vote                             |
| `Z`                        | Toggle fit / 1:1                                                             |
| `Alt`/`Cmd` + `↑` / `↓`    | Adjust brightness                                                            |
| `?`                        | Show this help                                                               |

## Authenticated Identification

Use **Action -> Authenticate to iNaturalist...** to open iNaturalist's API token page, paste the browser token into the app, and validate the login. Authenticated actions refresh the observation before posting, and the app avoids duplicate or self agreements.

Single-observation agreements are keyboard-only: press `a` to agree with the most recent non-self ID, or `A` to agree with the consensus/community ID.

The same menu also exposes:

- Agree to provisional IDs, which opens a guided review flow with optional pauses
- Bulk disagree to taxon from URL, which opens a separate supervised corrective-ID workflow
- Apply autovalidated identifications, which posts MycoMap's automated sequence IDs where the consensus has not caught up
- Propose a name to observation numbers, for a typed list of observations

### Automatic Provisional Identifications

Use **Action -> Agree to provisional IDs...** after loading a query and authenticating. The app scans the current results for the most recent current non-self identification with an apostrophe in the taxon name, then builds a candidate list.

The setup dialog enables **Only agree when needed** by default. With this option enabled, observations that are already Research Grade with the proposed provisional taxon as their community taxon are skipped, and the same check is repeated immediately before each identification is posted.

You get a preview first. If there are more than five candidates, type `AGREE` to enable **Start** since this can potentially add thousands of identifications. After that, each observation is shown one at a time in a supervised progress dialog.

The progress dialog buttons do this:

- **Cancel** stops the whole run
- **Pause** and **Resume** toggle the countdown
- **Skip this ID** skips the current observation once
- **Skip forever** skips the current observation and remembers it for future bulk runs. Use this if someone proposed a provisional name, but then someone else proposed a scientific name and you think the scientific name is better.
- **Post now / skip delay** posts immediately instead of waiting for the countdown

The dialog can also stop for human review before the countdown starts. Those checks are optional and can be turned off with the checkboxes at the top of the dialog:

- Most recent identification is not provisional (could be that a scientific name is the best name)
- Comments have been added since the provisional name was proposed (maybe people are arguing about the provisional name - better check the comments before deciding on an ID)

When either check is enabled and the condition is met, the run pauses and waits for you to review the observation before continuing. The app also refreshes each observation again right before posting, so it can stop if the target changed in the meantime. It is careful to not add duplicate ID's - for example if you view the observation in a web browser and click agree on the provisional, it won't add that ID again.

iNaturalist doesn't allow automated identifications, so each observation, its comments, previous ID's and photos are displayed in the main window while the run works through them. Watch that window as it goes, or use the Browse feature from the plan dialog to vet the observations before you turn it loose.

### Bulk Disagree to Taxon from URL

Use **Action -> Bulk disagree to taxon from URL...** after authenticating. Paste an iNaturalist observations URL. If it contains exactly one numeric `taxon_id`, that URL taxon becomes the source/current taxon safety check. A URL with no `taxon_id` (for example one filtered by an observation field such as `field:Provisional Species Name=...`) is also accepted; in that case there is no source-taxon safety check and the target identification is posted to every matching observation. A URL with more than one `taxon_id`, or a non-numeric one, is rejected as ambiguous. Then select the target taxon from autocomplete, enter the identification comment, preview candidates, and supervise one observation at a time.

The workflow posts a coarser ancestor target as an explicit disagreement, so an ancestor ID can move the community ID back from species to genus when iNaturalist accepts the disagreement. If the target taxon is not an ancestor of the source taxon, the setup dialog shows a strong warning and posts a normal conflicting ID instead of iNaturalist's explicit ancestor-disagreement flag, which supports same-rank corrections such as synonym-style species changes. It refreshes full observation details before preview and refreshes each observation again immediately before posting. It skips observations when the DNA Barcode ITS field is present by default, when you already have a current ID at the target taxon, when the source taxon no longer matches, or when the observation is on this workflow's permanent skip list.

After planning, the preview dialog includes **Browse photos...** for fast visual triage. The browser shows large photos for the planned observations, lets you skip candidates for the current run, permanently skip candidates for future bulk disagree runs, or immediately post an alternate ID with its own comment. Alternate IDs are refreshed before posting, can optionally be posted as explicit disagreements, and remove that observation from the planned bulk disagreement run after a successful post. Dry runs disable alternate-ID posting.

The workflow also skips observations where you already have a current ID at the selected target taxon, so running the same disagreement workflow again will not add duplicate genus disagreements. The DQA checkbox can also vote "ID is already as good as it can be." That vote is only attempted after the identification post succeeds and a refresh shows the community taxon now matches the selected target taxon, falling back to the current observation taxon only when no community taxon exists. If the observation stays at species level while the target is genus, the ID is counted separately and the DQA vote is skipped. If your existing "ID is already as good as it can be" vote is visible in the refreshed observation details, the workflow will not post it again. Dry runs never post identifications or DQA votes.

### Apply Autovalidated Identifications

Use **Action -> Apply autovalidated identifications...** after authenticating. MycoMap's
sequence autovalidator writes the identification it inferred into an observation field and
leaves a fixed comment from `@stevilkinevil`, but it never posts an identification, so the
community consensus often lags behind the autovalidated name. This workflow finds those
observations and lets you post the missing identifications.

Discovery filters on the `ID Update Needed` observation field, which the autovalidator sets
on every record it processes and which the observer flips to `Yes` to contest the automated
call. The iNaturalist observations API cannot filter by commenter, and observations the
observer contested are excluded. You can paste an optional observations URL to narrow the
search by place, taxon, or observer; the autovalidation filters are always applied on top, so
a URL can never widen the search. Observations are scanned in ascending observation-ID order.
**Continue next batch** remembers the last scanned ID separately for your account and search
filters, so you can keep the same batch size each time rather than increasing it. Each run
also restores up to one batch of pending reviews from fresh, batched API reads. Only IDs and
scan metadata are saved; closing the preview, deferring an observation, using dry run, or
encountering a failed read does not lose that work. Successfully posted or already-satisfied
observations leave the queue, and unresolved names remain available to retry.

Use **Retry pending reviews and unresolved names only** to revisit unfinished work without
scanning new observations. **Check for new or changed observations (restart scan)** restarts
the search without clearing pending reviews; continue subsequent batches to revisit the
full search. This can discover older observations that acquired autovalidation later, as
well as changed observations. Pending retries rotate through the queue so unresolved items
do not prevent later reviews from being reached.

The autovalidated name is read from `Provisional Species Name`, falling back to
`Species Name Override` when only that is set, and is matched to an iNaturalist taxon **by
exact name only**. Autocomplete's near misses are never accepted, because a wrong match
would post a wrong identification. A name with no matching taxon usually means the
provisional name has not been created on iNaturalist yet. Those observations can never be
posted to, and the **Skip observations whose autovalidated name is not on iNaturalist yet**
checkbox controls whether they are skipped quietly or listed afterwards so the missing names
can be created.

Each candidate carries its own target taxon. The identification is posted as an explicit
disagreement only when the autovalidated name is a strict ancestor of the current consensus;
refining to a descendant, or a same-rank correction, is posted as a plain ID. Preview,
photo browsing, per-observation skipping, the permanent skip list, delays, and dry run all
work exactly as in the bulk disagree workflow. Immediately before each post, the observation
is refreshed and re-checked: it must still carry a DNA Barcode ITS sequence and the
autovalidation comment, its autovalidated name must still be the one planned, and its
consensus must still differ from it. An observer changing `ID Update Needed` to `Yes` also
excludes a pending observation when it is restored or refreshed before posting.

### Propose a Name to Observation Numbers

Use **Action -> Propose a name to observation numbers...** after authenticating when you
already know which observations you want to identify. Enter observation numbers separated
by spaces, commas, or new lines (pasted iNaturalist observation URLs are also accepted),
then pick the name to propose from autocomplete and write the identification comment.

Because the observations are typed in rather than discovered from a query, there is no
source taxon and no source-taxon safety check. Each observation is refreshed from
iNaturalist before its identification is posted, and the explicit disagreement flag is set
per observation: only when the proposed name differs from that observation's current taxon.
An identification that agrees with the current taxon is posted as a plain ID.

**Tag users who proposed a different identification** appends a blank line and @-mentions of
the identifiers whose current ID differs from the name you are proposing. Preview, delays,
and **Preview only / dry run** work as in the bulk disagree workflow.

### Not tagging certain users

Bulk workflows that @-mention other identifiers read an opt-out list from a
`users_not_to_tag.txt` file in the application data directory, one login per line. On Linux
that is `~/.local/share/ObservationWorkbench/Observation Workbench/`, with the platform
equivalent elsewhere. A commented example file ships in the repo root; copy it into that
directory to start using it. Anyone listed there is never tagged, and you are never tagged
in your own runs. The file is re-read whenever it changes, so you do not have to restart the
app after editing it.

## Observation Field Editing

Three workflows edit observation fields rather than posting identifications. All of them
require authentication, plan before they write, and show you the plan first.

### Provisional Name Swap

Use **Action -> Provisional Name Swap...** to search for one `Provisional Species Name`
field value across iNaturalist and replace it with another. Use it after a provisional name
is superseded or corrected: search the old value, review the list of observations carrying
it, and swap the matching values in place. Field values that have disappeared since the
search are counted and reported rather than silently skipped.

### Update Species Name Override and Update Provisional Species Name

**Action -> Update Species Name Override...** and **Action -> Update Provisional Species
Name...** are the same workflow pointed at two different fields (`Species Name Override`
and `Provisional Species Name`).

Find the observations either by an existing provisional name or by pasting observation IDs
and URLs. **Only update observations matching genus:** narrows the selection further, which
matters when a provisional epithet is shared across genera. The plan dialog lists what will
change, and a photo browser lets you look at the observations before committing.

## DNA Barcode Linking

### Link Observations to DNA Barcodes

Use **Action -> Link observations to DNA barcodes...**. Some observations carry a `DNA
Barcode ITS` sequence while a *different* observation of the same physical organism does
not, often the same collection photographed twice, or a specimen posted by a second person.
This workflow finds those pairs and writes a link into the barcode-less observation's `DNA
Barcode ITS` field pointing at the observation that holds the sequence:

```
DNA barcode for this observation is in https://www.inaturalist.org/observations/<id>
```

Discovery is unauthenticated and read-only. The setup dialog takes:

- **Source observations URL**: the query for observations that already carry a sequence
- **Candidate username (optional)**: restrict candidate matches to one observer
- **Candidate radius**: how close a candidate has to be geographically
- **Time window (each side)**: how far apart the observation dates may be
- **Field-bearing source rows per chunk**: discovery batch size

Candidates are scored on coordinates and time, and a qualifying sequence is at least 120
nucleotide characters. Progress is shown as it scans and **Cancel safely** stops it without
losing what it has found.

Review is one pair at a time with photos side by side and **Open photo** for the full-size
image: **Same organism** records the link, **Not the same** rejects the pair, **Skip for
now** defers it. **History...** shows past decisions, reopens a pair you decided wrongly,
and can restart source discovery.

Writes are journaled to a dedicated SQLite database before they are sent. If an existing
value is already present in the destination field, the app shows it and asks rather than
overwriting it, and it will not write over a field that already contains a real sequence.

### Recover Uncertain DNA-Link Writes

Use **Action -> Recover uncertain DNA-link writes...** if a write's outcome was never
confirmed, meaning the request went out but the network failed before the answer came back.
**Verify Again** re-reads the observation field from iNaturalist and settles the journal
entry against what is actually there. Nothing is re-sent blindly.

### Review DNA-Contested Identifications

Use **Action -> Review DNA-contested identifications...**. iNaturalist does not expose when
an observation field was added, so this workflow uses a simple premise: if an observation
carries a qualifying `DNA Barcode ITS` sequence *and* its current identifications disagree,
the disagreement was probably driven by that sequence.

The whole workflow is read-only and unauthenticated. It never posts anything; it hands
observations off to iNaturalist in a browser. Set a **Username** (blank means anyone), an
optional **Restrict to URL**, and how many **Observations to scan**. The observations index
refuses to page past 10,000 results.

Findings are grouped by kind, most worth revisiting first:

| Finding                          | Meaning                                            |
| -------------------------------- | -------------------------------------------------- |
| Contested                        | Your identification is actively disputed           |
| Outvoted                         | The community moved away from your identification  |
| Others are more specific         | Someone has proposed a finer identification        |
| Identifiers disagree             | The identifiers have not converged                 |
| Consensus needs one more vote    | One more agreement would settle it                 |

Filter the results table by kind, **Open in browser**, or **Copy visible URLs** to work
through them elsewhere.

## Reconcile Mushroom Observer and iNaturalist (beta)

> **Beta.** This workflow works, but it is not fully tested. Review every preview carefully
> before confirming anything, and expect rough edges.

Use **Action -> Reconcile Mushroom Observer ↔ iNaturalist...** to compare your records on
both sites. First set up a profile: your iNaturalist login (taken from the authenticated
session) and your Mushroom Observer username, then **Resolve exact accounts** so both are
pinned to numeric account IDs rather than name strings. Profiles are stored per account, and
the MO API key is entered separately with **MO API key...**. **Field bindings...** maps which
observation fields on each side hold which data.

**Refresh inventory** does an incremental scan; **Full inventory** rebuilds it. The main
list pairs MO and iNat observations, and you confirm or reject each proposed pair with `C`
to confirm and `X` to reject. There is also **Undo last pair decision**, **Compare photos
(read-only)...**, and buttons to exclude a record, ignore an issue, or mark one confirmed
missing.

Every write is a separate, explicitly confirmed action behind its own preview:

- **Preview link repairs...** adds or fixes the reciprocal links between the two sites
- **Compare ITS...** compares and synchronizes ITS sequence data and accessions
- **Compare coordinates...** compares coordinates and privacy state, and copies them
- **Preview photo synchronization...** transfers photos, with per-upload confirmation
- **Create missing observation...** creates an iNaturalist observation that only exists on MO
- **Consolidate duplicate observations...** merges a duplicate set onto one canonical record
- **View consolidation history...** shows what was consolidated, and when
- **Propose name...** proposes an identification across the pair

Consolidation is non-destructive: it links and supersedes, and never deletes, hides,
withdraws, or edits the donor observation. Donor deletion is a separate reviewed step
(**Review donor deletion...**), and remains blocked pending live acceptance. Interrupted
actions can be picked up again with **Resume / verify journal action** or cancelled with
**Cancel pending journal action**.

Raw sequences, notes, coordinates, and field values are kept out of the local database, the
logs, and error messages. `docs/gate_*_capability_note.md` records what each gate is and is
not allowed to do.

## Cache, Settings, and Debugging

Open **File -> Settings** to configure:

- **Disk image cache** directory and max size (default: `~/.cache/observation_workbench`, 2 GB). Directory changes take effect after a restart, and files already in the previous images subfolder are not moved or deleted.
- **In-memory image cache** limit (default: 256 MB)
- **Prefetch radius** - how many nearby observations to pre-load images for
- **UI scale** and **result list text scale** (change this if the text is too small or large on your screen)
- **Scroll speed** - multiplies in-app mouse-wheel scrolling, useful when WSL does not use the Windows wheel-lines setting
- Whether to include common names alongside scientific names

The **Debug** menu can show the log panel (`Ctrl+Shift+L`), switch verbose logging on and
off, show **Cache info...** (current disk usage) and **Clear all caches...**.

The **Help** menu has a **Keyboard shortcuts** reference and **View Readme**, which opens
this file from inside the app.

## Architecture

- `main.py` and `start.sh` launch the app
- `observation_workbench/api/` handles iNat requests, auth, URL parsing, and JSON parsing
- `observation_workbench/services/` holds loading, image cache, prefetch, taxon summary, and identification workflow logic
- `observation_workbench/storage/` stores SQLite cache data and app settings
- `observation_workbench/reconciliation/` holds the Mushroom Observer client and the reconciliation gates (links, ITS, coordinates, photos, consolidation, deletion)
- `observation_workbench/dna_linking/` holds DNA-barcode discovery, the durable write state machine, and its own SQLite schema
- `observation_workbench/ui/` contains the Qt window, filter bar, result list, viewer, metadata panel, taxon summary, auth dialog, the Identify window, the reconciliation and DNA-linking windows, bulk agreement dialogs, settings, and log panel

## Taxon Summary Panel

The panel calls `/observations/species_counts` with the same filters as the main query. For username studies it uses `ident_user_login=<username>`. For observations URL mode it reuses the URL query parameters. Results are cached in SQLite for 1 hour.

## iNaturalist API Notes

- Rate limiting is handled with retries around 429 and 503 responses. API errors get printed to the console.
- Photo URLs are derived from the square URL by swapping the size token.
- Taxon filters include descendants on the iNat API.
