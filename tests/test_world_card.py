"""Actual card values, interrupted writes, lifecycle and ownership boundaries."""
import asyncio
import copy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from free_card_plugin.card_api import UpdateResult
from free_card_plugin.main import FreeCardPlugin
from free_card_plugin.world_card import (
    LEGACY_CHECK_VERSION,
    LEGACY_WORLD_RECORDS_KEY,
    WORLD_PLUGIN,
    WORLD_RECORDS_KEY,
    compose_presence,
)


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
                 "allowed": ["*"], "active": True, "baseline_proofs": {}}
    async def presence(_session):
        return situation["presence"]
    star = NS(name=WORLD_PLUGIN, root_dir_name=WORLD_PLUGIN, activated=True,
              star_cls=NS(get_group_card_override=presence, get_group_card_baseline=AsyncMock(
                  side_effect=lambda _session, card: situation["baseline_proofs"].get(card))))
    platform = NS(meta=lambda: NS(name="aiocqhttp"), get_client=lambda: client)
    context = NS(add_llm_tools=lambda *_: None, get_all_stars=lambda: [star] if situation["active"] else [],
                 get_config=lambda _session: {"plugin_set": situation["allowed"]},
                 get_platform_inst=lambda _platform: platform)
    plugin = FreeCardPlugin(context, {"min_update_seconds": 0, **(config or {})})
    plugin.put_kv_data = AsyncMock()
    plugin.get_kv_data = AsyncMock(return_value={})
    plugin.delete_kv_data = AsyncMock()
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


def ledger_record(baseline):
    return {"session": "qq:GroupMessage:123", "platform": "qq", "group": "123", "account": "456",
            "confirmed_slot": {"card": "艾拉 接待爱丽丝", "baseline": baseline,
                               "token": "visit-a", "status": "接待爱丽丝", "render_version": 2,
                               "display_base": "艾拉", "display_source": "world"},
            "pending_slot": {"card": "艾拉 拜访莉耶芙", "baseline": baseline,
                             "token": "visit-b", "status": "拜访莉耶芙", "render_version": 2,
                             "display_base": "艾拉", "display_source": "world"}}


def test_missing_v2_migrates_complete_legacy_slots_before_deleting_old_sdk_key(monkeypatch):
    async def scenario():
        plugin, _, _, _ = fixture(monkeypatch)
        legacy = {"qq:456:123": ledger_record("艾拉 自选名字 | 保留原文"),
                  "invalid": {"session": "qq:GroupMessage:bad", "platform": "qq", "group": "bad", "account": "456"}}
        plugin.get_kv_data.side_effect = lambda key, default: (
            None if key == WORLD_RECORDS_KEY else copy.deepcopy(legacy))
        order = []
        async def save(key, payload):
            order.append(("save", key, copy.deepcopy(payload)))
        async def delete(key):
            order.append(("delete", key))
        plugin.put_kv_data.side_effect = save
        plugin.delete_kv_data.side_effect = delete
        await plugin.initialize()
        try:
            expected = {"qq:456:123": legacy["qq:456:123"]}
            assert plugin._world_records == expected
            assert order == [("save", WORLD_RECORDS_KEY, expected), ("delete", LEGACY_WORLD_RECORDS_KEY)]
            assert [call.args for call in plugin.get_kv_data.await_args_list] == [
                (WORLD_RECORDS_KEY, None), (LEGACY_WORLD_RECORDS_KEY, {})]
            assert plugin._world_task is not None
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize("empty_v2", [False, True])
def test_existing_v2_is_authoritative_and_legacy_cache_is_deleted_without_reading_it(monkeypatch, empty_v2):
    async def scenario():
        plugin, _, _, _ = fixture(monkeypatch)
        current = {} if empty_v2 else {"qq:456:123": ledger_record("艾拉 已校正的新基线")}
        plugin.get_kv_data.side_effect = lambda key, default: (
            copy.deepcopy(current) if key == WORLD_RECORDS_KEY
            else {"qq:456:123": ledger_record("旧缓存里的错误名字")})
        await plugin.initialize()
        try:
            assert plugin._world_records == current
            plugin.get_kv_data.assert_awaited_once_with(WORLD_RECORDS_KEY, None)
            plugin.put_kv_data.assert_awaited_once_with(WORLD_RECORDS_KEY, current)
            plugin.delete_kv_data.assert_awaited_once_with(LEGACY_WORLD_RECORDS_KEY)
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize("existing_v2", [False, True])
def test_failed_v2_save_keeps_legacy_key_and_does_not_start_background_writer(monkeypatch, existing_v2):
    async def scenario():
        plugin, _, _, _ = fixture(monkeypatch)
        data = {"qq:456:123": ledger_record("完整原始基线")}
        plugin.get_kv_data.side_effect = lambda key, default: (
            copy.deepcopy(data) if key == LEGACY_WORLD_RECORDS_KEY or existing_v2 else None)
        plugin.put_kv_data.side_effect = RuntimeError("storage unavailable")
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await plugin.initialize()
        assert plugin._world_records == data
        plugin.delete_kv_data.assert_not_awaited()
        assert plugin._world_task is None
    asyncio.run(scenario())


