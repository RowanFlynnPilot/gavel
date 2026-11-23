"""Small text helpers shared by adapters and publish."""

from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")


def plain(text) -> str:
    """Text as a reader should see it: markup removed, entities decoded,
    whitespace collapsed. Meeting portals let clerks paste formatted text into
    an agenda item's name, so a name can arrive as
    '<span style="background-color: ...">Resolution ...</span>'."""
    if not isinstance(text, str):
        return ""
    return " ".join(html.unescape(_TAG.sub(" ", text)).split())


def plain_items(items):
    """A portal's agenda items (name, desc, votes' motions, children) with
    every text run through plain(). Returns new objects; safe to run twice."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append({
            **it,
            "name": plain(it.get("name")),
            "desc": plain(it.get("desc")),
            "votes": [{**v, "motion": plain(v.get("motion"))} if isinstance(v, dict) else v
                      for v in it.get("votes") or []],
            "children": plain_items(it.get("children")),
        })
    return out
