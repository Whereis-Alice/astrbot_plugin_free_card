"""Persistent management actions, honest history, and exact group boundaries."""

import asyncio
import copy
import csv
import io
import time
from types import SimpleNamespace

import pytest
from test_plugin import Event, make_plugin, request

from free_card_plugin.card_api import UpdateResult
from free_card_plugin.main import FreeCardPlugin
from free_card_plugin.management import MAX_HISTORY, STORE_KEY, validate_settings


class Config(dict):
    def __init__(self, **values):
        super().__init__(**values)
        self.saved = None
        self.fail = False

    async def save_config_async(self):
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("disk unavailable")
        self.saved = dict(self)


@pytest.mark.parametrize(
    "patch",
    [
        None,
        [],
        {},
        {"extra": True},
        {"base_name": None},
        {"base_name": "好" * 21},
        {"reminder_interval_minutes": True},
        {"min_update_seconds": -1},
        {"min_update_seconds": 3.5},
        {"world_presence_enabled": "false"},
        {"reminder_text": "x" * 2001},
    ],
)
def test_invalid_settings_cannot_be_saved(patch):
    with pytest.raises(ValueError):
        validate_settings(patch)


def test_settings_save_preserves_config_reference_and_unrelated_fields():
    async def scenario():
        config = Config(base_name="Alice", reminder_interval_minutes=30, unrelated="preserve")
        plugin = make_plugin(config)
        await plugin._management.save_settings({"base_name": "", "reminder_interval_minutes": 10})
        assert plugin.config is config and config["unrelated"] == "preserve"
        assert config.saved == dict(config)
        assert "不必保留角色名" in plugin._tool.description
        assert await plugin._management.save_settings({"base_name": " 星光 "})
        assert "星光" in plugin._tool.description and config["base_name"] == "星光"
        before = dict(config)
        config.fail = True
        with pytest.raises(OSError):
            await plugin._management.save_settings({"base_name": "失效配置"})
        assert dict(config) == before and "失效配置" not in plugin._tool.description

    asyncio.run(scenario())


def test_concurrent_settings_updates_save_the_combined_config():
    async def scenario():
        config = Config(base_name="", reminder_interval_minutes=30)
        plugin = make_plugin(config)
        await asyncio.gather(
            plugin._management.save_settings({"base_name": "固定"}),
            plugin._management.save_settings({"reminder_interval_minutes": 12}),
        )
        assert (
            config.saved["base_name"] == "固定" and config.saved["reminder_interval_minutes"] == 12
        )

    asyncio.run(scenario())


def test_real_write_records_before_after_once_and_free_mode_does_not_add_a_name():
    async def scenario():
        plugin, event = make_plugin({"base_name": ""}), Event()
        event.bot.call_action.side_effect = [
            {"group_id": 123, "user_id": 456, "card": "Alice old"},
            None,
            {"group_id": 123, "user_id": 456, "card": "月亮打了个盹"},
        ]
        assert "已确认" in await plugin.rename(event, "月亮打了个盹")
        manager = plugin._management
        assert len(manager.history) == 1
        row = manager.history[0]
        assert (row["source"], row["status"], row["previous"], row["actual"]) == (
            "tool",
            "confirmed",
            "Alice old",
            "月亮打了个盹",
        )
        assert manager.groups()[0]["card"] == "月亮打了个盹"
        assert event.bot.call_action.await_count == 3  # The history layer adds no OneBot reads.
        saved = next(
            call.args[1]
            for call in reversed(plugin.put_kv_data.await_args_list)
            if call.args[0] == STORE_KEY
        )
        replacement = make_plugin()
        replacement.get_kv_data.return_value = copy.deepcopy(saved)
        await replacement._management.load()
        assert replacement._management.history == manager.history
        assert replacement._management.observations == manager.observations

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result,status",
    [
        (UpdateResult(True, True, previous="old", actual="new"), "confirmed"),
        (UpdateResult(True, detail="read unavailable", previous="old"), "pending"),
        (UpdateResult(False, detail="1200", previous="old", actual="old"), "failed"),
        (UpdateResult(True, True, unchanged=True, previous="new", actual="new"), "unchanged"),
    ],
)
def test_history_does_not_claim_unverified_or_failed_writes_succeeded(result, status):
    async def scenario():
        plugin, event = make_plugin(), Event()
        row = await plugin._track_world_target(event, plugin._target(event))
        await plugin._management.record_result(row, "webui", "new", result)
        entry = plugin._management.history[-1]
        assert entry["status"] == status and entry["previous"] == result.previous
        assert plugin._management.groups()[0]["card"] == result.actual

    asyncio.run(scenario())


