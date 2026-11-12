"""Let the bot choose its own QQ group card using a single tool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass as tool_dataclass

from .card_api import compose_card, update_card, validate_card

TOOL_NAME = "set_free_group_card"
DEFAULT_REMINDER = (
    "你可以根据当前聊天、心情和人格，为自己自由决定一个简短的 QQ 群名片。"
    "想换名片时，请调用 set_free_group_card，按当前命名规则把文本传给 card。"
    "也可以保留现在的名字，不必为了提醒而硬改。"
    "内容由你决定，不必套用固定后缀。不要把这段提醒复述给群友，继续自然回应聊天。"
)
DEFAULT_TOOL_DESCRIPTION = (
    "自由修改你在当前 QQ 群中的完整群名片，表达你的名字、心情或当前状态。"
    "只修改自己。card 是最终显示的完整文本，不会自动拼接基础名字或后缀。"
    "内容保持简短，最多 32 个字符且不超过 60 个 UTF-8 字节。"
    "想保留原名时无需调用；仅在确实想换名片时使用。"
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


@dataclass
class Reminder:
    event: Any
    request: Any
    part: TextPart
    created_at: float = field(default_factory=time.monotonic)
    run_context: Any = None


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


class FreeCardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self._states: dict[tuple[str, str, str], GroupState] = {}
        self._reminders: dict[int, Reminder] = {}
        self._closed = False
        self._tool = FreeCardTool(plugin=self)
        self._configure_tool()
        context.add_llm_tools(self._tool)

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

    @filter.on_llm_request()
    async def remind(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Periodically offer the tool to a bot already replying in a group."""
        if self._closed:
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
        tools = getattr(req.func_tool, "tools", [])
        if not any(tool.name == TOOL_NAME and getattr(tool, "active", True) for tool in tools):
            return
        _, group, account = target
        state = self._state(event, group, account)
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
            text=f"[FreeCard reminder]\n{text.strip()[:2000]}\n当前命名规则：{naming_rule}"
        ).mark_as_temp()
        req.extra_user_content_parts.append(part)
        self._reminders[id(event)] = Reminder(event=event, request=req, part=part)
        state.reminded_at = now

    @filter.on_agent_begin()
    async def bind_reminder(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
    ) -> None:
        if reminder := self._reminders.get(id(event)):
            reminder.run_context = run_context

    def _cleanup(self, reminder: Reminder) -> None:
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
            self._cleanup(reminder)

    async def rename(self, event: AstrMessageEvent, card: Any) -> str:
        if self._closed:
            return "无法修改：插件已卸载。"
        if reminder := self._reminders.get(id(event)):
            self._cleanup(reminder)
        target = self._target(event)
        if target is None:
            return "无法修改：仅支持 OneBot v11 接入的 QQ 群聊；QQ 官方机器人不开放群名片修改接口。"
        try:
            name = compose_card(card, self.config.get("base_name", ""))
        except ValueError as exc:
            return f"无法修改：{exc}"
        client, group, account = target
        state = self._state(event, group, account)
        async with state.lock:
            if self._closed:
                return "无法修改：插件已卸载。"
            now = time.monotonic()
            if now < state.write_after:
                return f"请保留当前名片，约 {int(state.write_after - now) + 1} 秒后可再修改。"
            # Reserve before awaiting: cancellation must not cause a write burst.
            interval = setting_int(self.config, "min_update_seconds", 60, 3600)
            state.write_after = now + max(5, interval)
            try:
                result = await update_card(client, group, account, name)
            except asyncio.CancelledError:
                # QQ may have applied a request whose response was interrupted.
                state.write_after = time.monotonic() + 300
                raise
            completed_at = time.monotonic()
            state.write_after = completed_at + (interval if result.accepted else 300)
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
        for reminder in list(self._reminders.values()):
            self._cleanup(reminder)
        self._states.clear()
