"""A bounded standard OneBot write with honest readback confirmation."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


def validate_card(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请提供非空的完整群名片。")
    name = " ".join(value.split())
    if any(
        unicodedata.category(char) in {"Cc", "Cs"}
        or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for char in name
    ):
        raise ValueError("名片含有不可显示的控制字符，请换成普通文字。")
    if len(name) > 32 or len(name.encode("utf-8")) > 60:
        raise ValueError("名片太长，请缩短到 32 个字符和 60 个 UTF-8 字节以内。")
    return name


def compose_card(value: Any, base_name: Any = "") -> str:
    """Keep an optional configured name fixed and validate the final card."""
    if not isinstance(base_name, str):
        raise ValueError("基础名字配置无效，请填写文字或留空。")
    if not base_name.strip():
        return validate_card(value)
    base = validate_card(base_name)
    if not isinstance(value, str):
        raise ValueError("请提供文字后缀；空字符串表示只显示基础名字。")
    suffix = " ".join(value.split())
    # Accept a complete card from an earlier conversation without duplicating
    # the configured name. Only strip an exact name with a word boundary.
    if suffix == base:
        suffix = ""
    elif suffix.startswith(base + " "):
        suffix = suffix[len(base) + 1 :]
    return validate_card(f"{base} {suffix}" if suffix else base)


class ApiFailure(Exception):
    pass


def error_text(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "OneBot 接口超时，请检查客户端连接。"
    result = getattr(exc, "result", {})
    if isinstance(result, dict) and "retcode" in result:
        detail = (
            result.get("message")
            or result.get("wording")
            or "客户端未提供原因，请查看同一时间的客户端日志。"
        )
        return f"OneBot retcode={result['retcode']}：{' '.join(str(detail).split())[:180]}"
    return " ".join(str(exc).split())[:200] or type(exc).__name__


async def call(client: Any, account: str, action: str, **params: Any) -> Any:
    invoke = getattr(client, "call_action", None)
    if not callable(invoke):
        invoke = getattr(getattr(client, "api", None), "call_action", None)
    if not callable(invoke):
        raise ApiFailure("当前客户端没有 OneBot call_action 接口。")
    payload = await asyncio.wait_for(invoke(action, self_id=account, **params), timeout=10)
    if isinstance(payload, dict) and "status" in payload and "retcode" in payload:
        if payload["status"] != "ok" or payload["retcode"] not in (0, "0"):
            exc = ApiFailure()
            exc.result = payload
            raise exc
        return payload.get("data")
    return payload


async def read_member(client: Any, group: str, account: str) -> dict[str, Any]:
    member = await call(
        client,
        account,
        "get_group_member_info",
        group_id=int(group),
        user_id=int(account),
        no_cache=True,
    )
    if not isinstance(member, dict) or not isinstance(member.get("card"), str):
        raise ApiFailure("成员信息没有返回群名片。")
    if any(
        key in member and str(member[key]) != expected
        for key, expected in (("group_id", group), ("user_id", account))
    ):
        raise ApiFailure("回读的群或账号与当前目标不一致。")
    return member


async def read_card(client: Any, group: str, account: str) -> str:
    return (await read_member(client, group, account))["card"]


@dataclass(frozen=True)
class UpdateResult:
    accepted: bool
    verified: bool = False
    detail: str = ""


async def update_card(client: Any, group: str, account: str, name: str) -> UpdateResult:
    # Skip an unnecessary write only after a fresh check, never from local cache.
    try:
        if await read_card(client, group, account) == name:
            return UpdateResult(True, True)
    except Exception as exc:  # noqa: BLE001 - OneBot wrappers use different exception classes.
        log.debug("Initial card read unavailable: %s", type(exc).__name__)
    accepted = False
    detail = ""
    try:
        await call(
            client, account, "set_group_card", group_id=int(group), user_id=int(account), card=name
        )
        accepted = True
    except Exception as exc:  # noqa: BLE001 - Handle failures at the OneBot transport boundary.
        detail = error_text(exc)
    # An error/timeout may arrive after QQ applied the write. Always check.
    for attempt in range(2):
        try:
            actual = await read_card(client, group, account)
        except Exception as exc:  # noqa: BLE001 - Readback availability must be reported honestly.
            read_error = error_text(exc)
            return UpdateResult(accepted, detail=f"{detail} {read_error}".strip())
        if actual == name:
            return UpdateResult(True, True)
        if not attempt:
            await asyncio.sleep(1)
    return UpdateResult(False, detail=detail or "回读仍是其他名片，请检查 QQ 限制或其他改名插件。")
