# Phase 2 Parity Soak — checklist

Per RESHAPE.md Phase 2 acceptance: **side-by-side visual and behavioral
parity for one full week** between the gavel deployment and production.
Only then: swap the WordPress iframe src, flip `seo.noindex` to false,
archive marathon-meetings.

- **Production (reference):** https://rowanflynnpilot.github.io/marathon-meetings/
  (embedded at https://wausaupilotandreview.com/central-wisconsin-meeting-tracker/)
- **Gavel (candidate):** https://rowanflynnpilot.github.io/gavel/
- **Soak start:** July 11, 2026

## One-time setup (remaining)
- [ ] `gh secret set ANTHROPIC_API_KEY --repo RowanFlynnPilot/gavel`
      (user-side; until set, runs deploy fine but go red at the
      fail-loud "Surface pipeline failures" step)
- [ ] Optional: `gh secret set YOUTUBE_COOKIES --repo RowanFlynnPilot/gavel`
- [ ] **Reset the ANTHROPIC_API_KEY secret.** As of Nov 17, 2026 every Claude
      call in gavel's scheduled runs returns `401 invalid x-api-key`; the last
      meeting gavel summarized was Sept 28 (`processed_meetings.json` and
      `data/costs.json` have not changed since). The runs stay green because
      per-source and per-video failures are logged and the pipeline exits 0.
      Parity cannot hold until this is fixed.

## Daily (automated-friendly)
- [ ] `python tools/parity_diff.py` → PARITY OK
      (transient skew allowed within one 4-hour cron cycle; re-run after
      both crons fire before treating a diff as real)
- [ ] Both repos' latest scheduled runs: gavel red ONLY for a reason
      production also hit (or missing secret); no gavel-only named errors
      (TranscriptAuthError / AdapterStructureError / ConfigError)
- [ ] New meetings summarized by production appear on gavel within one cycle

## Visual/behavioral (spot-check across the week, all six jurisdictions)
- [ ] Masthead, tagline, header band, source accent colors, avatars
- [ ] Filter chips: All Sources / Marathon County / Wausau / Weston /
      School Board / Kronenwetter / DC Everest
- [ ] Detail tabs incl. Votes (non-CivicClerk) and civicItems (Wausau)
- [ ] Topic chips → full-text search across jurisdictions
- [ ] Upcoming panel grouping, FULL CALENDAR links per filter
- [ ] webcal subscribe + `meetings.ics` served at site root; event UIDs
      identical to production's (`instance.ics_uid_domain` is
      `marathon-meetings`, so subscribers' events keep their identity at
      cutover)
- [ ] `digest.png` renders identically to production's
- [ ] Bookmark button + hint, hash permalinks (`#m/<id>`), compact
      (<960px) and two-pane layouts
- [ ] `robots.txt` Disallow + SPA noindex meta + per-page noindex still
      present (soak safety — do NOT remove until cutover)

## Transcript path
Ports of the Aug-Nov 2026 production additions (required before cutover —
the residential fetcher points at gavel after the swap). Context: YouTube
blocks cloud IPs from captions AND per-video metadata regardless of cookies;
CI runs with CAPTION_FETCH=false, so the residential fetcher is the only
transcript path and must be watched.

Engine and CI (ported Nov 17, 2026; unit-tested, not yet run in CI):
- [x] Stuck-video watchdog: `skipped_videos.json` first-seen tracking
      (`engine/watchdog.py`) → `stale_unprocessed` in the run report →
      "🏠 Residential transcript fetcher may be down" issue step. Dismisses
      videos in `instance.skip_ids`, in `transcripts/unavailable.json`, or
      gone from their channel listing (`youtube.LISTED_IDS`).
- [x] Upcoming feed: 3-attempt fetches (`engine/net.py`) and last-known-good
      per jurisdiction when an upstream doesn't answer (`engine/upcoming.py`).
- [x] Overdue-upgrade check (`engine/overdue.py`, lags in instance.json
      `expected_lag_days`) replacing the standing agenda-only issue.
