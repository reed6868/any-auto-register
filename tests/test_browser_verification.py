from __future__ import annotations

from types import SimpleNamespace


class FakeLocator:
    def __init__(self, *, visible=False, attrs=None):
        self.visible = visible
        self.attrs = dict(attrs or {})
        self.filled = ""
        self.pressed = []

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.visible

    def get_attribute(self, key, timeout=0):
        if key == "value" and self.filled:
            return self.filled
        return self.attrs.get(key)

    def input_value(self, timeout=0):
        return self.filled or str(self.attrs.get("value") or "")

    def fill(self, value):
        self.filled = value

    def press(self, key):
        self.pressed.append(key)

    def click(self):
        return None


class FakePage:
    def __init__(self):
        self.url = "https://example.test/auth"
        self.turnstile = FakeLocator(visible=True, attrs={"data-sitekey": "site-key"})
        self.dial = FakeLocator(visible=False, attrs={"value": "+86"})
        self.phone = FakeLocator(visible=False)
        self.code = FakeLocator(visible=False)
        self.injected_token = ""
        self.clicked = []

    def locator(self, selector):
        if selector == "[data-sitekey]":
            return self.turnstile
        if selector.startswith('input[value^="+"]') or "country" in selector or "dial" in selector:
            return self.dial
        if "tel" in selector or "phone" in selector or "mobile" in selector or "手机号" in selector:
            return self.phone
        if "one-time-code" in selector or "code" in selector or "otp" in selector or "verification" in selector or "验证码" in selector:
            return self.code
        return FakeLocator(visible=False)

    def evaluate(self, script, value=None):
        if value:
            self.injected_token = value
            return 1
        return None

    def get_by_role(self, role, name=None):
        page = self

        class ClickLocator(FakeLocator):
            def __init__(self):
                super().__init__(visible=True)

            def click(self):
                page.clicked.append(str(name))

        return ClickLocator()

    def get_by_text(self, text, exact=False):
        return FakeLocator(visible=False)


class FakeCaptcha:
    def __init__(self):
        self.calls = []

    def solve_turnstile(self, page_url, site_key):
        self.calls.append((page_url, site_key))
        return "captcha-token"


class FakePhoneCallback:
    def __init__(self, number="18885551234", country=""):
        self.values = iter([number, "654321"])
        self.success = 0
        self.send_succeeded = 0
        self.activation = None
        self.country = country

    def __call__(self):
        value = next(self.values)
        if self.activation is None:
            self.activation = SimpleNamespace(
                country=self.country,
                metadata={},
                phone_number=value,
            )
        return value

    def mark_send_succeeded(self):
        self.send_succeeded += 1

    def report_success(self):
        self.success += 1


def test_turnstile_uses_framework_solver_and_injects_response():
    from core.registration.browser_verification import BrowserVerificationSupport

    page = FakePage()
    solver = FakeCaptcha()
    support = BrowserVerificationSupport(captcha_solver=solver)

    assert support.try_turnstile(page) is True
    assert solver.calls == [(page.url, "site-key")]
    assert page.injected_token == "captcha-token"
    assert support.try_turnstile(page) is False
    assert len(solver.calls) == 1


def test_invisible_standard_turnstile_widget_still_uses_framework_solver():
    from core.registration.browser_verification import BrowserVerificationSupport

    page = FakePage()
    page.turnstile.visible = False
    solver = FakeCaptcha()
    support = BrowserVerificationSupport(captcha_solver=solver)

    assert support.try_turnstile(page) is True
    assert solver.calls == [(page.url, "site-key")]


def test_phone_step_uses_framework_phone_callback_lazily_and_reports_success():
    from core.registration.browser_verification import BrowserVerificationSupport

    page = FakePage()
    page.turnstile.attrs = {}
    page.phone.visible = True
    phone = FakePhoneCallback()
    support = BrowserVerificationSupport(phone_callback=phone)

    assert support.try_phone(page) is True
    assert page.phone.filled == "18885551234"
    assert support.phone_number == "18885551234"
    assert phone.send_succeeded == 1
    assert any("Send" in item for item in page.clicked)

    page.phone.visible = False
    page.code.visible = True
    assert support.try_phone(page) is True
    assert page.code.filled == "654321"

    support.mark_authenticated()
    assert phone.success == 1


def test_phone_step_syncs_separate_country_code_before_send():
    from core.registration.browser_verification import BrowserVerificationSupport

    page = FakePage()
    page.turnstile.attrs = {}
    page.url = "https://www.kimi.com/"
    page.phone.visible = True
    page.dial.visible = True
    phone = FakePhoneCallback(number="+18885551234", country="187")
    support = BrowserVerificationSupport(
        phone_callback=phone,
        allowed_domain_substrings=("kimi.com",),
        sync_phone_country_code=True,
    )

    assert support.try_phone(page) is True
    assert page.dial.filled == "+1"
    assert page.phone.filled == "8885551234"
    assert support.phone_number == "+18885551234"
    assert phone.send_succeeded == 1


def test_scoped_browser_verification_does_not_touch_third_party_oauth_pages():
    from core.registration.browser_verification import BrowserVerificationSupport

    page = FakePage()
    page.url = "https://accounts.google.com/signin/v2/challenge"
    page.phone.visible = True
    solver = FakeCaptcha()
    phone = FakePhoneCallback()
    support = BrowserVerificationSupport(
        captcha_solver=solver,
        phone_callback=phone,
        allowed_domain_substrings=("kimi.com",),
        sync_phone_country_code=True,
    )

    assert support.try_handle(page) is False
    assert solver.calls == []
    assert page.phone.filled == ""
