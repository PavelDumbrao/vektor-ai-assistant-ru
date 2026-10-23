from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1]

# Load billing module
spec_bill = importlib.util.spec_from_file_location("hermes_forge_billing_test", DIR / "billing.py")
billing = importlib.util.module_from_spec(spec_bill)
assert spec_bill.loader is not None
sys.modules[spec_bill.name] = billing
spec_bill.loader.exec_module(billing)

# Load api module
spec_api = importlib.util.spec_from_file_location("hermes_forge_api_test", DIR / "api.py")
api = importlib.util.module_from_spec(spec_api)
assert spec_api.loader is not None
sys.modules[spec_api.name] = api
spec_api.loader.exec_module(api)

# Load control module
spec_ctrl = importlib.util.spec_from_file_location("hermes_forge_control_test", DIR / "control.py")
control = importlib.util.module_from_spec(spec_ctrl)
assert spec_ctrl.loader is not None
sys.modules[spec_ctrl.name] = control
spec_ctrl.loader.exec_module(control)


# ---------------------------------------------------------------------------
# 1. Signature algorithm & verification tests
# ---------------------------------------------------------------------------

KNOWN_VECTOR_DATA = {
    "order_id": "test",
    "products": [{"name": "Тест/1", "price": 2000, "quantity": 1}],
    "do": "pay",
}
KNOWN_SECRET = "secret"
# Escaped slashes: {"do":"pay","order_id":"test","products":[{"name":"Тест\/1","price":"2000","quantity":"1"}]}
EXPECTED_HEX_ESCAPED = "7db8e70adbad8b270f8f128a93d9a59f7c3c57e967fb2fc4d3b279f58a74a42a"
# Unescaped slashes: {"do":"pay","order_id":"test","products":[{"name":"Тест/1","price":"2000","quantity":"1"}]}
EXPECTED_HEX_UNESCAPED = "7c906906b9e50850ec227faf8dd2f884fc285ba614d67fd70897ff227258b689"


def test_signature_calculation_matches_known_vector():
    sig = billing.prodamus_signature(KNOWN_VECTOR_DATA, KNOWN_SECRET)
    assert sig == EXPECTED_HEX_ESCAPED


def test_verify_signature_accepts_both_escaping_variants():
    # 1. Escaped variant (PHP default)
    assert billing.verify_signature(KNOWN_VECTOR_DATA, KNOWN_SECRET, EXPECTED_HEX_ESCAPED) is True

    # 2. Unescaped variant (prodamuspy)
    assert billing.verify_signature(KNOWN_VECTOR_DATA, KNOWN_SECRET, EXPECTED_HEX_UNESCAPED) is True


def test_verify_signature_case_insensitive():
    # Capitalized / uppercase hex
    upper = EXPECTED_HEX_ESCAPED.upper()
    assert billing.verify_signature(KNOWN_VECTOR_DATA, KNOWN_SECRET, upper) is True


def test_verify_signature_rejects_invalid_signatures():
    assert billing.verify_signature(KNOWN_VECTOR_DATA, KNOWN_SECRET, "invalid_hex") is False
    assert billing.verify_signature(KNOWN_VECTOR_DATA, "wrong_secret", EXPECTED_HEX_ESCAPED) is False
    assert billing.verify_signature(KNOWN_VECTOR_DATA, KNOWN_SECRET, "") is False
    assert billing.verify_signature(KNOWN_VECTOR_DATA, "", EXPECTED_HEX_ESCAPED) is False


def test_signature_strips_top_level_signature_key():
    data_with_sig = dict(KNOWN_VECTOR_DATA)
    data_with_sig["signature"] = "existing_signature_to_ignore"
    sig = billing.prodamus_signature(data_with_sig, KNOWN_SECRET)
    assert sig == EXPECTED_HEX_ESCAPED
    assert billing.verify_signature(data_with_sig, KNOWN_SECRET, EXPECTED_HEX_ESCAPED) is True


# ---------------------------------------------------------------------------
# 2. parse_notification & notification_summary
# ---------------------------------------------------------------------------

