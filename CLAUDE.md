# CLAUDE.md — Gavel

## Project Overview
Config-driven civic meeting intelligence engine, productized from the WPR
marathon-meetings tracker (see RESHAPE.md for the plan and its July 2026
amendments). One `instance.json` defines an entire newsroom deployment:
jurisdictions, adapters, theme, committee maps. The engine scrapes agendas
and YouTube recordings, summarizes with Claude, and writes `data/*.json`
for a runtime-fetching React frontend. Deployed as one private repo per
customer newsroom, generated from this template repo, operated by Rowan.
WPR is Instance Zero and the permanent reference deployment.

## Tech Stack
- **Engine:** Python 3.12 package in `engine/` (yt-dlp, requests, anthropic,
  pdfplumber; optional faster-whisper locally)
- **Frontend:** React (Vite) in `frontend/` — Phase 2, fetches
  `instance.json` + `data/*.json` at runtime, theme via CSS variables
- **CI/CD:** GitHub Actions (Phase 2 port) — cron every 4 hours + manual
  dispatch, commits `data/` back, deploys to Pages

## Commands
- `python -m engine.pipeline` — incremental run (scrape → summarize →
  publish → upcoming). CI uses `--days 14`.
- `python -m engine.pipeline --source KEY` — one jurisdiction only
- `python -m engine.pipeline --url URL` — one specific YouTube video
- `python -m engine.pipeline --backfill` — all historical videos
- `python -m engine.pipeline --dry-run` — preview without processing
- `python -m engine.pipeline --publish-only` — rebuild `data/*.json` from
  existing state, no scraping/API calls (used for parity testing)
- `python -m engine.ingest_transcript --video-id ID --transcript FILE` —
  manually inject a pasted transcript for an agenda-only meeting
- `python -m engine.digest [--days N] [--out PATH] [--date YYYY-MM-DD]` —
  render the weekly newsletter PNG from data/upcoming.json, themed from
  instance.json (fonts via theme.font_files, footer accent via
  theme.digest_accent)
- `python -m engine.fetch_transcripts [IDs] [--all] [--push]` — residential
  transcript fetcher (operator machine only; CI IPs are caption-blocked).
  Saves to transcripts/ for the CI ingest step; --push commits, pushes, and
  triggers the workflow. BoardBook entries match recordings on the district
  channel; municode jurisdictions with an `audio_url` (e.g. Kronenwetter's
  SoundCloud) go through audio download + local Whisper (seeded with the
  jurisdiction's `officials` as a vocabulary hint).
- `python -m engine.ics [--out data/meetings.ics] [--days N]` — subscribable
  calendar feed (deterministic; America/Chicago only in v1, other timezones
  raise ConfigError)
- `python -m engine.roundup [--days-back N] [--dry-run]` — weekly newsletter
  draft (one Sonnet call, cost-ledgered purpose=roundup) → data/roundup.txt/.html
- `python -m engine.pages [--out-dir frontend/public]` — static indexable
  meeting pages + sitemap/robots. Gated by seo.pages_base_url; seo.noindex
  emits robots Disallow + per-page noindex (REQUIRED for parity-soak
  deployments). Pages persist after cards rotate off (permanent archive).
  Each footer carries the tracker permalink (`#m/<id>`, the hash the frontend
  reads) and, when `instance.corrections_email` is set, a "Request a
  correction" mailto; `refresh_archive_footers` brings pages already on disk
  up to date in place.
- `python -m engine.archive [--out data/archive.json] [--pages-dir
  frontend/public/meetings]` — the archive index: one short row per summarized
  meeting, newest first (`{id, source, title, date, committee, agendaOnly,
  overview, topics, issues, text, page}`), built from the sidecars with the
  same record builder as the displayed list. The frontend fetches it only when
  a reader searches or browses older meetings, and a row opens the meeting's
  static page. CI writes it after `engine.pages`, which also writes a page for
  any summarized meeting that has none. The committed file holds every board;
  `engine.visibility` stages the public copy without hidden boards. No API
  calls.
- `python -m engine.scrub [--check] [--pages-dir frontend/public/meetings]` —
  removes residents' home addresses from summaries/, data/meetings.json and
  archive pages, and re-applies every fix logged in corrections.json.
  Idempotent, no API calls. CI runs it after the last summarization and before
  the data file and pages are rebuilt. `--check` reports without writing.
- `python -m engine.overdue [--print-count] [--markdown FILE] [--today
  YYYY-MM-DD] [--source KEY]` — displayed meetings still agenda-only past
  their jurisdiction's `expected_lag_days` (closed sessions and jurisdictions
  with nothing to upgrade from are excluded). CI turns a nonzero count into
  one self-closing "⏰ Meeting summaries overdue" issue — several overdue from
  one jurisdiction usually means a matcher broke upstream.
