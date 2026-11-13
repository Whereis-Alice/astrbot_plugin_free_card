import asyncio
import importlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import FunctionTool, ToolSet
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.pipeline.context import call_event_hook
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    StarHandlerRegistry,
    star_handlers_registry,
)

from free_card_plugin.card_api import UpdateResult
from free_card_plugin.main import (
    DEFAULT_REMINDER,
    REQUIRED_ACTION,
    TOOL_NAME,
    FreeCardPlugin,
    FreeCardTool,
)


class Event:
    def __init__(self, group="123", account="456", connection="qq", platform="aiocqhttp"):
        self.group, self.account, self.connection, self.platform = (
            group,
            account,
            connection,
            platform,
        )
        self.bot = SimpleNamespace(call_action=AsyncMock())
        self.plugins_name = None
        self.stopped = False
        self.trace = SimpleNamespace(record=lambda *args, **kwargs: None)

    def is_stopped(self):
        return self.stopped

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


def test_first_request_reminded_once_and_gates_only_its_own_toolset():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    parts, tools = req.extra_user_content_parts, req.func_tool
    asyncio.run(plugin.remind(event, req))
    asyncio.run(plugin.remind(event, req))
    assert len(parts) == 1 and parts[0]._no_save
    assert DEFAULT_REMINDER in parts[0].text
    assert req.prompt == "hello" and req.func_tool is not tools
    assert req.func_tool.tools == [plugin._tool] and len(tools.tools) == 2
    assert REQUIRED_ACTION in parts[0].text


@pytest.mark.parametrize("outcome", ["invalid", "failed", "canceled", "successful"])
def test_card_tool_restores_followup_tools_for_all_outcomes(monkeypatch, outcome):
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    original = req.func_tool
    asyncio.run(plugin.remind(event, req))
    write = AsyncMock(return_value=UpdateResult(outcome == "successful", outcome == "successful"))
    if outcome == "canceled":
        write.side_effect = asyncio.CancelledError()
    monkeypatch.setattr("free_card_plugin.main.update_card", write)
    if outcome == "canceled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(plugin.rename(event, "Alice New"))
    else:
        asyncio.run(plugin.rename(event, "" if outcome == "invalid" else "Alice New"))
    assert req.func_tool is original and len(original.tools) == 2
    assert not req.extra_user_content_parts and not plugin._reminders


def test_proactive_guard_is_retained_and_cloned_gate_can_be_restored():
    plugin, event = make_plugin(), Event()
    guarded = FunctionTool(name=TOOL_NAME, description="guarded", parameters={})
    other = FunctionTool(name="other", description="other", parameters={})
    original = ToolSet([guarded, other])
    req = ProviderRequest(prompt="hello", func_tool=original)
    asyncio.run(plugin.remind(event, req))
    assert req.func_tool.get_tool(TOOL_NAME) is guarded
    req.func_tool = ToolSet(list(req.func_tool.tools))
    asyncio.run(plugin.finish_reminder(event, SimpleNamespace(messages=[]), None))
    assert req.func_tool is original and req.func_tool.get_tool(TOOL_NAME) is guarded


def test_cleanup_respects_permission_revocation_after_our_gate():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    revoked = ToolSet()
    req.func_tool = revoked
    asyncio.run(plugin.finish_reminder(event, SimpleNamespace(messages=[]), None))
    assert req.func_tool is revoked


def test_skills_like_keeps_original_toolset_for_its_raw_executors():
    plugin, event = make_plugin(), Event()
    plugin.context.get_config = lambda _: {"agent_runner": {"config": {
        "misc": {"tool_schema_mode": "skills_like"}}}}
    req = request(plugin)
    original = req.func_tool
    asyncio.run(plugin.remind(event, req))
    assert req.func_tool is original and REQUIRED_ACTION in req.extra_user_content_parts[0].text


def test_shared_host_tools_and_other_request_are_never_globally_restricted():
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    shared = req.func_tool
    other = ProviderRequest(prompt="other group", func_tool=shared)
    asyncio.run(plugin.remind(event, req))
    assert len(shared.tools) == 2 and other.func_tool is shared
    assert all(t.active for t in shared.tools)


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
    asyncio.run(plugin.bind_reminder(event, SimpleNamespace(messages=[])))
    plugin._cleanup(plugin._reminders[id(event)])
    plugin._state(event, "123", "456").reminded_at -= 1801
    next_req = request(plugin)
    asyncio.run(plugin.remind(Event(), next_req))
    assert len(next_req.extra_user_content_parts) == 1


def test_world_reply_gate_does_not_consume_the_next_real_reminder(monkeypatch):
    plugin = make_plugin()
    registry = StarHandlerRegistry()
    reminder = next(h for h in star_handlers_registry
                    if h.handler_module_path == "free_card_plugin.main" and h.handler_name == "remind")
    registry.append(replace(reminder, handler=plugin.remind))

    async def world_gate(event, req):
        if req.prompt == "suppressed":
            event.stopped = True

    registry.append(StarHandlerMetadata(
        event_type=EventType.OnLLMRequestEvent,
        handler_full_name="world_gate", handler_name="world_gate",
        handler_module_path="test_world_gate", handler=world_gate,
        event_filters=[], extras_configs={"priority": -1_000_000},
    ))
    monkeypatch.setitem(star_map, "test_world_gate", SimpleNamespace(
        name="my_world", display_name="my_world", activated=True))
    monkeypatch.setattr(importlib.import_module(call_event_hook.__module__),
                        "star_handlers_registry", registry)

    async def scenario():
        blocked = request(plugin)
        blocked.prompt = "suppressed"
        assert await call_event_hook(Event(), EventType.OnLLMRequestEvent, blocked)
        assert not blocked.extra_user_content_parts and not plugin._states
        allowed = request(plugin)
        assert not await call_event_hook(Event(), EventType.OnLLMRequestEvent, allowed)
        assert len(allowed.extra_user_content_parts) == 1
    asyncio.run(scenario())