def test_failed_history_storage_does_not_turn_an_applied_write_into_a_failure():
    async def scenario():
        plugin, event = make_plugin(), Event()
        row = await plugin._track_world_target(event, plugin._target(event))
        plugin.put_kv_data.side_effect = OSError("disk")
        await plugin._management.record_result(row, "tool", "new", UpdateResult(True, True))
        assert plugin._management.history[-1]["status"] == "confirmed"
        assert plugin._management.groups()[0]["card"] == "new"

    asyncio.run(scenario())


def test_world_no_op_does_not_add_duplicate_history():
    async def scenario():
        plugin, event = make_plugin(), Event()
        row = await plugin._track_world_target(event, plugin._target(event))
        for source in ("world", "world_restore"):
            await plugin._management.record_result(
                row, source, "same", UpdateResult(True, True, unchanged=True)
            )
        assert not plugin._management.history

    asyncio.run(scenario())


def test_pause_only_affects_target_group_reminders_and_not_the_model_tool():
    async def scenario():
        plugin, event = make_plugin({"min_update_seconds": 0}), Event()
        await plugin.remember_world_target(event)
        await plugin._management.action({"key": "qq:456:123", "action": "pause"})
        req = request(plugin)
        await plugin.remind(event, req)
        assert not req.extra_user_content_parts and plugin._tool.active
        other = Event(group="999")
        other_req = request(plugin)
        await plugin.remind(other, other_req)
        assert other_req.extra_user_content_parts
        event.bot.call_action.side_effect = [{"card": "old"}, None, {"card": "new"}]
        assert "已确认" in await plugin.rename(event, "new")
        assert plugin._management.groups()[0]["paused"]

    asyncio.run(scenario())


def test_next_chat_waits_for_an_actual_agent_start_and_respects_pause_and_cooldown():
    async def scenario():
        plugin, event = make_plugin({"reminder_interval_minutes": 0}), Event()
        await plugin.remember_world_target(event)
        key = "qq:456:123"
        await plugin._management.action({"key": key, "action": "next_chat"})
        assert not plugin._reminders  # No background model request.
        state = plugin._state(event, "123", "456")
        state.write_after = time.monotonic() + 100
        assert not (await _remind_request(plugin, event)).extra_user_content_parts
        state.write_after = 0
        req = await _remind_request(plugin, event)
        assert req.extra_user_content_parts and plugin._management.control(key)["next_chat"]
        await plugin.finish_reminder(event, SimpleNamespace(messages=[]), None)
        assert plugin._management.control(key)["next_chat"]  # Preparation didn't consume it.
        await _remind_request(plugin, event)
        await plugin.bind_reminder(event, SimpleNamespace(messages=[]))
        assert not plugin._management.control(key).get("next_chat")
        plugin._cleanup(plugin._reminders[id(event)])
        await plugin._management.action({"key": key, "action": "pause"})
        with pytest.raises(ValueError, match="恢复"):
            await plugin._management.action({"key": key, "action": "next_chat"})

    asyncio.run(scenario())


async def _remind_request(plugin, event):
    req = request(plugin)
    await plugin.remind(event, req)
    return req


