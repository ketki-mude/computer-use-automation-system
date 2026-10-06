"""Mock 'legacy core banking' app: the proxy target for the automation system.

Deliberately hostile to automation, the way real back-office apps are:
- a frameset (banner / menu / main), so every control lives inside a frame;
- table layouts with no <label> elements and no test IDs;
- ASP.NET-style control names, and element ids and class names that change on every render;
- buttons that navigate via JavaScript instead of links;
- errors returned as ordinary HTTP 200 pages, so detection has to read the screen.

Runtime faults are injectable (fault_injection.py). All data is synthetic (sample_members.py).
"""

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import fault_injection, sample_members
from .fault_injection import FAULTS
from .sample_members import MEMBERS, SHARE_TYPES, Member, Share

load_dotenv()
log = logging.getLogger("mock_bank")

# Synthetic teller account. Override with MOCK_BANK_USER / MOCK_BANK_PASSWORD.
USER = os.getenv("MOCK_BANK_USER", "teller01")
PASSWORD = os.getenv("MOCK_BANK_PASSWORD", "Demo#2026")
SESSION_TIMEOUT_S = int(os.getenv("MOCK_BANK_SESSION_TIMEOUT_S", "900"))
COOKIE = "ASP.NET_SessionId"
SLOW_MEMBER_DELAY_S = 4.0



def seed_demo_credentials() -> None:
    """Make this bank's synthetic teller sign-on available to the automation under the secret
    names its app profile references, unless they are already set. A real deployment reads
    them from its own secret store instead; the automation never sees them in code."""
    os.environ.setdefault("MOCK_BANK_USER", USER)
    os.environ.setdefault("MOCK_BANK_PASSWORD", PASSWORD)