def test_proven_old_world_name_is_cleaned_without_active_visit_and_only_checked_once(monkeypatch):
    async def scenario():
        old = "Alice 摸鱼中 | 事件中"
        clean = "Alice 摸鱼中"
        plugin, event, situation, updater = fixture(monkeypatch, card=old)
        situation["presence"] = None
        situation["baseline_proofs"][old] = clean
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        assert await plugin.sync_world_group_card(event)
        assert event.bot.card == clean
        row = next(iter(plugin._world_records.values()))
        assert row["legacy_world_checked"] and not row.get("confirmed_slot")
        helper.assert_awaited_once_with(event.unified_msg_origin, old)
        reads, writes = event.bot.read_count, updater.await_count
        for _ in range(5):
            await plugin.sync_world_group_card(event)
        assert event.bot.read_count == reads and updater.await_count == writes
        assert helper.await_count == 1
    asyncio.run(scenario())


def test_first_active_takeover_keeps_only_proven_clean_normal_baseline(monkeypatch):
    async def scenario():
        old, clean = "Alice 摸鱼中 | 事件中", "Alice 摸鱼中"
        plugin, event, situation, _ = fixture(monkeypatch, card=old)
        situation["presence"]["name"] = "Alice"
        situation["baseline_proofs"][old] = clean
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 拜访艾拉"
        row = next(iter(plugin._world_records.values()))
        assert row["confirmed_slot"]["baseline"] == clean
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == clean
    asyncio.run(scenario())


def test_existing_active_display_is_not_rewritten_when_only_legacy_baseline_is_corrected(monkeypatch):
    async def scenario():
        old, clean = "Alice 摸鱼中 | 事件中", "Alice 摸鱼中"
        plugin, event, situation, updater = fixture(monkeypatch, card="Alice 拜访艾拉")
        situation["presence"]["name"] = "Alice"
        situation["baseline_proofs"][old] = clean
        plugin._world_records["qq:456:123"] = {
            "session": event.unified_msg_origin, "platform": "qq", "group": "123", "account": "456",
            "confirmed_slot": {"card": event.bot.card, "baseline": old,
                               "token": "visit-a", "status": "拜访艾拉", "render_version": 2,
                               "display_base": "Alice", "display_source": "world"},
        }
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice 拜访艾拉"
        updater.assert_not_awaited()
        row = plugin._world_records["qq:456:123"]
        assert row["legacy_world_checked"] and row["confirmed_slot"]["baseline"] == clean
        assert plugin.put_kv_data.await_args.args[1]["qq:456:123"]["confirmed_slot"]["baseline"] == clean
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == clean
    asyncio.run(scenario())


def test_unproven_external_new_name_is_preserved_even_when_it_contains_a_pipe(monkeypatch):
    async def scenario():
        original = "Alice | Wonderland"
        plugin, event, situation, updater = fixture(monkeypatch, card=original)
        situation["presence"] = None
        situation["baseline_proofs"]["Alice | 事件中"] = "Alice"
        assert not await plugin.sync_world_group_card(event)
        assert event.bot.card == original
        updater.assert_not_awaited()
        reads = event.bot.read_count
        for _ in range(5):
            await plugin.sync_world_group_card(event)
        assert event.bot.read_count == reads
    asyncio.run(scenario())


def test_unavailable_old_writer_leaves_check_pending_until_proof_can_be_read(monkeypatch):
    async def scenario():
        old = "Alice | 事件中"
        plugin, event, situation, updater = fixture(monkeypatch, card=old)
        situation["presence"] = None
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        helper.side_effect = RuntimeError("world database is recovering")
        with pytest.raises(RuntimeError, match="recovering"):
            await plugin.sync_world_group_card(event)
        row = next(iter(plugin._world_records.values()))
        assert not row.get("legacy_world_checked") and event.bot.card == old
        updater.assert_not_awaited()
        helper.side_effect = None
        helper.return_value = "Alice"
        await plugin.sync_world_group_card(event)
        assert row["legacy_world_checked"] and event.bot.card == "Alice"
    asyncio.run(scenario())