def test_already_stopped_event_never_consumes_reminder():
    plugin, event = make_plugin(), Event()
    event.stopped = True
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    assert not req.extra_user_content_parts and not plugin._states


def test_reminder_lifecycle_logs_without_logging_prompt(caplog):
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    with caplog.at_level("INFO", logger="astrbot"):
        asyncio.run(plugin.remind(event, req))
        asyncio.run(plugin.bind_reminder(event, SimpleNamespace(messages=[])))
        asyncio.run(plugin.finish_reminder(event, SimpleNamespace(messages=[]), None))
    for label in ("已准备改名提醒", "模型已开始处理改名提醒", "没有调用改名工具"):
        assert label in caplog.text
    assert req.prompt not in caplog.text and DEFAULT_REMINDER not in caplog.text


def test_preparation_does_not_reset_interval_and_canceled_request_is_released(monkeypatch, caplog):
    clock = [1700.0]
    monkeypatch.setattr("free_card_plugin.main.time", SimpleNamespace(monotonic=lambda: clock[0]))
    plugin, event = make_plugin({"reminder_interval_minutes": 10}), Event()
    state = plugin._state(event, "123", "456")
    state.reminded_at = 1000.0
    req = request(plugin)
    original = req.func_tool
    asyncio.run(plugin.remind(event, req))
    assert state.reminded_at == 1000.0
    clock[0] += 121
    next_req = request(plugin)
    with caplog.at_level("INFO", logger="astrbot"):
        asyncio.run(plugin.remind(Event(), next_req))
    assert len(next_req.extra_user_content_parts) == 1
    assert not req.extra_user_content_parts and req.func_tool is original
    assert state.reminded_at == 1000.0 and "未开始的旧提醒" in caplog.text


def test_interval_starts_once_when_agent_begins(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("free_card_plugin.main.time", SimpleNamespace(monotonic=lambda: clock[0]))
    plugin, event = make_plugin({"reminder_interval_minutes": 10}), Event()
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    assert plugin._state(event, "123", "456").reminded_at is None
    clock[0] = 1100.0
    asyncio.run(plugin.bind_reminder(event, SimpleNamespace(messages=[])))
    clock[0] = 1150.0
    asyncio.run(plugin.bind_reminder(event, SimpleNamespace(messages=[])))
    assert plugin._state(event, "123", "456").reminded_at == 1100.0
    plugin._cleanup(plugin._reminders[id(event)])
    clock[0] = 1699.0
    early = request(plugin)
    asyncio.run(plugin.remind(Event(), early))
    assert not early.extra_user_content_parts
    clock[0] = 1700.0
    due = request(plugin)
    asyncio.run(plugin.remind(Event(), due))
    assert len(due.extra_user_content_parts) == 1


def test_closed_preparation_does_not_postpone_next_real_request(caplog):
    plugin, event = make_plugin(), Event()
    req = request(plugin)
    asyncio.run(plugin.remind(event, req))
    with caplog.at_level("INFO", logger="astrbot"):
        asyncio.run(plugin.finish_reminder(event, None, None))
    assert plugin._state(event, "123", "456").reminded_at is None
    assert "模型开始前结束" in caplog.text and "没有调用改名工具" not in caplog.text
    next_req = request(plugin)
    asyncio.run(plugin.remind(Event(), next_req))
    assert len(next_req.extra_user_content_parts) == 1


def test_failed_agent_task_without_done_hook_does_not_block_next_due_reminder(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr("free_card_plugin.main.time", SimpleNamespace(monotonic=lambda: clock[0]))
    plugin, event = make_plugin({"reminder_interval_minutes": 10}), Event()
    req = request(plugin)
    original = req.func_tool
    asyncio.run(plugin.remind(event, req))
    # The owning task terminates without the AgentDone hook, as on a provider error.
    asyncio.run(plugin.bind_reminder(event, SimpleNamespace(messages=[])))
    clock[0] = 1600.0
    next_req = request(plugin)
    with caplog.at_level("WARNING", logger="astrbot"):
        asyncio.run(plugin.remind(Event(), next_req))
    assert len(next_req.extra_user_content_parts) == 1
    assert req.func_tool is original and not req.extra_user_content_parts
    assert "上次模型任务已结束" in caplog.text


def test_skip_log_explains_remaining_time_and_is_rate_limited(caplog):
    plugin, event = make_plugin({"reminder_interval_minutes": 10}), Event()
    state = plugin._state(event, "123", "456")
    state.reminded_at = time.monotonic() - 300
    with caplog.at_level("INFO", logger="astrbot"):
        for _ in range(2):
            asyncio.run(plugin.remind(event, request(plugin)))
    assert caplog.text.count("暂不提醒改名") == 1
    assert "提醒间隔设为 10 分 0 秒" in caplog.text and "还需" in caplog.text


@pytest.mark.parametrize("unchanged", [False, True])
def test_success_log_reports_actual_name_and_distinguishes_unchanged(monkeypatch, caplog, unchanged):
    monkeypatch.setattr("free_card_plugin.main.update_card",
                        AsyncMock(return_value=UpdateResult(True, True, unchanged=unchanged)))
    with caplog.at_level("INFO", logger="astrbot"):
        asyncio.run(make_plugin().rename(Event(), "Alice Tea"))
    assert "Alice Tea" in caplog.text and "QQ 回读已确认" in caplog.text
    assert ("无需重复写入" if unchanged else "改名成功") in caplog.text


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
