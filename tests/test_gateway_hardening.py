"""Gateway hardening: no test-mode auth bypass, no credential-derived run ids."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from argus.gateway import server as gw_server
from argus.gateway.config import GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import app, init_gateway


def _cfg(key):
    return GatewayConfig(
        gateway_api_key=key,
        providers=[ProviderConfig(name="mock", base_url="https://api.mock.com/v1", api_key="sk-mock")],
        tiers={t: TierDefinition(provider="mock", model="m") for t in ("cheap", "strong", "frontier")},
    )


@pytest.mark.real_gateway_auth
def test_env_flags_do_not_disable_gateway_auth(monkeypatch):
    """ARGUS_TEST_MODE / PYTEST_CURRENT_TEST must not turn a keyless gateway into an open one."""
    monkeypatch.setenv("ARGUS_TEST_MODE", "1")
    monkeypatch.delenv("ARGUS_GATEWAY_API_KEY", raising=False)
    init_gateway(config=_cfg(None), workspace=".")
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.post("/v1/chat/completions", json={"model": "argus/auto", "messages": []})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "gateway_unauthenticated"


@pytest.mark.real_gateway_auth
def test_testserver_host_not_allowed_by_env(monkeypatch):
    monkeypatch.setenv("ARGUS_TEST_MODE", "1")
    init_gateway(config=_cfg("k"), workspace=".")
    resp = TestClient(app).get("/health")  # default Host is "testserver"
    assert resp.status_code == 400


class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_run_id_never_derived_from_api_key():
    secret = "sk-ant-super-secret-value"
    rid = gw_server.resolve_run_id(_Req({"x-api-key": secret}), "anthropic_run")
    assert rid == "anthropic_run"
    assert secret not in rid


@pytest.mark.parametrize("bad", ["", "../../etc", "a b", "x" * 200, "id\nInjected: 1", "sk-ant‮"])
def test_malformed_run_ids_fall_back_to_default(bad):
    assert gw_server.resolve_run_id(_Req({"x-argus-run-id": bad}), "default_run") == "default_run"


def test_well_formed_run_id_is_accepted():
    assert gw_server.resolve_run_id(_Req({"x-argus-run-id": "task-42.a:b"}), "d") == "task-42.a:b"
