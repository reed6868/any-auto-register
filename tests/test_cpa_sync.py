from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import Account


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _account(platform: str, email: str) -> Account:
    return Account(platform=platform, email=email, password="", extra={"cookies": "session=1"})


def test_kimi_cpa_sync_uses_native_device_flow_without_human_wait():
    from core.cpa_sync import sync_account_to_cpa

    http = FakeHttp(
        [
            FakeResponse(payload={"url": "https://auth.kimi.com/device?code=abc", "state": "state-1", "flow": "device"}),
            FakeResponse(payload={"status": "wait"}),
            FakeResponse(payload={"status": "ok"}),
        ]
    )
    authorizations = []

    def authorize(account, url, **kwargs):
        authorizations.append((account.platform, url, kwargs))
        return True, "authorized"

    result = sync_account_to_cpa(
        _account("kimi", "kimi@example.com"),
        api_url="http://127.0.0.1:8317",
        api_key="secret",
        http_client=http,
        device_authorizer=authorize,
        poll_interval=0,
        poll_timeout=2,
    )

    assert result.attempted is True
    assert result.supported is True
    assert result.ok is True
    assert http.calls[0][0] == "http://127.0.0.1:8317/v0/management/kimi-auth-url"
    assert http.calls[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert http.calls[1][0] == "http://127.0.0.1:8317/v0/management/get-auth-status"
    assert http.calls[1][1]["params"] == {"state": "state-1"}
    assert authorizations[0][0] == "kimi"
    assert authorizations[0][1].startswith("https://auth.kimi.com/")


def test_kimi_cpa_sync_fails_fast_when_device_grant_cannot_be_auto_authorized():
    from core.cpa_sync import sync_account_to_cpa

    http = FakeHttp([FakeResponse(payload={"url": "https://auth.kimi.com/device?code=abc", "state": "state-1"})])
    result = sync_account_to_cpa(
        _account("kimi", "kimi@example.com"),
        api_url="http://127.0.0.1:8317",
        api_key="secret",
        http_client=http,
        device_authorizer=lambda account, url, **kwargs: (False, "需要重新登录"),
        poll_interval=0,
        poll_timeout=2,
    )

    assert result.attempted is True
    assert result.ok is False
    assert "重新登录" in result.message
    assert len(http.calls) == 1


def test_zai_cpa_sync_is_best_effort_when_provider_route_is_not_supported():
    from core.cpa_sync import sync_account_to_cpa

    http = FakeHttp([FakeResponse(status_code=404, payload={"message": "not found"})])
    result = sync_account_to_cpa(
        _account("zai", "zai@example.com"),
        api_url="http://127.0.0.1:8317/v0/management",
        api_key="secret",
        http_client=http,
    )

    assert result.attempted is True
    assert result.supported is False
    assert result.ok is False
    assert "不支持" in result.message
    assert http.calls[0][0] == "http://127.0.0.1:8317/v0/management/zai-auth-url"


def test_zai_cpa_plugin_route_requires_noninteractive_authorizer():
    from core.cpa_sync import sync_account_to_cpa

    http = FakeHttp([FakeResponse(payload={"url": "https://plugin.example/authorize", "state": "state-z"})])
    result = sync_account_to_cpa(
        _account("zai", "zai@example.com"),
        api_url="http://127.0.0.1:8317",
        api_key="secret",
        http_client=http,
    )

    assert result.attempted is True
    assert result.supported is True
    assert result.ok is False
    assert "无人值守" in result.message
    assert len(http.calls) == 1


def test_unrelated_platform_preserves_previous_no_cpa_behavior():
    from core.cpa_sync import sync_account_to_cpa

    http = FakeHttp([])
    result = sync_account_to_cpa(
        _account("cursor", "cursor@example.com"),
        api_url="http://127.0.0.1:8317",
        api_key="secret",
        http_client=http,
    )

    assert result.attempted is False
    assert http.calls == []


def test_chatgpt_cpa_sync_preserves_existing_auth_file_upload(monkeypatch):
    from core.cpa_sync import sync_account_to_cpa
    from platforms.chatgpt import cpa_upload

    captured = {}

    monkeypatch.setattr(cpa_upload, "generate_token_json", lambda account: {"email": account.email, "account_id": "acct-1"})

    def fake_upload(token_data, api_url=None, api_key=None):
        captured.update({"token_data": token_data, "api_url": api_url, "api_key": api_key})
        return True, "上传成功"

    monkeypatch.setattr(cpa_upload, "upload_to_cpa", fake_upload)
    result = sync_account_to_cpa(
        _account("chatgpt", "gpt@example.com"),
        api_url="http://127.0.0.1:8317",
        api_key="secret",
    )

    assert result.ok is True
    assert captured["token_data"]["account_id"] == "acct-1"
    assert captured["api_url"] == "http://127.0.0.1:8317"
    assert captured["api_key"] == "secret"


def test_task_auto_upload_cpa_no_longer_filters_kimi(monkeypatch):
    import core.cpa_sync as cpa_sync
    from application.tasks import TaskLogger, _auto_upload_cpa

    calls = []

    monkeypatch.setattr(
        cpa_sync,
        "sync_account_to_cpa",
        lambda account, **kwargs: calls.append((account.platform, kwargs)) or SimpleNamespace(
            attempted=True,
            supported=True,
            ok=True,
            message="同步成功",
        ),
    )
    logger = TaskLogger("task-test")
    monkeypatch.setattr(logger, "log", lambda *args, **kwargs: None)

    _auto_upload_cpa(logger, _account("kimi", "kimi@example.com"))

    assert calls
    assert calls[0][0] == "kimi"
    assert calls[0][1]["task_id"] == "task-test"
