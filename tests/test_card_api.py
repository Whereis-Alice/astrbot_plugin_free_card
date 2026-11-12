import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from free_card_plugin.card_api import call, update_card, validate_card


def member(card="old", **extra):
    return {"group_id": 123, "user_id": 456, "card": card, **extra}


@pytest.mark.parametrize(
    "value", [None, 123, "", "  ", "a" * 33, "好" * 21, "bad\x00name", "bad\ud800", "bad\u202ename"]
)
def test_invalid_card_is_rejected(value):
    with pytest.raises(ValueError):
        validate_card(value)


def test_emoji_and_whitespace():
    assert validate_card(" 爱乃 \n 放飞中 👩‍✈️ ") == "爱乃 放飞中 👩‍✈️"
    assert validate_card("好" * 20) == "好" * 20


def test_success_is_verified_and_correct_account_is_routed():
    client = SimpleNamespace(call_action=AsyncMock(side_effect=[member(), None, member("new")]))
    result = asyncio.run(update_card(client, "123", "456", "new"))
    assert result.verified and result.accepted
    client.call_action.assert_any_await(
        "set_group_card", self_id="456", group_id=123, user_id=456, card="new"
    )
    assert client.call_action.await_args_list[0].kwargs["no_cache"] is True


def test_current_card_does_not_write():
    client = SimpleNamespace(call_action=AsyncMock(return_value=member("new")))
    assert asyncio.run(update_card(client, "123", "456", "new")).verified
    assert client.call_action.await_count == 1


def test_full_onebot_envelope_and_api_wrapper():
    client = SimpleNamespace(
        api=SimpleNamespace(
            call_action=AsyncMock(
                return_value={
                    "status": "ok",
                    "retcode": "0",
                    "data": member(),
                }
            )
        )
    )
    assert asyncio.run(call(client, "456", "get_group_member_info")) == member()


def test_error_after_applied_write_is_verified():
    client = SimpleNamespace(
        call_action=AsyncMock(
            side_effect=[
                member(),
                {"status": "failed", "retcode": 1200, "data": None},
                member("new"),
            ]
        )
    )
    assert asyncio.run(update_card(client, "123", "456", "new")).verified


def test_accepted_with_unavailable_readback_is_not_verified():
    client = SimpleNamespace(
        call_action=AsyncMock(side_effect=[member(), None, RuntimeError("no API")])
    )
    result = asyncio.run(update_card(client, "123", "456", "new"))
    assert result.accepted and not result.verified and "no API" in result.detail


def test_failure_has_detail_and_never_retries_write(monkeypatch):
    monkeypatch.setattr("free_card_plugin.card_api.asyncio.sleep", AsyncMock())
    client = SimpleNamespace(
        call_action=AsyncMock(
            side_effect=[
                member(),
                {"status": "failed", "retcode": 1200, "message": ""},
                member(),
                member(),
            ]
        )
    )
    result = asyncio.run(update_card(client, "123", "456", "new"))
    assert not result.accepted and "1200" in result.detail
    assert sum(c.args[0] == "set_group_card" for c in client.call_action.await_args_list) == 1


def test_success_with_stale_readback_is_failure(monkeypatch):
    monkeypatch.setattr("free_card_plugin.card_api.asyncio.sleep", AsyncMock())
    client = SimpleNamespace(
        call_action=AsyncMock(side_effect=[member(), None, member(), member()])
    )
    assert not asyncio.run(update_card(client, "123", "456", "new")).accepted


def test_response_for_another_account_cannot_verify():
    client = SimpleNamespace(
        call_action=AsyncMock(
            side_effect=[
                member(user_id=999),
                {"status": "failed", "retcode": 1200},
                member("new", user_id=999),
            ]
        )
    )
    result = asyncio.run(update_card(client, "123", "456", "new"))
    assert not result.accepted and "不一致" in result.detail


def test_timeout_after_application_uses_readback():
    client = SimpleNamespace(
        call_action=AsyncMock(side_effect=[member(), TimeoutError(), member("new")])
    )
    assert asyncio.run(update_card(client, "123", "456", "new")).verified


def test_transport_timeout_is_bounded(monkeypatch):
    timeout_values = []

    async def timed(awaitable, *, timeout):
        timeout_values.append(timeout)
        await awaitable
        raise TimeoutError()

    monkeypatch.setattr("free_card_plugin.card_api.asyncio.wait_for", timed)
    with pytest.raises(TimeoutError):
        asyncio.run(call(SimpleNamespace(call_action=AsyncMock()), "456", "test"))
    assert timeout_values == [10]
