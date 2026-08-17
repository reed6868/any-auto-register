from __future__ import annotations

from enum import Enum
import json
import re
import time
from urllib.parse import urlparse

from core.oauth_browser import OAuthBrowser, finalize_oauth_email
from core.registration import ChallengeRequest, ChallengeResponse
from core.registration.browser_verification import BrowserVerificationSupport


KIMI_URL = "https://www.kimi.com/"
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


class AuthSurface(str, Enum):
    LANDING = "landing"
    SECURITY_CHALLENGE = "security_challenge"
    AUTHENTICATED = "authenticated"
    UNKNOWN = "unknown"


def detect_auth_surface(*, url: str, text: str) -> AuthSurface:
    normalized_text = " ".join(str(text or "").lower().split())
    normalized_url = str(url or "").lower()

    security_markers = (
        "security verification",
        "security check",
        "verify you are human",
        "two-factor",
        "2fa",
        "captcha",
        "turnstile",
    )
    if any(marker in normalized_text for marker in security_markers):
        return AuthSurface.SECURITY_CHALLENGE

    login_markers = (
        "log in to chat with kimi for free",
        "continue with google",
        "log in with phone number",
        "log in to sync chat history",
    )
    if any(marker in normalized_text for marker in login_markers):
        return AuthSurface.LANDING

    parsed = urlparse(normalized_url)
    authenticated_markers = ("new chat", "chats", "kimi claw", "ask anything")
    if parsed.netloc.endswith("kimi.com") and any(marker in normalized_text for marker in authenticated_markers):
        return AuthSurface.AUTHENTICATED
    return AuthSurface.UNKNOWN


def _page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2000)
    except Exception:
        return ""


def _click_text(page, labels: tuple[str, ...]) -> bool:
    for label in labels:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=re.compile(re.escape(label), re.I)).first
                if locator.is_visible(timeout=300):
                    locator.click()
                    return True
            except Exception:
                pass
        try:
            locator = page.get_by_text(label, exact=False).first
            if locator.is_visible(timeout=300):
                locator.click()
                return True
        except Exception:
            pass
    return False


def _storage_snapshot(page) -> dict:
    try:
        return page.evaluate(
            """
            () => ({
              localStorage: Object.fromEntries(Object.entries(localStorage)),
              sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),
            })
            """
        ) or {}
    except Exception:
        return {}


def _extract_email(snapshot: dict) -> str:
    try:
        text = json.dumps(snapshot, ensure_ascii=False)
    except Exception:
        return ""
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else ""


def _is_authenticated(browser: OAuthBrowser) -> bool:
    page = browser.active_page()
    return detect_auth_surface(url=str(page.url or ""), text=_page_text(page)) is AuthSurface.AUTHENTICATED


def _wait_authenticated(
    browser: OAuthBrowser,
    timeout: float,
    verification: BrowserVerificationSupport | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_authenticated(browser):
            if verification:
                verification.mark_authenticated()
            return True
        if verification:
            page = browser.active_page()
            if verification.try_handle(page):
                time.sleep(0.75)
                continue
        time.sleep(0.5)
    return False


def register_with_google(
    *,
    proxy: str | None,
    email_hint: str,
    challenge_callback,
    captcha_solver=None,
    phone_callback=None,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    timeout: int = 300,
    log_fn=print,
) -> dict:
    verification = BrowserVerificationSupport(
        captcha_solver=captcha_solver,
        phone_callback=phone_callback,
        log_fn=log_fn,
    )
    with OAuthBrowser(
        proxy=proxy,
        headless=False,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        log_fn=log_fn,
    ) as browser:
        browser.goto(KIMI_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(1)

        if _is_authenticated(browser):
            verification.mark_authenticated()
            page = browser.active_page()
            storage = _storage_snapshot(page)
            email = finalize_oauth_email(_extract_email(storage), email_hint, "Kimi")
            return {
                "email": email,
                "cookies": browser.cookie_header(domain_substrings=("kimi.com",)),
                "storage_state": json.dumps(storage, ensure_ascii=False),
            }

        if not browser.try_click_provider("google"):
            page = browser.active_page()
            _click_text(page, ("Log In", "Log in to sync chat history"))
            time.sleep(0.5)
            if not browser.try_click_provider("google"):
                raise RuntimeError("Kimi 未找到 Google 登录入口")

        if chrome_user_data_dir or chrome_cdp_url:
            browser.auto_select_google_account(timeout=8)

        if not _wait_authenticated(browser, 8, verification=verification):
            if not challenge_callback:
                raise RuntimeError("Kimi Google 登录需要在可视浏览器中完成，但当前任务没有 HumanChallenge 回调")
            response: ChallengeResponse = challenge_callback(
                ChallengeRequest(
                    kind="oauth_confirmation",
                    message="请在可视浏览器中完成 Kimi Google 登录/安全验证，完成后返回面板确认继续",
                    url=str(browser.active_page().url or KIMI_URL),
                    metadata={"platform": "kimi", "provider": "google"},
                )
            )
            if not response.completed:
                raise RuntimeError("Kimi Google 登录人工验证未完成")

        if not _wait_authenticated(browser, max(5, timeout - 8), verification=verification):
            raise RuntimeError("Kimi Google 登录完成后未检测到已登录页面")

        page = browser.active_page()
        storage = _storage_snapshot(page)
        email = finalize_oauth_email(_extract_email(storage), email_hint, "Kimi")
        return {
            "email": email,
            "cookies": browser.cookie_header(domain_substrings=("kimi.com",)),
            "storage_state": json.dumps(storage, ensure_ascii=False),
        }
