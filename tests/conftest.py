"""Global test configuration and fixtures."""

import pytest

# tests/test_calculator.py exercises the legacy research prototype in src/.
# src/calculator.py intentionally contains a demo bug (``return a - b``) that the
# recovery engine uses as a fixture; it is not a product test and must not be
# collected. Deleting the fixture would break the demonstration, so it is kept.
collect_ignore = ["test_calculator.py"]


@pytest.fixture(autouse=True)
def isolate_user_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Point ARGUS_CONFIG_DIR at a throwaway directory so no test touches the real user config
    (providers.json, secrets.json, the run-signing key). Tests that need their own value override it."""
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path_factory.mktemp("argus_cfg")))
    yield


@pytest.fixture(autouse=True)
def gateway_test_auth(request, monkeypatch: pytest.MonkeyPatch):
    """Inject test behaviour into the gateway from the outside.

    Production code has no test-mode switches. Tests that build a GatewayConfig without
    a key get authentication skipped (only when no key is configured) and the Starlette
    ``testserver`` Host accepted. Tests marked ``real_gateway_auth`` get neither.
    """
    if request.node.get_closest_marker("real_gateway_auth"):
        yield
        return
    try:
        from argus.gateway import server
    except Exception:
        yield
        return

    real_verify = server.verify_gateway_auth

    def verify(req):
        if server._config is not None and not server._config.get_gateway_key():
            return None
        return real_verify(req)

    monkeypatch.setattr(server, "verify_gateway_auth", verify)
    monkeypatch.setattr(server, "_EXTRA_ALLOWED_HOSTS", {"testserver"})
    yield


@pytest.fixture(autouse=True)
def isolate_keyring_globally(monkeypatch: pytest.MonkeyPatch):
    """Ensure all tests use an in-memory keyring vault and never mutate the host OS credential manager."""
    vault: dict[tuple[str, str], str] = {}

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

    yield vault



def pytest_configure(config):
    config.addinivalue_line("markers", "real_gateway_auth: run with the gateway's real fail-closed auth and Host guard")
