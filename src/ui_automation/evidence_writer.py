"""Per-run evidence: a structured, redacted event log plus screenshots, snapshots and traces.

Layout: runs/<run_id>/events.jsonl, observations/, screenshots, dom/, trace.zip, result.json
"""

import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .safety.log_masking import Redactor
from .settings import ROOT, RUNS_DIR

CONSOLE_STYLE = {
    "error": typer.colors.RED, "failed": typer.colors.RED, "policy_blocked": typer.colors.RED,
    "detector": typer.colors.YELLOW, "recovery": typer.colors.YELLOW,
    "intervention": typer.colors.MAGENTA, "human_action": typer.colors.MAGENTA,
    "finished": typer.colors.GREEN,
}


class RunLog:
    def __init__(self, kind: str, redactor: Redactor, quiet: bool = False,
                 listeners: list | None = None):
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{kind}-{stamp}-{secrets.token_hex(2)}"
        self.dir = RUNS_DIR / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.quiet = quiet
        self.listeners = list(listeners or [])  # called with each redacted event record
        self._seq = 0
        self._started = time.monotonic()
        self._file = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        # Screens and prompts are written when the run closes, so values that only become
        # known as sensitive later (an extracted balance) are masked in earlier screens too.
        self._deferred: dict[str, list[str]] = {}

    def event(self, type_: str, message: str = "", **data: Any) -> None:
        if "type" in data:
            raise ValueError("event data may not use the key 'type'; it is the event's own type")
        self._seq += 1
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "run_id": self.run_id,
            "type": type_,
            # Only the payload is scrubbed; the envelope holds no user data.
            **self.redactor.scrub({"message": message, **data}),
        }
        self._file.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._file.flush()
        for listener in self.listeners:
            listener(record)
        if not self.quiet and message:
            color = next((c for k, c in CONSOLE_STYLE.items() if type_.startswith(k)), None)
            typer.secho(f"  {self.elapsed():6.1f}s  {type_:<16} {record['message']}", fg=color)

    def display_dir(self) -> str:
        """The run folder relative to the project (runs/<run_id>), so no local path is recorded."""
        try:
            return self.dir.relative_to(ROOT).as_posix()
        except ValueError:
            return self.dir.as_posix()

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def path(self, name: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_text(self, name: str, text: str) -> Path:
        p = self.path(name)
        p.write_text(self.redactor.text(text), encoding="utf-8")
        return p

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(self.redactor.scrub(obj), indent=2, default=str, ensure_ascii=False),
                     encoding="utf-8")
        return p

    def defer_text(self, name: str, text: str) -> None:
        self._deferred.setdefault(name, []).append(text)

    def close(self) -> None:
        for name, chunks in self._deferred.items():
            self.path(name).write_text(self.redactor.text("".join(chunks)), encoding="utf-8")
        self._deferred.clear()
        if not self._file.closed:
            self._file.close()
