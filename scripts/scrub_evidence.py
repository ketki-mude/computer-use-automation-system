"""Re-apply redaction to evidence recorded before deferred redaction existed.

Early discovery runs wrote each screen to disk as it was observed, so a value that only
became known as sensitive later (the balance the agent extracted at the end) stayed in
clear text in earlier screens. This applies the same Redactor afterwards, with the values
the run itself marked sensitive, and drops raw Playwright traces (DOM snapshots that
redaction cannot reach). New runs do this at write time and need no scrubbing.

    python scripts/scrub_evidence.py evidence/<folder> "$1,204.50:financial" ...
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from cua.redact import Redactor

TEXT = {".jsonl", ".json", ".txt", ".html", ".yaml"}


def main(folder: str, *values: str) -> None:
    profile = yaml.safe_load((ROOT / "config" / "apps" / "legacy_core.yaml").read_text())
    redactor = Redactor(profile.get("pii_patterns", []))
    for item in values:
        value, sensitivity = item.rsplit(":", 1)
        redactor.register(value, sensitivity)
    changed = 0
    for path in Path(folder).rglob("*"):
        if path.name == "trace.zip":
            path.unlink()
            changed += 1
        elif path.suffix in TEXT:
            before = path.read_text(encoding="utf-8")
            after = redactor.text(before)
            if after != before:
                path.write_text(after, encoding="utf-8")
                changed += 1
    print(f"{folder}: {changed} file(s) scrubbed")


if __name__ == "__main__":
    main(*sys.argv[1:])
