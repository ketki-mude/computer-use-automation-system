"""Plays the human operator, for recorded demos of the handoff.

It does what a person would do: waits for an intervention in the operator console,
takes control, works on the *same live browser* the automation was driving (attached
over the Chrome DevTools Protocol), then hands control back. The automation records
these actions through its injected listener, exactly as it would for a real person.

    python scripts/demo_operator.py otp     # enter the mock bank's one-time code, then resume
    python scripts/demo_operator.py abort   # abort the intervention instead
"""

import asyncio
import json
import sys
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

CONSOLE = "http://127.0.0.1:8001"
BANK = "http://127.0.0.1:8000"
CDP = "http://127.0.0.1:9222"


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def post(url: str, form: dict | None = None) -> None:
    data = urllib.parse.urlencode(form or {}).encode()
    urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=5).close()


async def main(action: str) -> None:
    print("operator: waiting for an intervention...")
    while True:
        try:
            pending = [i for i in get(f"{CONSOLE}/api/interventions") if i["state"] == "awaiting_human"]
        except OSError:
            pending = []
        if pending:
            break
        await asyncio.sleep(0.5)
    iv = pending[0]
    print(f"operator: {iv['id']} at {iv['step_id']}: {iv['reason']}")
    if action == "abort":
        post(f"{CONSOLE}/abort/{iv['id']}")
        print("operator: aborted")
        return

    post(f"{CONSOLE}/take/{iv['id']}", {"operator": "demo-operator"})
    print("operator: took control; attaching to the live browser over CDP")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP)
        page = browser.contexts[0].pages[0]
        code = get(f"{BANK}/__admin/state")["last_otp"]  # read from the supervisor's device
        await page.locator("input[name='ctl00$cph1$txtOtp']").fill(code)
        await page.get_by_role("button", name="Verify").click()
        await page.wait_for_load_state("load")
        await asyncio.sleep(1)
    post(f"{CONSOLE}/resume/{iv['id']}")
    print("operator: handed control back to automation")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "otp"))
