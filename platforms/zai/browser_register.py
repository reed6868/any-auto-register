from __future__ import annotations

from enum import Enum
import json
import re
import time
from typing import Callable
from urllib.parse import urlparse

from core.oauth_browser import OAuthBrowser, finalize_oauth_email
from core.registration import ChallengeRequest, ChallengeResponse
from core.registration.browser_verification import BrowserVerificationSupport


AUTH_URL = "https://chat.z.ai/auth?redirect=%2F"
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


class AuthSurface(str, Enum):
    LANDING = "landing"
    EMAIL = "email"
    PASSWORD = "password"
    OTP = "otp"
    SECURITY_CHALLENGE = "security_challenge"
    AUTHENTICATED = "authenticated"
    UNKNOWN = "unknown"


def detect_auth_surface(*, url: str, text: str, input_types: tuple[str, ...]) -> AuthSurface:
    normalized_url = str(url or "").lower()
    normalized_text = " ".join(str(text or "").lower().split())
    types = {str(item or "").lower() for item in input_types}
    security_markers = (
        "verify you are human", "security verification", "security check",
        "two-factor", "2fa", "captcha", "turnstile", "geetest",
    )
    if any(marker in normalized_text for marker in security_markers):
        return AuthSurface.SECURITY_CHALLENGE
    parsed = urlparse(normalized_url)
    if parsed.netloc.endswith("z.ai") and "/auth" not in parsed.path:
        return AuthSurface.AUTHENTICATED
    if "verification code" in normalized_text or "one-time code" in normalized_text or "otp" in normalized_text:
        return AuthSurface.OTP
    if "password" in types:
        return AuthSurface.PASSWORD
    if "email" in types:
        return AuthSurface.EMAIL
    if "continue with email" in normalized_text or "welcome to z.ai" in normalized_text:
        return AuthSurface.LANDING
    return AuthSurface.UNKNOWN


def _snapshot(page) -> tuple[str, str, tuple[str, ...]]:
    url = str(page.url or "")
    try:
        text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        text = ""
    try:
        input_types = tuple(page.locator("input").evaluate_all("els => els.map(el => (el.type || el.getAttribute('type') || 'text').toLowerCase())"))
    except Exception:
        input_types = ()
    return url, text, input_types


def _first_visible(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=300):
                return locator
        except Exception:
            continue
    return None


def _click_text(page, labels: tuple[str, ...]) -> bool:
    for label in labels:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)).first
                if locator.is_visible(timeout=300):
                    locator.click()
                    return True
            except Exception:
                pass
        try:
            locator = page.get_by_text(label, exact=True).first
            if locator.is_visible(timeout=300):
                locator.click()
                return True
        except Exception:
            pass
    return False


def _submit_password_surface(page, password: str) -> None:
    """Fill every visible password field before clicking the account submit action."""
    password_fields = page.locator('input[type="password"]')
    count = password_fields.count()
    if count <= 0:
        raise RuntimeError("Z.AI 未找到密码输入框")
    filled = 0
    for index in range(count):
        try:
            field = password_fields.nth(index)
            if field.is_visible(timeout=300):
                field.fill(password)
                filled += 1
        except Exception:
            pass
    if filled <= 0:
        raise RuntimeError("Z.AI 未找到可见密码输入框")
    if not _click_text(page, ("Sign Up", "Create Account", "Continue", "Next", "Log In", "Login")):
        raise RuntimeError("Z.AI 已填写密码，但未找到提交按钮")


def _storage_snapshot(page) -> dict:
    try:
        return page.evaluate("""() => ({localStorage: Object.fromEntries(Object.entries(localStorage)), sessionStorage: Object.fromEntries(Object.entries(sessionStorage))})""") or {}
    except Exception:
        return {}


def _extract_email(snapshot: dict) -> str:
    try:
        text = json.dumps(snapshot, ensure_ascii=False)
    except Exception:
        return ""
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else ""


def _capture_session(browser: OAuthBrowser, *, email_hint: str, password: str = "") -> dict:
    page = browser.active_page()
    storage = _storage_snapshot(page)
    email = finalize_oauth_email(_extract_email(storage), email_hint, "Z.AI")
    return {"email": email, "password": password, "cookies": browser.cookie_header(domain_substrings=("z.ai",)), "storage_state": json.dumps(storage, ensure_ascii=False)}


