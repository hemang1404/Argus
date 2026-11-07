"""Protocol Adapter: Bidirectional translation between Anthropic Messages API and OpenAI Chat API.

Allows tools built exclusively for Anthropic (Claude Code, Continue Anthropic provider, etc.)
to talk directly to Argus Gateway through /v1/messages with zero code changes in the client.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple


def _extract_text(content: Any) -> str:
    """Normalize string or block list to single text string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


def anthropic_to_openai_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an Anthropic /v1/messages request body into an OpenAI /v1/chat/completions body."""
    openai_messages: List[Dict[str, Any]] = []

    # Handle Anthropic top-level 'system' prompt
    if "system" in body and body["system"]:
        sys_text = _extract_text(body["system"])
        if sys_text.strip():
            openai_messages.append({"role": "system", "content": sys_text})

    # Convert Anthropic message sequence
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = _extract_text(msg.get("content", ""))
        openai_messages.append({"role": role, "content": content})

    result: Dict[str, Any] = {
        "model": body.get("model", "argus/auto"),
        "messages": openai_messages,
        "stream": body.get("stream", False),
    }

    if "max_tokens" in body:
        result["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]
    if "stop_sequences" in body and body["stop_sequences"]:
        result["stop"] = body["stop_sequences"]

    return result


def openai_to_anthropic_response(
    openai_data: Dict[str, Any],
    requested_model: str,
) -> Dict[str, Any]:
    """Convert an OpenAI /v1/chat/completions response into an Anthropic /v1/messages response."""
    msg_id = openai_data.get("id", f"msg_{uuid.uuid4().hex[:16]}")
    if not msg_id.startswith("msg_"):
        msg_id = f"msg_{msg_id}"

    choices = openai_data.get("choices", [])
    text_content = ""
    stop_reason = "end_turn"

    if choices:
        first = choices[0]
        msg = first.get("message", {})
        text_content = msg.get("content", "") or ""
        finish = first.get("finish_reason", "stop")
        if finish == "length":
            stop_reason = "max_tokens"
        elif finish == "stop":
            stop_reason = "end_turn"
        else:
            stop_reason = finish or "end_turn"

    usage = openai_data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": text_content,
            }
        ],
        "model": requested_model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def format_anthropic_sse(event: str, data: Dict[str, Any]) -> str:
    """Format an SSE frame for the Anthropic Messages streaming protocol."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def transform_openai_stream_to_anthropic(
    chunk_stream: AsyncGenerator[str, None],
    requested_model: str,
) -> AsyncGenerator[str, None]:
    """Transform an OpenAI SSE token stream into Anthropic Messages SSE event stream."""
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    
    # 1. message_start
    yield format_anthropic_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": requested_model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    # 2. content_block_start
    yield format_anthropic_sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )

    output_tokens_count = 0
    stop_reason = "end_turn"

    async for chunk_text in chunk_stream:
        if not chunk_text:
            continue
        
        # Parse chunk if it's SSE data
        lines = chunk_text.splitlines()
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if payload_str == "[DONE]":
                continue
            try:
                payload = json.loads(payload_str)
            except Exception:
                continue

            choices = payload.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content_delta = delta.get("content", "")
            finish = choices[0].get("finish_reason")

            if content_delta:
                output_tokens_count += 1
                yield format_anthropic_sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": content_delta},
                    },
                )

            if finish:
                if finish == "length":
                    stop_reason = "max_tokens"
                elif finish == "stop":
                    stop_reason = "end_turn"
                else:
                    stop_reason = finish

    # 3. content_block_stop
    yield format_anthropic_sse(
        "content_block_stop",
        {
            "type": "content_block_stop",
            "index": 0,
        },
    )

    # 4. message_delta
    yield format_anthropic_sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens_count},
        },
    )

    # 5. message_stop
    yield format_anthropic_sse(
        "message_stop",
        {
            "type": "message_stop",
        },
    )