def test_parse_notification_nested_structure():
    body = b"order_id=1&products[0][name]=X&products[0][price]=2000&subscription[id]=5"
    parsed = billing.parse_notification(body)
    assert parsed == {
        "order_id": "1",
        "products": [{"name": "X", "price": "2000"}],
        "subscription": {"id": "5"},
    }


def test_parse_notification_empty_body():
    assert billing.parse_notification(b"") == {}


def test_notification_summary_extraction():
    data = {
        "order_id": "ord_100",
        "order_num": "num_42",
        "sum": "2000.00",
        "currency": "RUB",
        "payment_status": "success",
        "customer_extra": "987654321",
        "products": [
            {"name": "Тариф Вектор", "price": "2000", "quantity": "1", "sum": "2000"},
        ],
        "subscription": {"id": "sub_777"},
        "date": "2026-10-23 12:00:00",
    }
    summary = billing.notification_summary(data)
    assert summary["order_id"] == "ord_100"
    assert summary["order_num"] == "num_42"
    assert summary["sum"] == "2000.00"
    assert summary["currency"] == "RUB"
    assert summary["payment_status"] == "success"
    assert summary["customer_extra"] == "987654321"
    assert summary["user_id"] == 987654321
    assert len(summary["products"]) == 1
    assert summary["products"][0]["name"] == "Тариф Вектор"
    assert summary["subscription"] == {"id": "sub_777"}
    assert summary["date"] == "2026-10-23 12:00:00"


def test_notification_summary_invalid_customer_extra():
    data = {"customer_extra": "not_a_number"}
    summary = billing.notification_summary(data)
    assert summary["user_id"] is None
    assert summary["customer_extra"] == "not_a_number"


# ---------------------------------------------------------------------------
# 3. Settings & Payment Link
# ---------------------------------------------------------------------------

def test_load_settings(tmp_path):
    env_file = tmp_path / "billing.env"
    env_file.write_text(
        "PRODAMUS_FORM_URL=https://shop.payform.ru/\n"
        "PRODAMUS_SECRET_KEY=secret_key_123\n"
        "FORGE_PLAN_NAME=Тариф Про\n"
        "FORGE_PLAN_PRICE_RUB=2500\n"
        "FORGE_PLAN_DAYS=30\n"
        "FORGE_PUBLIC_URL=https://forge.example.com\n"
        "PRODAMUS_DEMO_MODE=1\n",
        encoding="utf-8",
    )
    settings = billing.load_settings(env_file)
    assert settings is not None
    assert settings.form_url == "https://shop.payform.ru/"
    assert settings.secret_key == "secret_key_123"
    assert settings.plan_name == "Тариф Про"
    assert settings.plan_price_rub == 2500
    assert settings.plan_days == 30
    assert settings.public_url == "https://forge.example.com"
    assert settings.demo_mode is True
    assert settings.subscription_id == ""


def test_load_settings_missing_file(tmp_path):
    missing = tmp_path / "nonexistent.env"
    assert billing.load_settings(missing) is None


def test_load_settings_missing_required_keys(tmp_path):
    env_file = tmp_path / "billing.env"
    env_file.write_text("FORGE_PLAN_NAME=Test\n", encoding="utf-8")
    assert billing.load_settings(env_file) is None


def test_payment_link_generation_and_signature():
    settings = billing.BillingSettings(
        form_url="https://shop.payform.ru/",
        secret_key="my_secret_key",
        plan_name="Подписка Вектор",
        plan_price_rub=2000,
        plan_days=30,
        public_url="https://forge.example.com",
    )
    link = billing.payment_link(settings, user_id=12345678, username="pavel", order_id="ord_test_1")

    assert link.startswith("https://shop.payform.ru/?")
    parsed_url = urllib.parse.urlparse(link)
    qsl = urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    query_dict = dict(qsl)

    assert query_dict["do"] == "pay"
    assert query_dict["order_id"] == "ord_test_1"
    assert query_dict["customer_extra"] == "12345678"
    assert query_dict["products[0][name]"] == "Подписка Вектор"
    assert query_dict["products[0][price]"] == "2000"
    assert query_dict["products[0][quantity]"] == "1"
    assert query_dict["urlNotification"] == "https://forge.example.com/v1/webhooks/prodamus"
    assert query_dict["urlSuccess"] == "https://forge.example.com/pay/success"
    assert query_dict["urlReturn"] == "https://forge.example.com/pay/return"
    assert "signature" in query_dict

    # Check signature is valid for the exact parameters
    sig = query_dict.pop("signature")
    assert billing.verify_signature(query_dict, settings.secret_key, sig) is True


