"""Groq API Client Wrapper with Multi-Key Pool & Round-Robin Failover.

Features:
- Automatic environment loading from .env
- Multi-Key Round-Robin rotation across multiple accounts/keys
- Instant failover on 429 Rate Limit (swaps to next key without waiting)
- Dynamic backoff with regex replenishment parsing when all keys are exhausted
- Accurate token cost tracking and latency profiling
"""

import os
import re
import sys
import time
import random
from typing import Dict, Any, Optional, List
from groq import Groq, RateLimitError, InternalServerError

# Estimated cost per 1M tokens (USD)
PRICING = {
    "openai/gpt-oss-20b": {
        "input_per_m": 0.08,
        "output_per_m": 0.08
    },
    "openai/gpt-oss-120b": {
        "input_per_m": 0.65,
        "output_per_m": 0.65
    }
}


def load_env_file() -> None:
    """Loads environment variables from .env file if present."""
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k:
                    os.environ[k] = v
    except Exception as e:
        print(f"[WARN] Failed to load .env file: {e}")


def calculate_cost(model_name: str, input_tokens: int, output_tokens: int) -> float:
    """Calculates estimated cost in USD based on model pricing."""
    pricing = PRICING.get(model_name, {"input_per_m": 0.10, "output_per_m": 0.10})
    input_cost = (input_tokens / 1_000_000.0) * pricing["input_per_m"]
    output_cost = (output_tokens / 1_000_000.0) * pricing["output_per_m"]
    return round(input_cost + output_cost, 7)


def parse_wait_time_from_error(error_str: str) -> float:
    """Extracts wait duration in seconds from Groq rate limit message."""
    m_min_sec = re.search(r"try again in (\d+)m([\d\.]+)s", error_str, re.IGNORECASE)
    if m_min_sec:
        mins = float(m_min_sec.group(1))
        secs = float(m_min_sec.group(2))
        return (mins * 60.0) + secs + 2.0
        
    m_sec = re.search(r"try again in ([\d\.]+)s", error_str, re.IGNORECASE)
    if m_sec:
        return float(m_sec.group(1)) + 2.0
        
    m_min = re.search(r"try again in (\d+)m", error_str, re.IGNORECASE)
    if m_min:
        return (float(m_min.group(1)) * 60.0) + 2.0
        
    return 0.0


class GroqClient:
    """Wrapper around Groq API with Multi-Key Pool and instant failover."""

    def __init__(self, api_keys: Optional[List[str]] = None):
        load_env_file()
        self.keys = self._discover_keys(api_keys)
        if not self.keys:
            print("[WARN] No valid GROQ_API_KEY found in environment or .env file.")
            self.clients = []
        else:
            self.clients = [Groq(api_key=k) for k in self.keys]
            print(f"[SUCCESS] GroqClient initialized with a pool of {len(self.clients)} API key(s).")
        self.current_idx = 0

    def _discover_keys(self, provided_keys: Optional[List[str]]) -> List[str]:
        """Discovers all available Groq API keys from args and environment."""
        keys = []
        if provided_keys:
            keys.extend(provided_keys)

        # Check comma-separated GROQ_API_KEYS
        env_keys_str = os.environ.get("GROQ_API_KEYS", "")
        if env_keys_str:
            for k in env_keys_str.split(","):
                k = k.strip()
                if k and k not in keys and not k.startswith("your_"):
                    keys.append(k)

        # Check individual keys: GROQ_API_KEY, GROQ_API_KEY_1, GROQ_API_KEY_2, GROQ_API_KEY_3...
        for env_var in ["GROQ_API_KEY", "GROQ_API_KEY_1", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4"]:
            val = os.environ.get(env_var, "").strip()
            if val and val not in keys and not val.startswith("your_"):
                keys.append(val)

        return keys

    def reload_keys(self) -> None:
        """Reloads keys from .env if updated at runtime."""
        load_env_file()
        self.keys = self._discover_keys(None)
        self.clients = [Groq(api_key=k) for k in self.keys]
        print(f"[RELOAD] GroqClient reloaded pool with {len(self.clients)} API key(s).")

    def query(
        self,
        prompt: str,
        model: str = "openai/gpt-oss-20b",
        system_prompt: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 512,
        max_retries: int = 8,
        base_delay: float = 2.0,
        persistent_retry: bool = True
    ) -> Dict[str, Any]:
        """Queries Groq with round-robin key rotation and zero-wait failover."""
        if not self.clients:
            self.reload_keys()
            if not self.clients:
                return {
                    "success": False,
                    "content": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 0.0,
                    "estimated_cost_usd": 0.0,
                    "error": "Missing GROQ_API_KEY environment variable."
                }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        total_pool_size = len(self.clients)
        attempt = 0
        consecutive_rate_limits = 0
        min_wait_time = float("inf")

        while True:
            attempt += 1
            # Select client in round-robin fashion
            client_idx = self.current_idx % total_pool_size
            self.current_idx += 1
            client = self.clients[client_idx]
            masked_key = f"...{self.keys[client_idx][-6:]}"

            start_time = time.perf_counter()
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens
                )
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                
                content = response.choices[0].message.content or ""
                input_tokens = response.usage.prompt_tokens if response.usage else 0
                output_tokens = response.usage.completion_tokens if response.usage else 0
                cost = calculate_cost(model, input_tokens, output_tokens)

                return {
                    "success": True,
                    "content": content,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "latency_ms": round(latency_ms, 2),
                    "estimated_cost_usd": cost,
                    "error": None
                }

            except (RateLimitError, InternalServerError) as e:
                err_str = str(e)
                wait_sec = parse_wait_time_from_error(err_str)
                consecutive_rate_limits += 1
                if wait_sec > 0:
                    min_wait_time = min(min_wait_time, wait_sec)

                # If we have alternative keys in the pool that haven't been tried on this round, switch instantly!
                if consecutive_rate_limits < total_pool_size:
                    next_idx = self.current_idx % total_pool_size
                    next_masked = f"...{self.keys[next_idx][-6:]}"
                    print(f"[ROTATOR] Key {client_idx + 1}/{total_pool_size} ({masked_key}) rate-limited! Instant failover -> Key {next_idx + 1} ({next_masked})...")
                    continue  # Zero sleep, try next key immediately!

                # All keys in the pool are rate-limited simultaneously: sleep until shortest replenishment
                consecutive_rate_limits = 0
                if min_wait_time < float("inf") and min_wait_time > 0:
                    delay = min_wait_time
                    print(f"[!] All {total_pool_size} API keys in pool rate-limited. Sleeping {delay:.1f}s for quota replenishment...")
                else:
                    delay = (base_delay * (2 ** min(attempt - 1, 5))) + random.uniform(0.5, 1.5)
                    print(f"[!] All keys busy ({type(e).__name__}): sleeping {delay:.1f}s...")

                min_wait_time = float("inf")

                if attempt >= max_retries and not persistent_retry:
                    latency_ms = (time.perf_counter() - start_time) * 1000.0
                    return {
                        "success": False,
                        "content": "",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "latency_ms": round(latency_ms, 2),
                        "estimated_cost_usd": 0.0,
                        "error": err_str
                    }
                    
                time.sleep(delay)

            except Exception as e:
                # Non-retryable errors (e.g. 401 Invalid API Key, 400 Bad Request) -> Fail Fast
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                return {
                    "success": False,
                    "content": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": round(latency_ms, 2),
                    "estimated_cost_usd": 0.0,
                    "error": f"API Error: {str(e)}"
                }
