import base64
import json
from email import policy
from email.parser import BytesParser

import pytest

pytest_plugins = ["test_focus_assistant"]


class Mailbox:
    def __init__(self):
        self.sends = []
        self.fail = False
        self.read_fail = False

    def binding(self, app):
        return "mail-connection"

    def mail_profile(self):
        return "owner@example.test"

    def find_sent_mail(self, message_id):
        return ["message123"] if self.sends else []

    def send_mail(self, payload):
        self.sends.append(payload)
        if self.fail:
            raise TimeoutError("secret must not escape")
        return {"id": "message123"}

    def mail_message(self, mid, metadata=False):
        if self.read_fail:
            raise TimeoutError("readback failed")
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(self.sends[-1]["raw"])
        )
        return {
            "id": mid,
            "labelIds": ["SENT"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": k, "value": v} for k, v in message.items()],
                "body": {
                    "data": base64.urlsafe_b64encode(
                        message.get_content().encode()
                    ).decode()
                },
            },
        }


def make_draft(plugin, client, rid="mail-test-v1", body="Здравствуйте! Это тест."):
    return plugin.mail.run(
        {
            "action": "draft",
            "request_id": rid,
            "to": ["recipient@example.test"],
            "subject": "Тест",
            "body": body,
        },
        client,
    )


def test_draft_is_local_and_immutable(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    assert client.sends == []
    assert draft["sent"] is False
    assert "Здравствуйте" in draft["preview"]
    with pytest.raises(ValueError, match="immutable"):
        make_draft(plugin, client, body="Другой текст")


def test_send_is_once_with_exact_readback(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    args = {
        "action": "send",
        "draft_id": draft["draft_id"],
        "confirm_hash": draft["confirm_hash"],
    }
    first = plugin.mail.run(args, client)
    second = plugin.mail.run(args, client)
    assert first["sent"] is True and first["verified"] is True
    assert second["deduplicated"] is True
    assert len(client.sends) == 1


def test_unknown_send_result_cannot_be_retried_or_bypassed(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    client.fail = True
    args = {
        "action": "send",
        "draft_id": draft["draft_id"],
        "confirm_hash": draft["confirm_hash"],
    }
    first = plugin.mail.run(args, client)
    assert first["error"] == "mail_send_uncertain" and "secret" not in json.dumps(first)
    with pytest.raises(ValueError, match="not_retryable"):
        plugin.mail.run(args, client)
    other = make_draft(plugin, client, rid="mail-test-v2")
    with pytest.raises(ValueError, match="unresolved"):
        plugin.mail.run(
            {
                "action": "send",
                "draft_id": other["draft_id"],
                "confirm_hash": other["confirm_hash"],
            },
            client,
        )
    assert len(client.sends) == 1


def test_lost_readback_does_not_cause_resend(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    client.read_fail = True
    args = {
        "action": "send",
        "draft_id": draft["draft_id"],
        "confirm_hash": draft["confirm_hash"],
    }
    result = plugin.mail.run(args, client)
    assert result["sent"] is True and result["verified"] is False
    assert plugin.mail.run(args, client)["deduplicated"] is True
    assert len(client.sends) == 1
    client.read_fail = False
    verified = plugin.mail.run(
        {"action": "verify", "draft_id": draft["draft_id"]}, client
    )
    assert verified["verified"] is True
    assert len(client.sends) == 1


def test_uncertain_send_can_be_reconciled_without_second_post(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    client.fail = True
    plugin.mail.run(
        {
            "action": "send",
            "draft_id": draft["draft_id"],
            "confirm_hash": draft["confirm_hash"],
        },
        client,
    )
    result = plugin.mail.run(
        {"action": "verify", "draft_id": draft["draft_id"]}, client
    )
    assert result["verified"] is True
    assert len(client.sends) == 1


def test_changed_hash_and_header_injection_are_rejected(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    with pytest.raises(ValueError, match="mismatch"):
        plugin.mail.run(
            {"action": "send", "draft_id": draft["draft_id"], "confirm_hash": "bad"},
            client,
        )
    with pytest.raises(ValueError):
        plugin.mail.addresses(
            ["x@example.test\r\nBcc: outsider@example.test"], required=True
        )
    assert client.sends == []


def test_cron_cannot_send_and_owner_sees_full_preview(plugin):
    client = Mailbox()
    draft = make_draft(plugin, client)
    args = {
        "action": "send",
        "draft_id": draft["draft_id"],
        "confirm_hash": draft["confirm_hash"],
    }
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="cron", turn_id="t", platform="cron")
    assert (
        gate.guard(
            tool_name="assistant_mail", args=args, session_id="cron", turn_id="t"
        )["action"]
        == "block"
    )
    gate.observe(
        session_id="s",
        turn_id="t",
        sender_id="1",
        platform="telegram",
        chat_type="dm",
        raw_user_message="Отправь показанное письмо",
        is_internal_event=False,
    )
    approval = gate.guard(
        tool_name="assistant_mail",
        args=args,
        session_id="s",
        turn_id="t",
        tool_call_id="call",
    )
    assert approval["action"] == "approve"
    assert draft["preview"] in approval["message"]
    gate.authorize("assistant_mail", args, "s")
    with pytest.raises(ValueError):
        gate.authorize("assistant_mail", args, "s")


def test_unsafe_maton_send_route_is_redirected_to_journaled_send(plugin):
    result = plugin._maton_write_guard(
        tool_name="mcp__maton__run_action",
        args={"id": "google-mail.message.send", "args": {}},
    )
    assert result["action"] == "block"


def test_html_email_scripts_are_not_exposed_as_instructions(plugin):
    payload = {
        "mimeType": "text/html",
        "body": {
            "data": base64.urlsafe_b64encode(
                b"<p>Hello</p><script>steal()</script>"
            ).decode()
        },
    }
    assert "steal" not in plugin.mail.message_text(payload)


def test_encoded_subject_and_legacy_charset_are_decoded(plugin):
    from email.header import Header

    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": Header("Привет", "utf-8").encode()},
            {"name": "Content-Type", "value": "text/plain; charset=windows-1251"},
        ],
        "body": {"data": base64.urlsafe_b64encode("Привет".encode("cp1251")).decode()},
    }
    assert plugin.mail.decoded_headers(payload)["subject"] == "Привет"
    assert plugin.mail.message_text(payload) == "Привет"