- `python -m engine.costs --summary` — measured Claude spend (24h / 7d / 30d,
  by purpose and model) from data/costs.json. `--check --print-status
  [--markdown FILE]` is the spending alarm: TRIPPED when the last 24h exceed
  `SPEND_ALARM_DAY_USD` (default 2.00) or `SPEND_ALARM_DAY_CALLS` (default
  40). CI keeps one self-closing "💸 Unusual Claude API spend" issue.
- `python -m engine.watchdog --print-count [--markdown FILE]` — reads the run
  report's `stale_unprocessed` (videos the pipeline could not process for
  `STALE_VIDEO_HOURS`, default 36) and writes the body of the self-closing
  "🏠 Residential transcript fetcher may be down" issue.
- `python -m engine.tipsheet [--date YYYY-MM-DD] [--stdout]` — the reporter
  tip sheet → data/tipsheet.md + data/tipsheet.html (committed with the data;
  not copied onto the deployed site). No API calls; sorts data/meetings.json and
  data/upcoming.json into leads: votes and hearings on next week's agendas,
  failed/tabled/split votes with the members who voted no where the record
  names them (the portal's vote records when a meeting has them, else the
  recording or minutes), members with two or more no votes in 30 days,
  meetings where 3+ residents spoke, next week's and last week's meetings
  under each issue, named topics recurring across meetings, overdue upgrades,
  next week's meetings with no preview. Reports speaker counts only, never
  which residents spoke. CI writes it every run.
- `python -m engine.report [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--out FILE]
  [--json]` — the funder report for a period (default: last 180 days), counted
  from the state file, summary sidecars, corrections.json and the cost ledger:
  meetings by board and by what they were summarized from, residents speaking
  per meeting next to the prior equal-length period, corrections by kind,
  agenda previews written, spend. No API calls. Run on demand; reader numbers
  (Plausible) and alert subscribers are named in the report as not included.
  Hidden boards are left out of every count except spend unless
  `--include-hidden` is given.
