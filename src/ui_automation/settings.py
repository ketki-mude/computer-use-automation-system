"""Every path, port, model name and timing the system uses, in one place.

Values that differ per machine (keys, ports) come from environment variables, loaded from .env.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
CAPABILITIES_DIR = ROOT / "capabilities"
RUNS_DIR = ROOT / "runs"
SCRIPTED_DISCOVERY_DIR = ROOT / "scripted_discovery"
DEFAULT_APP = "acmecore_teller"

# The model that learns new tasks and picks a task for a request. Replay never uses one.
# "auto" uses OpenAI when its key is set, else Gemini; set "openai" or "gemini" to force one.
LLM_PROVIDER = os.getenv("UI_AUTOMATION_LLM_PROVIDER", "auto")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Tried in order: the next one is used when a model is overloaded or out of quota.
OPENAI_MODELS = [m for m in os.getenv(
    "UI_AUTOMATION_OPENAI_MODELS", "gpt-6-luna,gpt-5.6-luna").split(",") if m]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DISCOVERY_MODEL = os.getenv("UI_AUTOMATION_DISCOVERY_MODEL", "gemini-3.8-flash")  # Gemini's first choice
FALLBACK_MODELS = [m for m in os.getenv(
    "UI_AUTOMATION_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3-flash-preview").split(",") if m]
LLM_MAX_RETRY_WAIT_S = 60.0  # a longer wait means a quota is used up: move to the next model
OPERATOR_PORT = int(os.getenv("UI_AUTOMATION_OPERATOR_PORT", "8001"))

BANK_PORT = int(os.getenv("UI_AUTOMATION_BANK_PORT", "8100"))  # 8000 is often taken (e.g. by Docker)
BANK_URL = f"http://127.0.0.1:{BANK_PORT}"
# Values config files may refer to as ${NAME}, so a host or port is written in one place only.
CONFIG_VALUES = {"BANK_URL": BANK_URL}

# Waiting. Every wait is on a condition with a timeout; these are the timeouts and poll rates.
ACTION_TIMEOUT_MS = 5_000  # one click, fill or read
PAGE_SETTLE_TIMEOUT_S = 8.0  # network quiet and all frames loaded after an action
# How long the page must stay quiet (no request in flight, every frame loaded) to count as
# settled. A navigation a click starts by script within this window is still waited for.
PAGE_QUIET_PERIOD_S = 0.45
SETTLE_POLL_INTERVAL_S = 0.05  # how often settling re-checks requests and frames
CONDITION_POLL_INTERVAL_S = 0.25  # how often a known screen / target / postcondition is checked
SIGNED_IN_TIMEOUT_S = 10.0
TRANSIENT_RETRY_BACKOFF_S = 1.0  # pause before retrying after a transient page error
TICKET_POLL_INTERVAL_S = 0.4  # how often a waiting run checks its ticket
TICKET_TIMEOUT_S = 900.0  # a ticket nobody takes in this time ends the run as escalated
LIVE_VIEW_INTERVAL_S = 0.6  # how often the control room refreshes a run's live screen
LIVE_VIEW_JPEG_QUALITY = 60
LLM_REQUEST_TIMEOUT_MS = 60_000
TICKET_API_TIMEOUT_S = 5.0  # a run talking to the control room's ticket API
BANK_ADMIN_TIMEOUT_S = 5.0  # test controls talking to the mock bank's admin API

# A learned capability is saved only if this many validation replays in a row, each in a fresh
# browser, return the same outputs: one lucky pass is not evidence that it is stable.
VALIDATION_RUNS = 3

# Discovery gives up (and asks a person, if one is available) after this many actions or seconds.
DISCOVERY_MAX_STEPS = 25
DISCOVERY_TIMEOUT_S = 420.0

# An agent invoking a capability waits this long for the result; after that (typically a run
# waiting for a person on a ticket) it gets the job to poll instead.
INVOKE_WAIT_S = 120.0
INVOKE_POLL_INTERVAL_S = 0.25

# The browser window replay drives (points; the pixel surface also pins its window to this).
WINDOW_WIDTH, WINDOW_HEIGHT = 1280, 820

# Pixel surface (no DOM): perception is OCR of screenshots, actions are mouse and keys.
PIXEL_SCALE = 2.0  # screenshot pixels per window point (like a 200% display); OCR reads better
PIXEL_QUIET_PERIOD_S = 0.4  # the screen must stop changing for this long to count as settled
PIXEL_POLL_INTERVAL_S = 0.15  # how often settling re-captures the screen
SCREEN_CAPTURE_TIMEOUT_MS = 3_000  # one screenshot; mid-navigation it may not be possible
OCR_MIN_CONFIDENCE = 0.5  # text the OCR is less sure of is ignored
FIELD_SEARCH_WIDTH_PT = 300  # how far right of a label to look for its input box
FIELD_CLICK_INSET_PT = 6  # click this far inside an input box's left edge

# Watching. Fast (0) is the default everywhere; `--slow` or the control room's speed setting
# delays every browser action by this much so a person can follow along.
WATCHABLE_SLOW_MO_MS = 400
