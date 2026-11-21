"""Claude summarization: transcript, agenda, agenda+votes, BoardBook, minutes.

Prompts are ported verbatim from marathon-meetings, parameterized by the
instance's region and newsroom (instance.json) instead of hardcoded Wausau
strings. All calls flow through engine.claude.call_claude, which records
token usage in the cost ledger.

Model tiering (binding, cost-driven):
  CLAUDE_MODEL         transcript + minutes summaries (real outcomes)
  CLAUDE_MODEL_AGENDA  agenda-only / BoardBook summaries (scheduled language)
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import vote_announcements
from .claude import call_claude, parse_summary_json
from .config import CLAUDE_MODEL, CLAUDE_MODEL_AGENDA, MAX_TRANSCRIPT_CHARS

logger = logging.getLogger(__name__)


# How summaries treat private residents: officials are named; a resident only
# when the record is clear, never with a home address; students never. Shared
# by the transcript and minutes prompts; engine.scrub is the backstop for the
# address rule and cleans anything published before it.
RESIDENT_PRIVACY_RULES = """Privacy rules for private residents (these override "be specific"):
- Elected officials, government staff, and people appearing in a professional role (consultants, applicants' representatives, presenters) are named normally.
- A resident who speaks at public comment or is mentioned as a private individual is named ONLY when the source states the name clearly. If the name or its spelling is uncertain in any way, write "a resident" (or "a second resident") and never guess, never write [inaudible] next to a name.
- NEVER give a private person's home address, house number, street of residence, or say where they live ("of 123 Elm St", "who lives on Elm Street"). Say "a resident" or "a nearby resident" instead. A property address that is itself the subject of an agenda item (a rezoning, permit, or sale at that address) may be given.
- NEVER name a student or any minor; write "a student".
- NEVER connect a named private person to a health or medical condition; describe the point without the name."""


def _date_line(meeting_date: str | None) -> str:
    """Prompt line stating when the meeting was held (from YYYYMMDD). A
    recording almost never says the year aloud, and with no date in the prompt
    the model supplies one: six reference-instance summaries opened "met on
    November 19, 2024" and the like for 2026 meetings."""
    try:
        d = datetime.strptime(meeting_date or "", "%Y%m%d")
    except ValueError:
        return ""
    return (f"Meeting date: {d.strftime('%A, %B')} {d.day}, {d.year} (the day this meeting was "
            f"held; if you state the meeting's date, use exactly this one)\n")


def _roster_block(officials: list[str] | None) -> str:
    """Prompt fragment giving Claude authoritative name spellings. Auto
    transcripts and audio garble names; the jurisdiction's `officials` list
    in instance.json is the spelling reference."""
    if not officials:
        return ""
    return (
        "\n\nKNOWN OFFICIALS (authoritative spellings — transcripts often garble "
        "names; when a name in the source closely matches one of these, use "
        "EXACTLY this spelling):\n"
        + "\n".join(f"- {o}" for o in officials))


def summarize_meeting(transcript: str, title: str, url: str, *,
                      org_label: str, region: str, jurisdiction: str,
                      meeting_id: str, officials: list[str] | None = None,
                      meeting_date: str | None = None) -> dict:
    """Full-transcript summary — the flagship product. meeting_date is
    YYYYMMDD when known."""
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        logger.warning("Transcript for %s is %d chars; only the first %d reach the model",
                       title, len(transcript), MAX_TRANSCRIPT_CHARS)
    # Found by code over the WHOLE transcript, so a result is listed even past a cut.
    announced = vote_announcements.prompt_block(transcript)
    prompt = f"""You are a local government reporter covering {region}.

Meeting title: {title}
{_date_line(meeting_date)}Organization: {org_label}
YouTube link:  {url}
{_roster_block(officials)}
Below is the auto-generated transcript of the ACTUAL meeting recording. Your job is to report what ACTUALLY HAPPENED - votes taken, decisions made, who said what, outcomes, not just what was planned.

Produce a JSON object with this exact structure and nothing else - no markdown, no preamble, just valid JSON:

{{
  "overview": "2-3 sentence summary of what actually happened at this meeting and its significance to residents - include key decisions or votes if any",
  "committee": "exact committee or board name",
  "presiding": "name and title of person who chaired the meeting if mentioned",
  "agenda": [
    {{"time": "0:00", "item": "agenda item description"}}
  ],
  "discussions": [
    {{
      "item": "agenda item title",
      "body": "2-4 sentences describing what ACTUALLY occurred: who spoke, what positions they took, how votes went, what was approved or rejected, any notable debate or public input. Be specific - name the officials involved, cite vote counts, quote key statements if clear in the transcript."
    }}
  ],
  "publicComment": "Describe actual public comment offered - how many speakers and what each said, following the privacy rules below. Or 'No public comment was offered.'",
  "publicCommentSpeakers": "a whole number: how many members of the public spoke or had a written comment read during public comment and public hearings (officials, staff and presenters do not count). 0 if none. null only if the transcript does not let you count them.",
  "actionItems": ["specific decisions made or next steps directed by the committee"],
  "topics": ["3-5 short Title Case topic tags: recurring civic themes (Budget, Roads, Public Safety, Zoning, Staffing, Parks, Utilities, Development) plus specific named projects or places discussed (e.g. Highway J Extension, Riverlife District)"],
  "votes": [
    {{"item": "what was voted on, concisely", "motion": "the motion as made, if stated", "mover": "who moved it, or null", "second": "who seconded, or null", "outcome": "Approved | Failed | Tabled", "tally": "e.g. 6-0, 5-2, Unanimous, or null if not stated"}}
  ]
}}

Rules:
- This is a transcript of a REAL meeting. Report outcomes, not plans.
- agenda: extract timestamps from transcript (format: "M:SS" or "H:MM:SS"). Include 5-10 items.
- discussions: focus on WHAT WAS DECIDED or DEBATED, not just what the topic was.
- Include vote results where mentioned (e.g. "Approved 5-2", "Passed unanimously").
- votes: ONLY votes actually taken — empty array if none. Never invent movers, seconders, or tallies; use null for anything not stated in the transcript.
- Name officials who spoke or voted when identifiable from transcript.
- Note unclear audio as [inaudible] rather than guessing at what was said.
- Never supply a date or year the transcript and the lines above don't give. Speakers say "this year" and "next month" without the year; don't turn those into a year of your own.
- {RESIDENT_PRIVACY_RULES}
- Return ONLY the JSON object.
{announced}
--- TRANSCRIPT ---
{transcript[:MAX_TRANSCRIPT_CHARS]}
--- END ---"""

    raw = call_claude(model=CLAUDE_MODEL, max_tokens=4096, prompt=prompt,
                      meeting_id=meeting_id, jurisdiction=jurisdiction,
                      purpose="transcript_summary")
    result = parse_summary_json(raw, "transcript")
    # How much of the transcript the model saw (engine.audit_votes reports a cut).
    result["_transcript_chars"] = min(len(transcript), MAX_TRANSCRIPT_CHARS)
    return result


def summarize_from_agenda(agenda_text: str, title: str, url: str, *,
                          org_label: str, region: str, jurisdiction: str,
                          meeting_id: str) -> dict:
    """Agenda-only summary (no recording) — tentative language, Haiku tier."""
    prompt = f"""You are a local government reporter covering {region}.

Meeting title: {title}
Organization: {org_label}
Source: Agenda document only (no video recording or transcript available)
YouTube: {url}

Below is the text of the official meeting agenda. Produce a JSON object with this exact structure - no markdown, no preamble, just valid JSON:

{{
  "overview": "2-3 sentence factual summary starting with 'Based on the published agenda,' describing what this meeting was scheduled to address and its significance to the community.",
  "committee": "exact committee or board name",
  "presiding": "",
  "agenda": [
    {{"time": "N/A", "item": "agenda item description"}}
  ],
  "discussions": [
    {{"item": "agenda item title", "body": "2-3 sentence description of what this item involves. Use tentative language: 'was scheduled to discuss', 'was expected to consider', 'was set to review' - NOT past tense like 'discussed' or 'approved'."}}
  ],
  "publicComment": "Note whether public comment was on the agenda, or 'Not indicated on agenda.'",
  "actionItems": ["expected action items based on agenda - use 'scheduled to vote on', 'expected to consider', etc."],
  "topics": ["3-5 short Title Case topic tags: recurring civic themes (Budget, Roads, Public Safety, Zoning, Staffing, Parks, Utilities, Development) plus specific named projects or places on the agenda"]
}}

Rules:
- CRITICAL: This is an AGENDA, not a transcript. You do NOT know what actually happened. Use tentative/scheduled language throughout.
- Do NOT say items were "approved", "discussed", or "decided" - say they were "scheduled for action", "set for discussion", etc.
- Base your response ONLY on what the agenda says - do not invent outcomes or votes
- Include all substantive agenda items in both agenda[] and discussions[]
- Skip purely procedural items (call to order, roll call, adjournment)
- NEVER use placeholder text like [AGENDA_ITEM_NAME], [TBD], [INSERT], etc.
- Return ONLY the JSON object

--- AGENDA ---
{agenda_text[:12000]}
--- END ---"""

    raw = call_claude(model=CLAUDE_MODEL_AGENDA, max_tokens=4096, prompt=prompt,
                      meeting_id=meeting_id, jurisdiction=jurisdiction,
                      purpose="agenda_summary")
    return parse_summary_json(raw, "agenda")


def _format_civic_votes_for_prompt(civic_data: dict) -> str:
    """Format portal vote data (CivicClerk, Legistar) into text for inclusion in a prompt."""
    lines = []
    for item in civic_data.get("items", []):
        name = item.get("name", "").strip()
        if not name:
            continue
        number = item.get("number", "")
        prefix = f"{number}. " if number else ""
        lines.append(f"\n{prefix}{name}")
        for v in item.get("votes", []):
            motion = v.get("motion", "Unknown motion")
            passed = "PASSED" if v.get("passed") else "FAILED"
            yes = v.get("yes", [])
            no = v.get("no", [])
            abstain = v.get("abstain", [])
            initiator = v.get("initiator", "")
            seconder = v.get("seconder", "")
            vote_line = f"  Vote: {motion} — {passed}"
            if yes or no:
                vote_line += f" ({len(yes)}-{len(no)})"
            if initiator:
                vote_line += f" | Moved by {initiator}"
            if seconder:
                vote_line += f", seconded by {seconder}"
            lines.append(vote_line)
            if yes:
                lines.append(f"    Yes: {', '.join(yes)}")
            if no:
                lines.append(f"    No: {', '.join(no)}")
            if abstain:
                lines.append(f"    Abstain: {', '.join(abstain)}")
        for child in item.get("children", []):
            cname = child.get("name", "").strip()
            if cname:
                lines.append(f"  - {cname}")
                for cv in child.get("votes", []):
                    cpassed = "PASSED" if cv.get("passed") else "FAILED"
                    lines.append(f"    Vote: {cv.get('motion','')} — {cpassed}")
    return "\n".join(lines)


def summarize_from_agenda_with_votes(agenda_text: str, title: str, url: str,
                                     civic_data: dict, *, org_label: str,
                                     region: str, jurisdiction: str,
                                     meeting_id: str) -> dict:
    """Agenda + official vote records (CivicClerk, Legistar) — actual outcomes known."""
    vote_text = _format_civic_votes_for_prompt(civic_data)

    prompt = f"""You are a local government reporter covering {region}.

Meeting title: {title}
Organization: {org_label}
Source: Agenda document + official vote records
YouTube: {url}

Below is the meeting agenda AND the official vote/action records. The vote records show what ACTUALLY HAPPENED — motions made, who voted yes/no, and whether items passed or failed. Use this to report actual outcomes.

Produce a JSON object with this exact structure - no markdown, no preamble, just valid JSON:

{{
  "overview": "2-3 sentence factual summary of actual outcomes. Report what was decided: items approved/denied, vote counts, key decisions. Start with the most significant action taken.",
  "committee": "exact committee or board name",
  "presiding": "",
  "agenda": [
    {{"time": "N/A", "item": "agenda item description"}}
  ],
  "discussions": [
    {{"item": "agenda item title", "body": "2-3 sentences reporting the ACTUAL OUTCOME: was it approved or denied? What was the vote count? Who moved/seconded? Include specific names and numbers from the vote records."}}
  ],
  "publicComment": "Note whether public comment was on the agenda, or 'Not indicated on agenda.'",
  "actionItems": ["specific decisions made and next steps based on actual vote outcomes"],
  "topics": ["3-5 short Title Case topic tags: recurring civic themes (Budget, Roads, Public Safety, Zoning, Staffing, Parks, Utilities, Development) plus specific named projects or places acted on"]
}}

Rules:
- Use the VOTE RECORDS to report actual outcomes — this is real data, not speculation
- Include vote counts (e.g. "Approved 7-0", "Failed 4-3") and names of movers/seconders
- For items with no vote record, note they were on the agenda but outcome is not recorded
- Include all substantive agenda items in both agenda[] and discussions[]
- Skip purely procedural items (call to order, roll call, adjournment)
- NEVER use placeholder text like [AGENDA_ITEM_NAME], [TBD], [INSERT], etc.
- Return ONLY the JSON object

--- AGENDA ---
{agenda_text[:10000]}
--- END AGENDA ---

--- VOTE RECORDS ---
{vote_text[:5000]}
--- END VOTE RECORDS ---"""

    raw = call_claude(model=CLAUDE_MODEL_AGENDA, max_tokens=4096, prompt=prompt,
                      meeting_id=meeting_id, jurisdiction=jurisdiction,
                      purpose="agenda_with_votes_summary")
    return parse_summary_json(raw, "agenda_with_votes")


def summarize_from_boardbook(agenda: dict, title: str, *, district_label: str,
                             newsroom: str, jurisdiction: str,
                             meeting_id: str) -> dict:
    """BoardBook agenda summary (agenda-only, Haiku tier)."""
    items_text = "\n".join(f"  {item}" for item in agenda["items"])

    prompt = f"""You are a local government reporter for the {newsroom} covering the {district_label}.

Meeting: {title}
BoardBook agenda page: {agenda['agenda_url']}
Full packet download: {agenda['packet_url']}

Below is the complete agenda scraped from BoardBook. Items include descriptions, presenter information, time estimates, and attachment names where available. Lines with [Attachment: ...] indicate supporting documents. Lines with [Detail: ...] contain additional context.

--- AGENDA ---
{items_text}
--- END ---

IMPORTANT: This is an AGENDA only — no recording exists. Use tentative language ("was scheduled to discuss", "was expected to consider"). Do NOT say items were "approved" or "discussed" since you don't know the outcomes.

Produce a JSON object with this exact structure and nothing else:

{{
  "overview": "2-3 sentence summary starting with 'Based on the published agenda,' describing what this meeting was scheduled to address and its significance for the school district. Mention key action items and any notable topics.",
  "committee": "the meeting type (e.g. Regular Meeting, Committee of the Whole, Special Meeting)",
  "agenda": [
    {{"time": "N/A", "item": "agenda item description"}}
  ],
  "discussions": [
    {{"item": "agenda item title", "body": "2-3 sentence description incorporating any presenter names, time estimates, and detail text from the agenda. Use tentative language: 'was scheduled to present', 'was expected to request approval for'."}}
  ],
  "publicComment": "description of public comment if the agenda includes one, or 'No public comment period was included on this agenda.'",
  "actionItems": ["expected action items using tentative language — 'Board was expected to vote on...', 'Action was requested for...'"],
  "topics": ["3-5 short Title Case topic tags: recurring school-district themes (Budget, Curriculum, Facilities, Staffing, Policy, Athletics) plus specific named schools, programs, or projects on the agenda"]
}}

Rules:
- agenda: include ALL top-level items. Use "N/A" for time since there's no recording.
- discussions: include items with substantive descriptions. Incorporate presenter names and context from [Detail:] lines. Skip procedural items like Call to Order, Roll Call.
- actionItems: items marked "(Action Requested)" plus any motions implied.
- NEVER invent outcomes or votes — this is agenda-only.
- Return ONLY the JSON. No markdown, no explanation.
"""

    raw = call_claude(model=CLAUDE_MODEL_AGENDA, max_tokens=4096, prompt=prompt,
                      meeting_id=meeting_id, jurisdiction=jurisdiction,
                      purpose="boardbook_agenda_summary")
    return parse_summary_json(raw, "boardbook_agenda")


def summarize_from_minutes(minutes_text: str, title: str, url: str, *,
                           org_label: str, region: str, jurisdiction: str,
                           meeting_id: str, officials: list[str] | None = None,
                           meeting_date: str | None = None) -> dict:
    """Official-minutes summary — authoritative outcomes, Sonnet tier."""
    prompt = f"""You are a local government reporter covering {region}.

Meeting title: {title}
{_date_line(meeting_date)}Organization: {org_label}
Source: Official meeting minutes
Meeting page: {url}
{_roster_block(officials)}
Below are the official minutes of this meeting. Minutes are the authoritative record of what ACTUALLY HAPPENED — motions made, votes taken, who moved and seconded, what passed or failed. Report actual outcomes.

Produce a JSON object with this exact structure - no markdown, no preamble, just valid JSON:

{{
  "overview": "2-3 sentence summary of the meeting's actual outcomes and their significance to residents. Lead with the most consequential action taken. Include vote results.",
  "committee": "exact committee or board name",
  "presiding": "name and title of who chaired, if recorded",
  "agenda": [
    {{"time": "N/A", "item": "agenda item description"}}
  ],
  "discussions": [
    {{"item": "agenda item title", "body": "2-4 sentences reporting the ACTUAL OUTCOME: motion text, who moved/seconded, the vote result (e.g. 'carried 6-0', 'failed 3-4'), and any recorded discussion or public input. Name the officials involved."}}
  ],
  "publicComment": "Describe public comment as recorded in the minutes — how many spoke and on what, following the privacy rules below. Or 'No public comment was recorded.'",
  "publicCommentSpeakers": "a whole number: how many members of the public the minutes record as speaking or submitting a comment during public comment and public hearings (officials, staff and presenters do not count). 0 if none. null only if the minutes do not let you count them.",
  "actionItems": ["specific decisions made and directed next steps from the minutes"],
  "topics": ["3-5 short Title Case topic tags: recurring civic themes (Budget, Roads, Public Safety, Zoning, Staffing, Parks, Utilities, Development) plus specific named projects or places acted on"],
  "votes": [
    {{"item": "what was voted on, concisely", "motion": "the motion as recorded", "mover": "who moved it, or null", "second": "who seconded, or null", "outcome": "Approved | Failed | Tabled", "tally": "e.g. 6-0, 5-2, Unanimous, or null if not recorded"}}
  ]
}}

Rules:
- Minutes record REAL outcomes — report them as fact, in past tense.
- Include vote counts and mover/seconder names wherever the minutes record them.
- votes: ONLY votes actually recorded — empty array if none. Use null for movers/seconders/tallies the minutes don't state.
- Skip purely procedural items (call to order, roll call, adjournment) in discussions.
- NEVER use placeholder text like [AGENDA_ITEM_NAME], [TBD], [INSERT], etc.
- {RESIDENT_PRIVACY_RULES}
- Return ONLY the JSON object.

--- MINUTES ---
{minutes_text[:40000]}
--- END ---"""

    raw = call_claude(model=CLAUDE_MODEL, max_tokens=4096, prompt=prompt,
                      meeting_id=meeting_id, jurisdiction=jurisdiction,
                      purpose="minutes_summary")
    return parse_summary_json(raw, "minutes")
