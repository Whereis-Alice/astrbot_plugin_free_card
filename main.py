"""Let the bot choose its own QQ group card using a single tool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import AstrBotConfig, FunctionTool, ToolSet, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass as tool_dataclass

from .card_api import compose_card, read_member, update_card, validate_card
from .world_card import (
    LEGACY_CHECK_VERSION,
    WorldCardMixin,
    presence_display_base,
    presence_slot,
    resolve_slot,
    tracked_write,
)

TOOL_NAME = "set_free_group_card"
# AstrBot runs larger priorities first. My World's final reply gate is -1_000_000;
# requests it suppresses must not consume this plugin's reminder interval.
REMINDER_HOOK_PRIORITY = -1_000_001
DEFAULT_REMINDER = (
    "现在到了你自主调整 QQ 群名片的时间。请先调用 set_free_group_card，"
    "为自己自由决定一个简短的名字、心情或状态，按当前命名规则把文本传给 card。"
    "内容由你决定，不必套用固定后缀，也不用等待群友要求改名。"
    "工具返回后继续原来的回复或任务，不要把这段提醒复述给群友。"
)
REQUIRED_ACTION = (
    "本轮请先实际调用 set_free_group_card 完成自主改名，再继续原来的回复或任务。"
    "不要只在文字中声称已经改名，也不要等待用户再次要求；本轮不要重复调用。"
)
DEFAULT_TOOL_DESCRIPTION = (
    "自由修改你在当前 QQ 群中的完整群名片，表达你的名字、心情或当前状态。"
    "只修改自己。card 是最终显示的完整文本，不会自动拼接基础名字或后缀。"
    "内容保持简短，最多 32 个字符且不超过 60 个 UTF-8 字节。"
    "收到自主改名提醒时，请先调用此工具；平常也可以按自己的意愿使用。"
)


def setting_int(config: Any, key: str, default: int, maximum: int) -> int:
    try:
        value = config.get(key, default)
        if isinstance(value, bool):
            return default
        return min(maximum, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class GroupState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reminded_at: float | None = None
    write_after: float = 0
    seen_at: float = field(default_factory=time.monotonic)
    last_skip_reason: str = ""
    skip_noticed_at: float = 0


@dataclass
class Reminder:
    event: Any
    request: Any
    part: TextPart
    created_at: float = field(default_factory=time.monotonic)
    run_context: Any = None
    original_tools: Any = None
    gated_tools: Any = None


def reminder_schema_mode(context: Any, event: Any) -> str:
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return "full"
    config = getter(getattr(event, "unified_msg_origin", ""))
    misc = config.get("agent_runner", {}).get("config", {}).get("misc", {})
    return str(misc.get("tool_schema_mode") or
               config.get("provider_settings", {}).get("tool_schema_mode", "full"))


@tool_dataclass
class FreeCardTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = TOOL_NAME
    description: str = DEFAULT_TOOL_DESCRIPTION
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "card": {"type": "string", "description": "你想显示的完整群名片。"},
            },
            "required": ["card"],
            "additionalProperties": False,
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        event = getattr(getattr(context, "context", None), "event", None)
        if self.plugin is None or event is None:
            return "无法修改：没有当前群聊，请在 QQ 群聊中使用。"
        return await self.plugin.rename(event, kwargs.get("card"))


class FreeCardPlugin(WorldCardMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self._states: dict[tuple[str, str, str], GroupState] = {}
        self._reminders: dict[int, Reminder] = {}
        self._closed = False
        self._init_world_cards()
        self._tool = FreeCardTool(plugin=self)
        self._configure_tool()
        context.add_llm_tools(self._tool)

    @filter.event_message_type(filter.EventMessageType.ALL)
    @tracked_write
    async def remember_world_target(self, event: AstrMessageEvent) -> None:
        """Remember exact OneBot targets for automatic temporary world presence."""
        if self._closed:
            return
        target = self._target(event)
        if target is not None:
            await self._track_world_target(event, target)

    def _configure_tool(self) -> str:
        """Tell the model which meaning the single card argument has."""
        base = self.config.get("base_name", "")
        if isinstance(base, str) and base.strip():
            try:
                base = validate_card(base)
            except ValueError:
                return "基础名字配置无效，暂时不要使用改名工具，请告知管理员检查配置。"
            rule = (
                f"当前基础名字固定为 {base!r}。调用 {TOOL_NAME} 时，card 只填写你自由决定的后缀，"
                "不要重复基础名字。插件自动显示为“基础名字 后缀”。"
                "card 为空字符串时清除后缀，只显示基础名字。"
                "最终名片连同基础名字，最多 32 个字符且不超过 60 个 UTF-8 字节。"
            )
            self._tool.description = "修改自己在当前 QQ 群的名片后缀。" + rule
            self._tool.parameters["properties"]["card"]["description"] = (
                f"基础名字 {base!r} 已固定；只填写后缀，空字符串清除后缀。"
            )
            return rule
        self._tool.description = DEFAULT_TOOL_DESCRIPTION
        self._tool.parameters["properties"]["card"]["description"] = "你想显示的完整群名片。"
        return "未配置基础名字，card 填写你想显示的完整群名片，整个名字都可自由修改。"

    def _target(self, event: AstrMessageEvent) -> tuple[Any, str, str] | None:
        if event.get_platform_name() != "aiocqhttp":
            return None
        group_id, self_id = str(event.get_group_id()), str(event.get_self_id())
        if not all(
            value.isascii() and value.isdecimal() and int(value) > 0
            for value in (group_id, self_id)
        ):
            return None
        client = getattr(event, "bot", None)
        if client is None:
            return None
        return client, str(int(group_id)), str(int(self_id))

    def _state(self, event: AstrMessageEvent, group: str, account: str) -> GroupState:
        now = time.monotonic()
        # A quiet group needs no permanent cache. Never evict an in-flight write.
        retention = max(
            86400,
            setting_int(self.config, "reminder_interval_minutes", 30, 10080) * 60,
        )
        for key, state in list(self._states.items()):
            if now - state.seen_at > retention and not state.lock.locked():
                del self._states[key]
        key = (event.get_platform_id(), account, group)
        if key not in self._states and len(self._states) >= 1024:
            idle = [key for key, state in self._states.items() if not state.lock.locked()]
            if idle:
                del self._states[min(idle, key=lambda key: self._states[key].seen_at)]
        state = self._states.setdefault(key, GroupState())
        state.seen_at = now
        return state

    def _notice_reminder_skip(self, state: GroupState, group: str, reason: str) -> None:
        now = time.monotonic()
        if reason != state.last_skip_reason or now - state.skip_noticed_at >= 600:
            logger.info("[free_card] reminder skipped group=%s reason=%s", group, reason)
            state.last_skip_reason = reason
            state.skip_noticed_at = now

    @filter.on_llm_request(priority=REMINDER_HOOK_PRIORITY)
    async def remind(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Periodically offer the tool to a bot already replying in a group."""
        if self._closed:
            return
        stopped = getattr(event, "is_stopped", None)
        if callable(stopped) and stopped():
            return
        naming_rule = self._configure_tool()
        # Release abandoned requests; cleanup matches our exact content only.
        for reminder in list(self._reminders.values()):
            if time.monotonic() - reminder.created_at > 3600:
                self._cleanup(reminder)
        target = self._target(event)
        interval = setting_int(self.config, "reminder_interval_minutes", 30, 10080) * 60
        if target is None or interval == 0:
            return
        _, group, account = target
        state = self._state(event, group, account)
        try:
            if await self._world_presence(event):
                self._notice_reminder_skip(state, group, "world_visit_active")
                return  # Presence is automatically maintained; preserve ordinary chat rhythm.
        except Exception as exc:  # noqa: BLE001 - World startup must not interrupt normal chat.
            self._notice_reminder_skip(state, group, "world_not_ready:" + type(exc).__name__)
            return
        tools = getattr(req.func_tool, "tools", [])
        card_tool = next((tool for tool in tools
                          if tool.name == TOOL_NAME and getattr(tool, "active", True)), None)
        if card_tool is None:
            self._notice_reminder_skip(state, group, "tool_unavailable")
            return
        now = time.monotonic()
        if now < state.write_after or (
            state.reminded_at is not None and now - state.reminded_at < interval
        ):
            return
        # One event may create several provider requests; never stack reminders.
        if id(event) in self._reminders:
            return
        text = self.config.get("reminder_text", DEFAULT_REMINDER)
        if not isinstance(text, str) or not text.strip():
            text = DEFAULT_REMINDER
        part = TextPart(
            text=f"[FreeCard reminder]\n{text.strip()[:2000]}\n{REQUIRED_ACTION}\n当前命名规则：{naming_rule}"
        ).mark_as_temp()
        # Keep the selected permission proxy, including My World's guarded tool.
        # Replacing this request never changes global tool activation.
        original = req.func_tool
        gated = None
        if reminder_schema_mode(self.context, event) != "skills_like":
            gated = ToolSet([card_tool])
            req.func_tool = gated
        req.extra_user_content_parts.append(part)
        self._reminders[id(event)] = Reminder(event=event, request=req, part=part,
                                             original_tools=original if gated else None,
                                             gated_tools=gated)
        state.reminded_at = now
        state.last_skip_reason = ""
        logger.info("[free_card] reminder injected group=%s account=%s tools=%s->%s",
                    group, account, len(tools), len(req.func_tool.tools))

    @filter.on_agent_begin()
    async def bind_reminder(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
    ) -> None:
        if reminder := self._reminders.get(id(event)):
            reminder.run_context = run_context
            logger.info("[free_card] reminder reached agent group=%s", event.get_group_id())

    def _cleanup(self, reminder: Reminder) -> None:
        current = getattr(reminder.request, "func_tool", None)
        gated = reminder.gated_tools
        # The proactive runner may clone the ToolSet. Restore only our own gate;
        # an empty or differently narrowed set may represent revoked permission.
        if gated is not None and reminder.original_tools is not None and (
            current is gated or (
                len(getattr(current, "tools", [])) == len(gated.tools)
                and all(a is b for a, b in zip(current.tools, gated.tools))
            )
        ):
            reminder.request.func_tool = reminder.original_tools
            logger.info("[free_card] restored follow-up tools group=%s tools=%s->%s",
                        reminder.event.get_group_id(), len(gated.tools),
                        len(reminder.original_tools.tools))
        parts = reminder.request.extra_user_content_parts
        parts[:] = [part for part in parts if part is not reminder.part]
        messages = getattr(reminder.run_context, "messages", None)
        if isinstance(messages, list):
            for message in list(messages):
                content = message.get("content") if isinstance(message, dict) else message.content
                if not isinstance(content, list):
                    continue
                before = len(content)
                content[:] = [
                    part
                    for part in content
                    if not (
                        (
                            part.get("text")
                            if isinstance(part, dict)
                            else getattr(part, "text", None)
                        )
                        == reminder.part.text
                        and (
                            part is reminder.part
                            or (
                                part.get("_no_save", False)
                                if isinstance(part, dict)
                                else getattr(part, "_no_save", False)
                            )
                        )
                    )
                ]
                if before and not content:
                    messages.remove(message)
        self._reminders.pop(id(reminder.event), None)

    @filter.on_agent_done()
    async def finish_reminder(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
        response: LLMResponse,
    ) -> None:
        if reminder := self._reminders.get(id(event)):
            reminder.run_context = run_context
            logger.info(
                "[free_card] reminder finished without rename group=%s", event.get_group_id()
            )
            self._cleanup(reminder)

    @tracked_write
    async def rename(self, event: AstrMessageEvent, card: Any) -> str:
        if self._closed:
            return "无法修改：插件已卸载。"
        if reminder := self._reminders.get(id(event)):
            logger.info("[free_card] reminder reached tool group=%s", event.get_group_id())
            self._cleanup(reminder)
        target = self._target(event)
        if target is None:
            return "无法修改：仅支持 OneBot v11 接入的 QQ 群聊；QQ 官方机器人不开放群名片修改接口。"
        try:
            name = compose_card(card, self.config.get("base_name", ""))
        except ValueError as exc:
            return f"无法修改：{exc}"
        client, group, account = target
        world_row = await self._track_world_target(event, target)
        state = self._state(event, group, account)
        async with state.lock:
            if self._closed:
                return "无法修改：插件已卸载。"
            now = time.monotonic()
            if now < state.write_after:
                return f"请保留当前名片，约 {int(state.write_after - now) + 1} 秒后可再修改。"
            try:
                presence = await self._world_presence(event)
            except Exception as exc:  # noqa: BLE001 - Preserve a pending temporary name while its owner reloads.
                self._world_log("World presence is not ready", exc)
                return "小世界正在恢复状态，暂时保留当前名片，稍后再试。"
            has_slots = bool(world_row.get("confirmed_slot") or world_row.get("pending_slot"))
            member = None
            owned = None
            if has_slots:
                try:
                    member = await read_member(client, group, account)
                except Exception as exc:  # noqa: BLE001 - A failed read must preserve restoration slots.
                    self._world_log("Group-card read unavailable", exc)
                    state.write_after = time.monotonic() + 300
                    return "暂时无法回读当前群名片，已保留恢复记录并暂停尝试 300 秒，请稍后再试。"
                owned = resolve_slot(world_row, member["card"])
            if presence or has_slots:
                slot = {"baseline": name, "card": name, "restore": True, "explicit_baseline": True}
                if presence:
                    if member is None:
                        try:
                            member = await read_member(client, group, account)
                        except Exception as exc:  # noqa: BLE001 - Do not overwrite an unread temporary card.
                            self._world_log("Group-card read unavailable", exc)
                            state.write_after = time.monotonic() + 300
                            return "暂时无法回读当前群名片，已暂停尝试 300 秒，请稍后再试。"
                    display = presence_display_base(self.config, presence, member=member, baseline=name)
                    if display is None:
                        return "无法确定串门名片的显示名字，请设置基础名字或检查机器人昵称。"
                    slot = presence_slot(name, presence, display)
                    slot["explicit_baseline"] = True
                    if owned is not None and member["card"] == slot["card"]:
                        previous = dict(world_row)
                        world_row["confirmed_slot"] = slot
                        world_row.pop("pending_slot", None)
                        world_row["legacy_world_checked"] = LEGACY_CHECK_VERSION
                        try:
                            await self._save_world_records()
                        except BaseException:
                            world_row.clear()
                            world_row.update(previous)
                            raise
                        return (f"正常名片已保存为“{name}”；串门期间继续显示“{slot['card']}”，"
                                "归家后恢复刚保存的名字。")
                name = slot["card"]
                result = await self._write_world_slot(client, group, account, world_row, state, slot,
                                                      updater=update_card)
                completed_at = time.monotonic()
            else:
                # Retain explicit intent without claiming an unconfirmed write succeeded.
                slot = {"baseline": name, "card": name, "restore": True, "explicit_baseline": True}
                result = await self._write_world_slot(client, group, account, world_row, state, slot,
                                                      updater=update_card)
                completed_at = time.monotonic()
            if result.accepted:
                state.reminded_at = completed_at
            if result.verified:
                return f"群名片已确认是“{name}”。"
            if result.accepted:
                return f"接口已接受“{name}”，暂时无法回读确认。请不要立即重复调用。{result.detail}"
            logger.warning(
                "[free_card] set_group_card failed group=%s detail=%s", group, result.detail
            )
            return f"修改失败，已暂停尝试 300 秒：{result.detail}"

    async def terminate(self) -> None:
        self._closed = True
        self._tool.active = False
        try:
            await self._close_world_cards()
        finally:
            self._tool.active = False
            for reminder in list(self._reminders.values()):
                self._cleanup(reminder)
            self._states.clear()
