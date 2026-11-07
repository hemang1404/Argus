import os
import tempfile
from pathlib import Path
import pytest

from argus.config import ArgusConfig
from argus.providers.models import ProviderConfig, PROVIDER_PRESETS
from argus.providers.storage import ProviderStorage, scrub_secrets, maybe_load_dotenv
from argus.providers.adapter import OpenAICompatibleAdapter, ProviderError
from argus.providers.mock_server import MockOpenAIServer, create_mock_transport
import httpx


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch: pytest.MonkeyPatch):
    """Ensure provider tests use an in-memory keyring vault and never mutate the host OS credential manager."""
    vault = {}

    def fake_get_password(service: str, username: str):
        return vault.get((service, username))

    def fake_set_password(service: str, username: str, password: str):
        vault[(service, username)] = password

    def fake_delete_password(service: str, username: str):
        vault.pop((service, username), None)

    try:
        import keyring
        monkeypatch.setattr(keyring, "get_password", fake_get_password)
        monkeypatch.setattr(keyring, "set_password", fake_set_password)
        monkeypatch.setattr(keyring, "delete_password", fake_delete_password)
    except Exception:
        pass


def test_provider_presets():
    """Verify built-in presets exist and have sensible defaults."""
    assert "groq" not in PROVIDER_PRESETS
    assert "gemini" in PROVIDER_PRESETS
    assert "antigravity" in PROVIDER_PRESETS
    assert "openai" in PROVIDER_PRESETS
    assert "openrouter" in PROVIDER_PRESETS
    assert "ollama" in PROVIDER_PRESETS
    assert "custom" in PROVIDER_PRESETS

    gemini = PROVIDER_PRESETS["gemini"]
    assert "generativelanguage" in gemini.default_base_url
    assert gemini.requires_key is True

    antigravity = PROVIDER_PRESETS["antigravity"]
    assert "antigravity" in antigravity.key
    assert antigravity.requires_key is False

    ollama = PROVIDER_PRESETS["ollama"]
    assert "localhost" in ollama.default_base_url
    assert ollama.requires_key is False


def test_provider_storage_isolation():
    """Verify ProviderStorage writes configuration outside repo and handles keys."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        storage = ProviderStorage(config_dir=tmp_path)

        # 1. Save provider config with API key
        cfg = ProviderConfig(
            name="test-provider",
            base_url="https://api.test.com/v1",
            model="test-model",
            api_key="sk-secret-12345678",
            temperature=0.2,
            max_tokens=2048,
        )
        storage.save_provider(cfg)

        # 2. Key is scrubbed from providers.json
        providers_json = (tmp_path / "providers.json").read_text()
        assert "sk-secret-12345678" not in providers_json
        assert "test-provider" in providers_json

        # 3. Key is retrievable via get_api_key
        retrieved_key = storage.get_api_key("test-provider")
        assert retrieved_key == "sk-secret-12345678"

        # 4. Load provider
        loaded = storage.get_provider("test-provider")
        assert loaded is not None
        assert loaded.model == "test-model"
        assert loaded.api_key == "sk-secret-12345678"
        assert loaded.masked_key == "sk-s...5678"

        # 5. Set active provider
        storage.set_active_provider("test-provider")
        active = storage.get_active_provider()
        assert active.name == "test-provider"

        # 6. Delete provider
        storage.delete_provider("test-provider")
        assert storage.get_provider("test-provider") is None


def test_secret_scrubbing():
    """Verify scrub_secrets redacts API keys from arbitrary text."""
    secret = "sk-super-secret-key-9999"
    msg = f"Failed to connect using key {secret} at endpoint"
    scrubbed = scrub_secrets(msg, [secret])
    assert secret not in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed


def test_mock_server_chat_completion():
    """Verify OpenAICompatibleAdapter connects to MockOpenAIServer over localhost."""
    with MockOpenAIServer() as server:
        expected_patch = (
            "<<<<<<< SEARCH\n"
            "def foo(): return False\n"
            "=======\n"
            "def foo(): return True\n"
            ">>>>>>> REPLACE"
        )
        server.canned_response = expected_patch

        config = ProviderConfig(
            name="mock",
            base_url=server.base_url,
            model="mock-model",
            api_key="mock-key",
        )
        adapter = OpenAICompatibleAdapter(config)

        # Test connection
        conn_test = adapter.test_connection()
        assert conn_test.success is True
        assert conn_test.status_code == 200
        assert conn_test.model == "mock-model"

        # Test chat completion
        resp = adapter.chat_completion(messages=[{"role": "user", "content": "Fix code"}])
        assert resp.content == expected_patch
        assert resp.prompt_tokens > 0
        assert resp.completion_tokens > 0

        # Assert server captured request
        assert len(server.requests) == 2
        last_req = server.requests[-1]
        assert "Authorization" in last_req["headers"]
        assert last_req["headers"]["Authorization"] == "Bearer mock-key"


def test_adapter_error_handling_and_scrubbing():
    """Verify adapter wraps HTTP errors with scrubbed keys."""
    with MockOpenAIServer() as server:
        secret_key = "sk-private-auth-token-42"
        server.simulated_error = (401, f"Invalid token {secret_key}")

        config = ProviderConfig(
            name="failing-mock",
            base_url=server.base_url,
            model="mock-model",
            api_key=secret_key,
        )
        adapter = OpenAICompatibleAdapter(config)

        with pytest.raises(ProviderError) as exc_info:
            adapter.chat_completion(messages=[{"role": "user", "content": "test"}])

        err_msg = str(exc_info.value)
        assert exc_info.value.status_code == 401
        assert "Authentication failed" in err_msg
        assert secret_key not in err_msg


def test_fresh_installation_no_provider_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify fresh installation with empty config has no active provider and returns empty provider list."""
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ARGUS_API_KEY", raising=False)

    storage = ProviderStorage(config_dir=tmp_path)
    assert storage.get_active_provider() is None
    assert storage.list_providers() == []

    # ArgusConfig validates that no provider is configured
    cfg = ArgusConfig()
    issues = cfg.validate()
    assert any("No active LLM provider configured" in i for i in issues)


