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
  SoundCloud) go through audio download + local Whisper.

## Architecture

```
engine/
├── config.py            instance.json loader/validator + env-level knobs
├── errors.py            named errors (ConfigError, AdapterStructureError,
│                        TranscriptAuthError, UnknownModelPricingError)
├── state.py             processed_meetings.json / injected_meetings.json
├── costs.py             pricing constants + data/costs.json ledger
├── claude.py            Anthropic retry wrapper — ALL calls flow through
│                        call_claude(), which records costs
├── summarize.py         the 5 prompts: transcript / agenda / agenda+votes /
│                        boardbook / minutes (region + newsroom from config)
├── transcripts.py       caption fetch chain (yt-transcript-api → yt-dlp →
│                        Whisper), parse_vtt
├── output.py            summary .md + _summary.json/_votes.json sidecars
├── publish.py           state + sidecars → data/meetings.json
├── upcoming.py          adapters → data/upcoming.json
├── pipeline.py          orchestrator + CLI (python -m engine.pipeline)
├── ingest_transcript.py manual transcript injection tool
├── pdftext.py           shared pdfplumber helper
└── adapters/
    ├── __init__.py      registry — get_adapter(name), fail loud on unknown
    ├── youtube.py       channel listing (flat-playlist), title-date parsing
    ├── civicclerk.py    OData API: votes, agenda text, upcoming
    ├── boardbook.py     org-page scrape, agenda scrape, recording matcher
    ├── agendacenter.py  CivicPlus AgendaCenter scrape (Weston-style)
    ├── municode.py      Municode Meetings hub (Kronenwetter-style)
    └── rule_schedule.py nth-weekday recurring meeting projection
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
- `agendas`: `{adapter: civicclerk|boardbook|agendacenter|municode|
  rule_schedule, ...adapter-specific fields, days_ahead?, fallback_rules?}`
- `committee_map`: substring (lowercased, "and"→"&" normalized) → display
  name for badges
- `title_strip_patterns`: regexes removed from display titles (the source
  badge already identifies the jurisdiction)

### Normalized PAST-MEETING record (element of data/meetings.json)
```
{id, source, title, date "March 12, 2026", shortDate "MAR 12", committee,
 duration "1h 20m"|null, url, docUrl, isAgendaOnly, badge "new"|null,
 overview, agenda: [{time, item}], discussions: [{item, body}],
 publicComment, actionItems: [str], civicItems?: [...]}
```
Newest-first, pruned to `instance.max_meetings`. `id` is the YouTube video
ID, `bb_<meeting_id>` (BoardBook), or `<id_prefix>_<guid>` (Municode).

### Normalized UPCOMING record (data/upcoming.json, keyed by jurisdiction)
```
{date "2026-07-08", time "5:00 PM"|"", name, url, source}
```

### Cost ledger entry (data/costs.json)
```
{ts, meeting_id, jurisdiction, purpose, model, input_tokens, output_tokens, usd}
```
Priced per call from `PRICING_PER_MTOK` in `engine/costs.py` (the pipeline
is two-tier: Sonnet for transcript/minutes, Haiku for agenda-only). An
unknown model raises UnknownModelPricingError — never silently unpriced.

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

## Failure semantics (binding)
- Unknown adapter / invalid config → ConfigError at startup, exit non-zero.
- Scrape page fetched OK but zero rows matched → AdapterStructureError
  (HTML drift must never read as "no new meetings"). Other jurisdictions
  still complete; pipeline exits 1 AFTER writing outputs.
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
