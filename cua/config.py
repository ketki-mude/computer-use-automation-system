"""Paths and settings. Values come from environment variables, loaded from .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
CAPABILITIES_DIR = ROOT / "capabilities"
RUNS_DIR = ROOT / "runs"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DISCOVERY_MODEL = os.getenv("CUA_DISCOVERY_MODEL", "gemini-3.8-flash")
# Tried in order when the primary model is overloaded (503) or rate limited (429).
FALLBACK_MODELS = [m for m in os.getenv(
    "CUA_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3-flash-preview").split(",") if m]
OPERATOR_PORT = int(os.getenv("CUA_OPERATOR_PORT", "8001"))
# Chrome DevTools port that operator tools attach to during a run with --operator.
CDP_PORT = int(os.getenv("CUA_CDP_PORT", "9222"))
