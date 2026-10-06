"""Smoke tests for the mock bank: the target must behave deterministically for replay demos."""

import re

import pytest
from fastapi.testclient import TestClient

from mock_bank import bank_app as bank

MEMBER_FIELD = "ctl00$cph1$txtMbrNo"


@pytest.fixture
def client():
    c = TestClient(bank.app)
    c.post("/__admin/reset")
    return c


def sign_on(c: TestClient):
    return c.post("/login", data={"ctl00$cph1$txtUid": bank.USER, "ctl00$cph1$txtPwd": bank.PASSWORD})


def test_bad_password_is_rejected(client):
    r = client.post("/login", data={"ctl00$cph1$txtUid": bank.USER, "ctl00$cph1$txtPwd": "nope"})
    assert "Invalid User ID or Password." in r.text


def test_sign_on_lands_on_frameset(client):
    r = sign_on(client)
    assert r.url.path == "/app"
    assert '<frame name="main" src="/app/home">' in r.text


def test_ids_change_on_every_render(client):
    sign_on(client)
    ids = [re.search(r'id="(ctl00_cph1_txtMbrNo_[0-9a-f]+)"', client.get("/app/search").text)[1]
           for _ in range(2)]
    assert ids[0] != ids[1]


def test_class_names_change_on_every_render(client):
    sign_on(client)
    pages = [client.get("/app/search").text for _ in range(2)]
    classes = [set(re.findall(r'class="(css-[0-9a-f]+)"', page)) for page in pages]
    assert classes[0] and not classes[0] & classes[1]  # no class name survives a reload


def test_search_contains_match_returns_several_rows(client):
    sign_on(client)
    r = client.post("/app/search", data={MEMBER_FIELD: "12345"})
    assert "3 record(s) found." in r.text
    for number in ("12345", "123456", "512345"):
        assert re.search(rf"<td[^>]*>{number}</td>", r.text)


def test_search_not_found_and_validation(client):
    sign_on(client)
    assert "No records found" in client.post("/app/search", data={MEMBER_FIELD: "99999"}).text
    assert "must be numeric" in client.post("/app/search", data={MEMBER_FIELD: "12a"}).text


def test_member_detail_shows_balance(client):
    sign_on(client)
    r = client.get("/app/member?mbr=12345")
    assert "Member Inquiry - 12345" in r.text
    assert "$1,204.50" in r.text


def test_denied_and_error_members(client):
    sign_on(client)
    assert "ACCESS DENIED" in client.get("/app/member?mbr=66666").text
    r = client.get("/app/member?mbr=70000")
    assert r.status_code == 500 and "Server Error in '/Teller' Application." in r.text


def test_maintenance_interstitial_fires_once(client):
    sign_on(client)
    client.post("/__admin/faults", json={"maintenance": 1})
    first = client.get("/app/member?mbr=12345")
    assert "Scheduled Maintenance" in first.text
    assert "/app/member?mbr=12345" in first.text  # Continue goes back to the original page
    assert "Member Inquiry - 12345" in client.get("/app/member?mbr=12345").text


def test_expired_session_shows_sign_in_page(client):
    sign_on(client)
    client.post("/__admin/faults", json={"expire_sessions": True})
    r = client.get("/app/search")
    assert r.status_code == 200  # legacy apps report this as a normal page
    assert "Your session has expired" in r.text


def test_identity_verification_needs_the_one_time_code(client):
    client.post("/__admin/faults", json={"verify_identity": 1})
    r = sign_on(client)
    assert "Additional Verification Required" in r.text
    assert "Your session has expired" in client.get("/app/search").text
    code = client.get("/__admin/state").json()["last_otp"]
    assert client.post("/verify", data={"ctl00$cph1$txtOtp": code}).url.path == "/app"


def test_open_sub_account_validation_and_confirm(client):
    sign_on(client)
    form = {"mbr": "12345", "ctl00$cph1$ddlShareType": "20", "ctl00$cph1$txtAmt": "100",
            "ctl00$cph1$ddlFund": "0010", "ctl00$cph1$txtNick": "Rainy day"}
    assert "Minimum opening deposit for MONEY MARKET is $2,500.00." in \
        client.post("/app/member/open", data=form).text

    review = client.post("/app/member/open", data=form | {"ctl00$cph1$ddlShareType": "01"})
    assert "This cannot be undone." in review.text
    token = re.search(r'name="token" value="([0-9a-f]+)"', review.text)[1]
    receipt = client.post("/app/member/open/confirm", data={"token": token})
    assert "Sub-account opened successfully." in receipt.text
    # A double-submit of the same review is refused; a fresh run would open a second account.
    again = client.post("/app/member/open/confirm", data={"token": token})
    assert "already submitted" in again.text
    assert len(client.get("/__admin/state").json()["opened"]) == 1
