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
        "verification code",
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
        allowed_domain_substrings=("kimi.com",),
        sync_phone_country_code=True,
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
                "identity_mode": "google",
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
                raise RuntimeError("Kimi Google OAuth 未能通过已复用的浏览器会话自动完成")
            response: ChallengeResponse = challenge_callback(
                ChallengeRequest(
                    kind="oauth_confirmation",
                    message="调试模式：请在可视浏览器中完成 Kimi Google 登录/安全验证",
                    url=str(browser.active_page().url or KIMI_URL),
                    metadata={"platform": "kimi", "provider": "google"},
                )
            )
            if not response.completed:
                raise RuntimeError("Kimi Google 登录调试验证未完成")

        if not _wait_authenticated(browser, max(5, timeout - 8), verification=verification):
            raise RuntimeError("Kimi Google 登录完成后未检测到已登录页面")

        page = browser.active_page()
        storage = _storage_snapshot(page)
        email = finalize_oauth_email(_extract_email(storage), email_hint, "Kimi")
        return {
            "email": email,
            "cookies": browser.cookie_header(domain_substrings=("kimi.com",)),
            "storage_state": json.dumps(storage, ensure_ascii=False),
            "identity_mode": "google",
        }


class KimiPhoneRegister:
    """First-party Kimi registration/login using the configured SMS provider.

    Kimi's public web surface uses one login form for both existing and new users.
    A first successful mobile verification creates/binds the Kimi account, so there
    is no separate Register button to click.
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        challenge_callback=None,
        captcha_solver=None,
        phone_callback=None,
        timeout: int = 300,
        log_fn=print,
    ):
        self.proxy = proxy
        self.challenge_callback = challenge_callback
        self.timeout = timeout
        self.log = log_fn
        self.verification = BrowserVerificationSupport(
            captcha_solver=captcha_solver,
            phone_callback=phone_callback,
            allowed_domain_substrings=("kimi.com",),
            sync_phone_country_code=True,
            log_fn=log_fn,
        )

    def _debug_or_fail(self, page, message: str) -> None:
        if not self.challenge_callback:
            raise RuntimeError(message)
        response: ChallengeResponse = self.challenge_callback(
            ChallengeRequest(
                kind="security_check",
                message=f"调试模式：{message}",
                url=str(page.url or KIMI_URL),
                metadata={"platform": "kimi", "identity_provider": "phone"},
            )
        )
        if not response.completed:
            raise RuntimeError(message)

    def run(self) -> dict:
        if not self.verification.phone_callback:
            raise RuntimeError("Kimi 手机号注册需要配置可用的接码 provider")

        with OAuthBrowser(proxy=self.proxy, headless=False, log_fn=self.log) as browser:
            browser.goto(KIMI_URL, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + self.timeout
            unknown_cycles = 0

            while time.monotonic() < deadline:
                page = browser.active_page()
                text = _page_text(page)
                surface = detect_auth_surface(url=str(page.url or ""), text=text)

                if surface is AuthSurface.AUTHENTICATED:
                    if not self.verification.phone_started or not self.verification.phone_number:
                        raise RuntimeError("Kimi 手机号注册检测到已有登录会话，拒绝保存为新手机号账号")
                    self.verification.mark_authenticated()
                    storage = _storage_snapshot(page)
                    phone = self.verification.phone_number
                    return {
                        "email": phone,
                        "phone_number": phone,
                        "cookies": browser.cookie_header(domain_substrings=("kimi.com",)),
                        "storage_state": json.dumps(storage, ensure_ascii=False),
                        "identity_mode": "phone",
                    }

                if self.verification.try_handle(page):
                    unknown_cycles = 0
                    time.sleep(0.75)
                    continue

                if surface is AuthSurface.LANDING:
                    if _click_text(page, ("Log in with phone number", "Phone number", "手机号登录")):
                        unknown_cycles = 0
                        time.sleep(0.5)
                        continue
                    unknown_cycles += 1
                    if unknown_cycles < 4:
                        time.sleep(0.5)
                        continue
                    self._debug_or_fail(page, "Kimi 未检测到可自动处理的手机号输入/短信验证码步骤")
                    unknown_cycles = 0
                    continue

                if surface is AuthSurface.SECURITY_CHALLENGE:
                    self._debug_or_fail(page, "Kimi 出现无法由当前 captcha/SMS provider 自动处理的安全验证")
                    time.sleep(0.5)
                    continue

                unknown_cycles += 1
                if unknown_cycles < 4:
                    time.sleep(0.5)
                    continue
                if _click_text(page, ("Log In", "Log in to sync chat history", "Log in with phone number")):
                    unknown_cycles = 0
                    time.sleep(0.5)
                    continue
                self._debug_or_fail(page, "Kimi 出现未识别的手机号登录/注册步骤")
                unknown_cycles = 0

            raise RuntimeError(f"Kimi 手机号注册在 {self.timeout} 秒内未完成")
