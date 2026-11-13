"""Actual card values, interrupted writes, lifecycle and ownership boundaries."""
import asyncio
import copy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from free_card_plugin.card_api import UpdateResult
from free_card_plugin.main import FreeCardPlugin
from free_card_plugin.world_card import WORLD_PLUGIN, compose_presence


class Client:
    def __init__(self, card="Alice", nickname="Alice"):
        self.card, self.nickname = card, nickname
        self.read_count = 0

    async def call_action(self, action, **params):
        assert action == "get_group_member_info"
        self.read_count += 1
        return {"card": self.card, "nickname": self.nickname,
                "group_id": 123, "user_id": 456}


def fixture(monkeypatch, *, card="Alice", config=None):
    client = Client(card)
    situation = {"presence": {"source": WORLD_PLUGIN, "token": "visit-a", "status": "拜访艾拉"},
                 "allowed": ["*"], "active": True}
    async def presence(_session):
        return situation["presence"]
    star = NS(name=WORLD_PLUGIN, root_dir_name=WORLD_PLUGIN, activated=True,
              star_cls=NS(get_group_card_override=presence))
    platform = NS(meta=lambda: NS(name="aiocqhttp"), get_client=lambda: client)
    context = NS(add_llm_tools=lambda *_: None, get_all_stars=lambda: [star] if situation["active"] else [],
                 get_config=lambda _session: {"plugin_set": situation["allowed"]},
                 get_platform_inst=lambda _platform: platform)
    plugin = FreeCardPlugin(context, {"min_update_seconds": 0, **(config or {})})
    plugin.put_kv_data = AsyncMock()
    plugin.get_kv_data = AsyncMock(return_value={})
    event = NS(bot=client, unified_msg_origin="qq:GroupMessage:123", plugins_name=["*"],
               get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "qq",
               get_group_id=lambda: "123", get_self_id=lambda: "456")
    async def apply(bot, _group, _account, name):
        bot.card = name
        return UpdateResult(True, True)
    updater = AsyncMock(side_effect=apply)
    monkeypatch.setattr("free_card_plugin.world_card.update_card", updater)
    monkeypatch.setattr("free_card_plugin.main.update_card", updater)
    return plugin, event, situation, updater


def free_clock(plugin, event):
    plugin._state(event, "123", "456").write_after = 0


def test_empty_card_uses_nickname_and_restores_exact_empty_value(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch, card="")
        await plugin.remember_world_target(event)  # Does not need an LLM request.
        assert await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 拜访艾拉"
        assert next(iter(plugin._world_records.values()))["confirmed_slot"]["baseline"] == ""
        situation["presence"] = None
        assert await plugin.sync_world_group_card(event)
        assert event.bot.card == ""
        assert updater.await_count == 2
        assert not hasattr(plugin, "_world_events")
    asyncio.run(scenario())


@pytest.mark.parametrize("applied", [False, True])
def test_canceled_scene_change_preserves_both_possible_ownership_values(monkeypatch, applied):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)
        situation["presence"] = {"source": WORLD_PLUGIN, "token": "visit-b", "status": "拜访莉耶芙"}
        async def interrupted(bot, _group, _account, name):
            if applied:
                bot.card = name
            raise asyncio.CancelledError()
        updater.side_effect = interrupted
        with pytest.raises(asyncio.CancelledError):
            await plugin.sync_world_group_card(event)
        row = next(iter(plugin._world_records.values()))
        assert row["confirmed_slot"]["card"] == "Alice 拜访艾拉"
        assert row["pending_slot"]["card"] == "Alice 拜访莉耶芙"
        async def apply(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, True)
        updater.side_effect = apply
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 拜访莉耶芙"
        assert row["confirmed_slot"]["baseline"] == "Alice"
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"
    asyncio.run(scenario())


