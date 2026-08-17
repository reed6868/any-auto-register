from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import re
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests


CPA_SYNC_PLATFORMS = {"chatgpt", "kimi", "zai"}


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


def _cookie_header_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in str(value or "").split(";"):
        name, sep, raw = part.strip().partition("=")
        if sep and name:
            pairs.append((name.strip(), raw.strip()))
    return pairs


def _proxy_config(value: str) -> dict | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": raw}
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _auto_authorize_kimi_device(
    account,
    auth_url: str,
    *,
    timeout: float = 30,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Approve the normal Kimi device grant with the registered web session.

    This does not bypass Kimi security checks. It only reuses the session cookie
    produced by the just-completed Kimi registration and clicks an explicit
    positive authorization control. If Kimi requests a fresh login, QR, 2FA or
    another security step, unattended authorization fails immediately.
    """

    extra = dict(getattr(account, "extra", {}) or {})
    cookie_pairs = _cookie_header_pairs(extra.get("cookies") or "")
    if not cookie_pairs:
        return False, "Kimi 账号没有可复用的 Web session cookie"

    log = log_fn or (lambda message: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return False, f"Playwright 不可用: {exc}"

    proxy_cfg = _proxy_config(extra.get("registration_proxy") or "")
    browser = None
    context = None
    try:
        with sync_playwright() as pw:
            launch_kwargs: dict[str, Any] = {"headless": True}
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context()
            context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".kimi.com",
                        "path": "/",
                        "secure": True,
                    }
                    for name, value in cookie_pairs
                ]
            )
            page = context.new_page()
            page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + max(float(timeout or 0), 1)
            positive = re.compile(r"authorize|allow|confirm|approve|同意|允许|授权|确认", re.I)
            success_markers = (
                "authorization successful",
                "successfully authorized",
                "device authorized",
                "you may close",
                "authorization complete",
                "授权成功",
                "已授权",
            )
            login_markers = (
                "log in with phone number",
                "continue with google",
                "verification code",
                "scan qr",
                "two-factor",
                "2fa",
                "security verification",
            )
            clicked = False

            while time.monotonic() < deadline:
                try:
                    text = str(page.locator("body").inner_text(timeout=1000) or "")
                except Exception:
                    text = ""
                lowered = " ".join(text.lower().split())
                if any(marker in lowered for marker in success_markers):
                    return True, "Kimi CPA device authorization 已由现有 session 自动确认"
                if any(marker in lowered for marker in login_markers):
                    return False, "Kimi CPA 授权页要求重新登录/安全验证，不能无人值守确认"

                try:
                    button = page.get_by_role("button", name=positive).first
                    if button.is_visible(timeout=300):
                        button.click()
                        clicked = True
                        log("  [CPA] 已使用 Kimi Web session 自动确认 device authorization")
                        time.sleep(1)
                        continue
                except Exception:
                    pass

                try:
                    link = page.get_by_role("link", name=positive).first
                    if link.is_visible(timeout=300):
                        link.click()
                        clicked = True
                        log("  [CPA] 已使用 Kimi Web session 自动确认 device authorization")
                        time.sleep(1)
                        continue
                except Exception:
                    pass

                # Some device pages close or redirect after a successful click.
                if clicked and (page.is_closed() or "auth.kimi.com" not in str(page.url or "")):
                    return True, "Kimi CPA device authorization 已提交"
                time.sleep(0.5)

            if clicked:
                return True, "Kimi CPA device authorization 已提交，等待 CPA token polling 确认"
            return False, "Kimi CPA 授权页未找到可自动确认的标准授权按钮"
    except Exception as exc:
        return False, f"Kimi CPA 自动 device authorization 失败: {exc}"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def sync_account_to_cpa(
    account,
    *,
    task_id: str = "",
    log_fn: Callable[[str], None] | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    http_client: Any = None,
    device_authorizer: Callable[..., Any] | None = None,
    poll_interval: float = 3,
    poll_timeout: float = 180,
) -> CpaSyncResult:
    """Best-effort, non-interactive CPA synchronization after persistence."""

    platform = str(getattr(account, "platform", "") or "").strip().lower()
    if platform not in CPA_SYNC_PLATFORMS:
        return CpaSyncResult(False, False, False, f"{platform or 'unknown'} 未启用 CPA 自动同步")

    log = log_fn or (lambda message: None)
    resolved_url = str(api_url if api_url is not None else _config_value("cpa_api_url")).strip()
    resolved_key = str(api_key if api_key is not None else _config_value("cpa_api_key")).strip()
    if not resolved_url:
        return CpaSyncResult(False, True, False, "CPA API URL 未配置")

    if platform == "chatgpt":
        from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

        token_data = generate_token_json(_chatgpt_proxy(account))
        ok, message = upload_to_cpa(token_data, api_url=resolved_url, api_key=resolved_key)
        return CpaSyncResult(True, True, bool(ok), str(message or ""))

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
            f"当前 CPA 不支持 {platform} OAuth provider；如安装对应 CPA plugin 后可自动同步",
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

    if platform == "kimi":
        authorizer = device_authorizer or _auto_authorize_kimi_device
        try:
            auth_result = authorizer(account, auth_url, timeout=min(max(poll_timeout, 1), 60), log_fn=log)
        except TypeError:
            auth_result = authorizer(account, auth_url)
        if isinstance(auth_result, tuple):
            authorized, auth_message = bool(auth_result[0]), str(auth_result[1] or "")
        else:
            authorized, auth_message = bool(auth_result), ""
        if not authorized:
            return CpaSyncResult(
                True,
                True,
                False,
                auth_message or "Kimi CPA device authorization 无法无人值守完成",
            )
    elif platform == "zai":
        # Stock CLIProxyAPI currently has no built-in Z.AI provider. If a plugin
        # adds a route it must also supply a non-interactive authorizer contract;
        # never turn that into a hidden manual wait here.
        if device_authorizer is None:
            return CpaSyncResult(
                True,
                True,
                False,
                "CPA Z.AI provider 路由存在，但 any-auto-register 未获得其无人值守授权契约",
            )
        try:
            auth_result = device_authorizer(account, auth_url, timeout=min(max(poll_timeout, 1), 60), log_fn=log)
        except TypeError:
            auth_result = device_authorizer(account, auth_url)
        if isinstance(auth_result, tuple):
            authorized, auth_message = bool(auth_result[0]), str(auth_result[1] or "")
        else:
            authorized, auth_message = bool(auth_result), ""
        if not authorized:
            return CpaSyncResult(True, True, False, auth_message or "Z.AI CPA 授权无法无人值守完成")

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
