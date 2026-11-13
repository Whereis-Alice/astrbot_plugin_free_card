import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import FunctionTool, ToolSet
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import Message, TextPart

from free_card_plugin.card_api import UpdateResult
from free_card_plugin.main import DEFAULT_REMINDER, TOOL_NAME, FreeCardPlugin, FreeCardTool


class Event:
    def __init__(self, group="123", account="456", connection="qq", platform="aiocqhttp"):
        self.group, self.account, self.connection, self.platform = (
            group,
            account,
            connection,
            platform,
        )
        self.bot = SimpleNamespace(call_action=AsyncMock())

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.connection

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return self.account


def make_plugin(config=None):
    registered = []
    context = SimpleNamespace(add_llm_tools=lambda *tools: registered.extend(tools))
    plugin = FreeCardPlugin(context, config)
    plugin.put_kv_data = AsyncMock()
    plugin.get_kv_data = AsyncMock(return_value={})
    assert len(registered) == 1 and registered[0].name == TOOL_NAME
    return plugin


def request(plugin):
    unrelated = FunctionTool(name="other", description="other", parameters={})
    return ProviderRequest(prompt="hello", func_tool=ToolSet([unrelated, plugin._tool]))


def test_first_request_reminded_once_and_other_tools_unchanged():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    parts, tools = req.extra_user_content_parts, req.func_tool
    asyncio.run(plugin.remind(event, req))
    asyncio.run(plugin.remind(event, req))
    assert len(parts) == 1 and parts[0]._no_save
    assert DEFAULT_REMINDER in parts[0].text
    assert req.prompt == "hello" and req.func_tool is tools and len(tools.tools) == 2


def test_group_reminder_interval_applies_across_events():
    plugin = make_plugin()
    first, second = request(plugin), request(plugin)
    asyncio.run(plugin.remind(Event(), first))
    asyncio.run(plugin.remind(Event(), second))
    assert len(first.extra_user_content_parts) == 1 and not second.extra_user_content_parts


def test_elapsed_interval_reminds_again():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    asyncio.run(plugin.finish_reminder(event, SimpleNamespace(messages=[]), None))
    plugin._state(event, "123", "456").reminded_at -= 1801
    next_req = request(plugin)
    asyncio.run(plugin.remind(Event(), next_req))
    assert len(next_req.extra_user_content_parts) == 1


@pytest.mark.parametrize("mode", ["missing", "inactive", "disabled", "official", "private"])
def test_no_reminder_when_unavailable(mode):
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    if mode == "missing":
        req.func_tool = None
    if mode == "inactive":
        plugin._tool.active = False
    if mode == "disabled":
        plugin.config["reminder_interval_minutes"] = 0
    if mode == "official":
        event.platform = "qq_official"
    if mode == "private":
        event.group = ""
    asyncio.run(plugin.remind(event, req))
    assert not req.extra_user_content_parts


def test_exact_temporary_hint_cleanup_preserves_user_and_other_content():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    req.extra_user_content_parts.append(TextPart(text="other plugin"))
    asyncio.run(plugin.remind(event, req))
    hint = req.extra_user_content_parts[-1]
    user_text = TextPart(text=hint.text)  # Same visible text but not temporary.
    run = SimpleNamespace(
        messages=[
            Message(
                role="user",
                content=[
                    user_text,
                    TextPart(text="keep"),
                    TextPart(text=hint.text).mark_as_temp(),
                ],
            )
        ]
    )
    asyncio.run(plugin.bind_reminder(event, run))
    plugin._cleanup(plugin._reminders[id(event)])
    assert [p.text for p in req.extra_user_content_parts] == ["other plugin"]
    assert [p.text for p in run.messages[0].content] == [hint.text, "keep"]
    assert not plugin._reminders


