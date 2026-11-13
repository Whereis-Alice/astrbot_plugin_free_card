import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api import FunctionTool, ToolSet
from astrbot.api.provider import LLMResponse
from astrbot.core.agent.message import TextPart

from free_card_plugin.request_gate import CardCallPolicy, CardRequestGate


def test_shared_provider_does_not_force_unrelated_or_concurrent_requests():
    observed = []
    async def original(**kwargs):
        observed.append((kwargs["session_id"], kwargs.get("tool_choice", "auto")))
        await asyncio.sleep(0)
        return LLMResponse(role="assistant", tools_call_name=["card"], tools_call_args=[{"card": "free"}])

    provider = SimpleNamespace(text_chat=original)
    gate = CardRequestGate()
    tools = ToolSet([FunctionTool(name="card", description="card", parameters={})])

    async def scenario():
        policy = CardCallPolicy("exact temporary hint", "card", "123")
        gate.bind([provider], policy)
        await asyncio.gather(
            provider.text_chat(session_id="reminder", func_tool=tools,
                               extra_user_content_parts=[TextPart(text=policy.hint)]),
            provider.text_chat(session_id="unrelated", func_tool=tools,
                               extra_user_content_parts=[TextPart(text="ordinary chat")]),
        )
        policy.active = False
        await provider.text_chat(session_id="followup", func_tool=tools,
                                 extra_user_content_parts=[TextPart(text=policy.hint)])
    asyncio.run(scenario())
    assert observed == [("reminder", "required"), ("unrelated", "auto"), ("followup", "auto")]
    gate.close()
    assert provider.text_chat is original


def test_gate_does_not_retry_transport_failures():
    provider = SimpleNamespace(text_chat=AsyncMock(side_effect=TimeoutError("offline")))
    gate = CardRequestGate()
    tools = ToolSet([FunctionTool(name="card", description="card", parameters={})])
    async def scenario():
        policy = CardCallPolicy("hint", "card", "123")
        gate.bind([provider], policy)
        try:
            await provider.text_chat(func_tool=tools, extra_user_content_parts=[TextPart(text="hint")])
        except TimeoutError:
            pass
        else:
            raise AssertionError("timeout must propagate")
    asyncio.run(scenario())
    assert provider.text_chat.__wrapped__.await_count == 1
    gate.close()
