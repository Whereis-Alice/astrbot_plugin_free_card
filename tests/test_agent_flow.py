"""Exercise the real SDK runner's first tool request and restored follow-up."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import FunctionTool, ToolSet
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

from free_card_plugin.card_api import UpdateResult
from free_card_plugin.main import TOOL_NAME, FreeCardPlugin


def test_real_runner_calls_card_then_receives_restored_tools(monkeypatch):
    event = SimpleNamespace(
        bot=SimpleNamespace(call_action=AsyncMock()),
        get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "qq",
        get_group_id=lambda: "123", get_self_id=lambda: "456", is_stopped=lambda: False,
    )
    plugin = FreeCardPlugin(SimpleNamespace(add_llm_tools=lambda *_: None), {})
    plugin.put_kv_data = AsyncMock()
    update = AsyncMock(return_value=UpdateResult(True, True))
    monkeypatch.setattr("free_card_plugin.main.update_card", update)
    other = FunctionTool(name="lookup_weather", description="weather", parameters={})
    history = [{"role": "user", "content": "existing conversation memory"},
               {"role": "assistant", "content": "previous reply"}]
    request = ProviderRequest(prompt="normal chat", system_prompt="existing persona and memory",
                              contexts=history, func_tool=ToolSet([other, plugin._tool]))
    observed = []

    class Provider:
        provider_config = {"id": "offline"}

        def meta(self):
            return SimpleNamespace(id="offline", type="offline")

        def get_model(self):
            return "offline"

        async def text_chat(self, **kwargs):
            contexts = json.dumps(kwargs.get("contexts", []), ensure_ascii=False,
                                  default=lambda item: item.model_dump())
            assert "existing conversation memory" in contexts and "previous reply" in contexts
            assert "existing persona and memory" in contexts
            assert ("[FreeCard reminder]" in contexts) is (len(observed) == 0)
            observed.append((kwargs["func_tool"].names(),
                             any("[FreeCard reminder]" in p.text
                                 for p in kwargs.get("extra_user_content_parts", []))))
            if len(observed) == 1:
                return LLMResponse(role="assistant", tools_call_name=[TOOL_NAME],
                                   tools_call_args=[{"card": "Alice Ready"}], tools_call_ids=["card-1"])
            return LLMResponse(role="assistant", completion_text="normal reply")

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, run_context):
            await plugin.bind_reminder(event, run_context)

        async def on_agent_done(self, run_context, response):
            await plugin.finish_reminder(event, run_context, response)

    async def scenario():
        provider = Provider()
        plugin.context.get_all_providers = lambda: [provider]
        await plugin.remind(event, request)
        assert request.contexts == history and request.system_prompt == "existing persona and memory"
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=provider, request=request,
                           run_context=ContextWrapper(context=SimpleNamespace(event=event)),
                           tool_executor=FunctionToolExecutor(), agent_hooks=Hooks())
        async for _ in runner.step_until_done(3):
            pass
        assert runner.get_final_llm_resp().completion_text == "normal reply"
        assert observed == [([TOOL_NAME], True), (["lookup_weather", TOOL_NAME], False)]
        update.assert_awaited_once_with(event.bot, "123", "456", "Alice Ready")
        assert not plugin._reminders
        await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize("behavior", ["obey", "text_first", "reject_required", "text_always"])
@pytest.mark.parametrize("streaming", [False, True])
def test_required_card_call_repairs_text_draft_without_losing_normal_reply(monkeypatch, behavior, streaming):
    event = SimpleNamespace(
        bot=SimpleNamespace(call_action=AsyncMock()),
        get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "qq",
        get_group_id=lambda: "123", get_self_id=lambda: "456", is_stopped=lambda: False,
    )
    plugin = FreeCardPlugin(SimpleNamespace(add_llm_tools=lambda *_: None), {"reminder_interval_minutes": 10})
    plugin.put_kv_data = AsyncMock()
    update = AsyncMock(return_value=UpdateResult(True, True))
    monkeypatch.setattr("free_card_plugin.main.update_card", update)
    other = FunctionTool(name="other", description="other", parameters={})
    req = ProviderRequest(prompt="original chat", contexts=[{"role": "user", "content": "original memory"}],
                          func_tool=ToolSet([other, plugin._tool]))
    calls, streams = [], []

    class Provider:
        provider_config = {"id": "offline"}

        def meta(self):
            return SimpleNamespace(id="offline", type="offline")

        def get_model(self):
            return "offline"

        async def text_chat(self, **kwargs):
            names = kwargs["func_tool"].names()
            choice = kwargs.get("tool_choice", "auto")
            calls.append((names, choice))
            contexts = json.dumps(kwargs.get("contexts", []), default=lambda p: p.model_dump())
            assert "original memory" in contexts
            if names == [TOOL_NAME]:
                if behavior == "reject_required" and choice == "required":
                    raise ValueError("tool_choice required is not supported")
                if behavior == "text_always" or behavior == "text_first" and len(calls) == 1:
                    return LLMResponse(role="assistant", completion_text="unexecuted draft")
                return LLMResponse(role="assistant", tools_call_name=[TOOL_NAME],
                                   tools_call_args=[{"card": "Alice Free"}], tools_call_ids=["c1"])
            assert "[FreeCard reminder]" not in contexts and choice == "auto"
            return LLMResponse(role="assistant", completion_text="normal reply")

        async def text_chat_stream(self, **kwargs):
            streams.append(kwargs["func_tool"].names())
            yield await self.text_chat(**kwargs)

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, run):
            await plugin.bind_reminder(event, run)

        async def on_agent_done(self, run, response):
            await plugin.finish_reminder(event, run, response)

    async def scenario():
        provider = Provider()
        original_chat = provider.text_chat
        original_stream = provider.text_chat_stream
        plugin.context.get_all_providers = lambda: [provider]
        await plugin.remind(event, req)
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=provider, request=req,
                           run_context=ContextWrapper(context=SimpleNamespace(event=event)),
                           tool_executor=FunctionToolExecutor(), agent_hooks=Hooks(), streaming=streaming)
        async for _ in runner.step_until_done(3):
            pass
        assert calls[0] == ([TOOL_NAME], "required")
        if behavior == "text_always":
            assert calls == [([TOOL_NAME], "required"), ([TOOL_NAME], "required")]
            update.assert_not_called()
            state = plugin._state(event, "123", "456")
            assert state.reminded_at is None and state.remind_after > 0
        else:
            assert runner.get_final_llm_resp().completion_text == "normal reply"
            assert "unexecuted draft" not in str(runner.run_context.messages)
            update.assert_awaited_once_with(event.bot, "123", "456", "Alice Free")
            assert calls[-1] == (["other", TOOL_NAME], "auto")
            assert len(calls) == (2 if behavior == "obey" else 3)
        assert all(names != [TOOL_NAME] for names in streams)
        assert not plugin._reminders and len(req.func_tool.tools) == 2
        await plugin.terminate()
        assert provider.text_chat == original_chat and provider.text_chat_stream == original_stream
    asyncio.run(scenario())
