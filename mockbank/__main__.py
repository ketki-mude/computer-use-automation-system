"""`mockbank serve` runs the app; `mockbank fault k=v ...`, `reset` and `state` drive its test harness."""

import argparse
import json
import os
import urllib.request

from dotenv import load_dotenv


def _admin(port: int, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/__admin/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def _parse_value(raw: str):
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return int(raw)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="mockbank")
    parser.add_argument("--port", type=int, default=int(os.getenv("MOCKBANK_PORT", "8000")))
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the mock bank (default)")
    fault = sub.add_parser("fault", help="inject faults, e.g. maintenance=1 expire_sessions=true")
    fault.add_argument("settings", nargs="+", metavar="KEY=VALUE")
    sub.add_parser("reset", help="restore seed data and clear faults and sessions")
    sub.add_parser("state", help="print faults, last one-time code and irreversible actions")
    args = parser.parse_args()

    if args.cmd == "fault":
        settings = dict(s.split("=", 1) for s in args.settings)
        print(json.dumps(_admin(args.port, "faults", {k: _parse_value(v) for k, v in settings.items()}), indent=2))
    elif args.cmd == "reset":
        print(json.dumps(_admin(args.port, "reset", {}), indent=2))
    elif args.cmd == "state":
        print(json.dumps(_admin(args.port, "state"), indent=2))
    else:
        import uvicorn

        uvicorn.run("mockbank.app:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
