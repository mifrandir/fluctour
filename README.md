# Fluctour

A local personal travel assistant with persistent research, alternatives, feedback,
source evidence and resumable Codex jobs. Talk to Codex to operate it; the CLI is
also available directly. Python 3.11+ on Linux/macOS and a signed-in Codex CLI are
the runtime prerequisites. There are no third-party Python runtime dependencies.

## Local workflow

```bash
python -m fluctour init
python -m fluctour doctor
python -m fluctour profile
python -m fluctour new 'Explore an extension to a London visit'
```

`new` prints a trip ID. Preferences, policy and personal requirements stay in the
main local store. Public source research uses an isolated, sanitized store:

```bash
python -m fluctour public-research 'Compare public HKG-LHR-MAD-HKG and HKG-LHR-YVR-HKG route information. Dates and payers unknown.' --for-trip TRIP_ID
python -m fluctour show TRIP_ID --evidence
python -m fluctour feedback TRIP_ID --max-cost 8000 --max-leave 1 --no-overnight
python -m fluctour feedback TRIP_ID 'Prefer a quieter neighborhood'
python -m fluctour status TRIP_ID
```

The public brief must contain only information intended for external research.
Private profiles are blocked from research prompts. `--for-trip` combines public
findings with private preferences locally and charges source jobs to that trip's
cumulative allowance. All nested stores share the main monthly ledger and worker
lock. Cached composition is idempotent and uses no inference. `attach-public
TRIP_ID PUBLIC_ID` attaches existing public results without starting a model.

`public-research --trip PUBLIC_ID --show --for-trip TRIP_ID` reads existing results.
`--steps 1` runs one stage; another invocation continues remaining stages.
`--refresh` requests a focused update. Direct `research TRIP_ID` is available for
public-only stores. Each stage starts a fresh Codex session with live web search,
structured JSON output, a read-only sandbox and a 15-minute timeout. Shell tools,
apps, browser/computer control, child agents, plugins and hooks are disabled.
The app-server transport overrides user configuration and verifies the effective
configuration before inference; saved CLI authentication and the default model apply.
No purchases, messages or private-account access are permitted.

Structured filters and recognized free-text requirements (leave, cost, dates and
overnight flights) filter saved alternatives immediately without model calls. Other
clauses remain explicitly pending. A requested activity change can queue public
research using supported activity names; private text is never sent externally. Filters accumulate; `feedback TRIP_ID
--clear` clears them. Unknown or incomplete costs and unknown leave cannot satisfy numeric filters.
If nothing matches, the CLI says so rather than inventing an alternative.
`show TRIP_ID --json` exports the dossier, assessments, constraints and usage.

## Preferences, calendar and evidence

`profile FIELD JSON_VALUE` changes a preference, for example
`profile home_airport '"HKG"'`. Initial defaults use Asia/Hong_Kong, HKD and a
provisional Monday–Friday 09:00–18:00 workweek; home airport and nationality are
unknown. Public holidays are never automatically treated as employer holidays.
`calendar_confirmed` and `timing_confirmed` record confirmed assumptions.

`policy path/to/policy.txt` imports relevant UTF-8 text, Markdown or reStructuredText
passages. Store policy files inside the ignored local data directory. Import marks
applicability unconfirmed. `profile policy_confirmed true` confirms applicability,
not employer approval of a trip. Private policy evidence uses an application-issued
digest and exact excerpt; changing the policy invalidates old policy evidence.

`windows --start YYYY-MM-DD --max-leave 1` computes diverse calendar opportunities
without model calls. It includes after-work departures, transfer/buffer time,
explicit holidays, nonstandard workweeks, timezones and recovery. Assumed flights
are always labeled provisional. Ordinary trip leave counts whole workdays overlapping
door-to-door absence. Half-day leave, overnight work shifts, calendar integration
and automatic booking reconciliation are not implemented. For company-paid triangles,
personal leave remains unknown until confirmed business-working dates can be
accounted for; business-working days must not be presented as personal leave.

Unknown flight times, cost, usable hours and overnight status are JSON null.
Overnight filtering also inspects timestamps for every leg, independently of the
model flag. Supplier claims always retain an independent-verification blocker:
model-authored source labels cannot certify exact flight/room inventory.

Freshness thresholds: price/availability six hours, schedules 24 hours, entry seven
days, policy/destination facts 30 days. These trigger review; they do not promise
that conditions cannot change sooner. A final live check is needed before purchase.

## Cathay triangles

`triangle-check path/to/quotes.json` makes a local conditional comparison using the
JSON contract documented in `fluctour/policy.py`. It accepts three connected,
chronological legs, any one personally paid leg, a JOINT quote for the other two,
and a comparable Cathay business roundtrip for the same business visit. Company
legs and baseline must be CX business, with current sourced quotes in one currency.
The personal leg can use another airline. Decimal arithmetic enforces **strictly
less**; equality fails. No per-leg fare allocation or currency conversion is invented.

The modeled allowances are two London visits and one New York visit per benefit
year. Usage and reset boundaries remain unknown until recorded; previous routes do
not prove remaining entitlement. A mathematical pass remains conditional: written
terms, fare-rule comparability, supplier inventory and employer approval are separate
unresolved checks. Supplied source URLs do not independently verify a quote.

## Context, recovery and budgets

