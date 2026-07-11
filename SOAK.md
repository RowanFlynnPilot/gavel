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
- [ ] webcal subscribe + `meetings.ics` served at site root
- [ ] `digest.png` renders identically to production's
- [ ] Bookmark button + hint, hash permalinks (`#m/<id>`), compact
      (<960px) and two-pane layouts
- [ ] `robots.txt` Disallow + SPA noindex meta + per-page noindex still
      present (soak safety — do NOT remove until cutover)

## Transcript path
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