- `python -m engine.coverage [--month YYYY-MM | --from D --to D] [--out FILE]
  [--json] [--include-hidden]` — the monthly coverage check (default: last
  calendar month). Per board and per meeting: was it on the tracker (and, for a
  board with `rules` / `fallback_rules`, was each regular meeting), was its
  agenda read before it was held, and is there a summary of what happened or
  is it waiting / overdue against `expected_lag_days`. "Agenda before" comes
  from `data/agendas_seen.json` (written by `engine.previews` the first time it
  reads a meeting's posted agenda; never pruned, committed with the data) or
  from a card made from the agenda on or before the day. A regular meeting is
  matched to a card only when the card's title has every word of the rule's
  name; anything else is listed under "Needs a look", never guessed. No API
  calls. `--write-dir data/coverage [--months 3]` writes one file per month
  (`<YYYY-MM>.md`), this month so far and the ones before it; CI runs that
  every run and commits the files with the data (not copied onto the deployed
  site).

- `python -m engine.alerts [--since ISO-TIME] [--out data/alerts.json]` — the alert
  feed that email alerts will be sent from: one entry the first time the
  tracker has an agenda preview for an upcoming meeting (`kind: agenda`) or a
  summary from a recording, minutes or vote records (`kind: result`), with the
  board, issues, a short text and links. `{entries: [...], seen: {id:
  first_seen}}`; `seen` keeps an entry from being made twice, entries leave
  after 45 days, hidden boards are left out. `--since` lists what entered
  after a time (`alerts.new_since`, what a send covers). It never reads or
  writes a subscriber's address: the repo and its logs are public, and who
  receives what is the email service's business. CI updates it every run. No
  API calls.
- `python -m engine.audit_votes [--json] [--root DIR]` — check every transcript
  summary's `votes` against the results the chair announced with a tally
  (`engine.vote_announcements`, found by code): lists announced results a
  summary is missing or contradicts and transcripts that were cut before
  reaching the model, then prints the IDs to redo. Redo them by running the
  workflow with `resummarize` set to those IDs (one Sonnet call each, ~$0.15
  for a long meeting; `RESUMMARIZE_IDS` forces them in `ingest_transcript`).
  Changes nothing itself.
- `python -m engine.rosters [--out assets/rosters.json]` — exports the
  officials from instance.json for tools that read name spellings without the
  whole config. CI writes and commits it every run; edit instance.json, never
  the export.
- `python -m engine.reels --snapshot FILE | --since FILE | --repo` — optional
  hand-off to a companion repo that drafts video reels (instance.json `reels`
  {repo, bodies}). CI snapshots the transcript-built meetings before the run
  and, after the commit reaches main, sends one `transcript-ingested`
  dispatch (video ID + commit) per newly transcript-built full meeting of the
  named bodies. Needs the `REELS_DISPATCH_TOKEN` secret; skips quietly
  without it. Committees, educational sessions and split meetings are left
  to a person.
- `python -m engine.fetch_portals [--source KEY] [--dry-run]` — residential
  fetcher for boards whose sites refuse cloud servers (`agendas.fetch_from:
  "residential"`). Runs the real adapter from the operator's machine and writes
  `portal_cache/<key>.json` (listings plus the agenda/minutes text of meetings
  still pending); `scripts/refresh-transcripts.ps1` runs it and commits the
  result. In CI `adapters.for_jurisdiction` hands the pipeline a `CachedPortal`
  that answers from that file with no network. A cache older than 72 hours
  reads as "no answer". See `engine/portal_cache.py`.
- `python -m engine.visibility --out frontend/public --preview-dir
  frontend/public/preview` — stages the frontend's runtime files. A
  jurisdiction with `"hidden": true` is scraped and summarized as usual but
  left out of everything readers get: the site's instance.json and data, the
  calendar feed, the newsletter image, the roundup draft, static pages and the
  sitemap. While anything is hidden, the full data is also written under
  `preview/`, and `<site>/preview/` serves the same app over it (banner,
  noindex, no analytics) for an editor to review. New boards are added
  hidden and published by removing the flag. The preview address is unlisted,
  not secret.
- `python -m engine.health [--print-count] [--markdown FILE] [--hours N]` —
  boards whose sites have not answered for `UNREACHABLE_HOURS` (12), from
  `source_health.json` (per board and job: "meetings", "upcoming"; an entry
  exists only while failing). CI keeps one self-closing "📡 A board's site is
  not answering the tracker" issue.
- Tests: `python -m unittest discover tests`, or `python -m pytest tests` to
  include the pytest-style `tests/test_vote_announcements.py`.

## Transcript summaries and announced votes

Transcript summaries see the whole transcript (`MAX_TRANSCRIPT_CHARS`,
300,000). Each records `_transcript_chars`, what the model saw. The transcript
prompt also lists the results the chair announced with a tally ("Motion passes
9 to2", "motion failed. 5 to six"), found by code over the whole transcript
(`vote_announcements.prompt_block`), and tells the model each belongs in
`votes` with that outcome and tally. A tally followed by a unit ("the past 3
to 5 years") or by where an earlier vote happened ("That passed five to zero
at finance") isn't a result; a self-correction takes the second tally; a pass
with yes ≤ no is left out as unclear. Bodies that don't say tallies aloud
still get their votes from the model alone.

## Agenda previews

The normal pipeline run attaches a `preview` to upcoming meetings in the next
`PREVIEW_DAYS` (8) whose agenda is posted: `{overview, items: [{item, why,
action: vote|hearing|discussion|report}], hearings: [str], issues?, publicComment,
closedSession, generated}`, one Haiku call each (`purpose=agenda_preview`), through
`engine.previews.attach` inside `update_upcoming(previews="generate")`.
`--publish-only` attaches cached previews and never calls Claude.

- `data/previews.json` is the cache: one entry per meeting (key =
  sha1 of source|date|name) holding the agenda text's hash. An unchanged
  agenda never costs a second call; an agenda is re-read at most every
  `PREVIEW_RECHECK_HOURS` (12); a meeting is previewed at most 3 times however
  often its agenda is revised; at most `PREVIEW_MAX_PER_RUN` (10) calls per run,
  the rest deferred to the next run.
- A Claude failure stops preview generation for that run and charges no
  attempt; an agenda that isn't posted yet is looked for again every run. A
  preview failure never holds up the upcoming list.
- Agenda text per platform is in `previews._AGENDA_TEXT`. `rule_schedule`
  jurisdictions have no posted agenda and get no previews; a new agendas
  adapter needs an entry there to get them.
- `hearings` lists every public hearing on the agenda by subject (not capped
  by the six `items`). The Upcoming panel opens with "Your chance to speak":
  meetings in the next 8 days with a hearing, and the ones with only a public
  comment period behind one line; the Recent panel points to it. A click fires
  the Plausible event `Chance To Speak` with `{source, kind: hearing|comment|
  pointer, days_before}`. Add it as a Goal in Plausible.
- The frontend's Upcoming panel expands a preview and fires the Plausible
  event `Agenda Preview` with `{source, days_before}` — the "agenda views
  before the meeting" number in funder reports. Add it as a Goal in Plausible.

## Plain language, issues, votes by member

Every summarizing prompt and the agenda preview carry
`summarize.PLAIN_LANGUAGE_RULES`: short sentences, everyday words, government
terms explained on first use, every name and number kept exact. Summaries
written before it read at about a college level.

`instance.json` `issues` (optional) is the newsroom's fixed list:
`[{key, label, about?}]`, validated at load. With it, every summary and
preview is filed under up to three keys (`issues`); keys not on the list are
dropped in code. This is what alerts "by issue" will subscribe to. `topics`
stays free-form and is not a substitute: its tags almost never repeat.
Changing a key orphans the summaries filed under it, so add or relabel rather
than rename.

`python -m engine.backfill_issues [--include-hidden] [--all] [--limit N]
[--dry-run]` files summaries written before the list existed: one Haiku call
each from the summary's own text (`purpose=issues_backfill`, about a sixth of
a cent), written into the sidecar and onto the record in data/meetings.json in
place, with nothing re-published. Default scope is the displayed list of
published boards; a summary with an `issues` key, even an empty one, is
skipped. In CI: run the workflow with `backfill_issues` set to `true`. More
than 40 calls in a day trips the spending alarm's call count, by design.

The transcript and minutes prompts also ask who voted no or abstained on each
vote and who was present, by name, and only when the record says so.

A body that votes on an electronic board never says who voted how (the chair
reads "Motion failed. 5 to six."), so a recording can't supply names. For a
jurisdiction with a video channel whose agendas adapter keeps vote records and
has `events_on` (civicclerk), `engine.portal_votes.attach` finds each recent
recording's portal event — its `doc_url` when that is a portal link, else the
one event held that day whose body words equal the recording title's
("Wausau City Council Meeting" and "Common Council Meeting" are both
{council}; no fuzzy fallback) — and, once the portal has recorded votes,
saves them as the summary's `_votes.json`. publish puts them on the record as
`civicItems`; the Votes tab shows them in place of the transcript's votes, and
archive pages list them under "Votes by member". No Claude call. Each
recording is looked up at most once a day (`portal_votes_checked` in the state
file) for `votes_upgrade_window_days` (30).

## Speaker counts

The transcript and minutes prompts return `publicCommentSpeakers` (residents
who spoke; null when it can't be counted). `engine.speakers.speaker_count`
uses that field, and for summaries written before it existed reads the count
out of the `publicComment` prose where one is stated. Unknown stays unknown:
reports leave those meetings out of the average rather than counting zero.
`publish` carries the field onto the meeting record when present.

## Architecture

```
engine/
├── config.py            instance.json loader/validator + env-level knobs
├── errors.py            named errors (ConfigError, AdapterStructureError,
│                        UpstreamUnavailableError, CorrectionsFileError,
│                        TranscriptAuthError, UnknownModelPricingError)
├── net.py               shared HTTP GET for adapters: 3 attempts, then
│                        UpstreamUnavailableError ("no answer")
├── state.py             processed_meetings.json / injected_meetings.json
├── costs.py             pricing constants + data/costs.json ledger +
│                        spending alarm CLI
├── claude.py            Anthropic retry wrapper — ALL calls flow through
│                        call_claude(), which records costs
├── summarize.py         the 5 prompts: transcript / agenda / agenda+votes /
│                        boardbook / minutes (region + newsroom from config)
├── transcripts.py       caption fetch chain (yt-transcript-api → yt-dlp →
│                        Whisper), parse_vtt
├── output.py            summary .md + _summary.json/_votes.json sidecars
├── publish.py           state + sidecars → data/meetings.json
├── upcoming.py          adapters → data/upcoming.json (last-known-good
│                        per jurisdiction when an upstream doesn't answer)
├── previews.py          agenda previews on upcoming meetings (Haiku, cached
│                        in data/previews.json by agenda hash)
├── speakers.py          public-comment speaker counts (field, else prose)
├── report.py            funder report for a period (no API calls)
├── archive.py           data/archive.json: every summarized meeting in brief
├── coverage.py          monthly coverage check: agenda before, summary after
├── alerts.py            data/alerts.json: what is new enough to tell a subscriber
├── portal_votes.py      each member's vote from the portal, for recorded meetings
├── tipsheet.py          weekly reporter tip sheet (no API calls)
├── watchdog.py          skipped_videos.json first-seen tracking → stale list
├── health.py            source_health.json: which boards' sites stopped answering
├── portal_cache.py      CachedPortal + look_back for boards fetched residentially
├── fetch_portals.py     residential fetcher that fills portal_cache/
├── overdue.py           agenda-only meetings past expected_lag_days
├── scrub.py             residents' address scrub + corrections.json applier
├── pipeline.py          orchestrator + CLI (python -m engine.pipeline)
├── ingest_transcript.py manual transcript injection tool
├── pdftext.py           shared pdfplumber helper
├── textutil.py          plain(): markup out of text pasted into portal item names
└── adapters/
    ├── __init__.py      registry — get_adapter(name), fail loud on unknown
    ├── youtube.py       channel listing (flat-playlist), title-date parsing,
    │                    LISTED_IDS / LISTED_DURATIONS per run, ytdlp_base()
    ├── civicclerk.py    OData API: votes, agenda text, upcoming, one day's
    │                    events (every query reads all pages: 15 events each)
    ├── legistar.py      Legistar Web API: items + roll calls, agenda item list, upcoming
    ├── primegov.py      PrimeGov public portal: HTML agenda text, upcoming (no votes)
    ├── boardbook.py     org-page scrape, agenda scrape, recording matcher
    ├── agendacenter.py  CivicPlus AgendaCenter scrape (Weston-style)
    ├── municode.py      Municode Meetings hub (Kronenwetter-style)
    └── rule_schedule.py nth-weekday recurring meeting projection;
                         covered_by_posted (rule/posted dedupe)
```

## Contracts

### instance.json (THE deployment contract)
Validated at pipeline start by `engine/config.py` — missing required
fields or unknown adapter names abort with a named ConfigError before any
network call. No defaults for required fields, no partial runs. See the
WPR `instance.json` in this repo for the reference schema; notable blocks
per jurisdiction:
- `video` (nullable): `{adapter: "youtube", channel_url, doc_pattern?,
  upgrade_window_days?, scan_descriptions_for_upcoming?}`
- `agendas`: `{adapter: civicclerk|legistar|primegov|boardbook|agendacenter|municode|
  rule_schedule, ...adapter-specific fields, days_ahead?, fallback_rules?}`.
  `rules` / `fallback_rules` entries are `{name, weekday 0-6 (0=Monday), nth
  1-5, time, url?}`, validated at load. A BoardBook fallback projection is
  dropped when a posted meeting on the same date covers it under different
  wording (the rule's name words, minus filler like "school board meeting",
  all appear in the posted name).
  A civicclerk, legistar or primegov jurisdiction with `video: null` is
  **portal-first**: its meetings are created from the portal's own listing
  (`list_recent`, ids `cc_`/`lg_`/`pg_<portal id>`), summarized from the agenda
  as soon as it's posted (last 30 days on a first run, or `--days`), and
  summarized again from the agenda and the votes once CivicClerk or Legistar
  records them (`votes_upgrade_window_days`, default 30; PrimeGov posts none).
  With a video channel, the same adapters supply agenda text and votes for
  each recording instead.
  civicclerk, legistar and primegov (`client`: the city's Legistar or PrimeGov
  name, e.g. `sanjose`, `cityofoviedo`) read a meeting's agenda from its portal
  page, its doc_url (`PORTAL_ADAPTERS`); civicclerk and legistar also give
  structured items with recorded votes (`VOTE_ADAPTERS`).
- `committee_map`: substring (lowercased, "and"→"&" normalized) → display
  name for badges
- `title_strip_patterns`: regexes removed from display titles (the source
  badge already identifies the jurisdiction)
- `officials` (optional): "Name (Role)" strings — authoritative name
  spellings injected into transcript/minutes prompts and used as Whisper
  vocabulary hints. **Update after each spring election.**
- `calendar_url` (optional): the jurisdiction's official calendar page
  (frontend FULL CALENDARS link)
- `group` (optional): the kind of body ("Cities", "Schools"), for the board
  filter. On a wider screen an instance with nine or more boards shows one
  chip per kind and the chosen kind's boards in a second row (a kind with one
  board shows that board); phones keep one scrolling row and smaller
  instances keep a chip per board. Without `group` the frontend reads the
  kind from the name: "... County", "City of ...", "Village of ...", "Town of
  ...", anything with "School", else "Other".
- `expected_lag_days` (optional, positive integer, default 7): how long a
  meeting here normally stays agenda-only before its recording, audio,
  minutes or votes arrive. `engine.overdue` flags anything later.

Top-level optional blocks: `instance.tracker_page_url` (reader-facing embed
page; ics/roundup/pages link there), `seo` {pages_base_url, noindex,
description, og_image, favicon, touch_icon — the last four are written into
index.html's head at build time by the `gavel-instance-head` plugin in
`frontend/vite.config.js`, for link previews and crawlers that don't run JS},
`analytics` {plausible_domain}, `sponsor` {prompt, button, email, subject}.

Other optional fields, all validated at load:
- `instance.ics_uid_domain` (default `"gavel"`; letters, digits, dots,
  hyphens): suffix of every calendar-feed UID (`<hash>@<domain>`).
  Subscribers' calendars key events on the UID — **never change it on a live
  feed**; an instance taking over an existing feed sets it to that feed's
  suffix (WPR: `marathon-meetings`).
- `instance.corrections_email` (an email address): where readers send
  correction requests. Without it, static pages and the frontend show no
  "Request a correction" link.
- `instance.credit_line` (`{text, url?}`): a funder's or sponsor's credit, shown
  under the tracker's coverage note, linked when `url` is given. Without it
  nothing is shown. The wording is the funder's to approve, so there is no
  default.
- `place_names` (top level, list of non-empty strings): municipality and state
  names that may trail a street address (", Wausau, WI"). `engine.scrub`
  removes up to two along with a resident's address; with none listed only
  the address itself is removed.

### Normalized PAST-MEETING record (element of data/meetings.json)
```
{id, source, title, date "March 12, 2026", shortDate "MAR 12", committee,
 duration "1h 20m"|null, url, docUrl, isAgendaOnly, badge "new"|null,
 overview, agenda: [{time, item}], discussions: [{item, body}],
 publicComment, actionItems: [str],
 topics: [str],                       # 3-5 Title Case tags from all prompts
 issues?: [key],                      # up to 3 keys from instance.json `issues`
 votes: [{item, motion, mover, second, outcome, tally,
          no: [name], abstain: [name]}],                  # transcript/minutes
 attendance?: {present: [name], absent: [name]},          # transcript/minutes
 civicItems?: [...]}
```
`no` / `abstain` name a member only when the record says how that member
voted (`summarize.member_vote_rules`); names that disagree with a numeric
tally are dropped in code (`summarize._finish`). Summaries written before
Nov. 2026 have neither those lists, `attendance` nor `issues`.
`badge` is "new" for `NEW_BADGE_HOURS` (24) after the meeting's `processed_at`
in the state file, the first time the tracker summarized it; a later upgrade
or re-publication does not make it new again. The frontend leaves out any
record whose `source` is not a jurisdiction in instance.json (one such record
used to blank the page) and shows a reload screen if drawing fails.
Newest-first, pruned to `instance.max_meetings`. `id` is the YouTube video
ID, `bb_<meeting_id>` (BoardBook), or `<id_prefix>_<guid>` (Municode).

### Normalized UPCOMING record (data/upcoming.json, keyed by jurisdiction)
```
{date "2026-07-08", time "5:00 PM"|"", name, url, source}
```
Adapters' `fetch_upcoming` raises UpstreamUnavailableError when the portal
doesn't answer (after `engine.net`'s 3 attempts); `engine.upcoming` then keeps
that jurisdiction's previously published meetings dated today or later
instead of writing an empty list. An empty list means the upstream answered
with no meetings. AgendaCenter is the exception: a failed page fetch falls
through to its `fallback_rules` projection.

### Cost ledger entry (data/costs.json)
```
{ts, meeting_id, jurisdiction, purpose, model, input_tokens, output_tokens, usd}
```
Priced per call from `PRICING_PER_MTOK` in `engine/costs.py` (the pipeline
is two-tier: Sonnet for transcript/minutes, Haiku for agenda-only). An
unknown model raises UnknownModelPricingError — never silently unpriced.

### Residents' privacy (binding)
Officials are named; a private resident only when the record states the name
clearly, never with a home address; students and minors never; no named
private person is tied to a health condition. The transcript and minutes
prompts carry the rules (`summarize.RESIDENT_PRIVACY_RULES`); `engine.scrub`
is the backstop for the address rule. It leaves an address that is the
subject of business ("Rezoning of 123 Elm Street") and the government's own
wording (`item`, `title`, `motion` keys).

### corrections.json (repo root, committed)
```
[{id, find, replace, date "2026-11-17", kind "correction"|"privacy", note}]
```
The public log of fixes. `id` is the meeting ID; `find` → `replace` is an
exact string replacement re-applied to that meeting's summary, mirror, data
record and static page on every run, so a re-summarization can't undo it.
`correction` = an error fixed; `privacy` = a name or detail withheld under the
residents rule. A malformed log raises CorrectionsFileError.

### Run report (agenda_only_report.json, written every non-dry run, not committed)
```
{agenda_only: [...], transcript_based: [...], total_processed,
 stale_unprocessed: [{id, title, source, age_hours}]}
```
`stale_unprocessed` comes from `skipped_videos.json`: the pipeline records
when it first saw each YouTube video it had neither a transcript nor an
agenda for. A video leaves the watch list when it is processed, listed in
`instance.skip_ids`, recorded in `transcripts/unavailable.json` (the
residential fetcher saw YouTube report it private or removed), or absent from
its channel's listing this run.

### Summary sidecar `_source` tag (drives upgrade passes)
`transcript` | `minutes` (outcome-based, final) vs `agenda` |
`agenda_with_votes` | `boardbook_agenda` (upgradeable). Upgrade passes:
caption retry (14 days), BoardBook recording match
(`video.upgrade_window_days`), Municode minutes
(`agendas.minutes_upgrade_window_days`).

## State files (idempotency — semantics unchanged from marathon-meetings)
- `processed_meetings.json` — what's been summarized (ID → metadata +
  summary path). Committed back by CI.
- `injected_meetings.json` — which IDs are already in data/meetings.json.
  Upgrade passes clear their IDs to force re-publication.
- `summaries/` — `.md` + `_summary.json` / `_votes.json` sidecars.
- `skipped_videos.json` — `{video_id: {first_seen, title, source}}`, the
  stuck-video watch list. Committed back by CI.
- `corrections.json` — the corrections log (hand-edited; CI commits it back
  unchanged so it travels with the data).

## Failure semantics (binding)
- Unknown adapter / invalid config → ConfigError at startup, exit non-zero.
- Claude stopped at max_tokens → TruncatedResponseError from `call_claude`
  (after the cost is ledgered). A cut-off JSON answer is never parsed or
  published. Transcript and minutes summaries get `RECORD_MAX_TOKENS` (8192).
- A meeting rebuilt this run that is too old for the displayed list (a
  `resummarize` or a backfill) is queued in `data/archive_updates.json` by
  publish; `engine.pages` rewrites its archive page and removes the queue.
- Scrape page fetched OK but zero rows matched → AdapterStructureError
  (HTML drift must never read as "no new meetings"). Other jurisdictions
  still complete; pipeline exits 1 AFTER writing outputs.
- Upstream didn't answer an upcoming fetch → UpstreamUnavailableError, caught
  once in `engine.upcoming`: that jurisdiction keeps its last-known-good
  future meetings and the run continues (an outage is not drift, exit 0).
  Structure drift also keeps last-known-good, then still exits 1.
- YouTube bot-block/cookie expiry → TranscriptAuthError naming the
  jurisdiction; reported in a loud block at the end; exit 1. Distinct from
  NoCaptionsError (→ agenda fallback, normal).

## Adding a new adapter type
1. Create `engine/adapters/<name>.py` implementing the relevant duck-typed
   functions (`list_meetings` / `fetch_agenda_text` / `fetch_upcoming`).
2. Register it in `engine/adapters/__init__._ADAPTER_MODULES` and in
   `VIDEO_ADAPTERS`/`AGENDA_ADAPTERS` in `engine/config.py` (with required-
   field validation).
3. If it's meeting-creating (synthesizes IDs), add its dispatch in
   `engine/pipeline.py` (`meeting_creating` group) and, if upgradeable,
   an upgrade pass.

## Engineering Rules (binding)
- Windows, PowerShell 5.1: `;` chaining, never `&&`. `python -m pip`,
  never bare `pip`.
- One correct path, no fallbacks-of-fallbacks, no backups. Fail fast and
  loud with named errors.
- Surgical, single-responsibility changes. No overengineering. Root causes,
  not symptoms.
- Secrets in GitHub repo secrets only (`ANTHROPIC_API_KEY`), never in
  config files or code.
- All Anthropic calls go through `engine.claude.call_claude` — a direct
  `anthropic.Anthropic()` call anywhere else is a cost-ledger leak and a
  review-blocking defect.

## Known deltas from marathon-meetings (intentional)
- `WHISPER_DAYS` cutoff env knob dropped (default-off, unused).
- Bare-incremental mode no longer special-cases Marathon County (the
  take-until-first-processed loop already prevents historical reprocessing;
  CI uses `--days 14` anyway).
- `--url` mode fails loud when the video isn't found in any configured
  channel (was: silently assume marathon).
- Transcript auth failures and scrape structure drift now exit 1 with named
  errors (was: indistinguishable from quiet runs).
- Overdue lags, the corrections address, the scrub's place names and the ICS
  UID suffix are instance.json values (were: constants in check_overdue.py,
  generate_pages.py, scrub_summaries.py, generate_ics.py).
- The overdue check skips jurisdictions with nothing to upgrade from (no
  video channel, vote records or minutes) — every production source has one.
- Cost ledger entries carry `purpose` + `meeting_id` instead of `script`; the
  spending-alarm issue breaks spend down by purpose and lists meetings
  summarized more than once in the window. The ledger is not pruned (was: 120
  days) and unknown models raise instead of being priced at the top rate.
- No caption-health counters in the run report and no cookie-expiry issue: CI
  runs with CAPTION_FETCH=false, and a bot-block is a TranscriptAuthError.
- BoardBook and Municode listings raise on an HTTP error instead of parsing
  the error page (was: silently zero meetings).

## Phase status
- Phase 1 (engine extraction): COMPLETE July 2026. Acceptance passed:
  byte-exact meetings.json parity (49/49 incl. order) and upcoming.json
  parity (6/6 sources) vs the marathon-meetings reference on identical
  state; digest renders pixel-identical to production. Includes
  engine/digest.py and engine/fetch_transcripts.py per the amended scope.
- Phase 2: `frontend/` port (runtime fetch + CSS variables from theme
  tokens) + Actions workflow (pipeline.yml) + side-by-side parity week on a
  separate Pages URL. Smoke checks stay WPR-only.
- Phase 3: template repo + ONBOARDING.md (incl. residential transcript-fetch
  infrastructure — CI IPs are caption-blocked regardless of cookies).