def test_cleanup_one_event_does_not_touch_another():
    plugin = make_plugin()
    first, second = Event(), Event(group="789")
    req_a, req_b = request(plugin), request(plugin)
    asyncio.run(plugin.remind(first, req_a))
    asyncio.run(plugin.remind(second, req_b))
    asyncio.run(plugin.finish_reminder(first, SimpleNamespace(messages=[]), None))
    assert not req_a.extra_user_content_parts and len(req_b.extra_user_content_parts) == 1


def test_tool_dispatch_and_single_parameter():
    plugin, event = make_plugin(), Event()
    plugin.rename = AsyncMock(return_value="result")
    result = asyncio.run(
        plugin._tool.call(SimpleNamespace(context=SimpleNamespace(event=event)), card="Alice")
    )
    assert result == "result"
    plugin.rename.assert_awaited_once_with(event, "Alice")
    assert list(plugin._tool.parameters["properties"]) == ["card"]
    assert FreeCardTool().name == TOOL_NAME


@pytest.mark.parametrize("value", ["", None, "好" * 21])
def test_invalid_input_never_changes_state(monkeypatch, value):
    write = AsyncMock()
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin = make_plugin()
    assert "无法修改" in asyncio.run(plugin.rename(Event(), value))
    write.assert_not_called()
    assert not plugin._states


def test_failed_update_cooldown_and_reminder_share_state(monkeypatch):
    write = AsyncMock(return_value=UpdateResult(False, detail="retcode=1200"))
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin, event = make_plugin(), Event()
    assert "300" in asyncio.run(plugin.rename(event, "Alice"))
    assert "秒" in asyncio.run(plugin.rename(event, "Again"))
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    assert not req.extra_user_content_parts and write.await_count == 1


def test_concurrent_calls_write_once(monkeypatch):
    async def delayed(*args):
        await asyncio.sleep(0.01)
        return UpdateResult(True, True)

    write = AsyncMock(side_effect=delayed)
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin = make_plugin()

    async def run():
        return await asyncio.gather(plugin.rename(Event(), "Alice"), plugin.rename(Event(), "Aino"))

    replies = asyncio.run(run())
    assert write.await_count == 1 and sum("已确认" in reply for reply in replies) == 1


def test_accounts_groups_and_connections_are_isolated(monkeypatch):
    write = AsyncMock(return_value=UpdateResult(True, True))
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin = make_plugin()
    for event in [Event(), Event(account="789"), Event(group="789"), Event(connection="second")]:
        assert "已确认" in asyncio.run(plugin.rename(event, "Alice"))
    assert write.await_count == 4 and len(plugin._states) == 4


def test_cancelled_write_keeps_guard_and_releases_lock(monkeypatch):
    monkeypatch.setattr(
        "free_card_plugin.main.update_card", AsyncMock(side_effect=asyncio.CancelledError())
    )
    plugin, event = make_plugin(), Event()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.rename(event, "Alice"))
    state = plugin._state(event, "123", "456")
    assert not state.lock.locked() and state.write_after > time.monotonic()


def test_unverified_success_does_not_claim_confirmation(monkeypatch):
    monkeypatch.setattr(
        "free_card_plugin.main.update_card", AsyncMock(return_value=UpdateResult(True))
    )
    assert "暂时无法回读确认" in asyncio.run(make_plugin().rename(Event(), "Alice"))


def test_disabled_reminder_does_not_disable_tool(monkeypatch):
    monkeypatch.setattr(
        "free_card_plugin.main.update_card", AsyncMock(return_value=UpdateResult(True, True))
    )
    assert "已确认" in asyncio.run(
        make_plugin({"reminder_interval_minutes": 0}).rename(Event(), "Alice")
    )


def test_terminate_cleans_reminders_and_disables_tool():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    asyncio.run(plugin.terminate())
    assert not req.extra_user_content_parts and not plugin._tool.active
    assert "已卸载" in asyncio.run(plugin.rename(event, "Alice"))


def test_defaults_match_schema():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert len(schema) == 5 and schema["reminder_text"]["default"] == DEFAULT_REMINDER
    assert schema["base_name"]["default"] == ""
    assert schema["world_presence_enabled"]["default"] is True


