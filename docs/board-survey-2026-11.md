# Marathon County board survey for the 12-board expansion (Nov. 17, 2026)

How twelve candidate boards publish agendas, minutes and recordings, checked
on each board's own site. Purpose: choosing the six boards the Herb Kohl
grant adds (12 covered by June 30, 2027; the first three by March 2027) and
sizing the adapters each needs. The proposal limits the six to boards no news
outlet covers; the Record-Review weekly covers Edgar, Athens and Marathon City.

**Headline:** none of the twelve uses a platform the engine already reads
(CivicClerk, AgendaCenter, BoardBook, Municode Meetings, Legistar, PrimeGov).
Every added board needs a new adapter. Only two have recordings with captions
or structured video (Mosinee School District, City of Mosinee); the rest
would be agenda-then-minutes, like Kronenwetter's minutes upgrade.

| # | Board | Platform | Listing | Minutes with votes | Recordings | Cadence | Covered by | Blockers |
|---|---|---|---|---|---|---|---|---|
| 1 | Village of Rothschild | PDFs on Revize | rothschildwi.com/government/village_agendas_recordings___minute_meetings/index.php | Yes: mover/second, tally | None | Board 2nd & 4th Mon 6:30 PM; 15 bodies | none found | Inconsistent filenames (date is in the row text); one scanned packet |
| 2 | Village of Rib Mountain | PDFs on Revize | ribmountainwi.gov/government/meetings/town_board.php, .../plan_commission.php | Yes: mover/second, tally | None | Board Tue 6 PM ~1st & 3rd; Plan Comm. 2nd Wed 5 PM; 13 bodies | none found | none |
| 3 | City of Schofield | unverified (Drupal; possibly CivicPlus/Municode municipal site) | schofieldwi.gov/meetings | unverified | unverified | 2nd Tue (unverified) | none found | Cloudflare challenge blocks every automated fetch; needs a look in a browser |
| 4 | City of Mosinee | Granicus IQM2 | mosineecitywi.iqm2.com/Citizens/Default.aspx | Yes: structured RESULT / MOVER / SECONDER / AYES with names | IQM2 Media Center (not YouTube), posted in batches | Council 2nd Mon 7:30 PM; committees same night; ~11 bodies | WAOW occasionally | Minutes lag 1-2 months; standing committees post none |
| 5 | Mosinee School District | Diligent Community | mosineeschools.community.highbond.com/Portal/MeetingTypeList.aspx | Yes: "Moved by / seconded by / Carried 9-0" | YouTube @msdvideos4406 (channel UCgCb2gUp4RrEiJDkIERDUMg), auto-captions | ~3rd Tue 6 PM; 3 committees | none found | Minutes JSON flagged IsPublic:false though served; confirm they're meant to be public |
| 6 | Village of Maine | Town Web repository (WordPress REST) | villageofmaine.org/agendas-minutes/ | Yes: mover/second, tally, roll call by name | Official none; an unofficial citizen stream | Board ~2nd & 4th Mon | none found | Cloudflare blocks non-browser user agents |
| 7 | Village of Marathon City | PDFs on WordPress | marathoncitywi.gov/government/meetings/ | Partial: no tallies seen | None | Board 1st & 3rd Wed 6 PM; 8 bodies | Record-Review | none |
| 8 | Town of Weston | HeyGov (Town Web) JSON | townofwestonwi.com/agendas-minutes/ | unverified | None | Town Board 3rd Tue | none found | Minutes are .doc/.docx |
| 9 | NTC District Board | PDFs on Drupal | ntc.edu/about/leadership-team/board-trustees/minutes-agendas | Yes | None (Zoom) | 9 meetings/yr, Tue 1 PM | none found | Minutes lag ~2 months |
| 10 | Village of Edgar | PDFs on Catalis | vil.edgar.wi.us/2025-2026-trustee | Yes | None | Board 2nd Mon 8 PM | Record-Review | Agenda lead time unknown |
| 11 | Village of Athens | PDFs on Catalis | athenswis.com/agendas-minutes | Yes, no tally | None | Board 4th Mon 7 PM | Record-Review | Agendas page was empty 6 days before a meeting |
| 12 | Marathon County Public Library board | County website | marathoncounty.gov/about-us/minutes-agendas/ | unverified | None | 3rd Mon noon | WSAU occasionally | marathoncounty.gov blocks non-residential fetches |

## Useful endpoints found

- **Mosinee School District (Diligent):** public JSON, no login:
  `/Services/MeetingsService.svc/meetings?from=…&to=…&loadall=false` and
  `/Services/MeetingsService.svc/meetings/{id}/meetingDocuments` (agenda as
  HTML; draft minutes as HTML).
- **City of Mosinee (IQM2):** `Detail_Meeting.aspx?ID=`,
  `FileOpen.aspx?Type=14&ID=` (agenda), `Type=1` (packet), `Type=12`
  (minutes); `Calendar.aspx?From=…&To=…` lists a year on one page; media RSS
  at `/Services/RSS.aspx?Feed=Media`. 2026 to date: 116 meetings, 106 with
  agendas, 29 with minutes.
- **Rib Mountain (Revize):** consistent paths with the date in the filename,
  e.g. `Document_Center/Agenda & Minutes/Town Board/2026/Agenda/01 261117 VB Agenda.pdf`.
- **Rothschild (Revize):** per-body pages, year-grouped rows of Agenda /
  Packet / Minutes links; take the date from the row text.
- **Village of Maine (Town Web):** `/wp-json/wp/v2/twd_repository` records
  with `meeting_date` and media IDs resolved via `/wp-json/wp/v2/media/{id}`.
- **Town of Weston (HeyGov):**
  `api.heygov.com/townofwestonwi.com/meetings?api=1&order=desc&limit=N`,
  including a parsed `agenda_items` array.

## Reading of it

Easiest to hardest to read automatically: Mosinee School District, City of
Mosinee, Town of Weston, Village of Maine, Edgar, Rib Mountain, Marathon
City, Rothschild, NTC, Athens, library board, Schofield.

Within the proposal's "no outlet covers them" group, a workable six:
Mosinee School District, City of Mosinee, Village of Rib Mountain, Village
of Rothschild, Village of Maine, Town of Weston — four adapters (Diligent,
IQM2, Revize serving two boards, Town Web/HeyGov serving two). Schofield
fits the group but can't be read from CI until someone confirms its platform
in a browser; if it must be one of the six it would likely have to be
fetched from the residential machine, like transcripts.

Caveats: lead times come from a few file timestamps per board; "none found"
for recordings and coverage means the search found nothing, not proof of
absence. The coverage claim must be re-checked for each board chosen.
