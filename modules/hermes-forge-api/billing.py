#!/usr/bin/env python3
"""Prodamus payment integration and billing state management for Hermes Forge."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes_forge_billing")


@dataclass(frozen=True)
class BillingSettings:
    form_url: str
    secret_key: str
    plan_name: str
    plan_price_rub: int
    plan_days: int
    public_url: str
    subscription_id: str = ""
    demo_mode: bool = False


def load_settings(path: Path = Path("/etc/proai-hermes-forge/billing.env")) -> BillingSettings | None:
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        return None
    try:
        raw_text = p.read_text(encoding="utf-8")
    except Exception:
        return None

    vars_map: dict[str, str] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            vars_map[k] = v

    form_url = vars_map.get("PRODAMUS_FORM_URL") or vars_map.get("FORGE_PRODAMUS_FORM_URL") or vars_map.get("FORM_URL") or ""
    secret_key = vars_map.get("PRODAMUS_SECRET_KEY") or vars_map.get("FORGE_PRODAMUS_SECRET_KEY") or vars_map.get("SECRET_KEY") or ""

    if not form_url or not secret_key:
        return None

    plan_name = vars_map.get("FORGE_PLAN_NAME") or vars_map.get("PRODAMUS_PLAN_NAME") or vars_map.get("PLAN_NAME") or "Подписка Hermes Forge"

    # No price, no payment links: the tariff is a product decision, never a default.
    try:
        plan_price_rub = int(vars_map.get("FORGE_PLAN_PRICE_RUB") or vars_map.get("PRODAMUS_PLAN_PRICE_RUB") or vars_map.get("PLAN_PRICE_RUB") or "0")
    except ValueError:
        plan_price_rub = 0
    if plan_price_rub <= 0 and not (vars_map.get("PRODAMUS_SUBSCRIPTION_ID") or vars_map.get("FORGE_SUBSCRIPTION_ID")):
        return None

    try:
        plan_days = int(vars_map.get("FORGE_PLAN_DAYS") or vars_map.get("PRODAMUS_PLAN_DAYS") or vars_map.get("PLAN_DAYS") or "30")
    except ValueError:
        plan_days = 30

    public_url = vars_map.get("FORGE_PUBLIC_URL") or vars_map.get("PUBLIC_URL") or ""
    subscription_id = vars_map.get("PRODAMUS_SUBSCRIPTION_ID") or vars_map.get("FORGE_SUBSCRIPTION_ID") or vars_map.get("SUBSCRIPTION_ID") or ""

    demo_val = (vars_map.get("PRODAMUS_DEMO_MODE") or vars_map.get("FORGE_DEMO_MODE") or vars_map.get("DEMO_MODE") or "").lower()
    demo_mode = demo_val in {"1", "true", "yes"}

    return BillingSettings(
        form_url=form_url,
        secret_key=secret_key,
        plan_name=plan_name,
        plan_price_rub=plan_price_rub,
        plan_days=plan_days,
        public_url=public_url,
        subscription_id=subscription_id,
        demo_mode=demo_mode,
    )


def _clean_value(val: Any) -> Any:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "1" if val else ""
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, dict):
        return {str(k): _clean_value(v) for k, v in sorted(val.items()) if k != "signature"}
    if isinstance(val, list):
        return [_clean_value(x) for x in val]
    return str(val)


def _serialize_prodamus_json(data: dict, escape_slashes: bool = True) -> str:
    cleaned = _clean_value({k: v for k, v in data.items() if k != "signature"})
    raw = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if escape_slashes:
        return raw.replace("/", r"\/")
    return raw


def prodamus_signature(data: dict, secret: str) -> str:
    serialized = _serialize_prodamus_json(data, escape_slashes=True)
    return hmac.new(secret.encode("utf-8"), serialized.encode("utf-8"), hashlib.sha256).hexdigest().lower()


def verify_signature(data: dict, secret: str, sign: str) -> bool:
    if not secret or not sign:
        return False
    norm_sign = sign.strip().lower()

    # 1. Official PHP mode: / escaped as \/
    sig_escaped = prodamus_signature(data, secret)
    if hmac.compare_digest(sig_escaped, norm_sign):
        logger.debug("prodamus signature verified (escaped slashes)")
        return True

    # 2. Python prodamuspy mode: / unescaped
    raw_json = _serialize_prodamus_json(data, escape_slashes=False)
    sig_raw = hmac.new(secret.encode("utf-8"), raw_json.encode("utf-8"), hashlib.sha256).hexdigest().lower()
    if hmac.compare_digest(sig_raw, norm_sign):
        logger.debug("prodamus signature verified (unescaped slashes)")
        return True

    logger.debug("prodamus signature mismatch")
    return False


def payment_link(settings: BillingSettings, *, user_id: int, username: str, order_id: str) -> str:
    params: dict[str, str] = {
        "do": "pay",
        "order_id": str(order_id),
        "customer_extra": str(user_id),
    }
    if settings.subscription_id:
        params["subscription"] = str(settings.subscription_id)
    else:
        params["products[0][name]"] = str(settings.plan_name)
        params["products[0][price]"] = str(settings.plan_price_rub)
        params["products[0][quantity]"] = "1"

    base_public = settings.public_url.rstrip("/")
    params["urlNotification"] = f"{base_public}/v1/webhooks/prodamus"
    params["urlSuccess"] = f"{base_public}/pay/success"
    params["urlReturn"] = f"{base_public}/pay/return"

    if settings.demo_mode:
        params["demo_mode"] = "1"

    signature = prodamus_signature(params, settings.secret_key)
    params["signature"] = signature

    query = urllib.parse.urlencode(params, safe="[]", quote_via=urllib.parse.quote)
    form_url = settings.form_url
    if "?" in form_url:
        sep = "" if form_url.endswith("?") or form_url.endswith("&") else "&"
    else:
        sep = "?"
    return f"{form_url}{sep}{query}"


FORGE_ORDER_RE = re.compile(r"^forge_(\d{5,20})_(\d{6,})$")


def forge_order_id(user_id: int, now: int | None = None) -> str:
    return f"forge_{int(user_id)}_{int(time.time() if now is None else now)}"


def is_forge_payment(summary: dict) -> bool:
    """True only for payments created by Forge: order_num = forge_<uid>_<ts> and customer_extra = the same uid.
    The Prodamus form is shared with other products, so everything else must be acknowledged and ignored."""
    match = FORGE_ORDER_RE.fullmatch(str(summary.get("order_num") or ""))
    return bool(match) and str(summary.get("customer_extra") or "") == match.group(1)


def create_payment_link(settings: BillingSettings, *, user_id: int, username: str, order_id: str,
                        opener=None, timeout: float = 30.0) -> str:
    """Proven path (club backend): POST do=link to the payform, Prodamus answers with a short payment URL.
    Falls back to the signed GET link when the payform does not answer with a URL."""
    import urllib.request
    params: dict[str, str] = {"do": "link", "order_id": str(order_id), "customer_extra": str(user_id)}
    if settings.subscription_id:
        params["subscription"] = str(settings.subscription_id)
    else:
        params["products[0][name]"] = str(settings.plan_name)
        params["products[0][price]"] = str(settings.plan_price_rub)
        params["products[0][quantity]"] = "1"
        params["products[0][type]"] = "service"
    base_public = settings.public_url.rstrip("/")
    params["urlNotification"] = f"{base_public}/v1/webhooks/prodamus"
    params["urlSuccess"] = f"{base_public}/pay/success"
    params["urlReturn"] = f"{base_public}/pay/return"
    if settings.demo_mode:
        params["demo_mode"] = "1"
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(settings.form_url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
        if text.startswith("http"):
            return text
        logger.warning("prodamus do=link returned no url (%d chars)", len(text))
    except Exception as exc:  # noqa: BLE001 - fall back, never break the bot
        logger.warning("prodamus do=link failed: %s", type(exc).__name__)
    return payment_link(settings, user_id=user_id, username=username, order_id=order_id)


def parse_notification(body: bytes) -> dict:
    if not body:
        return {}
    query_str = body.decode("utf-8", errors="replace")
    pairs = urllib.parse.parse_qsl(query_str, keep_blank_values=True)
    root: dict[str, Any] = {}
    for raw_key, value in pairs:
        match = re.match(r"^([^\[]+)(.*)$", raw_key)
        if not match:
            root[raw_key] = value
            continue
        first, rest = match.groups()
        subkeys = re.findall(r"\[(.*?)\]", rest)
        tokens = [first] + subkeys

        curr = root
        for token in tokens[:-1]:
            if token == "":
                token = str(len(curr))
            if token not in curr or not isinstance(curr[token], dict):
                curr[token] = {}
            curr = curr[token]

        last = tokens[-1]
        if last == "":
            last = str(len(curr))
        curr[last] = value

    def _to_arrays(obj: Any) -> Any:
        if not isinstance(obj, dict):
            return obj
        res = {k: _to_arrays(v) for k, v in obj.items()}
        if res and all(k.isdigit() for k in res.keys()):
            return [res[k] for k in sorted(res.keys(), key=int)]
        return res

    return _to_arrays(root)


def notification_summary(data: dict) -> dict:
    order_id = str(data.get("order_id") or "")
    order_num = str(data.get("order_num") or "")
    raw_sum = str(data.get("sum") or "")
    currency = str(data.get("currency") or "RUB")
    payment_status = str(data.get("payment_status") or "")
    customer_extra = str(data.get("customer_extra") or "")

    user_id: int | None = None
    if customer_extra.isdigit():
        try:
            user_id = int(customer_extra)
        except ValueError:
            user_id = None

    products_raw = data.get("products")
    products: list[dict[str, str]] = []
    if isinstance(products_raw, list):
        for item in products_raw:
            if isinstance(item, dict):
                p_price = str(item.get("price") or "")
                p_qty = str(item.get("quantity") or "1")
                p_sum = str(item.get("sum") or "")
                if not p_sum and p_price:
                    try:
                        p_sum = str(float(p_price) * float(p_qty))
                    except Exception:
                        p_sum = p_price
                products.append({
                    "name": str(item.get("name") or ""),
                    "price": p_price,
                    "quantity": p_qty,
                    "sum": p_sum,
                })
    elif isinstance(products_raw, dict):
        for _, item in sorted(products_raw.items()):
            if isinstance(item, dict):
                p_price = str(item.get("price") or "")
                p_qty = str(item.get("quantity") or "1")
                p_sum = str(item.get("sum") or "")
                if not p_sum and p_price:
                    try:
                        p_sum = str(float(p_price) * float(p_qty))
                    except Exception:
                        p_sum = p_price
                products.append({
                    "name": str(item.get("name") or ""),
                    "price": p_price,
                    "quantity": p_qty,
                    "sum": p_sum,
                })

    sub = data.get("subscription")
    subscription = sub if isinstance(sub, dict) else None
    date_val = str(data.get("date") or "")

    return {
        "order_id": order_id,
        "order_num": order_num,
        "sum": raw_sum,
        "currency": currency,
        "payment_status": payment_status,
        "customer_extra": customer_extra,
        "user_id": user_id,
        "products": products,
        "subscription": subscription,
        "date": date_val,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError("billing_state_path_unsafe")
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        if os.geteuid() == 0:
            try:
                os.chown(temp, 0, 0)
            except Exception:
                pass
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def read_state(root: Path, user_id: int) -> dict | None:
    path = Path(root) / f"{user_id}.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema") == "hermes.billing/v1":
            return data
    except Exception:
        return None
    return None


def billing_state(user_id: int, root: Path = Path(os.environ.get("FORGE_BILLING_DIR", "/var/lib/proai-hermes-forge/billing"))) -> dict | None:
    return read_state(root, user_id)


def apply_payment(
    root: Path,
    user_id: int,
    summary: dict,
    plan_days: int = 30,
    now: int | None = None,
) -> dict:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    order_id = str(summary.get("order_id") or "")

    existing = read_state(root, user_id)
    payments: list[dict[str, Any]] = list(existing.get("payments", [])) if existing else []

    if order_id and any(p.get("order_id") == order_id for p in payments):
        return {"ok": True, "duplicate": True, "state": existing}

    current = int(time.time()) if now is None else int(now)
    existing_until = int(existing.get("paid_until", 0)) if existing else 0
    paid_until = max(current, existing_until) + int(plan_days) * 86400

    sub = summary.get("subscription")
    sub_id = str(summary.get("subscription_id") or (sub.get("id") if isinstance(sub, dict) else "") or "")

    plan_name = ""
    products = summary.get("products") or []
    if isinstance(products, list) and len(products) > 0:
        plan_name = str(products[0].get("name") or "")
    if not plan_name and existing:
        plan_name = str(existing.get("plan") or "")
    if not plan_name:
        plan_name = "default"

    new_payment = {
        "order_id": order_id,
        "order_num": str(summary.get("order_num") or ""),
        "sum": str(summary.get("sum") or ""),
        "currency": str(summary.get("currency") or "RUB"),
        "date": str(summary.get("date") or ""),
        "received_at": current,
        "products": products,
        "subscription_id": sub_id,
    }
    payments.append(new_payment)

    new_state = {
        "schema": "hermes.billing/v1",
        "user_id": int(user_id),
        "plan": plan_name,
        "paid_until": paid_until,
        "payments": payments,
        "updated_at": current,
    }
    _atomic_write_json(root / f"{user_id}.json", new_state)
    return {"ok": True, "duplicate": False, "state": new_state, "paid_until": paid_until}
