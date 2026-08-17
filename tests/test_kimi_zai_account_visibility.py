from __future__ import annotations

from core.base_platform import Account
from core.db import save_account


def test_kimi_and_zai_accounts_are_visible_through_generic_account_api(client):
    save_account(
        Account(
            platform="kimi",
            email="kimi-user@example.com",
            password="",
            extra={"cookies": "kimi_session=1", "storage_state": "{}"},
        )
    )
    save_account(
        Account(
            platform="zai",
            email="zai-user@example.com",
            password="TestPass123!",
            extra={"cookies": "zai_session=1", "storage_state": "{}"},
        )
    )

    kimi = client.get("/api/accounts", params={"platform": "kimi"})
    assert kimi.status_code == 200
    assert kimi.json()["total"] == 1
    assert kimi.json()["items"][0]["platform"] == "kimi"
    assert kimi.json()["items"][0]["email"] == "kimi-user@example.com"

    zai = client.get("/api/accounts", params={"platform": "zai"})
    assert zai.status_code == 200
    assert zai.json()["total"] == 1
    assert zai.json()["items"][0]["platform"] == "zai"
    assert zai.json()["items"][0]["email"] == "zai-user@example.com"