def test_explicit_new_normal_choice_is_not_reinterpreted_as_an_old_world_suffix(monkeypatch):
    async def scenario():
        deliberate = "Alice | 事件中"
        plugin, event, situation, _ = fixture(monkeypatch)
        situation["presence"]["name"] = "Alice"
        situation["baseline_proofs"][deliberate] = "Alice"
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        await plugin.sync_world_group_card(event)
        reply = await plugin.rename(event, deliberate)
        assert "正常名片已保存" in reply
        row = next(iter(plugin._world_records.values()))
        assert row["legacy_world_checked"] and row["confirmed_slot"]["baseline"] == deliberate
        situation["presence"]["status"] = "接待莉耶芙"
        await plugin.sync_world_group_card(event)
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == deliberate and helper.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("disabled", ["session_scope", "cooperation"])
def test_legacy_cleanup_respects_current_scope_and_cooperation_switch(monkeypatch, disabled):
    async def scenario():
        old = "Alice | 事件中"
        plugin, event, situation, updater = fixture(monkeypatch, card=old)
        situation["presence"] = None
        situation["baseline_proofs"][old] = "Alice"
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        if disabled == "session_scope":
            situation["allowed"] = ["astrbot_plugin_free_card"]
        else:
            plugin.config["world_presence_enabled"] = False
        await plugin.sync_world_group_card(event)
        assert event.bot.card == old
        helper.assert_not_awaited()
        updater.assert_not_awaited()
    asyncio.run(scenario())


def seed_unchecked_legacy_owned_card(plugin, event, *, baseline="Alice | 事件中"):
    event.bot.card = "Alice 拜访艾拉"
    plugin._world_records["qq:456:123"] = {
        "session": event.unified_msg_origin, "platform": "qq", "group": "123", "account": "456",
        "confirmed_slot": {"card": event.bot.card, "baseline": baseline,
                           "token": "visit-a", "status": "拜访艾拉", "render_version": 2,
                           "display_base": "Alice", "display_source": "world"},
    }


@pytest.mark.parametrize("active_visit", [False, True])
def test_old_boolean_check_rechecks_newly_proven_visit_suffix_once(monkeypatch, active_visit):
    async def scenario():
        old, clean = "Alice 窝在被窝里 | 拜访艾拉", "Alice 窝在被窝里"
        plugin, event, situation, updater = fixture(monkeypatch, card=old)
        if active_visit:
            seed_unchecked_legacy_owned_card(plugin, event, baseline=old)
            situation["presence"]["name"] = "Alice"
        else:
            situation["presence"] = None
            await plugin.remember_world_target(event)
        row = plugin._world_records["qq:456:123"]
        row["legacy_world_checked"] = True
        situation["baseline_proofs"][old] = clean
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        assert await plugin.sync_world_group_card(event)
        assert row["legacy_world_checked"] == LEGACY_CHECK_VERSION
        if active_visit:
            assert event.bot.card == "Alice 拜访艾拉"
            assert row["confirmed_slot"]["baseline"] == clean
            updater.assert_not_awaited()
        else:
            assert event.bot.card == clean
        helper.assert_awaited_once_with(event.unified_msg_origin, old)
        reads, writes = event.bot.read_count, updater.await_count
        for _ in range(5):
            await plugin.sync_world_group_card(event)
        assert event.bot.read_count == reads and updater.await_count == writes
        assert helper.await_count == 1
    asyncio.run(scenario())


def test_old_boolean_check_preserves_confirmed_explicit_baseline(monkeypatch):
    async def scenario():
        chosen = "Alice | 拜访艾拉"
        plugin, event, situation, updater = fixture(monkeypatch)
        seed_unchecked_legacy_owned_card(plugin, event, baseline=chosen)
        row = plugin._world_records["qq:456:123"]
        row["legacy_world_checked"] = True
        row["confirmed_slot"]["explicit_baseline"] = True
        situation["presence"]["name"] = "Alice"
        situation["baseline_proofs"][chosen] = "Alice"
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        assert await plugin.sync_world_group_card(event)
        assert row["legacy_world_checked"] == LEGACY_CHECK_VERSION
        assert row["confirmed_slot"]["baseline"] == chosen
        helper.assert_not_awaited()
        updater.assert_not_awaited()
        situation["presence"] = None
        assert await plugin.sync_world_group_card(event)
        assert event.bot.card == chosen
        helper.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["rejected", "unverified", "canceled"])
