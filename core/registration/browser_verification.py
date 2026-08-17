from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse


def _first_visible(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=250):
                return locator
        except Exception:
            continue
    return None


def _click_first(page, labels: tuple[str, ...]) -> bool:
    for label in labels:
        try:
            locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
            if locator.is_visible(timeout=250):
                locator.click()
                return True
        except Exception:
            pass
    return False


def _turnstile_sitekey(page) -> str:
    try:
        widget = page.locator("[data-sitekey]").first
        return str(widget.get_attribute("data-sitekey", timeout=250) or "").strip()
    except TypeError:
        try:
            widget = page.locator("[data-sitekey]").first
            return str(widget.get_attribute("data-sitekey") or "").strip()
        except Exception:
            return ""
    except Exception:
        return ""


def _inject_turnstile_response(page, token: str) -> bool:
    try:
        changed = page.evaluate(
            """
            token => {
              const selectors = [
                'input[name="cf-turnstile-response"]',
                'textarea[name="cf-turnstile-response"]'
              ];
              const nodes = selectors.flatMap(selector => Array.from(document.querySelectorAll(selector)));
              for (const node of nodes) {
                node.value = token;
                node.setAttribute('value', token);
                node.dispatchEvent(new Event('input', { bubbles: true }));
                node.dispatchEvent(new Event('change', { bubbles: true }));
              }
              const widget = document.querySelector('[data-sitekey][data-callback]');
              const callbackName = widget && widget.getAttribute('data-callback');
              if (callbackName && typeof window[callbackName] === 'function') {
                window[callbackName](token);
              }
              return nodes.length || Boolean(callbackName);
            }
            """,
            token,
        )
        return bool(changed)
    except Exception:
        return False


class BrowserVerificationSupport:
    """Bridge platform-owned browser verification to configured providers."""

    def __init__(
        self,
        *,
        captcha_solver: Any = None,
        phone_callback: Callable[[], str] | None = None,
        allowed_domain_substrings: tuple[str, ...] = (),
        log_fn=None,
    ):
        self.captcha_solver = captcha_solver
        self.phone_callback = phone_callback
        self.allowed_domain_substrings = tuple(
            str(item or "").strip().lower()
            for item in allowed_domain_substrings
            if str(item or "").strip()
        )
        self.log = log_fn or (lambda message: None)
        self._captcha_attempts: set[str] = set()
        self._phone_started = False
        self._phone_number = ""
        self._phone_code_filled = False
        self._phone_reported = False

    @property
    def phone_started(self) -> bool:
        return self._phone_started

    @property
    def phone_number(self) -> str:
        return self._phone_number

    def _is_allowed_page(self, page) -> bool:
        if not self.allowed_domain_substrings:
            return True
        url = str(getattr(page, "url", "") or "")
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        return bool(host and any(part in host for part in self.allowed_domain_substrings))

    def try_turnstile(self, page) -> bool:
        if not self._is_allowed_page(page):
            return False
        site_key = _turnstile_sitekey(page)
        if not site_key:
            return False
        attempt_key = f"{str(getattr(page, 'url', '') or '')}|{site_key}"
        if attempt_key in self._captcha_attempts:
            return False
        self._captcha_attempts.add(attempt_key)
        if not self.captcha_solver:
            self.log("[验证] 检测到 Turnstile，但当前没有可用 captcha provider")
            return False
        try:
            self.log("[验证] 检测到 Turnstile，使用框架 captcha provider")
            token = str(self.captcha_solver.solve_turnstile(str(getattr(page, "url", "") or ""), site_key) or "").strip()
            if not token:
                self.log("[验证] captcha provider 未返回有效 token")
                return False
            if not _inject_turnstile_response(page, token):
                self.log("[验证] Turnstile token 已获取但页面未找到标准响应字段")
                return False
            self.log("[验证] Turnstile token 已注入页面")
            return True
        except Exception as exc:
            self.log(f"[验证] captcha provider 处理失败: {exc}")
            return False

    def try_phone(self, page) -> bool:
        if not self._is_allowed_page(page) or not self.phone_callback:
            return False

        if not self._phone_started:
            phone_field = _first_visible(
                page,
                (
                    'input[type="tel"]',
                    'input[name*="phone" i]',
                    'input[autocomplete="tel"]',
                    'input[placeholder*="phone" i]',
                    'input[placeholder*="mobile" i]',
                    'input[placeholder*="手机号"]',
                ),
            )
            if phone_field is None:
                return False
            number = str(self.phone_callback() or "").strip()
            if not number:
                raise RuntimeError("接码 provider 未返回手机号")
            phone_field.fill(number)
            self._phone_number = number
            clicked = _click_first(
                page,
                (
                    "Send Code",
                    "Send code",
                    "Send",
                    "Get Code",
                    "Continue",
                    "Next",
                    "Verify",
                    "发送验证码",
                    "获取验证码",
                    "发送",
                ),
            )
            hook = getattr(self.phone_callback, "mark_send_succeeded", None)
            if clicked and callable(hook):
                hook()
            self._phone_started = True
            self.log(f"[接码] 已由框架接码 provider 填入手机号: {number[:5]}****")
            return True

        if self._phone_code_filled:
            return False
        code_field = _first_visible(
            page,
            (
                'input[autocomplete="one-time-code"]',
                'input[name*="otp" i]',
                'input[name*="code" i]',
                'input[placeholder*="code" i]',
                'input[placeholder*="verification" i]',
                'input[placeholder*="验证码"]',
            ),
        )
        if code_field is None:
            return False
        code = str(self.phone_callback() or "").strip()
        if not code:
            raise RuntimeError("接码 provider 未返回短信验证码")
        code_field.fill(code)
        _click_first(page, ("Log In", "Login", "Verify", "Continue", "Next", "Submit", "登录"))
        self._phone_code_filled = True
        self.log("[接码] 已由框架接码 provider 填入短信验证码")
        return True

    def try_handle(self, page) -> bool:
        if not self._is_allowed_page(page):
            return False
        if self.try_turnstile(page):
            return True
        return self.try_phone(page)

    def mark_authenticated(self) -> None:
        if not self._phone_started or self._phone_reported or not self.phone_callback:
            return
        hook = getattr(self.phone_callback, "report_success", None)
        if callable(hook):
            hook()
        self._phone_reported = True
