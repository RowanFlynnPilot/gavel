"""Export the officials rosters as a standalone file.

    python -m engine.rosters [--out assets/rosters.json]

instance.json is where each jurisdiction's officials live (name spellings for
the transcript and minutes prompts, vocabulary hints for Whisper). Other
tools want the same spellings without parsing the whole instance config: the
reference instance's reel drafter checks every on-screen name against
assets/rosters.json. This writes that file from instance.json, so there is
one place to edit and the copy can't drift. Same inputs, same bytes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import InstanceConfig, load_instance

README = ("GENERATED from instance.json by `python -m engine.rosters`; edit the officials "
          "there, not here. Official name rosters per jurisdiction: a spelling reference, "
          "not a membership registry. Update instance.json after each spring election.")


def build(cfg: InstanceConfig) -> dict:
    out: dict = {"_readme": README}
    for jur in cfg.jurisdictions:
        out[jur.key] = {"body": jur.name, "officials": list(jur.officials)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Write the officials rosters from instance.json.")
    ap.add_argument("--out", default="assets/rosters.json")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(load_instance()), indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"[rosters]  wrote {out}")


if __name__ == "__main__":
    main()
