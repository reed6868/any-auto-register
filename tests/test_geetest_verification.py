from __future__ import annotations


class FakeContext:
    def __init__(self):
        self.scripts = []

    def add_init_script(self, script):
        self.scripts.append(script)


class FakeGeeTestPage:
    def __init__(self):
        self.url = "https://www.kimi.com/"
        self.injected = None

    def evaluate(self, script, value=None):
        if value is None:
            return {
                "version": 4,
                "captcha_id": "captcha-id-1",
                "gt": "",
                "challenge": "",
                "api_server": "",
                "ready": True,
                "callback_count": 1,
            }
        self.injected = value
        return True

    def locator(self, selector):
        class Locator:
            def count(self):
                return 0
        return Locator()


class FakeGeeTestSolver:
    def __init__(self):
        self.calls = []

    def solve_geetest(self, page_url, params, *, proxy=""):
        self.calls.append((page_url, dict(params), proxy))
        return {
            "version": 4,
            "lot_number": "lot",
            "pass_token": "pass",
            "gen_time": "123",
            "captcha_output": "output",
        }


def test_geetest_hook_is_installed_before_navigation_and_solution_is_returned_to_page():
    from core.registration.browser_verification import BrowserVerificationSupport

    solver = FakeGeeTestSolver()
    support = BrowserVerificationSupport(
        captcha_solver=solver,
        allowed_domain_substrings=("kimi.com",),
        proxy="socks5://user:pass@127.0.0.1:1080",
    )
    context = FakeContext()
    page = FakeGeeTestPage()

    support.install(context)
    assert context.scripts
    assert "initGeetest4" in context.scripts[0]

    assert support.try_geetest(page) is True
    assert solver.calls[0][0] == "https://www.kimi.com/"
    assert solver.calls[0][1]["captcha_id"] == "captcha-id-1"
    assert solver.calls[0][2] == "socks5://user:pass@127.0.0.1:1080"
    assert page.injected["lot_number"] == "lot"
    assert support.try_geetest(page) is False


def test_geetest_fails_fast_when_selected_provider_does_not_support_it():
    import pytest
    from core.registration.browser_verification import BrowserVerificationSupport

    class Unsupported:
        def solve_geetest(self, page_url, params, *, proxy=""):
            raise NotImplementedError("unsupported")

    support = BrowserVerificationSupport(captcha_solver=Unsupported(), allowed_domain_substrings=("kimi.com",))
    with pytest.raises(RuntimeError, match="不支持 GeeTest"):
        support.try_geetest(FakeGeeTestPage())
