"""Unit tests for Anthropic <-> OpenAI Protocol Adapter (Milestone A3)."""

from __future__ import annotations

import asyncio
import json
import pytest

from argus.adapters.protocol import (
    anthropic_to_openai_request,
    format_anthropic_sse,
    openai_to_anthropic_response,
    transform_openai_stream_to_anthropic,
)


def test_anthropic_to_openai_request_mapping():
    """Verify conversion of Anthropic payload to standard OpenAI format."""
    anthropic_req = {
        "model": "argus/auto",
        "system": "You are a code fixer.",
        "messages": [
            {"role": "user", "content": "Fix the syntax error in math.py"},
            {"role": "assistant", "content": [{"type": "text", "text": "Here is the plan."}]},
            {"role": "user", "content": [{"type": "text", "text": "Proceed."}]},
        ],
        "max_tokens": 2048,
        "temperature": 0.2,
        "stream": True,
    }

    openai_req = anthropic_to_openai_request(anthropic_req)

    assert openai_req["model"] == "argus/auto"
    assert openai_req["stream"] is True
    assert openai_req["max_tokens"] == 2048
    assert openai_req["temperature"] == 0.2

    # System message prepended
    messages = openai_req["messages"]
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": "You are a code fixer."}
    assert messages[1] == {"role": "user", "content": "Fix the syntax error in math.py"}
    assert messages[2] == {"role": "assistant", "content": "Here is the plan."}
    assert messages[3] == {"role": "user", "content": "Proceed."}


def test_openai_to_anthropic_response_mapping():
    """Verify non-streaming conversion from OpenAI completion to Anthropic message format."""
    openai_resp = {
        "id": "chatcmpl-987654",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "def add(a, b):\n    return a + b\n",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 42,
            "completion_tokens": 12,
            "total_tokens": 54,
        },
    }

    anthropic_resp = openai_to_anthropic_response(openai_resp, requested_model="argus/auto")

    assert anthropic_resp["id"] == "msg_chatcmpl-987654"
    assert anthropic_resp["type"] == "message"
    assert anthropic_resp["role"] == "assistant"
    assert anthropic_resp["model"] == "argus/auto"
    assert anthropic_resp["stop_reason"] == "end_turn"
    assert len(anthropic_resp["content"]) == 1
    assert anthropic_resp["content"][0]["type"] == "text"
    assert anthropic_resp["content"][0]["text"] == "def add(a, b):\n    return a + b\n"
    assert anthropic_resp["usage"]["input_tokens"] == 42
    assert anthropic_resp["usage"]["output_tokens"] == 12


@pytest.mark.asyncio
async def test_transform_openai_stream_to_anthropic():
    """Verify streaming SSE generator translates chunks to Anthropic event sequence."""
    async def sample_openai_stream():
        chunks = [
            'data: {"choices": [{"delta": {"role": "assistant"}, "finish_reason": null}]}\n\n',
            'data: {"choices": [{"delta": {"content": "Hello "}, "finish_reason": null}]}\n\n',
            'data: {"choices": [{"delta": {"content": "world!"}, "finish_reason": "stop"}]}\n\n',
            'data: [DONE]\n\n',
        ]
        for c in chunks:
            yield c

    events = []
    async for frame in transform_openai_stream_to_anthropic(sample_openai_stream(), requested_model="argus/auto"):
        events.append(frame)

    combined_text = "".join(events)
    
    # Check event sequence
    assert "event: message_start" in combined_text
    assert "event: content_block_start" in combined_text
    assert "event: content_block_delta" in combined_text
    assert "Hello " in combined_text
    assert "world!" in combined_text
    assert "event: content_block_stop" in combined_text
    assert "event: message_delta" in combined_text
    assert "event: message_stop" in combined_text
