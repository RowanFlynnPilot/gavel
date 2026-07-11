"""Parity soak check: production marathon-meetings data vs deployed gavel.

Run any time during the Phase 2 soak week:

    python tools/parity_diff.py

Compares the production repo's committed src/data/meetings.json (the file
baked into the production bundle at build time) against the gavel Pages
deployment's runtime data/meetings.json, field by field, plus per-source
upcoming.json event sets. Exits non-zero on any drift so it can gate the
cutover decision.

Expected transient skew: the two instances cron at different minutes, so a
meeting summarized between their runs appears on one side for up to 4 hours.
Re-run after both crons have fired before treating a diff as real.
"""

import json
import sys
import urllib.request

PROD_MEETINGS = ("https://raw.githubusercontent.com/RowanFlynnPilot/"
                 "marathon-meetings/main/src/data/meetings.json")
PROD_UPCOMING = ("https://raw.githubusercontent.com/RowanFlynnPilot/"
                 "marathon-meetings/main/src/data/upcoming.json")
GAVEL_MEETINGS = "https://rowanflynnpilot.github.io/gavel/data/meetings.json"
GAVEL_UPCOMING = "https://rowanflynnpilot.github.io/gavel/data/upcoming.json"


def fetch(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def main() -> int:
    drift = 0

    prod = fetch(PROD_MEETINGS)
    gavel = fetch(GAVEL_MEETINGS)
    print(f"meetings: prod={len(prod)} gavel={len(gavel)}")

    prod_ids = [m["id"] for m in prod]
    gavel_ids = [m["id"] for m in gavel]
    if prod_ids != gavel_ids:
        drift += 1
        print("  ID order/membership DIFFERS:")
        for i in prod_ids:
            if i not in gavel_ids:
                print(f"    only in prod:  {i}")
        for i in gavel_ids:
            if i not in prod_ids:
                print(f"    only in gavel: {i}")
    else:
        print("  ID order: identical")

    gavel_by_id = {m["id"]: m for m in gavel}
    field_diffs = 0
    for pm in prod:
        gm = gavel_by_id.get(pm["id"])
        if gm is None:
            continue
        for k in sorted(set(pm) | set(gm)):
            if pm.get(k) != gm.get(k):
                field_diffs += 1
                if field_diffs <= 10:
                    pv = json.dumps(pm.get(k))[:80]
                    gv = json.dumps(gm.get(k))[:80]
                    print(f"  DIFF {pm['id']}.{k}: prod={pv} gavel={gv}")
    print(f"  field diffs: {field_diffs}")
    drift += field_diffs

    prod_up = fetch(PROD_UPCOMING)
    gavel_up = fetch(GAVEL_UPCOMING)
    for k in sorted(set(prod_up) | set(gavel_up)):
        pset = {(m["date"], m["name"]) for m in prod_up.get(k, [])}
        gset = {(m["date"], m["name"]) for m in gavel_up.get(k, [])}
        mark = "match" if pset == gset else "SKEW"
        print(f"upcoming {k:14s} prod={len(pset):2d} gavel={len(gset):2d}  {mark}")
        if pset != gset:
            drift += 1
            for d in sorted(gset - pset):
                print(f"    gavel-only: {d}")
            for d in sorted(pset - gset):
                print(f"    prod-only:  {d}")

    print(f"\n{'PARITY OK' if drift == 0 else f'DRIFT DETECTED ({drift})'}")
    return 0 if drift == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
