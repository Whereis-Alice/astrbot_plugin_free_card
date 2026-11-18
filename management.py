"""Bounded local audit history and authenticated native plugin-view actions."""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger

from .card_api import UpdateResult, call, compose_card, read_member, validate_card

STORE_KEY = "card_management_v1"
MAX_HISTORY = 1000
PLUGIN = "astrbot_plugin_free_card"
DEFAULTS = {
    "base_name": "",
    "reminder_interval_minutes": 30,
    "min_update_seconds": 60,
    "world_presence_enabled": True,
}
SOURCES = {
    "tool": "模型工具",
    "reminder": "定期提醒",
    "webui": "管理面板",
    "world": "小世界串门",
    "world_restore": "归家恢复",
}
STATUSES = {
    "confirmed": "QQ 已确认",
    "unchanged": "未变化",
    "pending": "尚未确认",
    "failed": "失败",
    "deferred": "待归家恢复",
    "interrupted": "提交中断",
}


def validate_settings(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("请提供需要保存的配置。")
    allowed = {*DEFAULTS, "reminder_text"}
    if patch.keys() - allowed:
        raise ValueError("配置中含有不支持的选项。")
    result = dict(patch)
    if "base_name" in result:
        base = result["base_name"]
        if not isinstance(base, str):
            raise ValueError("基础名字应为文字或留空。")
        result["base_name"] = validate_card(base) if base.strip() else ""
    for key, maximum, label in (
        ("reminder_interval_minutes", 10080, "提醒间隔"),
        ("min_update_seconds", 3600, "最短改名间隔"),
    ):
        if key in result:
            value = result[key]
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"{label}应为 0 到 {maximum} 之间的整数。")
    if "world_presence_enabled" in result and type(result["world_presence_enabled"]) is not bool:
        raise ValueError("小世界串门开关应为开启或关闭。")
    if "reminder_text" in result:
        text = result["reminder_text"]
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("提醒文字应不超过 2000 字，留空使用内置提醒。")
        result["reminder_text"] = text.strip()
    return result


def csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # Spreadsheet apps interpret these leading characters as formulas.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


