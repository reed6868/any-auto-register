from __future__ import annotations


class FakeLocator:
    def __init__(self, *, visible=False, attrs=None):
        self.visible = visible
        self.attrs = dict(attrs or {})
        self.filled = ""

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.visible

    def get_attribute(self, key):
        return self.attrs.get(key)

    def fill(self, value):
        self.filled = value


class FakePage:
    def __init__(self):
        self.url = "https://example.test/auth"
        self.turnstile = FakeLocator(visible=True, attrs={"data-sitekey": "site-key"})
        self.phone = FakeLocator(visible=False)
        self.code = FakeLocator(visible=False)
        self.injected_token = ""
        self.clicked = []

    def locator(self, selector):
        if selector == "[data-sitekey]":
            return self.turnstile
        if "tel" in selector:
            return self.phone
        if "one-time-code" in selector or "code" in selector or "otp" in selector:
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


class FakeCaptcha:
    def __init__(self):
        self.calls = []

    def solve_turnstile(self, page_url, site_key):
        self.calls.append((page_url, site_key))
        return "captcha-token"


class FakePhoneCallback:
    def __init__(self):
        self.values = iter(["18885551234", "654321"])
        self.success = 0
        self.send_succeeded = 0

    def __call__(self):
        return next(self.values)

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
    # The same widget must not consume provider quota twice.
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
    assert phone.send_succeeded == 1

    page.phone.visible = False
    page.code.visible = True
    assert support.try_phone(page) is True
    assert page.code.filled == "654321"

    support.mark_authenticated()
    assert phone.success == 1
