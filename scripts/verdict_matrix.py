"""Write docs/product/VERDICT_LANGUAGE.md from swingguru/verdicts.py (the single source).

    .venv\\Scripts\\python.exe scripts\\verdict_matrix.py

tests/test_verdicts.py fails if the document and the code disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.verdicts import VERDICT_VERSION, verdict_matrix  # noqa: E402

DOC = ROOT / "docs" / "product" / "VERDICT_LANGUAGE.md"

HEADER = f"""# Swing Guru verdict language (locked, v{VERDICT_VERSION})

Generated from `src/swingguru/verdicts.py` by `scripts/verdict_matrix.py` - edit the code, then regenerate.
A test fails if this file and the code disagree.

## Rules

* **Short, coach-like, readable in under a second.** One verdict per check; the same kind of measurement
  always uses the same pattern: `<thing> good` / `Slightly <x>` / `Too <x>` (or the golf term: `Reaching`,
  `Loss of posture`, `Early extension`).
* **Colour has one meaning everywhere** (status dot, overlay line, active progress dot):
  green = within the target band; amber = slight deviation; coral = clear deviation; neutral = information.
  Colour never floods the screen: the dark verdict pill stays the calm, dominant element.
* **The verdict is the strongest text.** The measured value is secondary (`43° · Estimated`).
* **Nothing is shown that the engine cannot support.** Unsupported vocabulary is agreed below but never shown.
* **Validation status** is recorded on every verdict (`check.verdict.mapping.validation`):
  `validated_rule` (none yet), `provisional_ui_mapping` (all current verdicts: a stated rule, not yet
  calibrated against coach or 3D labels), `unsupported` (not shown).
* Each shown verdict also records `verdict_id`, `phase`, `sources`, `band`, `confidence`, `basis` and
  `approximation`; `?dev=1` in the player displays them.

## Matrix

"""


def render() -> str:
    return HEADER + verdict_matrix() + "\n"


def main() -> int:
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(render(), encoding="utf-8")
    print(DOC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