def test_payment_link_subscription_variant():
    settings = billing.BillingSettings(
        form_url="https://shop.payform.ru/",
        secret_key="my_secret_key",
        plan_name="Подписка Вектор",
        plan_price_rub=2000,
        plan_days=30,
        public_url="https://forge.example.com",
        subscription_id="sub_vector_monthly",
    )
    link = billing.payment_link(settings, user_id=12345678, username="pavel", order_id="ord_sub_1")
    parsed_url = urllib.parse.urlparse(link)
    query_dict = dict(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))

    assert query_dict["subscription"] == "sub_vector_monthly"
    assert "products[0][name]" not in query_dict
    assert "products[0][price]" not in query_dict
    sig = query_dict.pop("signature")
    assert billing.verify_signature(query_dict, settings.secret_key, sig) is True


def test_payment_link_demo_mode():
    settings = billing.BillingSettings(
        form_url="https://shop.payform.ru/",
        secret_key="my_secret_key",
        plan_name="Подписка Вектор",
        plan_price_rub=2000,
        plan_days=30,
        public_url="https://forge.example.com",
        demo_mode=True,
    )
    link = billing.payment_link(settings, user_id=12345678, username="pavel", order_id="ord_demo_1")
    parsed_url = urllib.parse.urlparse(link)
    query_dict = dict(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))
    assert query_dict["demo_mode"] == "1"


# ---------------------------------------------------------------------------
# 4. HTTP API tests (Webhook, Status, Pay pages)
# ---------------------------------------------------------------------------