- [x] DC Everest 3rd-Wednesday `fallback_rules` entry + rule/posted dedupe
      (`rule_schedule.covered_by_posted`).
- [x] Running-time backfill from channel listings
      (`pipeline.fill_missing_durations`, `youtube.LISTED_DURATIONS`).
- [x] Quiet runs: the engine never had production's early return; the run
      report is now written on every non-dry run (it used to be skipped when
      nothing was agenda-only).
- [x] Spending alarm (`python -m engine.costs --check`) + workflow step.
- [x] Residents' privacy (marathon-meetings 1564236): prompt rules, address
      scrub + corrections applier (`engine/scrub.py`, `corrections.json`,
      `place_names`), workflow step before publish.
- [x] Static pages: `#m/<id>` permalink, "Request a correction" link
      (`instance.corrections_email`), archive footer refresh.
- [x] Calendar feed UIDs end in `@marathon-meetings`
      (`instance.ics_uid_domain`), matching production.
- [x] yt-dlp launcher falls back to `python -m yt_dlp`
      (`youtube.ytdlp_base`; marathon-meetings 6f22d67) — the residential
      machine has a module-only install.
- [x] Workflow: frontend `BASE_PATH` from the repository name; commit
      `skipped_videos.json` and `corrections.json`; rebase before push.

Residential fetcher and ingest (`engine/fetch_transcripts.py`,
`engine/ingest_transcript.py`, `scripts/`):
- [ ] Gap sweep of unprocessed channel videos
      (`find_unprocessed_channel_videos` + `GAP_SWEEP_DAYS`),
      `transcripts/<id>.meta.json` metadata sidecars (incl. `duration`), and
      sidecar-aware resolution in ingest.
- [ ] Kronenwetter SoundCloud `YYYY MM DD` title dates + duplicate-upload
      collapsing; private/removed videos recorded in
      `transcripts/unavailable.json`; `MAX_AUDIO_JOBS` with audio jobs last;
      the rewritten `scripts/refresh-transcripts.ps1` (log file, weekly
      yt-dlp update, commit-everything-under-transcripts push).

Frontend (`frontend/`):
- [ ] Multi-part meeting grouping (one card per meeting + part switcher in
      the detail view; PARTS_BY_ID in marathon-meetings.jsx), "CLOSED
      SESSION" labels and notice, the detail tab-reset guard (stale Votes tab
      rendered blank), and the "Request a correction" link in the detail view.

First CI run after these ports (expect, then confirm):
- [ ] The scrub step cleans what gavel already published: a local
      `python -m engine.scrub --check` on Nov 17 reported 27 summaries, 28
      mirrors, 6 displayed meetings and 5 pages (12 corrections on file).
- [ ] The static-pages step rewrites the footer of every archive page
      (`#<id>` → `#m/<id>` + correction link).
- [ ] While the residential fetcher still pushes to marathon-meetings, gavel
      receives no transcripts: expect the "🏠 Residential transcript fetcher
      may be down" and "⏰ Meeting summaries overdue" issues to open in the
      gavel repo and stay open until cutover step 6. They close themselves.
- [x] Port the Oct 23, 2026 production fix (marathon-meetings dfe04a9):
      CivicClerk agenda text via `GetMeetingFileStream(plainText=true)`;
      `GetMeetingFile` (blob URL) returns 404, which left agenda-only
      CivicClerk meetings with no text (`engine/adapters/civicclerk.py`).
- [ ] Residential fetcher pointed at gavel repo for one cycle
      (`python -m engine.fetch_transcripts --all --push`) upgrades an
      agenda-only meeting end to end

## Cutover (after a clean week, in order)
1. Set `seo.noindex: false` in instance.json; commit.
2. Remove the noindex checklist item above; redeploy (dispatch workflow).
3. Swap the WordPress iframe src on the tracker page to the gavel URL.
4. Update Plausible expectations (same property; traffic continuity).
5. Disable marathon-meetings' scheduled workflow; archive the repo.
6. Point the residential scheduled task
   (`MarathonMeetings-RefreshTranscripts`) at the gavel repo.
