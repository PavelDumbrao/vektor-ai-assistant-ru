"""Pure, fail-closed Telegram Business rights classification."""

from __future__ import annotations

from typing import Any, Mapping


_REPLY_PROFILE_NON_ACTION_RIGHTS = frozenset({"can_read_messages"})


def classify_business_connection_rights_mapping(
    value: Any,
) -> tuple[dict[str, bool], bool, bool, bool]:
    """Return serialized rights, validity, receive-only and reply-profile flags.

    Telegram Desktop currently couples ``can_reply`` with
    ``can_read_messages``. The latter is accepted only as a non-action right:
    this module grants no read-receipt capability. Every other reported or
    future ``can_*`` action right must remain false.
    """
    if not isinstance(value, Mapping):
        return {}, False, False, False
    rights: dict[str, bool] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key.startswith("can_")
            or type(item) is not bool
        ):
            return {}, False, False, False
        rights[key] = item
    receive_only = all(enabled is False for enabled in rights.values())
    reply_profile = rights.get("can_reply") is True and all(
        enabled is False
        for name, enabled in rights.items()
        if name != "can_reply" and name not in _REPLY_PROFILE_NON_ACTION_RIGHTS
    )
    return rights, True, receive_only, reply_profile