def _run_http_request(sock_path: Path, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(str(sock_path))
    req_lines = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close"]
    hdrs = dict(headers or {})
    if body and "Content-Length" not in hdrs:
        hdrs["Content-Length"] = str(len(body))
    for k, v in hdrs.items():
        req_lines.append(f"{k}: {v}")
    raw_req = "\r\n".join(req_lines).encode("utf-8") + b"\r\n\r\n" + body
    client.sendall(raw_req)

    resp = b""
    while True:
        try:
            chunk = client.recv(4096)
            if not chunk:
                break
            resp += chunk
        except Exception:
            break
    client.close()

    header_part, body_part = resp.split(b"\r\n\r\n", 1)
    status_line = header_part.split(b"\r\n", 1)[0].decode("utf-8")
    status_code = int(status_line.split(" ")[1])

    resp_headers = {}
    for line in header_part.decode("utf-8", errors="replace").split("\r\n")[1:]:
        if ":" in line:
            hk, hv = line.split(":", 1)
            resp_headers[hk.strip().lower()] = hv.strip()

    return status_code, resp_headers, body_part


@pytest.fixture
def api_server(tmp_path, monkeypatch):
    sock_path = tmp_path / "api_billing_test.sock"
    server = api.BoundedThreadingUnixHTTPServer(str(sock_path), api.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    try:
        yield sock_path
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)
        if sock_path.exists():
            sock_path.unlink()


def test_api_webhook_unconfigured(api_server, monkeypatch):
    monkeypatch.delenv("PRODAMUS_SECRET_KEY", raising=False)
    status, _, body = _run_http_request(api_server, "POST", "/v1/webhooks/prodamus", {"Sign": "some_sig"}, b"order_id=1")
    assert status == 503
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "billing_unconfigured"


def test_api_webhook_missing_sign(api_server, monkeypatch):
    monkeypatch.setenv("PRODAMUS_SECRET_KEY", "test_secret_123")
    status, _, body = _run_http_request(api_server, "POST", "/v1/webhooks/prodamus", {}, b"order_id=1")
    assert status == 401
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "prodamus_signature_invalid"


def test_api_webhook_invalid_signature(api_server, monkeypatch):
    monkeypatch.setenv("PRODAMUS_SECRET_KEY", "test_secret_123")
    status, _, body = _run_http_request(
        api_server,
        "POST",
        "/v1/webhooks/prodamus",
        {"Sign": "bad_signature"},
        b"order_id=1&customer_extra=12345",
    )
    assert status == 401
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "prodamus_signature_invalid"


def test_api_webhook_ignored_status(api_server, monkeypatch):
    secret = "test_secret_123"
    monkeypatch.setenv("PRODAMUS_SECRET_KEY", secret)

    called_control = []
    monkeypatch.setattr(api, "call_control", lambda payload, timeout=5.0: called_control.append(payload) or {"ok": True})

    data = {
        "order_id": "test_ord",
        "payment_status": "fail",
        "customer_extra": "12345678",
    }
    sig = billing.prodamus_signature(data, secret)
    body = urllib.parse.urlencode(data).encode("utf-8")

    status, _, resp_body = _run_http_request(
        api_server,
        "POST",
        "/v1/webhooks/prodamus",
        {"Sign": sig, "Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    assert status == 200
    # non-success notifications are acknowledged exactly like successes so Prodamus never retries them
    assert resp_body.decode("utf-8").strip() == "success"
    assert len(called_control) == 0


def test_api_webhook_success_calls_control_and_returns_plain_text_success(api_server, monkeypatch):
    secret = "test_secret_123"
    monkeypatch.setenv("PRODAMUS_SECRET_KEY", secret)

    called_control = []
    monkeypatch.setattr(api, "call_control", lambda payload, timeout=5.0: called_control.append(payload) or {"ok": True})

    data = {
        "order_id": "ord_success_1",
        "order_num": "forge_12345678_1792700000",
        "sum": "2000",
        "payment_status": "success",
        "customer_extra": "12345678",
    }
    sig = billing.prodamus_signature(data, secret)
    body = urllib.parse.urlencode(data).encode("utf-8")

    status, headers, resp_body = _run_http_request(
        api_server,
        "POST",
        "/v1/webhooks/prodamus",
        {"Sign": sig, "Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    assert resp_body == b"success"

    assert len(called_control) == 1
    call = called_control[0]
    assert call["op"] == "billing_payment"
    assert call["notification"]["order_id"] == "ord_success_1"
    assert call["notification"]["user_id"] == 12345678
    assert call["notification"]["sum"] == "2000"


def test_api_pay_pages(api_server):
    # Test /pay/success
    status, headers, body = _run_http_request(api_server, "GET", "/pay/success")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert "Оплата принята, вернитесь в Telegram" in body.decode("utf-8")

    # Test /pay/return
    status, headers, body = _run_http_request(api_server, "GET", "/pay/return")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert "Оплата не завершена" in body.decode("utf-8")


def test_api_route_billing_me(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "call_control", lambda payload: seen.append(payload) or {"active": True, "plan": "vector"})
    headers = {"Authorization": "Bearer session_token_123"}
    status, result = api.route("GET", "/v1/billing/me", {}, headers)
    assert status == 200
    assert result == {"active": True, "plan": "vector"}
    assert seen == [{"op": "billing_status", "session": "session_token_123"}]


def test_api_route_billing_me_requires_session():
    with pytest.raises(api.ApiError, match="session_required") as exc:
        api.route("GET", "/v1/billing/me", {}, {})
    assert exc.value.status == 401


# ---------------------------------------------------------------------------
# 5. Control daemon operations (billing_payment & billing_status)
# ---------------------------------------------------------------------------

def test_control_billing_payment_and_prolongation(tmp_path, monkeypatch):
    billing_dir = tmp_path / "billing"
    billing_dir.mkdir()
    monkeypatch.setattr(control, "BILLING_DIR", billing_dir)
    monkeypatch.setattr(control, "_send_manager_telegram_message", lambda chat_id, text: True)

    user_id = 99887766
    summary1 = {
        "order_id": "ord_1",
        "order_num": f"forge_{user_id}_1792700001",
        "sum": "2000",
        "customer_extra": str(user_id),
        "user_id": user_id,
        "plan": "Вектор",
        "plan_days": 30,
        "products": [{"name": "Вектор", "price": "2000", "quantity": "1", "sum": "2000"}],
    }

    t0 = 1_800_000_000
    res1 = control.dispatch({"op": "billing_payment", "notification": summary1, "now": t0})
    assert res1.get("ok") is True
    assert res1.get("paid_until") == t0 + 30 * 86400

    # Verify state file exists and matches schema
    state = billing.read_state(billing_dir, user_id)
    assert state is not None
    assert state["schema"] == "hermes.billing/v1"
    assert state["user_id"] == user_id
    assert state["paid_until"] == t0 + 30 * 86400
    assert len(state["payments"]) == 1
    assert state["payments"][0]["order_id"] == "ord_1"

    # Second payment stacks (extends already active subscription)
    t1 = t0 + 10 * 86400  # 10 days later
    summary2 = {
        "order_id": "ord_2",
        "order_num": f"forge_{user_id}_1792700002",
        "sum": "2000",
        "customer_extra": str(user_id),
        "user_id": user_id,
        "plan": "Вектор",
        "plan_days": 30,
    }
    res2 = control.dispatch({"op": "billing_payment", "notification": summary2, "now": t1})
    assert res2.get("ok") is True
    # Existing paid_until was t0 + 30*86400. Stacking adds another 30*86400
    expected_until = t0 + 60 * 86400
    assert res2.get("paid_until") == expected_until

    state = billing.read_state(billing_dir, user_id)
    assert state["paid_until"] == expected_until
    assert len(state["payments"]) == 2


def test_control_billing_payment_deduplication(tmp_path, monkeypatch):
    billing_dir = tmp_path / "billing"
    billing_dir.mkdir()
    monkeypatch.setattr(control, "BILLING_DIR", billing_dir)
    monkeypatch.setattr(control, "_send_manager_telegram_message", lambda chat_id, text: True)

    user_id = 55443322
    summary = {
        "order_id": "duplicate_ord",
        "order_num": f"forge_{user_id}_1792700001",
        "sum": "2000",
        "customer_extra": str(user_id),
        "user_id": user_id,
        "plan_days": 30,
    }

    t0 = 1_800_000_000
    res1 = control.dispatch({"op": "billing_payment", "notification": summary, "now": t0})
    assert res1.get("ok") is True
    until1 = res1.get("paid_until")

    # Resend exact same order_id
    res2 = control.dispatch({"op": "billing_payment", "notification": summary, "now": t0 + 100})
    assert res2.get("duplicate") is True

    # State paid_until must remain unchanged
    state = billing.read_state(billing_dir, user_id)
    assert state["paid_until"] == until1
    assert len(state["payments"]) == 1


def test_control_billing_payment_invalid_customer_extra():
    # Customer extra is not valid 5-20 digits
    for invalid_uid in ["", "abc", "123", "1" * 25]:
        summary = {"customer_extra": invalid_uid, "order_id": "ord_bad"}
        with pytest.raises(control.ControlError, match="billing_customer_invalid") as exc:
            control.dispatch({"op": "billing_payment", "notification": summary})
        assert exc.value.status == 400


def test_control_billing_status_no_record(tmp_path, monkeypatch):
    billing_dir = tmp_path / "billing"
    billing_dir.mkdir()
    monkeypatch.setattr(control, "BILLING_DIR", billing_dir)

    control.SESSIONS = control.SessionStore(ttl=1000)
    user_id = 11112222
    session, _ = control.SESSIONS.create(user_id)

    res = control.dispatch({"op": "billing_status", "session": session})
    assert res == {"active": None}


def test_control_billing_status_active_record(tmp_path, monkeypatch):
    billing_dir = tmp_path / "billing"
    billing_dir.mkdir()
    monkeypatch.setattr(control, "BILLING_DIR", billing_dir)

    user_id = 33334444
    now = int(time.time())
    state = {
        "schema": "hermes.billing/v1",
        "user_id": user_id,
        "plan": "Вектор Плюс",
        "paid_until": now + 15 * 86400,
        "payments": [],
        "updated_at": now,
    }
    billing._atomic_write_json(billing_dir / f"{user_id}.json", state)

    control.SESSIONS = control.SessionStore(ttl=1000)
    session, _ = control.SESSIONS.create(user_id)

    res = control.dispatch({"op": "billing_status", "session": session})
    assert res["active"] is True
    assert res["plan"] == "Вектор Плюс"
    assert res["paid_until"] == now + 15 * 86400
    assert res["days_left"] in {14, 15}  # depending on seconds elapsed


# ---- architect review follow-ups ---------------------------------------------------------------

def _settings(**over):
    base = dict(form_url="https://shop.payform.ru/", secret_key="secret", plan_name="Hermes Forge", plan_price_rub=2000,
                plan_days=30, public_url="https://forge.example.com")
    base.update(over)
    return billing.BillingSettings(**base)


def test_load_settings_requires_explicit_price(tmp_path):
    env = tmp_path / "billing.env"
    env.write_text("PRODAMUS_FORM_URL=https://shop.payform.ru/\nPRODAMUS_SECRET_KEY=abc\n", encoding="utf-8")
    assert billing.load_settings(env) is None
    env.write_text("PRODAMUS_FORM_URL=https://shop.payform.ru/\nPRODAMUS_SECRET_KEY=abc\nFORGE_PLAN_PRICE_RUB=2990\n", encoding="utf-8")
    assert billing.load_settings(env).plan_price_rub == 2990


def test_is_forge_payment_requires_matching_order_and_customer():
    assert billing.is_forge_payment({"order_num": "forge_450206471_1792700000", "customer_extra": "450206471"})
    assert not billing.is_forge_payment({"order_num": "forge_450206471_1792700000", "customer_extra": "111111"})
    assert not billing.is_forge_payment({"order_num": "ProAiManual_club_3990", "customer_extra": "manual_club"})
    assert not billing.is_forge_payment({"order_num": "", "customer_extra": "450206471"})
    assert billing.forge_order_id(450206471, now=1792700000) == "forge_450206471_1792700000"


class _Resp:
    def __init__(self, text):
        self._t = text.encode()

    def read(self):
        return self._t

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_create_payment_link_posts_do_link_and_falls_back():
    seen = {}

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return _Resp("https://payform.ru/x12345/")

    link = billing.create_payment_link(_settings(), user_id=450206471, username="pavel", order_id="forge_450206471_1", opener=opener)
    assert link == "https://payform.ru/x12345/"
    assert seen["url"] == "https://shop.payform.ru/" and "do=link" in seen["body"] and "customer_extra=450206471" in seen["body"]
    assert "urlNotification=https%3A%2F%2Fforge.example.com%2Fv1%2Fwebhooks%2Fprodamus" in seen["body"]

    def broken(req, timeout=0):
        raise OSError("down")

    fallback = billing.create_payment_link(_settings(), user_id=450206471, username="pavel", order_id="forge_450206471_1", opener=broken)
    assert fallback.startswith("https://shop.payform.ru/?") and "do=pay" in fallback and "signature=" in fallback


def test_webhook_acknowledges_foreign_payment_without_control(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "call_control", lambda payload, **kw: calls.append(payload) or {"ok": True})
    monkeypatch.setenv("PRODAMUS_SECRET_KEY", "secret")
    data = {"order_id": "45915377", "order_num": "ProAiManual_club_3990", "sum": "3990", "payment_status": "success",
            "customer_extra": "manual_club", "products": [{"name": "Клуб", "price": "3990", "quantity": "1"}]}
    body = urllib.parse.urlencode({"order_id": data["order_id"], "order_num": data["order_num"], "sum": data["sum"],
                                   "payment_status": data["payment_status"], "customer_extra": data["customer_extra"],
                                   "products[0][name]": "Клуб", "products[0][price]": "3990", "products[0][quantity]": "1"}).encode()
    parsed = billing.parse_notification(body)
    sign = billing.prodamus_signature(parsed, "secret")

    class H:
        headers = {"Sign": sign, "Content-Length": str(len(body))}
        command = "POST"
        out = []

        def _raw_body(self):
            return body

        def _text(self, status, text):
            self.out.append(("text", status, text))

        def _json(self, status, payload):
            self.out.append(("json", status, payload))

    h = H()
    api.Handler._prodamus_webhook(h)
    assert h.out == [("text", 200, "success")]
    assert calls == []