def test_unapplied_explicit_choice_does_not_block_cleaning_original_legacy_baseline(monkeypatch, failure):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        seed_unchecked_legacy_owned_card(plugin, event)
        situation["baseline_proofs"]["Alice | 事件中"] = "Alice"
        situation["presence"]["name"] = "Different Alice"
        async def fail(_bot, _group, _account, _name):
            if failure == "canceled":
                raise asyncio.CancelledError()
            return UpdateResult(failure == "unverified", False)
        updater.side_effect = fail
        if failure == "canceled":
            with pytest.raises(asyncio.CancelledError):
                await plugin.rename(event, "Chosen | 事件中")
        else:
            await plugin.rename(event, "Chosen | 事件中")
        row = plugin._world_records["qq:456:123"]
        assert not row.get("legacy_world_checked")
        assert row["confirmed_slot"]["baseline"] == "Alice | 事件中"
        assert row["pending_slot"]["explicit_baseline"]
        assert event.bot.card == "Alice 拜访艾拉"
        async def apply(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, True)
        updater.side_effect = apply
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        assert row["confirmed_slot"]["baseline"] == "Alice"
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["rejected", "unverified", "canceled"])
def test_explicit_choice_that_actually_landed_is_preserved_after_uncertain_response(monkeypatch, failure):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch)
        seed_unchecked_legacy_owned_card(plugin, event)
        chosen = "Chosen | 事件中"
        situation["baseline_proofs"].update({"Alice | 事件中": "Alice", chosen: "Chosen"})
        situation["presence"]["name"] = "Different Alice"
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        async def landed(bot, _group, _account, name):
            bot.card = name
            if failure == "canceled":
                raise asyncio.CancelledError()
            return UpdateResult(failure == "unverified", False)
        updater.side_effect = landed
        if failure == "canceled":
            with pytest.raises(asyncio.CancelledError):
                await plugin.rename(event, chosen)
        else:
            await plugin.rename(event, chosen)
        assert not plugin._world_records["qq:456:123"].get("legacy_world_checked")
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        row = plugin._world_records["qq:456:123"]
        assert row["legacy_world_checked"] and row["confirmed_slot"]["baseline"] == chosen
        assert row["confirmed_slot"]["explicit_baseline"]
        helper.assert_not_awaited()
        async def apply(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, True)
        updater.side_effect = apply
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == chosen
    asyncio.run(scenario())


def test_failed_plain_rename_has_no_premature_global_legacy_check(monkeypatch):
    async def scenario():
        plugin, event, situation, updater = fixture(monkeypatch, card="Alice | 事件中")
        situation["presence"] = None
        situation["baseline_proofs"]["Alice | 事件中"] = "Alice"
        updater.side_effect = None
        updater.return_value = UpdateResult(False, False)
        await plugin.rename(event, "Chosen")
        row = next(iter(plugin._world_records.values()))
        assert not row.get("legacy_world_checked") and row["pending_slot"]["explicit_baseline"]
        async def apply(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, True)
        updater.side_effect = apply
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"
    asyncio.run(scenario())


def test_landed_plain_rename_is_not_cleaned_after_unverified_response(monkeypatch):
    async def scenario():
        chosen = "Chosen | 事件中"
        plugin, event, situation, updater = fixture(monkeypatch, card="Alice | 事件中")
        situation["presence"] = None
        situation["baseline_proofs"][chosen] = "Chosen"
        helper = plugin.context.get_all_stars()[0].star_cls.get_group_card_baseline
        async def landed(bot, _group, _account, name):
            bot.card = name
            return UpdateResult(True, False)
        updater.side_effect = landed
        await plugin.rename(event, chosen)
        free_clock(plugin, event)
        await plugin.sync_world_group_card(event)
        assert event.bot.card == chosen
        assert next(iter(plugin._world_records.values()))["legacy_world_checked"]
        helper.assert_not_awaited()
    asyncio.run(scenario())


def test_failed_same_display_local_save_rolls_back_explicit_baseline_marker(monkeypatch):
    async def scenario():
        plugin, event, situation, _ = fixture(monkeypatch)
        seed_unchecked_legacy_owned_card(plugin, event)
        situation["presence"]["name"] = "Alice"
        situation["baseline_proofs"]["Alice | 事件中"] = "Alice"
        plugin.put_kv_data.side_effect = RuntimeError("cannot persist choice")
        with pytest.raises(RuntimeError, match="cannot persist choice"):
            await plugin.rename(event, "Chosen | 事件中")
        row = plugin._world_records["qq:456:123"]
        assert not row.get("legacy_world_checked")
        assert not row["confirmed_slot"].get("explicit_baseline")
        assert row["confirmed_slot"]["baseline"] == "Alice | 事件中"
        plugin.put_kv_data.side_effect = None
        situation["presence"] = None
        await plugin.sync_world_group_card(event)
        assert event.bot.card == "Alice"
    asyncio.run(scenario())