def _wait_authenticated(browser: OAuthBrowser, timeout: float, verification: BrowserVerificationSupport | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = browser.active_page()
        url, text, input_types = _snapshot(page)
        if detect_auth_surface(url=url, text=text, input_types=input_types) is AuthSurface.AUTHENTICATED:
            if verification:
                verification.mark_authenticated()
            return True
        if verification and verification.try_handle(page):
            time.sleep(0.75)
            continue
        time.sleep(0.5)
    return False


def register_with_oauth(
    *, proxy: str | None, oauth_provider: str, email_hint: str,
    challenge_callback: Callable[[ChallengeRequest], ChallengeResponse] | None,
    captcha_solver=None, phone_callback=None, chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "", timeout: int = 300, log_fn=print,
) -> dict:
    verification = BrowserVerificationSupport(
        captcha_solver=captcha_solver,
        phone_callback=phone_callback,
        allowed_domain_substrings=("z.ai",),
        sync_phone_country_code=True,
        proxy=proxy,
        log_fn=log_fn,
    )
    with OAuthBrowser(
        proxy=proxy, headless=False, chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url, log_fn=log_fn,
    ) as browser:
        verification.install(browser.context)
        browser.goto(AUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(1)
        if not browser.try_click_provider(oauth_provider):
            raise RuntimeError(f"Z.AI 未找到 {oauth_provider} 登录入口")
        if oauth_provider == "google" and (chrome_user_data_dir or chrome_cdp_url):
            browser.auto_select_google_account(timeout=8)
        if not _wait_authenticated(browser, 8, verification=verification):
            if not challenge_callback:
                raise RuntimeError(f"Z.AI {oauth_provider} OAuth 未能通过已复用的浏览器会话自动完成")
            response = challenge_callback(ChallengeRequest(
                kind="oauth_confirmation", message=f"调试模式：请在可视浏览器中完成 Z.AI {oauth_provider} 登录",
                url=str(browser.active_page().url or AUTH_URL), metadata={"platform": "zai", "provider": oauth_provider},
            ))
            if not response.completed:
                raise RuntimeError("Z.AI OAuth 调试验证未完成")
        if not _wait_authenticated(browser, max(5, timeout - 8), verification=verification):
            raise RuntimeError("Z.AI OAuth 登录完成后未检测到已登录页面")
        return _capture_session(browser, email_hint=email_hint)


class ZAIBrowserRegister:
    def __init__(
        self, *, proxy: str | None = None, otp_callback: Callable[[], str] | None = None,
        challenge_callback: Callable[[ChallengeRequest], ChallengeResponse] | None = None,
        captcha_solver=None, phone_callback=None, log_fn=print, timeout: int = 300,
    ):
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.challenge_callback = challenge_callback
        self.log = log_fn
        self.timeout = timeout
        self.verification = BrowserVerificationSupport(
            captcha_solver=captcha_solver,
            phone_callback=phone_callback,
            allowed_domain_substrings=("z.ai",),
            sync_phone_country_code=True,
            proxy=proxy,
            log_fn=log_fn,
        )
        self.captcha_solver = captcha_solver
        self.phone_callback = phone_callback

    def _request_human(self, page, kind: str, message: str) -> None:
        if not self.challenge_callback:
            raise RuntimeError(f"Z.AI 无人值守模式无法自动处理: {message}")
        response = self.challenge_callback(ChallengeRequest(
            kind=kind, message=f"调试模式：{message}", url=str(page.url or AUTH_URL), metadata={"platform": "zai"},
        ))
        if not response.completed:
            raise RuntimeError("Z.AI 调试验证未完成")

    def run(self, email: str, password: str) -> dict:
        if not email:
            raise RuntimeError("Z.AI 邮箱注册需要 email")
        if not password:
            raise RuntimeError("Z.AI 邮箱注册需要 password")
        with OAuthBrowser(proxy=self.proxy, headless=False, log_fn=self.log) as browser:
            self.verification.install(browser.context)
            browser.goto(AUTH_URL, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + self.timeout
            otp_used = False
            unknown_cycles = 0
            while time.monotonic() < deadline:
                page = browser.active_page()
                url, text, input_types = _snapshot(page)
                surface = detect_auth_surface(url=url, text=text, input_types=input_types)
                if surface is AuthSurface.AUTHENTICATED:
                    self.verification.mark_authenticated()
                    return _capture_session(browser, email_hint=email, password=password)
                if self.verification.try_handle(page):
                    unknown_cycles = 0
                    time.sleep(1)
                    continue
                if surface is AuthSurface.LANDING:
                    if not _click_text(page, ("Continue with Email",)):
                        raise RuntimeError("Z.AI 未找到 Continue with Email")
                    time.sleep(1)
                    continue
                if surface is AuthSurface.EMAIL:
                    field = _first_visible(page, ('input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]'))
                    if not field:
                        raise RuntimeError("Z.AI 未找到邮箱输入框")
                    field.fill(email)
                    if not _click_text(page, ("Continue", "Next", "Sign Up", "Create Account", "Log In", "Login")):
                        raise RuntimeError("Z.AI 已填写邮箱，但未找到继续按钮")
                    time.sleep(1)
                    continue
                if surface is AuthSurface.PASSWORD:
                    _submit_password_surface(page, password)
                    time.sleep(1)
                    continue
                if surface is AuthSurface.OTP:
                    if otp_used:
                        time.sleep(1)
                        continue
                    if not self.otp_callback:
                        raise RuntimeError("Z.AI 页面要求邮箱验证码，但当前 mailbox 未提供 otp_callback")
                    code = str(self.otp_callback() or "").strip()
                    if not code:
                        raise RuntimeError("Z.AI 未获取到邮箱验证码")
                    field = _first_visible(page, ('input[autocomplete="one-time-code"]', 'input[name*="code" i]', 'input[placeholder*="code" i]', 'input[inputmode="numeric"]'))
                    if not field:
                        raise RuntimeError("Z.AI 未找到验证码输入框")
                    field.fill(code)
                    otp_used = True
                    if not _click_text(page, ("Verify", "Continue", "Next", "Submit")):
                        raise RuntimeError("Z.AI 已填写邮箱验证码，但未找到验证按钮")
                    time.sleep(1)
                    continue
                if surface is AuthSurface.SECURITY_CHALLENGE:
                    self._request_human(page, "security_check", "出现无法由当前 captcha/SMS provider 自动处理的安全验证")
                    time.sleep(0.5)
                    continue
                unknown_cycles += 1
                if unknown_cycles < 4:
                    time.sleep(0.75)
                    continue
                if _click_text(page, ("Sign Up", "Create Account", "Continue")):
                    unknown_cycles = 0
                    time.sleep(1)
                    continue
                self._request_human(page, "security_check", "出现未识别的登录/注册步骤")
                unknown_cycles = 0
            raise RuntimeError(f"Z.AI 注册在 {self.timeout} 秒内未完成")
