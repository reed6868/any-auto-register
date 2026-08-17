from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import time
from typing import Any, Callable

import requests

from core.registration import ChallengeRequest, ChallengeResponse


@dataclass(slots=True)
class CpaSyncResult:
    attempted: bool
    supported: bool
    ok: bool
    message: str


def _config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _management_base(api_url: str) -> str:
    base = str(api_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v0/management"):
        return base
    return f"{base}/v0/management"


def _message_from_response(response, fallback: str) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or fallback)
    except Exception:
        pass
    text = str(getattr(response, "text", "") or "").strip()
    return text[:240] or fallback


def _chatgpt_proxy(account):
    extra = dict(getattr(account, "extra", {}) or {})
    return SimpleNamespace(
        email=str(getattr(account, "email", "") or ""),
        access_token=str(extra.get("access_token") or getattr(account, "token", "") or ""),
        refresh_token=str(extra.get("refresh_token") or ""),
        id_token=str(extra.get("id_token") or ""),
        session_token=str(extra.get("session_token") or ""),
        user_id=str(getattr(account, "user_id", "") or ""),
        account_id=str(getattr(account, "user_id", "") or ""),
        cookies=str(extra.get("cookies") or ""),
    )


def _build_challenge_callback(task_id: str, timeout: float):
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    from core.task_challenges import build_task_challenge_callback

    return build_task_challenge_callback(task_id, timeout=max(timeout, 1))


def sync_account_to_cpa(
    account,
    *,
    task_id: str = "",
    log_fn: Callable[[str], None] | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    http_client: Any = None,
    challenge_callback: Callable[[ChallengeRequest], ChallengeResponse] | None = None,
    poll_interval: float = 3,
    poll_timeout: float = 180,
) -> CpaSyncResult:
    """Best-effort CPA synchronization after the account has been persisted.

    ChatGPT keeps the existing auth-file upload path. Other platforms use CPA's
    provider OAuth management endpoint. This deliberately does not convert a web
    cookie into an unrelated CPA credential format.
    """

    log = log_fn or (lambda message: None)
    resolved_url = str(api_url if api_url is not None else _config_value("cpa_api_url")).strip()
    resolved_key = str(api_key if api_key is not None else _config_value("cpa_api_key")).strip()
    if not resolved_url:
        return CpaSyncResult(False, True, False, "CPA API URL 未配置")

    platform = str(getattr(account, "platform", "") or "").strip().lower()
    if platform == "chatgpt":
        from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

        token_data = generate_token_json(_chatgpt_proxy(account))
        ok, message = upload_to_cpa(token_data, api_url=resolved_url, api_key=resolved_key)
        return CpaSyncResult(True, True, bool(ok), str(message or ""))

    if not platform:
        return CpaSyncResult(False, False, False, "账号缺少 platform，无法同步 CPA")

    client = http_client or requests
    management_base = _management_base(resolved_url)
    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "X-Management-Key": resolved_key,
        "Accept": "application/json",
    }
    start_url = f"{management_base}/{platform}-auth-url"
    try:
        response = client.get(start_url, headers=headers, timeout=20, verify=False)
    except Exception as exc:
        return CpaSyncResult(True, True, False, f"CPA {platform} OAuth 启动失败: {exc}")

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code == 404:
        return CpaSyncResult(
            True,
            False,
            False,
            f"当前 CPA 未提供 {platform} OAuth provider；如安装对应 CPA plugin 后可自动同步",
        )
    if status_code < 200 or status_code >= 300:
        return CpaSyncResult(
            True,
            True,
            False,
            _message_from_response(response, f"CPA {platform} OAuth 启动失败: HTTP {status_code}"),
        )

    try:
        payload = response.json()
    except Exception:
        payload = {}
    auth_url = str(payload.get("url") or "").strip() if isinstance(payload, dict) else ""
    state = str(payload.get("state") or "").strip() if isinstance(payload, dict) else ""
    if not auth_url or not state:
        return CpaSyncResult(True, True, False, f"CPA {platform} OAuth 响应缺少 url/state")

    log(f"  [CPA] {platform} OAuth 授权地址: {auth_url}")
    callback = challenge_callback or _build_challenge_callback(task_id, max(poll_timeout, 60))
    if callback:
        response_value = callback(
            ChallengeRequest(
                kind="cpa_oauth",
                message=f"账号已保存。请完成 CPA 的 {platform} OAuth 授权，然后返回面板确认继续",
                url=auth_url,
                metadata={"platform": platform, "provider": platform, "target": "cpa"},
            )
        )
        if not response_value.completed:
            return CpaSyncResult(True, True, False, f"CPA {platform} OAuth 未完成")
    else:
        log(f"  [CPA] 当前不是任务上下文，无法等待人工 OAuth；请手动打开上面的授权地址")

    deadline = time.monotonic() + max(float(poll_timeout or 0), 1)
    status_url = f"{management_base}/get-auth-status"
    while time.monotonic() < deadline:
        try:
            status_response = client.get(
                status_url,
                headers=headers,
                params={"state": state},
                timeout=20,
                verify=False,
            )
        except Exception as exc:
            return CpaSyncResult(True, True, False, f"CPA {platform} OAuth 状态查询失败: {exc}")

        code = int(getattr(status_response, "status_code", 0) or 0)
        if code < 200 or code >= 300:
            return CpaSyncResult(
                True,
                True,
                False,
                _message_from_response(status_response, f"CPA OAuth 状态查询失败: HTTP {code}"),
            )
        try:
            status_payload = status_response.json()
        except Exception:
            status_payload = {}
        auth_status = str(status_payload.get("status") or "").strip().lower() if isinstance(status_payload, dict) else ""
        if auth_status == "ok":
            return CpaSyncResult(True, True, True, f"{platform} OAuth 已同步到 CPA")
        if auth_status == "error":
            detail = str(status_payload.get("error") or "unknown error") if isinstance(status_payload, dict) else "unknown error"
            return CpaSyncResult(True, True, False, f"CPA {platform} OAuth 失败: {detail}")
        if poll_interval > 0:
            time.sleep(float(poll_interval))

    return CpaSyncResult(True, True, False, f"CPA {platform} OAuth 等待超时")
