"""Exercise the real SDK runner's first tool request and restored follow-up."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        await plugin.remind(event, request)
        assert request.contexts == history and request.system_prompt == "existing persona and memory"
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=Provider(), request=request,
                           run_context=ContextWrapper(context=SimpleNamespace(event=event)),
                           tool_executor=FunctionToolExecutor(), agent_hooks=Hooks())
        async for _ in runner.step_until_done(3):
            pass
        assert runner.get_final_llm_resp().completion_text == "normal reply"
        assert observed == [([TOOL_NAME], True), (["lookup_weather", TOOL_NAME], False)]
        update.assert_awaited_once_with(event.bot, "123", "456", "Alice Ready")
        assert not plugin._reminders
    asyncio.run(scenario())
