"""Global test configuration and fixtures."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def run_in_scratch_cwd(request, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Run every test with a throwaway working directory.

    Several code paths default to ``Path.cwd()`` (``.argus/`` run artifacts, session and
    telemetry files, policy lookup). Without this fixture they were written into the repository
    checkout and leaked state between tests. A test that needs a particular directory calls
    ``monkeypatch.chdir`` itself; tests that need the repository root use ``REPO_ROOT``.
    """
    if request.node.get_closest_marker("keep_cwd"):
        yield
        return
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    yield


@pytest.fixture(autouse=True)
def offline_tokenizer(request, monkeypatch: pytest.MonkeyPatch):
    """Budget estimates must not depend on the network: use the byte-based heuristic.

    tiktoken fetches its vocabulary on first use, which made estimates differ between online
    and offline machines. Tests that really need the vocabulary are marked
    ``tokenizer_download`` and are excluded from the offline CI selector.
    """
    if request.node.get_closest_marker("tokenizer_download"):
        yield
        return
    monkeypatch.setenv("ARGUS_OFFLINE", "1")
    yield


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
    config.addinivalue_line("markers", "keep_cwd: do not chdir into a scratch directory for this test")
    config.addinivalue_line("markers", "real_gateway_auth: run with the gateway's real fail-closed auth and Host guard")