class CardManagement:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.history: list[dict[str, Any]] = []
        self.controls: dict[str, dict[str, bool]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.routes: list[tuple] = []
        self.tasks: set[asyncio.Task] = set()

    async def load(self) -> None:
        saved = await self.plugin.get_kv_data(STORE_KEY, {})
        if not isinstance(saved, dict):
            return
        history = saved.get("history", [])
        if isinstance(history, list):
            self.history = [
                dict(row)
                for row in history[-MAX_HISTORY:]
                if isinstance(row, dict)
                and isinstance(row.get("time"), (int, float))
                and row.get("status") in STATUSES
                and row.get("source") in SOURCES
            ]
        for name in ("controls", "observations"):
            values = saved.get(name, {})
            if isinstance(values, dict):
                setattr(
                    self,
                    name,
                    {
                        k: dict(v)
                        for k, v in list(values.items())[-1024:]
                        if isinstance(k, str) and isinstance(v, dict)
                    },
                )

    async def _save(self) -> None:
        await self.plugin.put_kv_data(
            STORE_KEY,
            copy.deepcopy(
                {
                    "history": self.history,
                    "controls": self.controls,
                    "observations": self.observations,
                }
            ),
        )

    def control(self, key: str) -> dict[str, bool]:
        return self.controls.get(key, {})

    async def consume_next_chat(self, key: str) -> None:
        if not self.control(key).get("next_chat"):
            return
        async with self.lock:
            self.controls.setdefault(key, {}).pop("next_chat", None)
            try:
                await self._save()
            except Exception as exc:  # noqa: BLE001 - Storage failure must not abort an Agent.
                logger.warning("[随心名片] 下次聊天提醒状态保存失败：%s", type(exc).__name__)

    async def record(
        self,
        row: dict,
        source: str,
        status: str,
        *,
        requested: str,
        previous: str | None = None,
        actual: str | None = None,
        detail: str = "",
    ) -> None:
        key = f"{row['platform']}:{row['account']}:{row['group']}"
        async with self.lock:
            item = {
                "id": uuid.uuid4().hex,
                "time": time.time(),
                "key": key,
                "platform": row["platform"],
                "account": row["account"],
                "group": row["group"],
                "source": source,
                "status": status,
                "requested": requested,
                "previous": previous,
                "actual": actual,
                "detail": detail[:400],
            }
            self.history.append(item)
            self.history = self.history[-MAX_HISTORY:]
            if actual is not None:
                self.observations.setdefault(key, {}).update(card=actual, observed_at=item["time"])
            self._trim()
            try:
                await self._save()
            except Exception as exc:  # noqa: BLE001 - An applied QQ write remains successful.
                logger.warning(
                    "[随心名片] 改名记录保存失败（本轮记录仍在内存）：%s", type(exc).__name__
                )

    def _trim(self) -> None:
        for values in (self.controls, self.observations):
            while len(values) > 1024:
                values.pop(next(iter(values)))

    async def record_result(
        self, row: dict, source: str, requested: str, result: UpdateResult
    ) -> None:
        # The periodic world watcher does not create history for repeated no-op checks.
        if source in {"world", "world_restore"} and result.unchanged:
            return
        status = (
            ("unchanged" if result.unchanged else "confirmed")
            if result.verified
            else ("pending" if result.accepted else "failed")
        )
        await self.record(
            row,
            source,
            status,
            requested=requested,
            previous=result.previous,
            actual=requested if result.verified else result.actual,
            detail=result.detail,
        )

    def settings(self) -> dict[str, Any]:
        from .main import DEFAULT_REMINDER

        return {
            key: self.plugin.config.get(key, default)
            for key, default in {**DEFAULTS, "reminder_text": DEFAULT_REMINDER}.items()
        }

    async def save_settings(self, patch: Any) -> dict[str, Any]:
        values = validate_settings(patch)
        config = self.plugin.config
        async with self.config_lock:
            if self.plugin._closed:
                raise ValueError("插件已卸载，请刷新页面。")
            previous = dict(config)
            config.update(values)
            try:
                saver = getattr(config, "save_config_async", None)
                if callable(saver):
                    await saver()
                else:
                    saver = getattr(config, "save_config", None)
                    if not callable(saver):
                        raise ValueError("当前配置不支持持久保存，请使用 AstrBot 插件配置页。")
                    await asyncio.to_thread(saver)
            except BaseException:
                config.clear()
                config.update(previous)
                raise
            self.plugin._configure_tool()
        logger.info("[随心名片] 管理面板配置已保存并立即生效。")
        return self.settings()

    def target(self, key: Any) -> tuple[dict, Any]:
        if not isinstance(key, str) or key not in self.plugin._world_records:
            raise ValueError("请选择插件已识别的群。群收到实际消息后会自动出现。")
        row = self.plugin._world_records[key]
        if key != f"{row['platform']}:{row['account']}:{row['group']}":
            raise ValueError("群记录与目标不一致，请重新选择。")
        event = self.plugin._world_recovered_event(key, row)
        return row, event

    async def action(self, data: Any) -> dict:
        if not isinstance(data, dict):
            raise ValueError("操作内容无效。")
        if self.plugin._closed:
            raise ValueError("插件已卸载，请刷新页面。")
        key, action = data.get("key"), data.get("action")
        row, event = self.target(key)
        if action in {"pause", "resume", "next_chat"}:
            async with self.lock:
                old = copy.deepcopy(self.controls)
                control = self.controls.setdefault(key, {})
                if action == "next_chat":
                    if control.get("paused"):
                        raise ValueError("请先恢复此群的日常提醒。")
                    control["next_chat"] = True
                    message = "已安排下次合格聊天请求提醒；仍遵守冷却和串门暂停。"
                else:
                    control["paused"] = action == "pause"
                    message = (
                        "已暂停之后的日常提醒；已开始的请求、主动工具和串门不受影响。"
                        if action == "pause"
                        else "已恢复日常提醒。"
                    )
                self._trim()
                try:
                    await self._save()
                except BaseException:
                    self.controls = old
                    raise
            logger.info("[随心名片] 群 %s / 机器人 %s：%s", row["group"], row["account"], message)
            return {"message": message}
        if event is None:
            raise ValueError("此群的 OneBot 连接暂不可用，请检查平台连接。")
        if action == "refresh":
            target = self.plugin._target(event)
            if target is None:
                raise ValueError("目标不支持读取群名片。")
            client, group, account = target
            member = await read_member(client, group, account)
            observed = {"card": member["card"], "observed_at": time.time()}
            try:
                info = await call(
                    client, account, "get_group_info", group_id=int(group), no_cache=True
                )
                if (
                    isinstance(info, dict)
                    and str(info.get("group_id")) == group
                    and isinstance(info.get("group_name"), str)
                ):
                    observed["group_name"] = info["group_name"][:120]
            except Exception as exc:  # noqa: BLE001 - Card read success is independent of group-title availability.
                logger.debug("[随心名片] 群名称读取不可用：%s", type(exc).__name__)
            async with self.lock:
                self.observations.setdefault(key, {}).update(observed)
                self._trim()
                await self._save()
            return {"message": "已从 QQ 回读当前名片。", "card": member["card"]}
        if action == "rename":
            compose_card(data.get("card"), self.plugin.config.get("base_name", ""))
            message = await self.plugin.rename(event, data.get("card"), origin="webui")
            return {"message": message}
        raise ValueError("不支持此操作。")

    def groups(self) -> list[dict]:
        from .main import setting_int

        now = time.monotonic()
        interval = setting_int(self.plugin.config, "reminder_interval_minutes", 30, 10080) * 60
        result = []
        for key, row in self.plugin._world_records.items():
            state = self.plugin._states.get((row["platform"], row["account"], row["group"]))
            observation = self.observations.get(key, {})
            control = self.control(key)
            remaining = (
                max(0, interval - (now - state.reminded_at))
                if state and state.reminded_at is not None
                else 0
            )
            write = max(0, state.write_after - now) if state else 0
            retry = max(0, state.remind_after - now) if state else 0
            pending = next(
                (
                    r
                    for r in self.plugin._reminders.values()
                    if r.key == (row["platform"], row["account"], row["group"])
                ),
                None,
            )
            event = self.plugin._world_recovered_event(key, row)
            slot = row.get("confirmed_slot") or row.get("pending_slot") or {}
            status = "waiting"
            if event is None:
                status = "offline"
            elif pending is not None:
                status = "running" if pending.started_at is not None else "prepared"
            elif control.get("paused"):
                status = "paused"
            elif slot.get("status"):
                status = "world"
            elif not interval and not control.get("next_chat"):
                status = "disabled"
            elif write or retry:
                status = "cooldown"
            elif control.get("next_chat"):
                status = "queued"
            elif remaining:
                status = "interval"
            elif state and state.last_skip_reason == "tool_unavailable":
                status = "tool_unavailable"
            result.append(
                {
                    "key": key,
                    "platform": row["platform"],
                    "group": row["group"],
                    "account": row["account"],
                    "group_name": observation.get("group_name", ""),
                    "card": observation.get("card"),
                    "observed_at": observation.get("observed_at"),
                    "normal_card": slot.get("baseline"),
                    "world_status": slot.get("status"),
                    "status": status,
                    "connected": event is not None,
                    "paused": bool(control.get("paused")),
                    "next_chat": bool(control.get("next_chat")),
                    "remaining_seconds": math.ceil(0 if control.get("next_chat") else remaining),
                    "cooldown_seconds": math.ceil(max(write, retry)),
                    "seen_at": row.get("seen_at"),
                }
            )
        return sorted(result, key=lambda row: row.get("seen_at") or 0, reverse=True)

    def filtered_history(self, query: Any) -> list[dict]:
        key = query.get("key", "")
        search = str(query.get("search", "")).strip().casefold()[:200]
        source, status = query.get("source", ""), query.get("status", "")
        try:
            start = float(query.get("from", 0) or 0)
            end = float(query.get("to", 0) or 0)
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("日期范围无效。") from None
        return [
            copy.deepcopy(row)
            for row in reversed(self.history)
            if (not key or row["key"] == key)
            and (not source or row["source"] == source)
            and (not status or row["status"] == status)
            and (not start or row["time"] >= start)
            and (not end or row["time"] <= end)
            and (
                not search
                or search
                in " ".join(
                    str(row.get(k) or "")
                    for k in ("requested", "previous", "actual", "group", "account", "detail")
                ).casefold()
            )
        ]

    def history_page(self, query: Any) -> dict:
        try:
            page = max(1, int(query.get("page", 1)))
            size = min(100, max(1, int(query.get("size", 20))))
        except (ValueError, TypeError):
            raise ValueError("页码无效。") from None
        rows = self.filtered_history(query)
        page = min(page, max(1, math.ceil(len(rows) / size)))
        return {
            "items": rows[(page - 1) * size : page * size],
            "total": len(rows),
            "page": page,
            "size": size,
            "limit": MAX_HISTORY,
        }

    def overview(self, tz_minutes: int = 0) -> dict:
        now = time.time()
        groups = self.groups()
        offset = min(840, max(-840, tz_minutes)) * 60
        midnight = math.floor((now + offset) / 86400) * 86400 - offset
        trend = []
        for offset in range(6, -1, -1):
            start = midnight - offset * 86400
            trend.append(
                {
                    "time": start,
                    "count": sum(
                        start <= r["time"] < start + 86400 and r["status"] == "confirmed"
                        for r in self.history
                    ),
                }
            )
        return {
            "groups": groups,
            "recent": self.history_page({"size": 5})["items"],
            "records": len(self.history),
            "confirmed": sum(r["status"] == "confirmed" for r in self.history),
            "last_24h": sum(
                r["status"] == "confirmed" and now - 86400 <= r["time"] <= now for r in self.history
            ),
            "trend": trend,
            "settings": self.settings(),
            "sources": SOURCES,
            "statuses": STATUSES,
            "limit": MAX_HISTORY,
            "server_time": now,
        }

    def export(self, query: Any) -> tuple[str, str, str]:
        rows = self.filtered_history(query)
        if query.get("format", "json") == "json":
            return json.dumps(rows, ensure_ascii=False, indent=2), "application/json", "json"
        if query.get("format") != "csv":
            raise ValueError("导出格式应为 CSV 或 JSON。")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "时间(UTC)",
                "连接",
                "机器人",
                "群号",
                "来源",
                "结果",
                "之前名片",
                "请求名片",
                "回读名片",
                "说明",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    csv_cell(value)
                    for value in (
                        datetime.fromtimestamp(row["time"], timezone.utc).isoformat(),
                        row["platform"],
                        row["account"],
                        row["group"],
                        SOURCES[row["source"]],
                        STATUSES[row["status"]],
                        row["previous"],
                        row["requested"],
                        row["actual"],
                        row["detail"],
                    )
                ]
            )
        return "\ufeff" + stream.getvalue(), "text/csv; charset=utf-8", "csv"

    def register(self) -> None:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return
        try:
            from astrbot.api.web import error_response, json_response, request, stream_response
        except ImportError:
            logger.info(
                "[随心名片] 当前 AstrBot 未提供原生视图 Web SDK，群名片功能正常；管理面板需升级核心。"
            )
            return

        async def handler():
            if self.plugin._closed:
                return error_response("插件已卸载，请刷新页面。", status_code=503)
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                path = request.path.rstrip("/").rsplit("/", 1)[-1]
                if request.method == "GET":
                    if path == "overview":
                        result = self.overview(int(request.query.get("tz", 0)))
                    elif path == "history":
                        result = self.history_page(request.query)
                    elif path == "config":
                        result = self.settings()
                    elif path == "export":
                        contents, media_type, extension = self.export(request.query)
                        return stream_response(
                            iter([contents.encode("utf-8")]),
                            content_type=media_type,
                            headers={
                                "Content-Disposition": f'attachment; filename="free-card-history.{extension}"',
                                "Cache-Control": "no-store",
                            },
                        )
                    else:
                        return error_response("页面接口不存在。", status_code=404)
                elif path == "config":
                    result = await self.save_settings(await request.json())
                else:
                    result = await self.action(await request.json())
                return json_response(
                    {"status": "ok", "data": result}, headers={"Cache-Control": "no-store"}
                )
            except ValueError as exc:
                return error_response(str(exc))
            except Exception as exc:  # noqa: BLE001 - Never leak configuration or transport internals.
                logger.warning("[随心名片] 管理面板操作失败：%s", type(exc).__name__)
                return error_response(
                    "操作暂时失败，请检查连接或查看 [随心名片] 后台日志。", status_code=500
                )
            finally:
                self.tasks.discard(task)

        for path, methods in (
            ("overview", ["GET"]),
            ("history", ["GET"]),
            ("export", ["GET"]),
            ("config", ["GET", "POST"]),
            ("action", ["POST"]),
        ):
            route = f"/{PLUGIN}/manage/{path}"
            register(route, handler, methods, "随心名片管理面板")
            self.routes.append((route, handler, methods))

    async def close(self) -> None:
        # Remove only this instance's handlers, preserving a newer reload instance.
        registry = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(registry, list):
            registry[:] = [
                row
                for row in registry
                if not any(
                    row[0] == route and row[1] is handler and row[2] == methods
                    for route, handler, methods in self.routes
                )
            ]
        tasks = {task for task in self.tasks if task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