app = FastAPI(title="Mock AcmeCore Teller", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Fresh random suffixes on every render: ids and tokens are useless as locators.
templates.env.globals["rid"] = lambda base: f"{base}_{secrets.token_hex(3)}"
# Generated class names that change on every render, like a modern CSS-in-JS build
# ("css-1x2y3z"): automation that keys on classes breaks; ours never uses them.
templates.env.globals["cls"] = lambda: f"css-{secrets.token_hex(3)}"
templates.env.globals["viewstate"] = lambda: secrets.token_urlsafe(48)
templates.env.globals["variant"] = lambda: FAULTS.variant > 0  # the 7.5 screen variant
templates.env.globals["rtok"] = lambda: secrets.token_hex(4)
templates.env.filters["money"] = lambda v: f"${v:,.2f}"


@dataclass
class Session:
    user: str
    created: float
    last_seen: float
    otp: str | None = None  # set while a one-time verification code is outstanding


SESSIONS: dict[str, Session] = {}
PENDING: dict[str, dict] = {}  # review token -> sub-account request awaiting Confirm
# What the app did, so idempotency of irreversible actions can be checked from outside.
AUDIT: dict = {"last_otp": None, "opened": [], "closed": []}


def pending_codes() -> dict[str, str]:
    """Outstanding one-time codes by the last 4 characters of the session id (the
    'supervisor's device' in the demo; several runs can wait for codes at once)."""
    return {sid[-4:]: sess.otp for sid, sess in SESSIONS.items() if sess.otp}


POPUPS = {
    "popup": 'alert("Your password will expire in 3 days. Change it from the Teller menu.");',
    "confirm_popup": 'if (confirm("Print a receipt for this inquiry?")) { document.title += " (printing)"; }',
}


def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("popup", getattr(request.state, "popup", None))
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def message(request: Request, title: str, code: str, text: str) -> HTMLResponse:
    return page(request, "message.html", title=title, screen_code=code, message=text)


def session_state(request: Request) -> tuple[str, Session | None]:
    """Returns (state, session) where state is ok | none | expired | unverified."""
    sess = SESSIONS.get(request.cookies.get(COOKIE, ""))
    if sess is None:
        return "none", None
    if (sess.created < FAULTS.sessions_expired_before
            or time.time() - sess.last_seen > SESSION_TIMEOUT_S):
        return "expired", sess
    if sess.otp is not None:
        return "unverified", sess
    return "ok", sess


async def guard(request: Request) -> Response | None:
    """Runs before every page rendered in the main frame; returns a page to show instead, if any."""
    if FAULTS.expire_after:
        FAULTS.expire_after -= 1
        if FAULTS.expire_after == 0:
            FAULTS.sessions_expired_before = time.time()
    state, sess = session_state(request)
    if state != "ok":
        return page(request, "session_expired.html", title="Session Expired", screen_code="SYS901")
    sess.last_seen = time.time()
    if FAULTS.slow_ms:
        await asyncio.sleep(FAULTS.slow_ms / 1000)
    if FAULTS.take("app_error"):
        return page(request, "server_error.html", status=500)
    for fault, script in POPUPS.items():
        if request.method == "GET" and FAULTS.take(fault):
            request.state.popup = script
    if request.method == "GET" and FAULTS.take("maintenance"):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return page(request, "maintenance.html", title="System Notice", screen_code="SYS100",
                    continue_url=target)
    return None


def form_str(form, name: str) -> str:
    return str(form.get(name, "")).strip()


# ---------------------------------------------------------------- sign-on

@app.get("/")
def root():
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return page(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    uid, pwd = form_str(form, "ctl00$cph1$txtUid"), str(form.get("ctl00$cph1$txtPwd", ""))
    if not (secrets.compare_digest(uid.encode(), USER.encode())
            and secrets.compare_digest(pwd.encode(), PASSWORD.encode())):
        return page(request, "login.html", error="Invalid User ID or Password.")
    now = time.time()
    sess = Session(uid, now, now)
    if FAULTS.take("verify_identity"):
        sess.otp = f"{secrets.randbelow(10**6):06d}"
        AUDIT["last_otp"] = sess.otp
        log.warning("One-time verification code for %s: %s", uid, sess.otp)
    sid = secrets.token_hex(12)
    SESSIONS[sid] = sess
    resp = RedirectResponse("/verify" if sess.otp else "/app", status_code=303)
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
    return resp


@app.get("/verify", response_class=HTMLResponse)
def verify_form(request: Request):
    state, _ = session_state(request)
    if state != "unverified":
        return RedirectResponse("/app" if state == "ok" else "/login", status_code=302)
    return page(request, "verify.html")


@app.post("/verify")
async def verify(request: Request):
    state, sess = session_state(request)
    if state != "unverified":
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not secrets.compare_digest(form_str(form, "ctl00$cph1$txtOtp").encode(), sess.otp.encode()):
        return page(request, "verify.html", error="The code entered is not valid.")
    sess.otp = None
    sess.last_seen = time.time()
    return RedirectResponse("/app", status_code=303)


@app.get("/logout")
def logout(request: Request):
    SESSIONS.pop(request.cookies.get(COOKIE, ""), None)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- frameset shell

@app.get("/app", response_class=HTMLResponse)
def frameset(request: Request):
    state, _ = session_state(request)
    if state == "unverified":
        return RedirectResponse("/verify", status_code=302)
    if state != "ok":
        return RedirectResponse("/login", status_code=302)
    return page(request, "frameset.html")


@app.get("/app/banner", response_class=HTMLResponse)
def banner(request: Request):
    _, sess = session_state(request)
    return page(request, "banner.html", user=sess.user if sess else "")


@app.get("/app/menu", response_class=HTMLResponse)
def menu(request: Request):
    return page(request, "menu.html")


# ---------------------------------------------------------------- main-frame screens

@app.get("/app/home", response_class=HTMLResponse)
async def home(request: Request):
    if blocked := await guard(request):
        return blocked
    _, sess = session_state(request)
    return page(request, "home.html", title="Teller Home", screen_code="HOM000", user=sess.user)


def search_page(request: Request, **ctx) -> HTMLResponse:
    ctx.setdefault("q_number", "")
    ctx.setdefault("q_last", "")
    return page(request, "search.html", title="Member Search", screen_code="MBR010", **ctx)


@app.get("/app/search", response_class=HTMLResponse)
async def search_form(request: Request):
    if blocked := await guard(request):
        return blocked
    return search_page(request)


@app.post("/app/search", response_class=HTMLResponse)
async def search(request: Request):
    """Legacy 'postback': results render on the same URL, so only the page content changes."""
    if blocked := await guard(request):
        return blocked
    form = await request.form()
    number, last = form_str(form, "ctl00$cph1$txtMbrNo"), form_str(form, "ctl00$cph1$txtLName")
    if not number and not last:
        return search_page(request, error="ERROR: Enter a Member Number or Last Name.")
    if number and not number.isdigit():
        return search_page(request, error="ERROR: Member Number must be numeric.",
                           q_number=number, q_last=last)
    return search_page(request, searched=True, results=sample_members.search(number, last),
                       q_number=number, q_last=last)


def find_member(request: Request, mbr: str, title: str, code: str) -> Member | HTMLResponse:
    m = MEMBERS.get(mbr)
    if m is None:
        return message(request, title, code, f"ERROR: Member {mbr} not found.")
    if m.behavior == "denied":
        return message(request, title, code, "ACCESS DENIED: You do not have permission to "
                       "view this member record. (SEC-403)")
    return m


@app.get("/app/member", response_class=HTMLResponse)
async def member_detail(request: Request, mbr: str = ""):
    if blocked := await guard(request):
        return blocked
    m = find_member(request, mbr, "Member Inquiry", "MBR020")
    if not isinstance(m, Member):
        return m
    if FAULTS.take("unavailable"):
        return page(request, "unavailable.html", status=503)
    if m.behavior == "slow":
        await asyncio.sleep(SLOW_MEMBER_DELAY_S)
    if m.behavior == "error":
        return page(request, "server_error.html", status=500)
    return page(request, "member.html", title=f"Member Inquiry - {m.number}",
                screen_code="MBR020", m=m)


def open_page(request: Request, m: Member, **ctx) -> HTMLResponse:
    return page(request, "open_account.html", title=f"Open Sub-Account - {m.number}",
                screen_code="SHR100", m=m, share_types=SHARE_TYPES, **ctx)


@app.get("/app/member/open", response_class=HTMLResponse)
async def open_form(request: Request, mbr: str = ""):
    if blocked := await guard(request):
        return blocked
    m = find_member(request, mbr, "Open Sub-Account", "SHR100")
    return open_page(request, m) if isinstance(m, Member) else m


@app.post("/app/member/open", response_class=HTMLResponse)
async def open_submit(request: Request):
    if blocked := await guard(request):
        return blocked
    form = await request.form()
    m = find_member(request, form_str(form, "mbr"), "Open Sub-Account", "SHR100")
    if not isinstance(m, Member):
        return m
    type_code = form_str(form, "ctl00$cph1$ddlShareType")
    fund = form_str(form, "ctl00$cph1$ddlFund")
    nickname = form_str(form, "ctl00$cph1$txtNick")[:20]
    amount_raw = form_str(form, "ctl00$cph1$txtAmt").replace("$", "").replace(",", "")
    entered = {"type_code": type_code, "fund": fund, "nickname": nickname,
               "amount_raw": amount_raw}

    def reject(error: str) -> HTMLResponse:
        return open_page(request, m, error=error, **entered)

    try:
        amount = Decimal(amount_raw)
    except InvalidOperation:
        return reject("ERROR: Initial Deposit must be a valid amount.")
    if not amount.is_finite() or amount <= 0:
        return reject("ERROR: Initial Deposit must be a valid amount.")
    if type_code not in SHARE_TYPES:
        return reject("ERROR: Select a Share Type.")
    desc, minimum = SHARE_TYPES[type_code]
    if amount < minimum:
        return reject(f"ERROR: Minimum opening deposit for {desc} is ${minimum:,.2f}.")
    source = m.share(fund)
    if source is None:
        return reject("ERROR: Select a Funding Source.")
    if amount > source.available:
        return reject("ERROR: Insufficient available funds in funding account.")
    token = secrets.token_hex(8)
    PENDING[token] = {"mbr": m.number, "type_code": type_code, "amount": amount, "fund": fund,
                      "nickname": nickname}
    return page(request, "review.html", title=f"Review Sub-Account - {m.number}",
                screen_code="SHR110", m=m, token=token, desc=desc, amount=amount,
                source=source, nickname=nickname)


@app.post("/app/member/open/confirm", response_class=HTMLResponse)
async def open_confirm(request: Request):
    """The irreversible step: creates the account and moves money."""
    if blocked := await guard(request):
        return blocked
    form = await request.form()
    req = PENDING.pop(form_str(form, "token"), None)
    if req is None:
        return message(request, "Open Sub-Account", "SHR120",
                       "ERROR: This request has expired or was already submitted.")
    m = MEMBERS[req["mbr"]]
    source = m.share(req["fund"])
    source.available -= req["amount"]
    source.current -= req["amount"]
    desc, _ = SHARE_TYPES[req["type_code"]]
    new = Share(sample_members.next_suffix(m), desc, req["amount"], req["amount"], nickname=req["nickname"])
    m.shares.append(new)
    conf = f"{secrets.token_hex(3).upper()}-{len(AUDIT['opened']) + 1:04d}"
    AUDIT["opened"].append({"confirmation": conf, "member": m.number,
                            "account": m.account_number(new), "amount": str(req["amount"])})
    return page(request, "receipt.html", title="Sub-Account Opened", screen_code="SHR120",
                m=m, share=new, conf=conf, account=m.account_number(new))


@app.get("/app/member/close", response_class=HTMLResponse)
async def close_form(request: Request, mbr: str = ""):
    if blocked := await guard(request):
        return blocked
    m = find_member(request, mbr, "Close Membership", "MBR900")
    if not isinstance(m, Member):
        return m
    return page(request, "close.html", title=f"Close Membership - {m.number}",
                screen_code="MBR900", m=m, closed=False)


@app.post("/app/member/close", response_class=HTMLResponse)
async def close_submit(request: Request):
    if blocked := await guard(request):
        return blocked
    form = await request.form()
    m = find_member(request, form_str(form, "mbr"), "Close Membership", "MBR900")
    if not isinstance(m, Member):
        return m
    m.status = "CLOSED"
    for s in m.shares:
        s.status = "CLOSED"
    AUDIT["closed"].append(m.number)
    return page(request, "close.html", title=f"Close Membership - {m.number}",
                screen_code="MBR900", m=m, closed=True)


@app.get("/app/admin", response_class=HTMLResponse)
async def system_admin(request: Request):
    if blocked := await guard(request):
        return blocked
    return message(request, "System Administration", "ADM000", "ACCESS DENIED: Your role "
                   "(TELLER) is not authorized for System Administration. (SEC-401)")


@app.get("/app/{function}", response_class=HTMLResponse)
async def unavailable(request: Request, function: str):
    if blocked := await guard(request):
        return blocked
    return message(request, function.title(), "SYS404",
                   "This function is not available on the demo system.")


# ---------------------------------------------------------------- test harness (not the "app")

@app.get("/__admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return page(request, "admin.html", faults=FAULTS.as_dict(), audit=AUDIT,
                sessions=len(SESSIONS), codes=pending_codes())


@app.get("/__admin/state")
def admin_state():
    return {"faults": FAULTS.as_dict(), "pending_codes": pending_codes(), **AUDIT}


@app.post("/__admin/faults")
async def admin_faults(request: Request):
    try:
        FAULTS.update(await request.json())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"faults": FAULTS.as_dict()}


@app.post("/__admin/reset")
def admin_reset():
    sample_members.reset()
    fault_injection.reset()
    SESSIONS.clear()
    PENDING.clear()
    AUDIT.update(last_otp=None, opened=[], closed=[])
    return {"ok": True}
