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

### Requirements

- Python 3.11+
- A display (Linux: X11 or Wayland + Qt6 platform plugins)

## Setup

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

## Keyboard Shortcuts

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

## Authenticated Identification

Use **Action -> Authenticate to iNaturalist...** to open iNaturalist's API token page, paste the browser token into the app, and validate the login. Authenticated actions refresh the observation before posting, and the app avoids duplicate or self agreements.

The same menu also exposes:

- Agree with most recent ID
- Agree with consensus ID
- Agree to provisional IDs, which opens a guided review flow with optional pauses
- Bulk disagree to taxon from URL, which opens a separate supervised corrective-ID workflow

### Automatic Provisional Identifications

Use **Action -> Agree to provisional IDs...** after loading a query and authenticating. The app scans the current results for the most recent current non-self identification with an apostrophe in the taxon name, then builds a candidate list.

The setup dialog enables **Only agree when needed** by default. With this option enabled, observations that are already Research Grade with the proposed provisional taxon as their community taxon are skipped; the same check is repeated immediately before each identification is posted.

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

iNaturalist doesn't allow automated identifications - so each observation / comments / previous ID's / photos are displayed in the main window so you can keep an eye on it and make sure it's doing the right thing. You can also use the Browse feature from the plan dialog to vet the observations before you turn it loose. iNaturalist doesn't allow automated identifications, so you need to either vet the photos beforehand, or watch the main window as it adds the identifications.

### Bulk Disagree to Taxon from URL

Use **Action -> Bulk disagree to taxon from URL...** after authenticating. Paste an iNaturalist observations URL. If it contains exactly one numeric `taxon_id`, that URL taxon becomes the source/current taxon safety check. A URL with no `taxon_id` (for example one filtered by an observation field such as `field:Provisional Species Name=...`) is also accepted; in that case there is no source-taxon safety check and the target identification is posted to every matching observation. A URL with more than one `taxon_id`, or a non-numeric one, is rejected as ambiguous. Then select the target taxon from autocomplete, enter the identification comment, preview candidates, and supervise one observation at a time.

The workflow posts a coarser ancestor target as an explicit disagreement, so an ancestor ID can move the community ID back from species to genus when iNaturalist accepts the disagreement. If the target taxon is not an ancestor of the source taxon, the setup dialog shows a strong warning and posts a normal conflicting ID instead of iNaturalist's explicit ancestor-disagreement flag, which supports same-rank corrections such as synonym-style species changes. It refreshes full observation details before preview and refreshes each observation again immediately before posting. It skips observations when the DNA Barcode ITS field is present by default, when you already have a current ID at the target taxon, when the source taxon no longer matches, or when the observation is on this workflow's permanent skip list.

After planning, the preview dialog includes **Browse photos...** for fast visual triage. The browser shows large photos for the planned observations, lets you skip candidates for the current run, permanently skip candidates for future bulk disagree runs, or immediately post an alternate ID with its own comment. Alternate IDs are refreshed before posting, can optionally be posted as explicit disagreements, and remove that observation from the planned bulk disagreement run after a successful post. Dry runs disable alternate-ID posting.

The workflow also skips observations where you already have a current ID at the selected target taxon, so running the same disagreement workflow again will not add duplicate genus disagreements. The DQA checkbox can also vote "ID is already as good as it can be." That vote is only attempted after the identification post succeeds and a refresh shows the community taxon now matches the selected target taxon, falling back to the current observation taxon only when no community taxon exists. If the observation stays at species level while the target is genus, the ID is counted separately and the DQA vote is skipped. If your existing "ID is already as good as it can be" vote is visible in the refreshed observation details, the workflow will not post it again. Dry runs never post identifications or DQA votes.

## Cache, Settings, and Debugging

Open **File -> Settings** to configure:

- **Disk image cache** directory and max size (default: `~/.cache/observation_workbench`, 2 GB)
- **In-memory image cache** limit (default: 256 MB)
- **Prefetch radius** - how many nearby observations to pre-load images for
- **UI scale** and **result list text scale** (change this if the text is too small or large on your screen)
- **Scroll speed** - multiplies in-app mouse-wheel scrolling, useful when WSL does not use the Windows wheel-lines setting
- Whether to include common names alongside scientific names

Open **Cache -> Cache info** to see current disk usage, or **Cache -> Clear all caches** to free disk space.

The **Debug** menu can show the log panel and switch verbose logging on and off.

## Architecture

- `main.py` and `start.sh` launch the app
- `observation_workbench/api/` handles iNat requests, auth, URL parsing, and JSON parsing
- `observation_workbench/services/` holds loading, image cache, prefetch, taxon summary, and identification workflow logic
- `observation_workbench/storage/` stores SQLite cache data and app settings
- `observation_workbench/ui/` contains the Qt window, filter bar, result list, viewer, metadata panel, taxon summary, auth dialog, bulk agreement dialogs, settings, and log panel

## Taxon Summary Panel

The panel calls `/observations/species_counts` with the same filters as the main query. For username studies it uses `ident_user_login=<username>`. For observations URL mode it reuses the URL query parameters. Results are cached in SQLite for 1 hour.

## iNaturalist API Notes

- Rate limiting is handled with retries around 429 and 503 responses. API errors get printed to the console.
- Photo URLs are derived from the square URL by swapping the size token.
- Taxon filters include descendants on the iNat API.
