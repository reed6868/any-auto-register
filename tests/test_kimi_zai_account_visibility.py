from __future__ import annotations

from sqlmodel import Session, select

from application.tasks import TASK_STATUS_SUCCEEDED, create_account_check_task, execute_task, get_task
from core.base_platform import Account
from core.db import AccountModel, engine, save_account


def _save_browser_account(
    platform: str,
    email: str,
    password: str,
    cookie: str = "",
    storage_state: str = "{}",
) -> int:
    save_account(
        Account(
            platform=platform,
            email=email,
            password=password,
            extra={"cookies": cookie, "storage_state": storage_state},
        )
    )
    with Session(engine) as session:
        model = session.exec(
            select(AccountModel).where(
                AccountModel.platform == platform,
                AccountModel.email == email,
            )
        ).first()
        assert model is not None
        assert model.id is not None
        return int(model.id)


def test_kimi_and_zai_accounts_are_visible_through_generic_account_api(client):
    _save_browser_account("kimi", "kimi-user@example.com", "", "kimi_session=1")
    _save_browser_account("zai", "zai-user@example.com", "TestPass123!", "zai_session=1")

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


def test_kimi_and_zai_saved_accounts_complete_existing_account_check_tasks():
    saved_ids = [
        _save_browser_account("kimi", "kimi-check@example.com", "", "kimi_session=1"),
        _save_browser_account("zai", "zai-check@example.com", "TestPass123!", "zai_session=1"),
    ]

    for account_id in saved_ids:
        task = create_account_check_task(account_id)
        execute_task(task["task_id"])
        result = get_task(task["task_id"])
        assert result is not None
        assert result["status"] == TASK_STATUS_SUCCEEDED
        assert result["result"]["data"]["valid"] is True


def test_browser_storage_only_session_remains_valid_after_account_graph_round_trip():
    saved_ids = [
        _save_browser_account("kimi", "kimi-storage@example.com", "", storage_state='{"localStorage":{"session":"kimi"}}'),
        _save_browser_account("zai", "zai-storage@example.com", "secret", storage_state='{"localStorage":{"session":"zai"}}'),
    ]

    for account_id in saved_ids:
        task = create_account_check_task(account_id)
        execute_task(task["task_id"])
        result = get_task(task["task_id"])
        assert result is not None
        assert result["status"] == TASK_STATUS_SUCCEEDED
        assert result["result"]["data"]["valid"] is True