@pytest.mark.parametrize("applied", [False, True])
def test_canceled_new_name_restores_the_baseline_corresponding_to_actual_write(monkeypatch, applied):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)
        situation["presence"]["name"] = "Changed visible identity"
        async def interrupted(bot, _group, _account, name):
            if applied:
                bot.card = name
            raise asyncio.CancelledError()
        updater.side_effect = interrupted
        with pytest.raises(asyncio.CancelledError):
            await plugin.rename(event, "Alice 喝茶中")
        row = next(iter(plugin._world_records.values()))
        assert row["confirmed_slot"]["baseline"] == "Alice"
        assert row["pending_slot"]["baseline"] == "Alice 喝茶中"
        situation["presence"] = None
        async def apply(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, True)
        updater.side_effect = apply
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        assert event.bot.card == ("Alice 喝茶中" if applied else "Alice")
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["rejected", "accepted_unverified", "canceled"])
def test_failed_normal_rename_after_visit_does_not_discard_restoration(monkeypatch, failure):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)
        situation["presence"] = None
        if failure == "canceled":
            updater.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await plugin.rename(event, "New Alice")
        else:
            updater.side_effect = None
            updater.return_value = UpdateResult(failure == "accepted_unverified", False)
            await plugin.rename(event, "New Alice")
        row = next(iter(plugin._world_records.values()))
        assert row["confirmed_slot"]["card"] == "Alice 拜访艾拉"
        assert row["pending_slot"]["card"] == "New Alice"
        async def apply(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, True)
        updater.side_effect = apply
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"
    asyncio.run(scenario())


def test_background_scope_uses_current_config_and_respects_external_new_name(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)
        situation["allowed"] = ["astrbot_plugin_free_card"]
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"  # Old event still says plugins_name=['*'].
        situation["allowed"] = ["*"]
        await plugin.sync_world_group_card(event)
        event.bot.card = "Externally renamed"
        situation["active"] = False
        count = updater.await_count
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Externally renamed"
        assert updater.await_count == count
    asyncio.run(scenario())


def test_tool_names_save_new_baseline_without_changing_same_temporary_display(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch, config={"base_name": "Alice"})
        await plugin.sync_world_group_card(event)
        first = await plugin.rename(event, "喝茶中")
        second = await plugin.rename(event, "看书中")
        assert "正常名片已保存" in first and "正常名片已保存" in second
        assert event.bot.card == "Alice 拜访艾拉"
        assert updater.await_count == 1
        assert next(iter(plugin._world_records.values()))["confirmed_slot"]["baseline"] == "Alice 看书中"
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 看书中"
    asyncio.run(scenario())


