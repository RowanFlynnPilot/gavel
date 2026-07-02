# RESHAPE.md — Productize marathon-meetings → Gavel

**Working product name:** Gavel (rename is a find-replace; do not block on naming)
**Source repo (reference implementation):** `RowanFlynnPilot/marathon-meetings`
**Target repo:** this project folder (the engine). `marathon-meetings` remains live in production until Phase 2 parity is confirmed, then it is retired.

---

## Mission

Transform the WPR meeting tracker from a single hardcoded instance into a **config-driven civic meeting intelligence engine** that can be deployed as a managed instance for any small newsroom. Market validation: Glen Nelson Center (Press Forward partner) funded Hamlet on this exact premise. Our wedge: Hamlet targets big markets; Gavel targets the one-reporter, zero-engineer newsroom at $50–150/month per municipality, operated by us as a managed service.

**Business model (fixed, do not redesign):** one private GitHub repo per customer newsroom, generated from a template, operated by Rowan. No self-serve. No billing system (invoiced manually). WPR is Instance Zero and the permanent reference deployment.

---

## What Exists Today (marathon-meetings)

- **Pipeline:** Python scrapers (yt-dlp transcripts, CivicClerk OData, BoardBook, AgendaCenter HTML) → Claude summarization via Anthropic API → JSON in `summaries/` → `inject_meetings.py` rewrites a `MEETINGS` array inside `marathon-meetings.jsx` → Vite build → GitHub Pages. Actions cron every 4 hours.
- **Jurisdictions (4, hardcoded):** Marathon County, City of Wausau, Village of Weston, Wausau School Board.
- **State files:** `processed_meetings.json` (scraper state), `injected_meetings.json` (injection tracking). Summaries as `{video_id}_summary.json` + `{video_id}_votes.json` sidecars.
- **Upcoming meetings:** per-source logic (rule-based nth-weekday, CivicClerk OData, AgendaCenter scrape, BoardBook scrape), stored in separate hardcoded JSX arrays.
- **Frontend:** single-file React component, inline styles, WPR branding baked in.

### Structural problems blocking productization

1. **Data-as-code:** `inject_meetings.py` rewrites source code to publish data. Cannot scale to N instances. Root cause, not symptom — this pattern must be removed, not wrapped.
2. **Jurisdictions hardcoded** in the summarizer, `update_upcoming.py`, and the JSX (SOURCE_CONFIG, per-source arrays, committee colors).
3. **Branding hardcoded:** WPR teal/cream/Playfair is inline throughout the component.
4. **No cost telemetry:** we have no per-meeting API cost number, which we need for the pricing floor.

---

## Target Architecture

```
gavel/                          ← engine repo (this folder)
├── engine/                     ← Python package
│   ├── adapters/
│   │   ├── youtube.py          ← transcript acquisition (yt-dlp, cookie file)
│   │   ├── civicclerk.py       ← OData API
│   │   ├── boardbook.py
│   │   ├── agendacenter.py
│   │   └── rule_schedule.py    ← nth-weekday recurring meeting rules
│   ├── summarize.py            ← Anthropic API, summary + votes extraction
│   ├── pipeline.py             ← orchestrator: config in → data/*.json out
│   └── costs.py                ← per-meeting token/cost ledger
├── frontend/                   ← React/Vite, config + data fetched at runtime
├── instance.json               ← THE contract (see below)
├── data/                       ← pipeline output, committed by Actions
│   ├── meetings.json
│   ├── upcoming.json
│   └── costs.json
├── .github/workflows/pipeline.yml
├── CLAUDE.md
└── ONBOARDING.md               ← customer instance runbook (Phase 3)
```

**Deployment model:** the engine repo doubles as the GitHub **template repo**. A customer instance = new private repo from template + their `instance.json` + repo secrets. Same Actions workflow everywhere. No divergent code per customer — instance differences live only in `instance.json`.

### The instance.json contract

One file defines an entire deployment. Schema by example (WPR / Instance Zero):

```json
{
  "instance": {
    "name": "Marathon County Meeting Tracker",
    "newsroom": "Wausau Pilot & Review",
    "newsroom_url": "https://wausaupilotandreview.com",
    "timezone": "America/Chicago",
    "max_meetings": 30
  },
  "theme": {
    "primary": "#4aaba7",
    "primary_dark": "#3e847a",
    "background": "#F7F3EC",
    "ink": "#1A1209",
    "divider": "#E0D8CC",
    "font_headline": "Playfair Display",
    "font_body": "Source Sans 3",
    "font_data": "JetBrains Mono",
    "logo": "assets/logo.jpg"
  },
  "jurisdictions": [
    {
      "key": "marathon",
      "name": "Marathon County",
      "accent": "#4aaba7",
      "video": { "adapter": "youtube", "channel": "@marathoncountyboardmeetings" },
      "agendas": { "adapter": "rule_schedule", "rules": [ /* nth-weekday committee rules */ ] }
    },
    {
      "key": "wausau",
      "name": "City of Wausau",
      "accent": "#C0392B",
      "video": { "adapter": "youtube", "channel": "@CityofWausauMeetings" },
      "agendas": { "adapter": "civicclerk", "portal": "wausauwi.portal.civicclerk.com" }
    },
    {
      "key": "weston",
      "name": "Village of Weston",
      "accent": "#1F4E79",
      "video": null,
      "agendas": { "adapter": "agendacenter", "url": "https://westonwi.gov/agendacenter" }
    },
    {
      "key": "school_board",
      "name": "Wausau School Board",
      "accent": "#8E6C1E",
      "video": null,
      "agendas": { "adapter": "boardbook", "org_id": 1360 }
    }
  ],
  "committee_styles": { /* migrated from COMMITTEE_STYLES */ }
}
```