def test_actions_cannot_address_unknown_groups_or_change_another_account():
    async def scenario():
        plugin, event = make_plugin(), Event()
        await plugin.remember_world_target(event)
        with pytest.raises(ValueError, match="已识别"):
            await plugin._management.action(
                {"key": "qq:999:123", "action": "rename", "card": "new"}
            )
        event.bot.call_action.side_effect = [
            {"card": "real", "user_id": 456, "group_id": 123},
            {"group_name": "另一个群", "group_id": 999},
        ]
        await plugin._management.action({"key": "qq:456:123", "action": "refresh"})
        assert "group_name" not in plugin._management.observations["qq:456:123"]
        assert not plugin._management.history
        event.bot.call_action.assert_any_await(
            "get_group_member_info", self_id="456", group_id=123, user_id=456, no_cache=True
        )

    asyncio.run(scenario())


def test_overview_and_history_filters_are_read_only_and_csv_cannot_execute_formulas():
    async def scenario():
        plugin, event = make_plugin(), Event()
        row = await plugin._track_world_target(event, plugin._target(event))
        await plugin._management.record(
            row, "tool", "confirmed", requested="=1+1", previous=None, actual="=1+1"
        )
        overview = plugin._management.overview(480)
        assert overview["last_24h"] == 1 and sum(day["count"] for day in overview["trend"]) == 1
        assert plugin._management.history_page({"search": "1+1"})["total"] == 1
        assert plugin._management.history_page({"key": "other"})["total"] == 0
        assert plugin._management.history_page({"status": "failed"})["total"] == 0
        content, _, _ = plugin._management.export({"format": "csv"})
        cells = list(csv.reader(io.StringIO(content)))[1]
        assert "'=1+1" in cells
        event.bot.call_action.assert_not_awaited()

    asyncio.run(scenario())


def test_history_is_bounded_and_controls_roll_back_when_persistence_fails():
    async def scenario():
        plugin, event = make_plugin(), Event()
        row = await plugin._track_world_target(event, plugin._target(event))
        await plugin._management.record(row, "tool", "confirmed", requested="first", actual="first")
        plugin._management.history *= MAX_HISTORY
        await plugin._management.record(row, "tool", "failed", requested="last")
        assert len(plugin._management.history) == MAX_HISTORY
        assert plugin._management.history_page({"page": 99999})["page"] == 50
        plugin.put_kv_data.side_effect = OSError("disk")
        with pytest.raises(OSError):
            await plugin._management.action({"key": "qq:456:123", "action": "pause"})
        assert not plugin._management.controls

    asyncio.run(scenario())


def test_closed_plugin_rejects_management_mutations():
    async def scenario():
        plugin = make_plugin()
        plugin._closed = True
        with pytest.raises(ValueError, match="卸载"):
            await plugin._management.action({"key": "unknown", "action": "next_chat"})
        with pytest.raises(ValueError, match="卸载"):
            await plugin._management.save_settings({"base_name": "new"})

    asyncio.run(scenario())


def test_native_route_reload_replaces_handlers_and_old_close_preserves_new_instance(monkeypatch):
    async def scenario():
        import sys
        import types

        sdk = types.ModuleType("astrbot.api.web")
        sdk.error_response = sdk.json_response = sdk.stream_response = lambda *args, **kwargs: None
        sdk.request = SimpleNamespace()
        monkeypatch.setitem(sys.modules, "astrbot.api.web", sdk)
        registry = []

        def register(route, handler, methods, desc):
            registry[:] = [row for row in registry if not (row[0] == route and row[2] == methods)]
            registry.append((route, handler, methods, desc))

        context = SimpleNamespace(
            add_llm_tools=lambda *_: None, register_web_api=register, registered_web_apis=registry
        )
        first, second = FreeCardPlugin(context), FreeCardPlugin(context)
        first._management.register()
        assert len(registry) == 5
        second._management.register()
        await first._management.close()
        assert len(registry) == 5
        await second._management.close()
        assert not registry

    asyncio.run(scenario())
