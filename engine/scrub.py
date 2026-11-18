"""Keep residents' home addresses out of published summaries, and apply
logged corrections.

    python -m engine.scrub            # apply
    python -m engine.scrub --check    # report what would change, write nothing

Two jobs, both idempotent, run in CI after every summarization and before
data/meetings.json and the static pages are rebuilt. No API calls.

1. Address scrub. Summaries name officials; a private resident's home address
   never belongs in one. The transcript and minutes prompts carry the rule
   (summarize.RESIDENT_PRIVACY_RULES); this is the backstop for when the model
   slips, and the cleanup of anything published before the rule. It removes an
   address attached to a person ("Jane Doe (123 Elm St)", "Jane Doe of 123 Elm
   Street, Springfield", "a neighbor at 123 Elm St") and leaves an address
   that is the subject of business (a rezoning at 123 Elm St). The
   municipality and state names that may trail an address come from
   instance.json `place_names`.

2. Corrections. corrections.json is the public log of fixes readers or the
   editor asked for: [{"id", "find", "replace", "date", "kind", "note"}].
   Each is re-applied here every run, so re-summarizing a meeting can't undo
   a fix. kind is "correction" (an error fixed) or "privacy" (a name or detail
   withheld under the residents rule).

Targets: summaries/*_summary.json and their .md mirrors, data/meetings.json,
and the static pages (archive pages are never re-rendered, so they are edited
in place).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

from .config import (CORRECTIONS_FILE, MEETINGS_JSON, STATE_FILE, SUMMARIES_DIR,
                     load_instance, utf8_console)
from .errors import CorrectionsFileError

CORRECTION_KINDS = ("correction", "privacy")

# ── Address grammar ───────────────────────────────────────────────────────────
_SUFFIX = (r"(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Dr(?:ive)?|Ln|Lane|Ct|Court|Blvd|Boulevard|"
           r"Way|Cir(?:cle)?|Trail|Pl(?:ace)?|Pkwy|Parkway|Terrace|Row)\b\.?")
_DIR = r"(?:(?:[NSEW]\.?|North|South|East|West)\s+)?"
_STREET_WORD = r"(?:\d{1,3}(?:st|nd|rd|th)|[A-Z][A-Za-z'’]+)"
_HIGHWAY = (r"(?:(?:State|County|U\.?S\.?)\s+)?(?:Highway|Hwy\.?|Trunk\s+Highway|Road)\s+"
            r"[A-Z0-9]{1,3}\b")
# Rural grid addresses have five- or six-digit house numbers and often no
# street suffix ("236617 County Road W", "167949 Sunny Lane Rd").
_RURAL = r"\d{5,6}\s+[A-Z][A-Za-z'’]*\.?(?:\s+[A-Z0-9][A-Za-z0-9'’]*\.?){0,3}"
ADDR = (r"(?:\d{1,5}(?:-\d{1,4})?[A-Z]?\s+" + _DIR +
        r"(?:(?:" + _STREET_WORD + r"\s+){1,4}" + _SUFFIX + r"|" + _HIGHWAY + r")"
        r"(?:\s+(?:Apt|Unit|#)\s*\w+)?|" + _RURAL + r")")

# Speakers give an address at the podium, and captions often lose the street
# suffix or the street itself: "(1206 Elm)", "(412 [street name inaudible])",
# "(3305 Oak Hill, Rib Mountain)". Inside public comment, a parenthetical after
# a speaker's name that is nothing but a house number and capitalized or
# bracketed words is that address. Numbers that read as years are left alone
# unless a bracket follows ("(2027 Budget Committee)" is not an address).
_LOOSE_PAREN = re.compile(
    r"^(?:(?:address given as|of|at|from|resides at|lives at|residing at)\s+)?"
    r"(?P<num>\d{3,6})\s+"
    r"(?P<first>\[[^\]]*\]|[A-Z][A-Za-z'’.]*)"
    r"(?:\s+(?:\[[^\]]*\]|[A-Z0-9][A-Za-z0-9'’.]*)){0,3}"
    r"(?:,\s*[A-Z][A-Za-z'’ ]*){0,2}$")


_LOOSE_NOT_HOME = re.compile(
    r"\b(?:LLC|Inc|Corp|Company|Church|School|Bank|Hospital|Center|Centre|Club|Association|"
    r"Authority|District|Department|Committee|Commission|Board|Council|Tavern|Bar|"
    r"Restaurant|Store|Market|Clinic|Office|Storage|Apartments|Resolution|Ordinance|"
    r"Motion|Item|Phase|Project|Budget|Plan|Fund|Program|Award|Act|Census|Study|Report)\b",
    re.IGNORECASE)


def _loose_address(inner: str) -> bool:
    m = _LOOSE_PAREN.match(inner.strip())
    # A business or body in the parenthetical ("412 Main Tavern") is not a
    # home. Street words ("Lane", "Park") are the opposite: part of an address.
    if not m or _LOOSE_NOT_HOME.search(inner):
        return False
    year_like = len(m.group("num")) == 4 and 1990 <= int(m.group("num")) <= 2039
    return not year_like or m.group("first").startswith("[")


# A person's name: 1-3 capitalized words, optional title and quotes.
NAME = (r"(?:(?:Dr|Mr|Mrs|Ms)\.?\s+)?['\"‘“]?[A-Z][a-zA-Z'’\-]+"
        r"(?:\s+(?:[A-Z]\.|[A-Z][a-zA-Z'’\-]+)){0,2}['\"’”]?")

# Capitalized words that mark a business, place or body, not a person: an
# address after one of these is the subject of business and stays.
_NOT_A_PERSON = re.compile(
    r"\b(?:LLC|Inc|Corp|Company|Co|Church|School|Park|Station|Trip|Bank|Hospital|Center|"
    r"Centre|Club|Association|Authority|District|Village|City|County|Town|Department|"
    r"Committee|Commission|Board|Council|Street|Avenue|Road|Drive|Lane|Court|Boulevard|"
    r"Parcel|Lot|Property|Properties|Building|Plaza|Mall|Apartments|Estates|Addition|"
    r"Subdivision|Resolution|Ordinance|Motion|Item|Phase|Project|Lodge|Hall|Library|"
    r"Hotel|Motel|Restaurant|Bar|Tavern|Store|Market|Clinic|Office|Garage|Farm|Farms|"
    r"Storage|Steakhouse|Electric|Bakery|Baseball|Deli|Motors|Auto|Automotive|Homes|"
    r"Builders|Construction|Realty|Rentals|Holdings|Enterprises|Group|Partners|Ventures|"
    r"Investments|Services|Supply|Foods|Brewing|Brewery|Cafe|Coffee|Pizza|Grill|Pub|Salon|"
    r"Fitness|Dental|Health|Care|Pharmacy|Foundation|Society|League|Habitat|Humanity|"
    r"Ministries|Lutheran|Catholic|Baptist|Methodist|University|College|Academy|"
    r"Elementary|Airport|Landfill|Cemetery|Tower|Towers|Lofts|Villas|Manor|"
    # action nouns that start a sentence about a property ("Rezoning of 123 Elm St")
    r"Sale|Purchase|Rezoning|Zoning|Discussion|Approval|Appeal|Acquisition|Demolition|"
    r"Vacation|Annexation|Development|Redevelopment|Review|Consideration|Owner|Owners|"
    r"Tenant|Tenants|Applicant|Request|Permit|License|Hearing|Update|Status|Site|Plan|"
    r"Plat|Survey|Map|Lease|Easement|Transfer|Conveyance|Dedication|Inspection|Razing|"
    r"Abatement|Enforcement|Cleanup|Complaint|Repair|Renovation|Reconstruction|"
    r"Improvement|Expansion|Extension|Variance|Exception|Use)\b\.?",
    re.IGNORECASE)
# All-caps tokens (USM, LNS), possessives (Tommy's) and digits mark a business.
_BUSINESS_SHAPE = re.compile(r"\b[A-Z]{2,}\b|['’]s\b|\d")

_RESIDENT_NOUN = (r"(?:neighbors?|residents?|homeowners?|speakers?|attendees?|citizens?|"
                  r"woman|man|couple|family)")

_TIDY = [
    (re.compile(r"[ \t]{2,}"), " "),
    (re.compile(r"\s+([,.;:)])"), r"\1"),
    (re.compile(r"\(\s+"), "("),
]

# "item", "title" and "motion" are the government's own wording (agenda item
# titles, motions): a permit applicant's address there is public agenda data.
_SKIP_KEYS = {"id", "url", "docUrl", "doc_url", "video_url", "source", "_source", "date",
              "shortDate", "time", "item", "title", "motion"}
# Corrections reach every text field, including item titles; only ids, links
# and dates are off limits.
_NEVER_EDIT_KEYS = {"id", "url", "docUrl", "doc_url", "video_url", "source", "_source",
                    "date", "shortDate"}


def _is_person(name: str, public_comment: bool) -> bool:
    """In public comment a capitalized name followed by an address is a
    speaker, even when the transcript garbled it ("Mary CL"), so only the
    word list applies there."""
    if _NOT_A_PERSON.search(name):
        return False
    return public_comment or not _BUSINESS_SHAPE.search(name)


class Scrubber:
    """Address scrub for one instance. place_names are the municipality and
    state names that may follow an address (", Springfield, IL"); up to two
    are removed with it."""

    def __init__(self, place_names: list[str] | tuple[str, ...] = ()):
        trail = ""
        if place_names:
            # Longest first, so "Shelbyville" is tried before "Shelby".
            names = "|".join(re.escape(p) for p in sorted(place_names, key=len, reverse=True))
            trail = r"(?:,\s*(?:(?:Town|Village|City)\s+of\s+)?(?:" + names + r")\b){0,2}"
        addr = ADDR + trail
        self._addr_in_paren = re.compile(
            r"(?:(?:address given as|both of|of|at|from|resides at|lives at|residing at)\s+)?"
            + addr)
        self._rules_all = [
            # Jane Doe (123 Elm St[, Springfield][, anything else])
            (re.compile(r"(?P<name>" + NAME + r")\s*\((?P<inner>[^()]*)\)"), "paren"),
            # Jane Doe[,] of 123 Elm Street[, Springfield]
            (re.compile(r"(?P<name>" + NAME + r"),?\s+of\s+" + addr), "name"),
            # Jane Doe, 123 Elm Street[, Springfield][,]
            (re.compile(r"(?P<name>" + NAME + r"),\s+" + addr + r",?\s*"), "name_space"),
            # a neighbor at / a resident of 123 Elm Street
            (re.compile(r"(?P<name>\b" + _RESIDENT_NOUN + r")\s+(?:at|of|from|living at|"
                        r"who lives at|who resides at|residing at)\s+" + addr, re.IGNORECASE),
             "noun"),
        ]
        # Only in public comment is a name followed by "at/from <address>" a
        # speaker's home rather than a business location.
        self._rules_comment = [
            (re.compile(r"(?P<name>" + NAME + r")\s+(?:from|at|who lives at|who resides at|"
                        r"residing at|living at)\s+" + addr), "name"),
        ]

    def text(self, text: str, public_comment: bool = False) -> str:
        def person(m: re.Match) -> bool:
            return _is_person(m.group("name"), public_comment)

        def paren(m: re.Match) -> str:
            """Jane Doe (123 Elm St) -> Jane Doe; Jane Doe (applicant, 123 Elm
            St) -> Jane Doe (applicant). No address inside: left alone."""
            name, inner = m.group("name"), m.group("inner")
            if not person(m):
                return m.group(0)
            if public_comment and _loose_address(inner):
                return name
            if not self._addr_in_paren.search(inner):
                return m.group(0)
            rest = self._addr_in_paren.sub("", inner)
            rest = re.sub(r"\s*,\s*,+", ",", rest).strip(" ,;")
            return f"{name} ({rest})" if rest else name

        replace = {
            "paren": paren,
            "name": lambda m: m.group("name") if person(m) else m.group(0),
            "name_space": lambda m: m.group("name") + " " if person(m) else m.group(0),
            "noun": lambda m: m.group("name"),
        }
        out = text
        for pattern, kind in self._rules_all + (self._rules_comment if public_comment else []):
            out = pattern.sub(replace[kind], out)
        if out != text:
            for pattern, repl in _TIDY:
                out = pattern.sub(repl, out)
        return out

    def record(self, obj, key: str = ""):
        """Scrub every string in a summary or meeting record, recursively."""
        if isinstance(obj, str):
            return obj if key in _SKIP_KEYS else self.text(obj, public_comment=(key == "publicComment"))
        if isinstance(obj, list):
            return [self.record(x, key) for x in obj]
        if isinstance(obj, dict):
            return {k: self.record(v, k) for k, v in obj.items()}
        return obj


def replace_in_record(obj, find: str, replace: str, key: str = ""):
    if isinstance(obj, str):
        return obj if key in _NEVER_EDIT_KEYS else obj.replace(find, replace)
    if isinstance(obj, list):
        return [replace_in_record(x, find, replace, key) for x in obj]
    if isinstance(obj, dict):
        return {k: replace_in_record(v, find, replace, k) for k, v in obj.items()}
    return obj


def load_corrections(path: Path | None = None) -> list[dict]:
    """The corrections log, validated. A missing file is an empty log."""
    path = path or CORRECTIONS_FILE
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CorrectionsFileError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(rows, list):
        raise CorrectionsFileError(f"{path} must be a list of corrections")
    for r in rows:
        if not (isinstance(r, dict) and r.get("id") and r.get("find")
                and isinstance(r.get("replace"), str)):
            raise CorrectionsFileError(
                f"{path}: every entry needs id, find and replace — got {r!r}")
        if r.get("kind", "correction") not in CORRECTION_KINDS:
            raise CorrectionsFileError(
                f"{path}: kind must be one of {CORRECTION_KINDS} — got {r!r}")
    return rows


def _summary_id(path: Path, processed: dict) -> str | None:
    """Meeting ID for a summary sidecar, via the state file."""
    stem = str(path).replace("\\", "/").replace("_summary.json", "")
    for vid, info in processed.items():
        sf = (info.get("summary_file") or "").replace("\\", "/").replace(".md", "")
        if sf and (sf == stem or sf.endswith("/" + Path(stem).name)):
            return vid
    return None


# The "## Public Comment" section of a summary's markdown mirror (engine.output).
_MD_COMMENT = re.compile(r"(^## Public Comment\n)(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)


def scrub_files(place_names: list[str], pages_dir: Path, check: bool = False) -> dict:
    """Scrub and correct every target. Returns counts of what changed (or
    would change, with check=True) plus the number of corrections on file."""
    scrubber = Scrubber(place_names)
    corrections = load_corrections()
    by_id: dict[str, list[dict]] = {}
    for c in corrections:
        by_id.setdefault(c["id"], []).append(c)

    processed = {}
    if STATE_FILE.exists():
        processed = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("processed", {})

    changed = {"summaries": 0, "mirrors": 0, "meetings": 0, "pages": 0,
               "corrections": len(corrections)}

    def fix(record, mid):
        out = scrubber.record(record)
        for c in by_id.get(mid or "", []):
            out = replace_in_record(out, c["find"], c["replace"])
        return out

    def fix_text(text, mid, is_html=False, addresses=True, mirror=False):
        out = scrubber.text(text) if addresses else text
        if mirror:  # the mirror's public-comment section gets the speaker rules too
            out = _MD_COMMENT.sub(
                lambda m: m.group(1) + scrubber.text(m.group(2), public_comment=True), out)
        for c in by_id.get(mid or "", []):
            out = out.replace(c["find"], c["replace"])
            if is_html:  # pages escape quotes and ampersands
                out = out.replace(html.escape(c["find"]), html.escape(c["replace"]))
        return out

    # 1. Summary sidecars and their markdown mirrors.
    for path in sorted(SUMMARIES_DIR.glob("*_summary.json")):
        mid = _summary_id(path, processed)
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        new = fix(rec, mid)
        if new != rec:
            changed["summaries"] += 1
            if not check:
                path.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
        md = Path(str(path).replace("_summary.json", ".md"))
        if md.exists():
            text = md.read_text(encoding="utf-8")
            new_text = fix_text(text, mid, mirror=True)
            if new_text != text:
                changed["mirrors"] += 1
                if not check:
                    md.write_text(new_text, encoding="utf-8")

    # 2. The published data file. Carried-over cards never pass back through
    #    their sidecars, so it is scrubbed directly.
    displayed = set()
    if MEETINGS_JSON.exists():
        meetings = json.loads(MEETINGS_JSON.read_text(encoding="utf-8"))
        displayed = {m.get("id") for m in meetings}
        new_meetings = [fix(m, m.get("id")) for m in meetings]
        changed["meetings"] = sum(1 for a, b in zip(meetings, new_meetings) if a != b)
        if changed["meetings"] and not check:
            MEETINGS_JSON.write_text(
                json.dumps(new_meetings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 3. Static pages. Pages for displayed meetings are re-rendered from the
    #    data file every run, so they only need corrections; the address scrub
    #    is for archive pages. (Raw HTML has no field names, so scrubbing a
    #    displayed page would also touch the agenda item titles the data-file
    #    pass deliberately leaves alone.)
    if pages_dir.exists():
        for page in sorted(pages_dir.glob("*/index.html")):
            page_html = page.read_text(encoding="utf-8")
            mid = page.parent.name
            new_html = fix_text(page_html, mid, is_html=True, addresses=mid not in displayed)
            if new_html != page_html:
                changed["pages"] += 1
                if not check:
                    page.write_text(new_html, encoding="utf-8")

    return changed


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(
        description="Keep residents' home addresses out of published summaries, "
                    "and apply logged corrections.")
    parser.add_argument("--check", action="store_true", help="report only; write nothing")
    parser.add_argument("--pages-dir", default="frontend/public/meetings",
                        help="static meeting pages (engine.pages --out-dir, plus /meetings)")
    args = parser.parse_args()

    cfg = load_instance()
    changed = scrub_files(cfg.place_names, Path(args.pages_dir), check=args.check)
    verb = "would change" if args.check else "changed"
    print(f"[scrub] {verb}: {changed['summaries']} summaries, {changed['mirrors']} mirrors, "
          f"{changed['meetings']} displayed meetings, {changed['pages']} pages; "
          f"{changed['corrections']} correction(s) on file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