def test_separate_credentials_and_models_across_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify separate credentials, models, and endpoints across distinct providers."""
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    storage = ProviderStorage(config_dir=tmp_path)

    p1 = ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
        api_key="sk-openai-distinct-secret-111",
    )
    p2 = ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="sk-deepseek-distinct-secret-222",
    )

    storage.save_provider(p1)
    storage.save_provider(p2)

    loaded_p1 = storage.get_provider("openai")
    loaded_p2 = storage.get_provider("deepseek")

    assert loaded_p1 is not None and loaded_p2 is not None
    assert loaded_p1.model == "gpt-4o"
    assert loaded_p1.api_key == "sk-openai-distinct-secret-111"
    assert loaded_p1.masked_key == "sk-o...-111"

    assert loaded_p2.model == "deepseek-chat"
    assert loaded_p2.api_key == "sk-deepseek-distinct-secret-222"
    assert loaded_p2.masked_key == "sk-d...-222"

    # Verify no secret cross-contamination in stored files
    providers_json = (tmp_path / "providers.json").read_text(encoding="utf-8")
    assert "sk-openai-distinct-secret-111" not in providers_json
    assert "sk-deepseek-distinct-secret-222" not in providers_json


def test_provider_switching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify switching active provider persists and updates active configuration."""
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    storage = ProviderStorage(config_dir=tmp_path)

    p1 = ProviderConfig(name="provider-alpha", base_url="http://alpha:8000/v1", model="model-a")
    p2 = ProviderConfig(name="provider-beta", base_url="http://beta:8000/v1", model="model-b")
    storage.save_provider(p1)
    storage.save_provider(p2)

    # First saved provider defaults as active
    assert storage.get_active_provider().name == "provider-alpha"

    # Switch to beta
    assert storage.set_active_provider("provider-beta") is True
    assert storage.get_active_provider().name == "provider-beta"
    assert storage.get_active_provider().model == "model-b"

    # Switch back to alpha
    assert storage.set_active_provider("provider-alpha") is True
    assert storage.get_active_provider().name == "provider-alpha"


def test_missing_key_error_handling():
    """Verify immediate ProviderError when provider requiring an API key lacks one."""
    cfg = ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key=None,
    )
    adapter = OpenAICompatibleAdapter(cfg)

    with pytest.raises(ProviderError) as exc_info:
        adapter.chat_completion(messages=[{"role": "user", "content": "hello"}])

    assert "Missing API key for provider 'openai'" in str(exc_info.value)

    # test_connection also captures the missing key error gracefully
    res = adapter.test_connection()
    assert res.success is False
    assert "Missing API key" in res.message


def test_offline_by_default_and_dotenv_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify workspace .env files are NOT loaded by default, and opt-in works via ARGUS_LOAD_DOTENV=1."""
    env_file = tmp_path / ".env"
    env_file.write_text("ARGUS_TEST_UNLOADED_SECRET=injected_val_1234\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARGUS_LOAD_DOTENV", raising=False)
    monkeypatch.delenv("ARGUS_DOTENV_PATH", raising=False)
    monkeypatch.delenv("ARGUS_TEST_UNLOADED_SECRET", raising=False)

    # Default: must NOT load .env
    maybe_load_dotenv()
    assert "ARGUS_TEST_UNLOADED_SECRET" not in os.environ

    # Opt-in via ARGUS_LOAD_DOTENV=1
    monkeypatch.setenv("ARGUS_LOAD_DOTENV", "1")
    maybe_load_dotenv()
    assert os.environ.get("ARGUS_TEST_UNLOADED_SECRET") == "injected_val_1234"
