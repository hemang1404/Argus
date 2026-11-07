"""Secure, persistent storage for provider configurations and API credentials."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Dict, List, Optional

from argus.providers.models import ProviderConfig, PROVIDER_PRESETS

SERVICE_NAME = "argus_ai_inference"


def get_config_dir() -> Path:
    """Return the user-level configuration directory outside the repository."""
    override = os.getenv("ARGUS_CONFIG_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if appdata:
            path = Path(appdata) / "Argus"
        else:
            path = Path.home() / ".argus"
    else:
        xdg_config = os.getenv("XDG_CONFIG_HOME")
        if xdg_config:
            path = Path(xdg_config) / "argus"
        else:
            path = Path.home() / ".config" / "argus"

    path.mkdir(parents=True, exist_ok=True)
    return path


def maybe_load_dotenv(force: bool = False) -> None:
    """Conditionally load .env if explicitly requested via environment or parameter.
    
    Security note:
    Argus does NOT automatically load arbitrary workspace .env files by default.
    To enable workspace .env loading, set ARGUS_LOAD_DOTENV=1 or ARGUS_DOTENV_PATH=/path/to/.env.
    """
    should_load = force or os.getenv("ARGUS_LOAD_DOTENV", "").strip().lower() in ("1", "true", "yes")
    custom_path = os.getenv("ARGUS_DOTENV_PATH")
    if should_load or custom_path:
        try:
            from dotenv import load_dotenv, find_dotenv
            if custom_path and Path(custom_path).is_file():
                load_dotenv(custom_path)
            else:
                found = find_dotenv(usecwd=True)
                if found:
                    load_dotenv(found)
                else:
                    load_dotenv()
        except Exception:
            pass


class ProviderStorage:
    """Manages persistent provider records and OS-keyring backed secrets."""

    def __init__(self, config_dir: Optional[Path] = None, load_dotenv: bool = False) -> None:
        self.config_dir = config_dir or get_config_dir()
        self.config_file = self.config_dir / "providers.json"
        self.fallback_secrets_file = self.config_dir / "secrets.json"
        if load_dotenv:
            maybe_load_dotenv(force=True)

    # -------------------------------------------------------------------------
    # Secret Management (Keyring with Fallback)
    # -------------------------------------------------------------------------

    def get_api_key(self, provider_name: str) -> Optional[str]:
        """Retrieve API key for a provider.
        
        Resolution order:
        1. OS Keyring (service: 'argus_ai_inference')
        2. Local user fallback file (secrets.json with 0600 permissions)
        3. Explicitly enabled .env file (if ARGUS_LOAD_DOTENV=1 or ARGUS_DOTENV_PATH is set)
        4. Provider-specific system environment variables:
           - openai:     OPENAI_API_KEY
           - openrouter: OPENROUTER_API_KEY
           - deepseek:   DEEPSEEK_API_KEY
           - custom:     ARGUS_API_KEY
           - fallback:   ARGUS_API_KEY
        """
        maybe_load_dotenv()

        # 1. Check OS Keyring
        try:
            import keyring
            key = keyring.get_password(SERVICE_NAME, provider_name)
            if key:
                return key.strip()
        except Exception:
            pass

        # 2. Check local fallback secrets file
        if self.fallback_secrets_file.is_file():
            try:
                data = json.loads(self.fallback_secrets_file.read_text(encoding="utf-8"))
                if provider_name in data and data[provider_name]:
                    return str(data[provider_name]).strip()
            except Exception:
                pass

        # 3. Check well-known provider-specific environment variables
        env_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "google": "GOOGLE_API_KEY",
            "custom": "ARGUS_API_KEY",
        }
        env_var = env_map.get(provider_name.lower())
        if env_var and os.getenv(env_var):
            return os.getenv(env_var, "").strip()

        # Generic ARGUS_API_KEY fallback
        if os.getenv("ARGUS_API_KEY"):
            return os.getenv("ARGUS_API_KEY", "").strip()

        return None

    def set_api_key(self, provider_name: str, api_key: str) -> bool:
        """Store API key using OS Keyring or fallback secrets file."""
        if provider_name.strip().lower() == "groq":
            raise ValueError("Groq is strictly prohibited as an inference provider in Argus.")
        saved_in_keyring = False
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, provider_name, api_key.strip())
            saved_in_keyring = True
        except Exception:
            saved_in_keyring = False

        # If keyring failed, write to isolated fallback file with restricted permissions
        if not saved_in_keyring:
            secrets: Dict[str, str] = {}
            if self.fallback_secrets_file.is_file():
                try:
                    secrets = json.loads(self.fallback_secrets_file.read_text(encoding="utf-8"))
                except Exception:
                    secrets = {}
            secrets[provider_name] = api_key.strip()
            self.fallback_secrets_file.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
            try:
                # Restrict permissions to user read/write only on POSIX
                os.chmod(self.fallback_secrets_file, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass

        return True

    def delete_api_key(self, provider_name: str) -> None:
        """Remove API key from Keyring and fallback file."""
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, provider_name)
        except Exception:
            pass

        if self.fallback_secrets_file.is_file():
            try:
                secrets = json.loads(self.fallback_secrets_file.read_text(encoding="utf-8"))
                if provider_name in secrets:
                    del secrets[provider_name]
                    self.fallback_secrets_file.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Provider Configuration Management
    # -------------------------------------------------------------------------

    def _read_data(self) -> Dict[str, Any]:
        if not self.config_file.is_file():
            return {"active_provider": None, "providers": {}}

        try:
            return json.loads(self.config_file.read_text(encoding="utf-8"))
        except Exception:
            return {"active_provider": None, "providers": {}}

    def _write_data(self, data: Dict[str, Any]) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_providers(self) -> List[ProviderConfig]:
        """List all configured providers with keys resolved and attached."""
        data = self._read_data()
        providers_data = data.get("providers", {})
        results: List[ProviderConfig] = []

        for name, p_data in providers_data.items():
            if name.lower() == "groq":
                continue
            if isinstance(p_data, dict):
                try:
                    cfg = ProviderConfig.from_dict(p_data)
                except ValueError:
                    continue
            elif name in PROVIDER_PRESETS:
                try:
                    cfg = ProviderConfig.from_preset(name)
                except ValueError:
                    continue
            else:
                continue
            cfg.api_key = self.get_api_key(cfg.name)
            results.append(cfg)

        return results

    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        """Get a specific provider configuration by name."""
        if not name:
            return None
        data = self._read_data()
        providers_data = data.get("providers", {})
        p_data = providers_data.get(name)

        if p_data and isinstance(p_data, dict):
            try:
                cfg = ProviderConfig.from_dict(p_data)
            except ValueError:
                return None
        elif name in PROVIDER_PRESETS:
            try:
                cfg = ProviderConfig.from_preset(name)
            except ValueError:
                return None
        else:
            return None

        cfg.api_key = self.get_api_key(cfg.name)
        return cfg

    def get_active_provider(self) -> Optional[ProviderConfig]:
        """Get the currently selected active provider, or None if none configured."""
        data = self._read_data()
        active_name = data.get("active_provider")
        if not active_name:
            active_name = os.getenv("ARGUS_PROVIDER")
        if not active_name:
            return None
        return self.get_provider(active_name)

    def set_active_provider(self, name: str) -> bool:
        """Set the active provider by name."""
        if not name:
            return False
        data = self._read_data()
        providers = data.get("providers", {})
        if name not in providers and name not in PROVIDER_PRESETS:
            return False

        data["active_provider"] = name
        self._write_data(data)
        return True

    def save_provider(self, config: ProviderConfig) -> None:
        """Save or update a provider configuration. Stores API key securely."""
        if config.name.strip().lower() == "groq":
            raise ValueError("Groq is strictly prohibited as an inference provider in Argus.")
        data = self._read_data()
        if "providers" not in data or not isinstance(data["providers"], dict):
            data["providers"] = {}

        # Save non-sensitive metadata in providers.json
        data["providers"][config.name] = config.to_dict(include_key=False)

        # If no active provider set yet, default active provider to this configured provider
        if not data.get("active_provider"):
            data["active_provider"] = config.name

        self._write_data(data)

        # Save key if provided
        if config.api_key:
            self.set_api_key(config.name, config.api_key)

    def delete_provider(self, name: str) -> bool:
        """Delete a configured provider and its stored key."""
        data = self._read_data()
        providers = data.get("providers", {})
        if name in providers:
            del providers[name]
            if data.get("active_provider") == name:
                data["active_provider"] = next(iter(providers.keys()), None)
            self._write_data(data)
            self.delete_api_key(name)
            return True
        return False


from argus.utils.redaction import scrub_secrets

