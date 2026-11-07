"""Global test configuration and fixtures."""

import pytest

# tests/test_calculator.py exercises the legacy research prototype in src/.
# src/calculator.py intentionally contains a demo bug (``return a - b``) that the
# recovery engine uses as a fixture; it is not a product test and must not be
# collected. Deleting the fixture would break the demonstration, so it is kept.
collect_ignore = ["test_calculator.py"]


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