Contract rules:
- Validate `instance.json` at pipeline start against a schema. Invalid config = immediate exit with a named error. No defaults for required fields, no partial runs.
- Adapters are looked up by name from a registry dict. Unknown adapter name = fail loud.
- The frontend fetches `instance.json` and `data/*.json` at runtime. Zero build-time data injection. Theme tokens map to CSS variables on the root element.

---

## Phases

### Phase 1 — Extract the engine

1. Port scraper logic from `marathon_meeting_summarizer.py` / `update_upcoming.py` into the adapter modules. Each adapter has a single responsibility: given its config block, return normalized meeting records.
2. Define the **normalized meeting record** (one shape for past meetings, one for upcoming) — document both in `CLAUDE.md`. This is the internal contract between adapters, summarizer, and frontend.
3. `pipeline.py` orchestrates: load config → run adapters per jurisdiction → summarize new meetings → write `data/meetings.json`, `data/upcoming.json` (prune to `max_meetings`) → append to `data/costs.json`.
4. `costs.py`: every Anthropic API call logs `{meeting_id, jurisdiction, input_tokens, output_tokens, model, usd}` to `data/costs.json`. Compute USD from current published Haiku pricing, stored as constants at top of file.
5. Preserve idempotency: `processed_meetings.json` semantics carry over unchanged.
6. CLI: `python -m engine.pipeline` with `--source KEY`, `--url URL`, `--backfill`, `--dry-run`. Same flags as today, `argparse`, one entry point.

**Acceptance:** running the pipeline against the WPR `instance.json` on a machine with existing state produces `data/meetings.json` whose records match the current MEETINGS array content (field-for-field, allowing for the new normalized shape).

### Phase 2 — Frontend + Instance Zero parity

1. Port `marathon-meetings.jsx` into `frontend/`, replacing all hardcoded data (MEETINGS, *_UPCOMING arrays, SOURCE_CONFIG, COMMITTEE_STYLES, brand colors) with runtime fetch of `instance.json` + `data/*.json`. Keep single-file component, inline styles, but styles read CSS variables set from theme tokens.
2. Port the Actions workflow: pipeline → commit `data/` → build → deploy. Commit message format: `chore: update meetings data [skip ci]`. Cron every 4 hours + manual dispatch with backfill input.
3. Deploy the Gavel WPR instance to its own Pages URL alongside the live marathon-meetings. Run both for at least one full week of cron cycles.

**Acceptance:** side-by-side visual and behavioral parity with the production widget across all four jurisdictions, including upcoming-meeting refresh and new-meeting ingestion. Only then: swap the WordPress iframe src, archive `marathon-meetings`.

### Phase 3 — Licensable packaging

1. Mark the repo as a GitHub template. Verify a fresh instance can be stood up from template + a second `instance.json` (use a fictional two-jurisdiction config as the test fixture — one CivicClerk, one rule_schedule).
2. Write `ONBOARDING.md`: the runbook for launching a customer instance. Must cover: information to collect from the newsroom (jurisdictions, YouTube channels, agenda platform URLs, brand tokens, logo), repo creation, secrets (`ANTHROPIC_API_KEY`), YouTube cookie provisioning, Pages setup, iframe embed snippet, expected time-to-launch.
3. Update `CLAUDE.md` for the engine repo: architecture, contracts, adapter registry, how to add a new adapter type, engineering rules.

**Acceptance:** a fresh instance reaches a live Pages deployment following only `ONBOARDING.md`, with no code changes.

---

## Non-Goals (v1 — do not build)