def test_current_scope_excluding_free_card_restores_and_stops_its_maintenance(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 拜访艾拉"
        situation["allowed"] = [WORLD_PLUGIN]
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"
        count = updater.await_count
        await plugin.sync_world_group_card(event)
        assert updater.await_count == count
    asyncio.run(scenario())


def test_reload_settles_canceled_inflight_writer_before_persisting_final_ledger(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)
        situation["presence"]["name"] = "Changed visible identity"
        entered = asyncio.Event()
        async def blocked(bot, _group, _account, name):
            bot.card = name  # QQ applied before the interrupted response.
            entered.set()
            await asyncio.Event().wait()
        updater.side_effect = blocked
        writer = asyncio.create_task(plugin.rename(event, "Alice New"))
        await entered.wait()
        await plugin.terminate()
        assert writer.done() and writer.cancelled()
        assert not plugin._card_writers and not plugin._tool.active
        saved = copy.deepcopy(plugin._world_records)
        replacement, next_event, next_situation, _next_updater = fixture(monkeypatch)
        next_event.bot.card = event.bot.card
        replacement.get_kv_data.return_value = saved
        next_situation["presence"] = None
        await replacement.initialize()
        await asyncio.sleep(.01)
        assert next_event.bot.card == "Alice New"
        await replacement.terminate()
    asyncio.run(scenario())


def test_configured_write_interval_applies_to_temporary_changes(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch, config={"min_update_seconds": 120})
        await plugin.sync_world_group_card(event)
        situation["presence"] = None
        assert not await plugin.sync_world_group_card(event)
        assert updater.await_count == 1
        free_clock(plugin, event)
        assert await plugin.sync_world_group_card(event)
    asyncio.run(scenario())


def test_qq_length_limits_keep_base_intact():
    for base in ("Alice", "好" * 20, "😀" * 14):
        name = compose_presence(base, "拜访莉耶芙")
        assert name.startswith(base)
        assert len(name) <= 32 and len(name.encode("utf-8")) <= 60


@pytest.mark.parametrize("config, identity", [
    ({"base_name": "艾拉"}, "艾拉"),
    ({"base_name": ""}, "艾拉"),
    ({"base_name": "艾拉 | Aino"}, "艾拉 | Aino"),
])
def test_only_current_status_is_displayed_and_original_complete_card_is_restored(monkeypatch, config, identity):
    async def scenario():
        original = "艾拉 拒绝贴贴中 | 事件中"
        plugin, event, situation, _ = fixture(monkeypatch, card=original, config=config)
        situation["presence"].update(name="艾拉", status="接待爱丽丝")
        await plugin.sync_world_group_card(event)
        assert event.bot.card == identity + " 接待爱丽丝"
        slot = next(iter(plugin._world_records.values()))["confirmed_slot"]
        assert slot["baseline"] == original and slot["display_base"] == identity
        assert slot["render_version"] == 2
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == original
    asyncio.run(scenario())


def test_deliberately_selected_pipe_in_normal_name_is_preserved_for_restoration(monkeypatch):
    async def scenario():
        original = "Alice | Wonderland"
        plugin, event, situation, _ = fixture(monkeypatch, card=original)
        situation["presence"]["name"] = "Alice"
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 拜访艾拉"
        assert next(iter(plugin._world_records.values()))["confirmed_slot"]["baseline"] == original
        await plugin.rename(event, "Alice | Looking-glass")
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice | Looking-glass"
    asyncio.run(scenario())


def test_old_slot_is_redrawn_after_reload_even_with_same_visit_token(monkeypatch):
    async def scenario():
        baseline = "艾拉 拒绝贴贴中 | 事件中"
        old_card = baseline + " | 接待爱丽丝"
        plugin, event, situation, _ = fixture(monkeypatch, card=old_card)
        situation["presence"].update(name="艾拉", status="接待爱丽丝")
        plugin.get_kv_data.return_value = {"qq:456:123": {
            "session": event.unified_msg_origin, "platform": "qq", "group": "123", "account": "456",
            "confirmed_slot": {"card": old_card, "baseline": baseline,
                               "token": "visit-a", "status": "接待爱丽丝"},
        }}
        await plugin.initialize()
        try:
            await asyncio.sleep(.01)
            assert event.bot.card == "艾拉 接待爱丽丝"
            row = next(iter(plugin._world_records.values()))
            assert row["confirmed_slot"]["baseline"] == baseline
            assert row["confirmed_slot"]["render_version"] == 2
            situation["presence"] = None
            await plugin.sync_world_group_card(event)
            assert event.bot.card == baseline
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_stable_identity_uses_no_qq_reads_but_changed_identity_is_redrawn(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        await plugin.sync_world_group_card(event)  # Nickname fallback is recorded once.
        reads, writes = event.bot.read_count, updater.await_count
        for _ in range(5):
            await plugin.sync_world_group_card(event)
        assert event.bot.read_count == reads and updater.await_count == writes
        situation["presence"]["name"] = "World Alice"
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "World Alice 拜访艾拉"
        reads, writes = event.bot.read_count, updater.await_count
        for _ in range(5):
            await plugin.sync_world_group_card(event)
        assert event.bot.read_count == reads and updater.await_count == writes
        plugin.config["base_name"] = "Configured Alice"
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Configured Alice 拜访艾拉"
    asyncio.run(scenario())


def test_target_limit_does_not_evict_pending_restore_or_retain_full_events(monkeypatch):
    async def scenario():
        plugin, event, _, _ = fixture(monkeypatch)
        monkeypatch.setattr("free_card_plugin.world_card.MAX_TARGETS", 1)
        await plugin.sync_world_group_card(event)
        event.get_group_id = lambda: "124"
        event.unified_msg_origin = "qq:GroupMessage:124"
        with pytest.raises(RuntimeError, match="table is full"):
            await plugin.remember_world_target(event)
        assert len(plugin._world_records) == len(plugin._world_clients) == 1
    asyncio.run(scenario())
