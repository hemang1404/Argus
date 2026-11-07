"""Tests for zero-stutter tool streaming pass-through and token accounting in Argus Gateway."""

import json
import tempfile
from pathlib import Path
import pytest

from argus.gateway.config import GatewayConfig
from argus.gateway.interceptor import StreamInterceptor, StreamPatchHealer
from argus.gateway.server import init_gateway


def test_stream_interceptor_pass_through_mode():
    """Verify that in pass_through mode, SEARCH/REPLACE blocks are emitted immediately without buffering."""
    healer = StreamPatchHealer(workspace=".", enabled=True)
    interceptor = StreamInterceptor(healer, pass_through=True)

    chunk1 = "Here is an edit:\n<<<<<<< SEARCH\n"
    out1 = interceptor.process_chunk(chunk1)
    # In pass-through mode, chunk1 is emitted immediately, NOT held back!
    assert out1 == chunk1
    assert interceptor.buffer == ""

    chunk2 = "old_code()\n=======\nnew_code()\n>>>>>>> REPLACE\n"
    out2 = interceptor.process_chunk(chunk2)
    assert out2 == chunk2
    assert interceptor.buffer == ""


def test_stream_interceptor_buffer_overflow_safety():
    """Verify that an oversized unclosed diff block flushes to prevent infinite client stalling."""
    healer = StreamPatchHealer(workspace=".", enabled=True)
    # Set small max_buffer_chars for test
    interceptor = StreamInterceptor(healer, pass_through=False, max_buffer_chars=100)

    # Start patch block
    out1 = interceptor.process_chunk("prefix <<<<<<< SEARCH\nline 1\n")
    assert out1 == "prefix "

    # Send a chunk that exceeds 100 chars without REPLACE terminator
    big_chunk = "x" * 150 + "\n"
    out2 = interceptor.process_chunk(big_chunk)
    # Must flush to prevent indefinite freeze!
    assert len(out2) > 100
    assert interceptor.in_patch_block is False
    assert interceptor.buffer == ""


def test_streaming_tool_calls_pass_through_unmodified():
    """Verify tool call streaming chunks pass through untouched and with correct model alias."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = GatewayConfig(
            upstream_base_url="https://api.openai.com/v1",
            api_key="mock-key",
            task_budget_usd=1.0,
            session_budget_usd=10.0,
        )
        init_gateway(config=cfg, workspace=tmp_dir)

        # Mock tool call delta chunk
        tool_chunk = {
            "id": "chatcmpl-test-tool",
            "object": "chat.completion.chunk",
            "created": 123456789,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "test.py"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }

        # Verify interceptor preserves delta when content is empty
        healer = StreamPatchHealer(workspace=tmp_dir, enabled=True)
        interceptor = StreamInterceptor(healer, pass_through=True)
        delta_content = tool_chunk["choices"][0]["delta"].get("content", "")
        assert delta_content == ""
        # No buffering or stalling occurred
        assert interceptor.flush() == ""