def test_long_reminder_interval_survives_a_day_of_quiet():
    plugin, event = make_plugin({"reminder_interval_minutes": 10080}), Event()
    state = plugin._state(event, "123", "456")
    state.reminded_at = state.seen_at = time.monotonic() - 172800
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    assert not req.extra_user_content_parts


def test_success_postpones_reminder_even_with_no_write_cooldown(monkeypatch):
    monkeypatch.setattr(
        "free_card_plugin.main.update_card", AsyncMock(return_value=UpdateResult(True, True))
    )
    plugin, event = make_plugin({"min_update_seconds": 0}), Event()
    asyncio.run(plugin.rename(event, "Alice"))
    req = request(plugin)
    asyncio.run(plugin.remind(Event(), req))
    assert not req.extra_user_content_parts


@pytest.mark.parametrize("card", ["摸鱼中", "爱丽丝 摸鱼中"])
def test_fixed_base_is_applied_even_without_reminders(monkeypatch, card):
    write = AsyncMock(return_value=UpdateResult(True, True))
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin, event = make_plugin({"base_name": "爱丽丝", "reminder_interval_minutes": 0}), Event()
    reply = asyncio.run(plugin.rename(event, card))
    assert "爱丽丝 摸鱼中" in reply
    write.assert_awaited_once_with(event.bot, "123", "456", "爱丽丝 摸鱼中")


def test_fixed_base_suffixes_replace_each_other(monkeypatch):
    write = AsyncMock(return_value=UpdateResult(True, True))
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin, event = make_plugin({"base_name": "爱丽丝", "min_update_seconds": 0}), Event()
    for suffix in ["摸鱼中", "喝茶中", ""]:
        asyncio.run(plugin.rename(event, suffix))
    assert [call.args[-1] for call in write.await_args_list] == [
        "爱丽丝 摸鱼中",
        "爱丽丝 喝茶中",
        "爱丽丝",
    ]


def test_empty_base_preserves_full_card_behavior(monkeypatch):
    write = AsyncMock(return_value=UpdateResult(True, True))
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin, event = make_plugin({"base_name": "  "}), Event()
    asyncio.run(plugin.rename(event, "任意新名字"))
    write.assert_awaited_once_with(event.bot, "123", "456", "任意新名字")


def test_model_is_told_to_supply_only_suffix_in_fixed_mode():
    plugin, event = make_plugin({"base_name": "爱丽丝"}), Event()
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    assert "基础名字固定为 '爱丽丝'" in req.extra_user_content_parts[0].text
    assert "只填写后缀" in plugin._tool.parameters["properties"]["card"]["description"]
    assert "完整文本" not in plugin._tool.description
    assert list(plugin._tool.parameters["properties"]) == ["card"]


def test_each_tool_has_its_own_naming_description():
    fixed, free = make_plugin({"base_name": "爱丽丝"}), make_plugin()
    assert "只填写后缀" in fixed._tool.parameters["properties"]["card"]["description"]
    assert "完整群名片" in free._tool.parameters["properties"]["card"]["description"]


def test_clear_base_configuration_restores_full_name_description():
    plugin = make_plugin({"base_name": "爱丽丝", "reminder_interval_minutes": 0})
    plugin.config["base_name"] = ""
    asyncio.run(plugin.remind(Event(), request(plugin)))
    assert "完整群名片" in plugin._tool.parameters["properties"]["card"]["description"]


def test_combined_name_too_long_does_not_write_or_enter_cooldown(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    plugin = make_plugin({"base_name": "爱丽丝"})
    reply = asyncio.run(plugin.rename(Event(), "好" * 17))
    assert "太长" in reply
    assert not plugin._states
    write.assert_not_called()


@pytest.mark.parametrize("base", [42, "好" * 21, "bad\x00name"])
def test_invalid_base_configuration_never_writes(monkeypatch, base):
    write = AsyncMock()
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    assert "无法修改" in asyncio.run(make_plugin({"base_name": base}).rename(Event(), "新后缀"))
    write.assert_not_called()
