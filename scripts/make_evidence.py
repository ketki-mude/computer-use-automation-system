"""Run every replay scenario against the mock bank and collect the evidence.

Needs the mock bank running (`mockbank serve`) and both capabilities approved.
Nothing in this script calls an LLM.

    python scripts/make_evidence.py
"""

import json
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"
CUA = str(ROOT / ".venv" / "bin" / "cua")
PY = str(ROOT / ".venv" / "bin" / "python")
BANK = "http://127.0.0.1:8000/__admin"
BALANCE = "get_savings_balance"
OPEN = "open_sub_account"
NEW_ACCOUNT = {"member_number": "23456", "share_type": "REGULAR SAVINGS", "initial_deposit": "100.00"}


@dataclass
class Scenario:
    folder: str
    capability: str
    params: dict
    expected: str
    shows: str
    faults: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)


SCENARIOS = [
    Scenario("replay-01-success", BALANCE, {"member_number": "12345"}, "success",
             "Happy path. Three rows match the search; the row-scoped locator picks 12345."),
    Scenario("replay-02-success-other-member", BALANCE, {"member_number": "23456"}, "success",
             "Same capability, another member: it generalizes. 123456 also matches the search."),
    Scenario("replay-03-business-not-found", BALANCE, {"member_number": "99999"}, "business_outcome",
             "RECORD_NOT_FOUND: a legitimate answer, not an error."),
    Scenario("replay-04-business-permission-denied", BALANCE, {"member_number": "66666"},
             "business_outcome", "PERMISSION_DENIED on a restricted employee record."),
    Scenario("replay-05-failed-invalid-input", BALANCE, {"member_number": "12a45"}, "failed",
             "INPUT_INVALID: rejected against the input schema before a browser starts."),
    Scenario("replay-06-recovered-maintenance-notice", BALANCE, {"member_number": "12345"}, "success",
             "Recoverable: a maintenance interstitial is dismissed and the run continues.",
             faults={"maintenance": 1}),
    Scenario("replay-07-recovered-session-expired", BALANCE, {"member_number": "12345"}, "success",
             "Recoverable: the session expires mid-run; replay signs on again and restarts, "
             "which is safe because every completed step was read-only.",
             faults={"expire_after": 2}),
    Scenario("replay-08-success-slow-page", BALANCE, {"member_number": "50000"}, "success",
             "A 4 s server delay: replay waits for the postcondition, with no fixed sleeps."),
    Scenario("replay-09-failed-app-error", BALANCE, {"member_number": "70000"}, "failed",
             "APP_ERROR: stops with a screenshot, DOM snapshot and Playwright trace."),
    Scenario("replay-10-escalated-identity-check", BALANCE, {"member_number": "12345"}, "success",
             "Handoff: sign-on asks for a one-time code; a person takes over the same live "
             "session, enters it and hands back; automation finishes the task.",
             faults={"verify_identity": 1}, flags=["--operator"]),
    Scenario("replay-11-refused-without-approval", OPEN, NEW_ACCOUNT, "failed",
             "APPROVAL_REQUIRED: an irreversible capability is refused before it starts."),
    Scenario("replay-12-committed-with-approval", OPEN, NEW_ACCOUNT, "success",
             "With --allow-irreversible the account is opened; the confirmation number is returned.",
             flags=["--allow-irreversible"]),
    Scenario("replay-13-business-validation-error", OPEN,
             {**NEW_ACCOUNT, "share_type": "MONEY MARKET"}, "business_outcome",
             "VALIDATION_ERROR: below the money market minimum; stops before the commit point.",
             flags=["--allow-irreversible"]),
]


def admin(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{BANK}/{path}", headers={"Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def run(s: Scenario) -> dict:
    admin("reset", {})
    if s.faults:
        admin("faults", s.faults)
    operator = None
    if "--operator" in s.flags:
        operator = subprocess.Popen([PY, str(ROOT / "scripts" / "demo_operator.py"), "otp"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    params = [arg for k, v in s.params.items() for arg in ("-p", f"{k}={v}")]
    cmd = [CUA, "replay", s.capability, *params, "--json", *s.flags]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=600, check=False)
    if operator:
        operator.wait(timeout=60)
    result = json.loads(proc.stdout[proc.stdout.index("{"):])
    opened = len(admin("state")["opened"])
    dest = EVIDENCE / s.folder
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(ROOT / "runs" / result["run_id"], dest)
    (dest / "command.txt").write_text(
        shlex.join(["cua", *cmd[1:]]) + "\n"
        + (f"faults injected first: {s.faults}\n" if s.faults else "")
        + f"accounts the app opened during this run: {opened}\n")
    mark = "ok " if result["status"] == s.expected else "UNEXPECTED"
    print(f"{mark} {s.folder:<42} {result['status']:<17} exit={proc.returncode} "
          f"accounts_opened={opened} {time.monotonic() - started:5.1f}s")
    # The caller's view: what an agent receives on stdout (real values; the run folder is redacted).
    detail = result.get("outcome") or result.get("error") or result.get("outputs")
    if isinstance(detail, dict):
        detail = {k: v for k, v in detail.items() if k not in ("screenshot", "dom_snapshot")}
    return {"folder": s.folder, "capability": s.capability, "params": s.params, "faults": s.faults,
            "flags": s.flags, "expected": s.expected, "status": result["status"],
            "exit_code": proc.returncode, "accounts_opened": opened, "shows": s.shows,
            "caller_received": detail, "recoveries": result.get("recoveries"),
            "handoffs": result.get("handoffs")}


def main() -> None:
    summary = [run(s) for s in SCENARIOS]
    admin("reset", {})
    (EVIDENCE / "replay-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    sys.exit(0 if all(s["status"] == s["expected"] for s in summary) else 1)


if __name__ == "__main__":
    main()
