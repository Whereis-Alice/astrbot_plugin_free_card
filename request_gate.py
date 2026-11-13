"""Require a card tool call only inside the Agent carrying this reminder."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from astrbot.api import ToolSet, logger
from astrbot.core.agent.message import Message, TextPart


@dataclass
class CardCallPolicy:
    hint: str
    tool_name: str
    group: str
    active: bool = True
    repair_used: bool = False
    rejected_providers: set[int] = field(default_factory=set)

    def matches(self, kwargs: dict[str, Any]) -> bool:
        tools = getattr(kwargs.get("func_tool"), "tools", [])
        if not self.active or not any(t.name == self.tool_name for t in tools):
            return False
        parts = list(kwargs.get("extra_user_content_parts") or [])
        for message in kwargs.get("contexts") or []:
            content = message.get("content") if isinstance(message, dict) else message.content
            if isinstance(content, list):
                parts.extend(content)
        return any((p.get("text") if isinstance(p, dict) else getattr(p, "text", None))
                   == self.hint for p in parts)


_current_policy: ContextVar[CardCallPolicy | None] = ContextVar("free_card_request", default=None)


def _choice_rejected(value: Any) -> bool:
    text = str(value).lower()
    return ("tool_choice" in text or "tool choice" in text) and any(
        word in text for word in ("not support", "unsupported", "invalid", "not allowed", "不支持", "required")
    )


class CardRequestGate:
    """Provider delegates preserve the original methods and other Agent requests."""

    def __init__(self) -> None:
        self.closed = False
        self._bindings: list[tuple[Any, str, Any, Any]] = []
        self._providers: set[int] = set()

    def bind(self, providers: list[Any], policy: CardCallPolicy) -> bool:
        for provider in providers:
            if id(provider) in self._providers:
                continue
            chat = getattr(provider, "text_chat", None)
            if not callable(chat):
                continue
            self._install(provider, chat)
        if not self._providers:
            return False
        _current_policy.set(policy)
        return True

    def _install(self, provider: Any, original_chat: Any) -> None:
        @wraps(original_chat)
        async def chat(*args: Any, **kwargs: Any) -> Any:
            policy = _current_policy.get()
            if self.closed or policy is None or not policy.matches(kwargs):
                return await original_chat(*args, **kwargs)
            return await self._call(provider, original_chat, policy, args, kwargs)

        self._bindings.append((provider, "text_chat", original_chat, chat))
        provider.text_chat = chat
        original_stream = getattr(provider, "text_chat_stream", None)
        if callable(original_stream):
            @wraps(original_stream)
            async def stream(*args: Any, **kwargs: Any):
                policy = _current_policy.get()
                if not self.closed and policy is not None and policy.matches(kwargs):
                    # Do not publish a text-only draft before deciding whether
                    # the reminder was obeyed. Later normal replies still stream.
                    yield await self._call(provider, original_chat, policy, args, kwargs)
                else:
                    async for response in original_stream(*args, **kwargs):
                        yield response

            self._bindings.append((provider, "text_chat_stream", original_stream, stream))
            provider.text_chat_stream = stream
        self._providers.add(id(provider))

    async def _call(self, provider: Any, chat: Any, policy: CardCallPolicy,
                    args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        selected = [t for t in kwargs["func_tool"].tools if t.name == policy.tool_name]
        forced = {**kwargs, "func_tool": ToolSet(selected), "request_max_retries": 1}
        required = id(provider) not in policy.rejected_providers
        if required:
            forced["tool_choice"] = "required"
        else:
            forced["tool_choice"] = "auto"
        logger.info("[随心名片] 群 %s：本轮模型请求要求调用改名工具（%s），调用后恢复普通回复。",
                    policy.group, "接口强制调用" if required else "兼容提醒")
        rejection = False
        rejected_error = None
        try:
            response = await chat(*args, **forced)
        except Exception as exc:
            if not required or not _choice_rejected(exc):
                raise
            rejection = True
            rejected_error = exc
            response = None
        if response is not None and getattr(response, "role", "") == "err":
            rejection = required and _choice_rejected(getattr(response, "completion_text", ""))
        if rejection:
            policy.rejected_providers.add(id(provider))
            logger.warning("[随心名片] 群 %s：当前接口不支持强制工具调用，改用兼容方式补一次提醒。",
                           policy.group)
        elif response is not None and policy.tool_name in getattr(response, "tools_call_name", []):
            return response
        elif response is not None and getattr(response, "role", "") == "err":
            return response
        # Only repair a missing tool call, once for this reminder. The original
        # text draft is not emitted or added to memory before this decision.
        if policy.repair_used or not policy.active:
            if response is None and rejected_error is not None:
                raise rejected_error
            return response
        policy.repair_used = True
        if not rejection:
            logger.warning("[随心名片] 群 %s：模型只返回文字、没有调用改名工具；本轮补一次明确提醒。",
                           policy.group)
        repair = TextPart(text=(
            f"本次群名片维护尚未完成。下一步必须直接调用 {policy.tool_name}，"
            "根据本轮已有上下文自由决定 card，不要先回复自然语言。"
            "这是独立的新提醒，历史中已经改过名字也不能替代本次调用。"
            "工具返回后再回答原来的问题。"
        )).mark_as_temp()
        retry = {**forced, "contexts": list(forced.get("contexts") or [])
                 + [Message(role="user", content=[repair])]}
        if rejection:
            retry["tool_choice"] = "auto"
        return await chat(*args, **retry)

    def close(self) -> None:
        self.closed = True
        for provider, name, original, delegated in reversed(self._bindings):
            if getattr(provider, name, None) is delegated:
                setattr(provider, name, original)
        self._bindings.clear()
        self._providers.clear()