- Multi-tenant database or shared backend. Instances are isolated repos by design.
- Billing, accounts, auth, or self-serve signup.
- Full-text search across meetings (Hamlet's territory; revisit post-revenue).
- Legistar/Granicus adapter (largest coverage gap, but no customer needs it yet — backlog, and the adapter registry makes it a bounded add later).
- Email digests, alerts, or notification features.
- Whisper/audio transcription. Transcript source remains YouTube captions via yt-dlp.

---

## Known Risks (encode, don't solve speculatively)

1. **YouTube cloud-IP blocking.** yt-dlp transcript extraction from Actions requires cookie injection and periodic cookie refresh. This is the #1 operational scaling cost per instance. Phase 3 `ONBOARDING.md` must document the cookie provisioning and refresh procedure explicitly. When a transcript fetch fails on auth, fail loud with a named error identifying which instance/jurisdiction needs cookie refresh — this failure must be distinguishable from "no new meetings."
2. **Agenda platform HTML drift.** AgendaCenter is HTML-scraped and will break silently upstream. Adapters must throw on unexpected structure, never return empty-and-continue.
3. **API cost per customer.** Unknown until `costs.json` accumulates real data. Pricing floor decision is deferred to Rowan after ~1 month of Instance Zero telemetry.

---

## Engineering Rules (unchanged, binding)

- Windows, PowerShell 5.1: `;` chaining, never `&&`. `python -m pip`, never bare `pip`.
- One correct path, no fallbacks, no backups. Fail fast and loud with named errors.
- Surgical, single-responsibility changes. No overengineering. Fix root causes, not symptoms.
- Evidence-based debugging: minimal targeted logging before writing fixes.
- Secrets in GitHub repo secrets only; never in config files or code.
- Python 3.12+ per current repo; project lives at `C:\Users\rpfly\Projects\`.

---

## Amendments — July 1, 2026 (pre-Phase-1 prep)

Decisions confirmed by Rowan and corrections against the current state of `marathon-meetings`. Where this section conflicts with the text above, this section wins.

### Decisions

- **Engine location:** new sibling repo at `C:\Users\rpfly\Projects\gavel`. `marathon-meetings` stays untouched and live until Phase 2 parity.
- **Engine scope additions** (not in the target architecture above):
  - `generate_digest.py` → engine module, themed from `instance.json` (per-instance newsletter feature).
  - The `[sb-video]` and `[kw-minutes]` upgrade passes (agenda-only → transcript/minutes re-summarization) → pipeline stages. Required for WPR parity.
  - `fetch_transcript.py` + the residential scheduled-task pattern → formalized as **shared operator infrastructure** serving all instances.
  - `smoke_check.py` stays WPR-only.

### Corrections (doc was written against an older repo state)

1. **Data-as-code is half-solved already.** `inject_meetings.py` now writes `src/data/meetings.json`; the JSX imports JSON at build time. Remaining Phase 2 gap: build-time import → runtime fetch.
2. **Six jurisdictions, not four.** Kronenwetter (Municode Meetings hub, `kw_` IDs, ADA HTML agendas) and DC Everest (BoardBook org 1315) exist. Adapter list needs `municode.py`; `boardbook.py` must be multi-org (1360 Wausau, 1315 DC Everest — see `BOARDBOOK_DISTRICTS` in config.py).
3. **`costs.py` must price per-call model.** The pipeline is two-tier by design: Sonnet for full-transcript summaries, Haiku for agenda-only (config.py `CLAUDE_MODEL` / `CLAUDE_MODEL_AGENDA`). Haiku-only pricing would understate the pricing floor.
4. **Risk #1 is wrong about cookies.** CI IPs are caption-blocked by YouTube regardless of cookie injection. The working transcript path is `fetch_transcript.py` running as a scheduled task on a residential machine. Architectural consequence: customer instances' Actions workflows cannot self-serve transcripts; transcript acquisition is operator-side shared infrastructure, and ONBOARDING.md must document it that way (not as a per-instance cookie-refresh chore).
5. **Example `instance.json` had wrong video configs.** Weston has a YouTube channel (`@WestonWI`). Wausau School Board recordings live on channel `UCw63l8UWL_hpDtUy9IBIVvw` (matched by the `[sb-video]` pass, 45-day window). DC Everest requires yt-dlp extractor args `youtube:player_client=default,android`.
6. **Theme tokens must match production for Instance Zero** — Phase 2 acceptance is visual parity. The example above diverged (Source Sans 3 vs. Lora body, wrong Weston/school-board accents) and omitted the Bebas Neue display face. Corrected draft lives at `gavel/instance.json`. Any rebrand is a separate post-parity decision.
7. **`rule_schedule` rules for Marathon County** are hardcoded in `update_upcoming.py` and must be ported into `instance.json` during Phase 1 (draft ships with an empty `rules` array). Weston is AgendaCenter scrape **plus** rule-based fallback; School Board is BoardBook **plus** rule-based fill (2nd Monday Regular, 4th Monday Ed/Op).

---

## Open Items for Rowan (not blockers for Phases 1–2)

1. Confirm or replace the working name "Gavel" before Phase 3 (repo template name is customer-facing).
2. Pricing floor: revisit after one month of `costs.json` data from Instance Zero.
3. Press Forward narrative asset: once Phase 3 acceptance passes, the ONBOARDING.md + a second live instance is the proof artifact for the infrastructure-grant "operations" priority ("tools and technology to become more efficient").
