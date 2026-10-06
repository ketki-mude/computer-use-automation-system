"""Shared test setup: runs need the mock bank's synthetic sign-on, with or without a .env."""

from mock_bank.bank_app import seed_demo_credentials

seed_demo_credentials()