SQLite is authoritative. A successful stage saves a complete dossier with sources,
timestamps, alternatives and remaining work. Public prompts carry active public
filters and all public user requirements, including requirements applied in prior
stages. Private requirements never enter that context. Oversized prompts fail
without silently truncating constraints. Raw web pages and full chat histories are
not replayed between stages.

Source leads are checkpointed during tool execution and included as unverified leads
on retries. Failed or interrupted stages do not replace a valid dossier. `recover
TRIP_ID` validates completed structured output after an import failure without
rerunning inference; completed-event usage is replayed into accounting. Incomplete
responses cannot become authoritative. Codex handles in-session context compaction;
the application bounds context between stages.

Defaults: 150,000 tokens and six sessions per trip, 500,000 tokens per UTC month.
Every launch needs headroom for observed usage plus a 50,000-token unknown-tail
reservation. Each stage requests a checkpoint at 20,000 observed tokens and
interrupts at 30,000, with lower thresholds when cumulative headroom is smaller.
Known usage counts input plus output, including cached input once. Live usage
updates the shared ledger; an unfinished session charges observed tokens plus
50,000 for its unknown tail. Completed usage remains known even if validation fails.
Provider usage events can arrive after a billed request, so these are spending
guards, **not hard provider token or monetary caps**; billing can overshoot.
All linked public jobs, fresh sessions,
failures, retries and refreshes share cumulative limits. Local composition is free.
A discovery stage can consume most of an allowance; pending stages remain pending.
Budget exhaustion pauses research and displays saved work. There is no automatic
quota-reset loop. Use the same data root to retain accounting. `limits TRIP_ID
--token-budget N --max-sessions N` explicitly changes a job allowance; the monthly
limit is a profile setting.

State defaults to `.fluctour/`, with restrictive permissions and Git exclusion.
It includes private policy, prompts and research event logs. `--data-dir` or
`FLUCTOUR_DATA_DIR` changes its location. No telemetry or notification delivery is
configured. Tests require no network, credentials or real model inference:

```bash
python -m unittest discover -s tests -v
```

## App and deployment direction

Run `python -m fluctour serve` and open **http://localhost:8765/**. The local
application saves trip briefs and feedback to SQLite. **+ Trip → Plan trip** queues
bounded Codex research in the background and shows progress, saved results and
Stop, Resume, Change request and Recheck controls. Stopping interrupts the worker
and preserves checkpoints; server shutdown also interrupts active research.
Destination, dates, any visibly selected via city and activities form the public research
request; private notes, employer policy and activity history stay local. Jobs share
cumulative session/token allowances and reuse completed checkpoints on retry.
The work-visit selector keeps business classification local and shows the public
London/New York route waypoint before submission. The local **Your rules** section
uses saved work hours and the selected benefit, without inferring remaining usage,
working dates or employer approval. Options for a previous request remain labeled
until matching research is saved.
Date inputs use **DD/MM/YYYY** with calendar selection; API and stored dates use ISO.
Open **Research** below an itinerary to inspect prompt templates, per-stage tasks,
execution settings, output schemas, session usage and shared token allowances.
Sessions record the exact public user prompt plus base/developer instruction layers
before inference. The harness records observed limits and any finalization request; historical
prompts that were never captured are explicitly unavailable. Inspection starts
no model session.

Select saved trips from the header. The CLI `new` command saves a draft without
starting research.
Dated proposals show activity timelines, transport, sourced hiking routes and beach
locations. Flight candidates distinguish reference schedules from verified seats.
Maps support dragging, mouse-wheel and pinch zoom, and fullscreen with Escape
to exit. Leaflet is bundled locally; only visible OpenStreetMap tiles are fetched.
Route source links are available with the research details.


The **Strava** button provides read-only OAuth connection, activity sync and a
private activity-history view. A one-time application registration at
[Strava API settings](https://www.strava.com/settings/api) supplies the client ID
and secret; Strava currently requires a subscription to register an API app.
Enter these in the local UI. Set the authorization callback domain to `localhost`.
The connection requests `activity:read_all`, refreshes expired tokens, and binds
history to the authenticated athlete so different accounts cannot silently merge.
Secrets stay in `.fluctour/strava.json` with mode 0600 and are excluded from browser
responses and external research. Recorded distances and elevation provide local
comparison metrics; past activity alone does not establish what the user enjoyed.
Sync reads at most 1,000 recent activities and reports incomplete results honestly.

`python -m fluctour preview` also writes `.fluctour/preview/index.html`. Opening
that file directly provides a read-only snapshot. Use the local server to save
trips and feedback. The server binds only to this computer and does not expose
the state directory or database file.

The future private responsive app targets **fluctour.miltz.me**. Android will use
concise proposal cards, a trip timeline and Web Push; desktop will add wider calendar
views and side-by-side alternatives. Both will share state and trip-specific chat.
Proposals should research likely feedback alternatives upfront, lead with usable
time, leave, cost and unresolved checks, and expose source details on demand.

Domain/DNS, hosted authentication, push, managed backups and unattended
scheduling are deferred while the local planning workflow is iterated. The hosted
runtime must be managed and continue with the laptop off. The domain is a deployment
target, not a live service. Planning, policy and persistence are independent of the
CLI so another delivery adapter can use the same rules.

Research uses the [official Codex app-server protocol](https://developers.openai.com/codex/app-server/).
