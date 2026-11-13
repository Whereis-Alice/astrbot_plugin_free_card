"""One writer with confirmed and pending temporary-card restoration slots."""
from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import time
from types import SimpleNamespace
from typing import Any

from .card_api import read_member, update_card, validate_card

WORLD_PLUGIN = "astrbot_plugin_my_world"
WORLD_RECORDS_KEY = "world_card_records_v2"
LEGACY_WORLD_RECORDS_KEY = "world_card_records_v1"
LEGACY_CHECK_VERSION = 2
MAX_TARGETS = 1024
SLOT_KEYS = ("confirmed_slot", "pending_slot")
RENDER_VERSION = 2


def compose_presence(base: str, status: str) -> str:
    """Keep the chosen name intact and shorten only the temporary status."""
    base = validate_card(base)
    suffix = " " + " ".join(status.split())
    while suffix and (len(base + suffix) > 32 or len((base + suffix).encode("utf-8")) > 60):
        suffix = suffix[:-1]
    return validate_card(base + suffix) if suffix.strip() else base


def presence_display_base(config: Any, presence: dict[str, str], *,
                          member: dict[str, Any] | None = None, baseline: str = "",
                          saved: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """Select an identity independently of the full normal card kept for restoration."""
    candidates = [(config.get("base_name", ""), "config"), (presence.get("name", ""), "world")]
    if member is not None:
        candidates.append((member.get("nickname", ""), "nickname"))
    elif saved and saved.get("display_source") in {"nickname", "baseline"}:
        candidates.append((saved.get("display_base", ""), saved["display_source"]))
    candidates.append((baseline, "baseline"))
    for candidate, source in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            return validate_card(candidate), source
        except ValueError:
            continue
    return None


def presence_slot(baseline: str, presence: dict[str, str], display: tuple[str, str]) -> dict[str, Any]:
    return {"card": compose_presence(display[0], presence["status"]), "baseline": baseline,
            "token": presence["token"], "status": presence["status"],
            "render_version": RENDER_VERSION, "display_base": display[0], "display_source": display[1]}


def tracked_write(function):
    """Unload waits for every old-instance writer before another instance starts."""
    @functools.wraps(function)
    async def invoke(self, *args, **kwargs):
        task = asyncio.current_task()
        self._card_writers.add(task)
        try:
            return await function(self, *args, **kwargs)
        finally:
            self._card_writers.discard(task)
    return invoke


def resolve_slot(row: dict[str, Any], current: str) -> dict[str, Any] | None:
    """Match actual QQ state to an interrupted write before committing its baseline."""
    pending = row.get("pending_slot")
    if isinstance(pending, dict) and pending.get("card") == current:
        if pending.get("explicit_baseline"):
            row["legacy_world_checked"] = LEGACY_CHECK_VERSION
        if pending.get("restore"):
            for key in SLOT_KEYS:
                row.pop(key, None)
            return None
        row["confirmed_slot"] = dict(pending)
        row.pop("pending_slot", None)
        return row["confirmed_slot"]
    confirmed = row.get("confirmed_slot")
    if isinstance(confirmed, dict) and confirmed.get("card") == current:
        if confirmed.get("explicit_baseline"):
            row["legacy_world_checked"] = LEGACY_CHECK_VERSION
        row.pop("pending_slot", None)
        return confirmed
    for key in SLOT_KEYS:
        row.pop(key, None)
    return None


class WorldCardMixin:
    def _init_world_cards(self) -> None:
        self._world_records: dict[str, dict[str, Any]] = {}
        self._world_clients: dict[str, Any] = {}
        self._world_store_lock = asyncio.Lock()
        self._world_task: asyncio.Task | None = None
        self._card_writers: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        reader = getattr(self, "get_kv_data", None)
        if callable(reader):
            saved = await reader(WORLD_RECORDS_KEY, None)
            if saved is None:
                saved = await reader(LEGACY_WORLD_RECORDS_KEY, {})
            if isinstance(saved, dict):
                for key, row in list(saved.items())[:MAX_TARGETS]:
                    if (isinstance(key, str) and isinstance(row, dict)
                            and all(isinstance(row.get(field), str)
                                    for field in ("session", "platform", "group", "account"))
                            and all(row[field].isascii() and row[field].isdecimal()
                                    and int(row[field]) > 0 for field in ("group", "account"))):
                        self._world_records[key] = dict(row)
        # Commit through the SDK before dropping its cached legacy ledger.
        # If this write fails, retain v1 and leave the background writer stopped.
        await self._save_world_records()
        deleter = getattr(self, "delete_kv_data", None)
        if callable(deleter):
            await deleter(LEGACY_WORLD_RECORDS_KEY)
        if not self._closed:
            self._world_task = asyncio.create_task(self._world_card_loop(), name="free_card:world_presence")

    def _world_log(self, text: str, exc: Exception) -> None:
        from astrbot.api import logger
        logger.warning("[free_card] %s: %s", text, type(exc).__name__)

    async def _save_world_records(self) -> None:
        writer = getattr(self, "put_kv_data", None)
        if callable(writer):
            async with self._world_store_lock:
                await writer(WORLD_RECORDS_KEY, copy.deepcopy(self._world_records))

    def _world_session(self, event: Any) -> str:
        origin = getattr(event, "unified_msg_origin", "")
        return str(origin or f"{event.get_platform_id()}:GroupMessage:{event.get_group_id()}")

    def _world_key(self, event: Any, group: str, account: str) -> str:
        return f"{event.get_platform_id()}:{account}:{group}"

    async def _track_world_target(self, event: Any, target: tuple[Any, str, str]) -> dict[str, Any]:
        client, group, account = target
        key = self._world_key(event, group, account)
        row = self._world_records.get(key)
        changed = False
        if row is None:
            if len(self._world_records) >= MAX_TARGETS:
                idle = [item for item, value in self._world_records.items()
                        if not any(value.get(slot) for slot in SLOT_KEYS)]
                if not idle:
                    raise RuntimeError("Temporary card restoration table is full")
                oldest = min(idle, key=lambda item: self._world_records[item].get("seen_at", 0))
                self._world_records.pop(oldest, None)
                self._world_clients.pop(oldest, None)
            row = {"session": self._world_session(event), "platform": str(event.get_platform_id()),
                   "group": group, "account": account, "seen_at": time.time()}
            self._world_records[key] = row
            changed = True
        elif row.get("session") != self._world_session(event):
            row["session"] = self._world_session(event)
            changed = True
        row["seen_at"] = time.time()
        self._world_clients[key] = client
        if changed:
            await self._save_world_records()
        return row

    def _world_plugins(self, event: Any, *, current_event: bool = True) -> list[Any]:
        """Resolve cooperation using current activation and the same session scope."""
        if self.config.get("world_presence_enabled", True) is False:
            return []
        getter = getattr(self.context, "get_all_stars", None)
        if not callable(getter):
            return []
        session = self._world_session(event)
        allowed = getattr(event, "plugins_name", None) if current_event else None
        config_getter = getattr(self.context, "get_config", None)
        if allowed is None and callable(config_getter):
            allowed = config_getter(session).get("plugin_set", ["*"])
        if allowed is not None and "*" not in allowed and "astrbot_plugin_free_card" not in allowed:
            return []
        result = []
        for star in getter() or []:
            names = {str(getattr(star, field, "")) for field in ("name", "root_dir_name")}
            if WORLD_PLUGIN not in names or not getattr(star, "activated", False):
                continue
            if allowed is not None and "*" not in allowed and not names.intersection(allowed):
                continue
            world = getattr(star, "star_cls", None)
            if world is not None:
                result.append(world)
        return result

    async def _world_presence(self, event: Any, *, current_event: bool = True) -> dict[str, str] | None:
        session = self._world_session(event)
        for world in self._world_plugins(event, current_event=current_event):
            presence = getattr(world, "get_group_card_override", None)
            if not callable(presence):
                continue
            value = presence(session)
            if inspect.isawaitable(value):
                value = await value
            if (isinstance(value, dict) and value.get("source") == WORLD_PLUGIN
                    and isinstance(value.get("status"), str) and value["status"].strip()):
                result = {"status": " ".join(value["status"].split())[:40],
                          "token": str(value.get("token") or "")}
                if isinstance(value.get("name"), str) and value["name"].strip():
                    result["name"] = value["name"]
                return result
        return None

    async def _world_baseline(self, event: Any, card: str) -> str | None:
        """Ask the old writer for exact provenance; never infer a suffix from text."""
        session = self._world_session(event)
        for world in self._world_plugins(event, current_event=False):
            helper = getattr(world, "get_group_card_baseline", None)
            if not callable(helper):
                continue
            value = helper(session, card)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, str):
                try:
                    baseline = validate_card(value)
                except ValueError:
                    continue
                if baseline != card:
                    return baseline
        return None

    def _world_recovered_event(self, key: str, row: dict[str, Any]) -> Any:
        getter = getattr(self.context, "get_platform_inst", None)
        platform = getter(row["platform"]) if callable(getter) else None
        if platform is not None:
            if platform.meta().name != "aiocqhttp":
                return None
            client = platform.get_client()
        else:
            client = self._world_clients.get(key)
        if client is None:
            return None
        return SimpleNamespace(bot=client, unified_msg_origin=row["session"],
            get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: row["platform"],
            get_group_id=lambda: row["group"], get_self_id=lambda: row["account"])

    async def _world_card_loop(self) -> None:
        while not self._closed:
            for key, row in list(self._world_records.items()):
                if self._closed:
                    break
                try:
                    event = self._world_recovered_event(key, row)
                    if event is not None:
                        await self.sync_world_group_card(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - One unavailable target must not stop others.
                    self._world_log("Temporary group-card check failed", exc)
            await asyncio.sleep(30)

    def _world_interval(self) -> int:
        from .main import setting_int
        return setting_int(self.config, "min_update_seconds", 60, 3600)

    async def _write_world_slot(self, client: Any, group: str, account: str,
                                row: dict[str, Any], state: Any, slot: dict[str, Any],
                                *, updater=None):
        row["pending_slot"] = dict(slot)
        await self._save_world_records()
        interval = self._world_interval()
        state.write_after = time.monotonic() + max(5, interval)
        try:
            result = await (updater or update_card)(client, group, account, slot["card"])
        except asyncio.CancelledError:
            state.write_after = time.monotonic() + 300
            raise
        state.write_after = time.monotonic() + (interval if result.accepted else 300)
        if result.verified:
            resolve_slot(row, slot["card"])
        await self._save_world_records()
        return result

    @tracked_write
    async def sync_world_group_card(self, event: Any) -> bool:
        target = self._target(event)
        if target is None or self._closed:
            return False
        client, group, account = target
        row = await self._track_world_target(event, target)
        state = self._state(event, group, account)
        async with state.lock:
            if self._closed or time.monotonic() < state.write_after:
                return False
            presence = await self._world_presence(event, current_event=False)
            confirmed = row.get("confirmed_slot") or {}
            display = (presence_display_base(self.config, presence,
                       baseline=confirmed.get("baseline", ""), saved=confirmed) if presence else None)
            if (row.get("legacy_world_checked") == LEGACY_CHECK_VERSION
                    and presence and not row.get("pending_slot")
                    and confirmed.get("token") == presence["token"]
                    and confirmed.get("status") == presence["status"]
                    and confirmed.get("render_version") == RENDER_VERSION
                    and display is not None
                    and confirmed.get("display_base") == display[0]
                    and confirmed.get("display_source") == display[1]):
                return True
            if (row.get("legacy_world_checked") == LEGACY_CHECK_VERSION and not presence
                    and not any(row.get(key) for key in SLOT_KEYS)):
                return False
            member = await read_member(client, group, account)
            current = member["card"]
            owned = resolve_slot(row, current)
            baseline = owned["baseline"] if owned is not None else current
            if row.get("legacy_world_checked") != LEGACY_CHECK_VERSION:
                clean = await self._world_baseline(event, baseline)
                # A failing helper leaves this unset so the next bounded check retries.
                row["legacy_world_checked"] = LEGACY_CHECK_VERSION
                if clean is not None:
                    baseline = clean
                    if owned is not None:
                        owned["baseline"] = clean
                    elif not presence:
                        owned = {"card": current, "baseline": clean}
                        row["confirmed_slot"] = owned
            if not presence:
                if owned is None:
                    await self._save_world_records()
                    return False
                slot = {"card": owned["baseline"], "baseline": owned["baseline"], "restore": True}
            else:
                display = presence_display_base(self.config, presence, member=member, baseline=baseline)
                if not display:
                    await self._save_world_records()
                    return False
                slot = presence_slot(baseline, presence, display)
                if owned is not None and owned.get("explicit_baseline"):
                    slot["explicit_baseline"] = True
                if owned is not None and current == slot["card"]:
                    row["confirmed_slot"] = slot
                    row.pop("pending_slot", None)
                    await self._save_world_records()
                    return True
            result = await self._write_world_slot(client, group, account, row, state, slot)
            return result.verified

    async def _close_world_cards(self) -> None:
        current = asyncio.current_task()
        tasks = {task for task in self._card_writers if task is not current}
        if self._world_task is not None and self._world_task is not current:
            tasks.add(self._world_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._save_world_records()
        self._world_clients.clear()
