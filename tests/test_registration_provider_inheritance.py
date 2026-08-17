from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    BrowserRegistrationFlow,
    ChallengeResponse,
    RegistrationArtifacts,
    RegistrationContext,
    RegistrationResult,
)
import core.registration.flows as flows_module


def _oauth_context(platform, name: str = "target") -> RegistrationContext:
    return RegistrationContext(
        platform_name=name,
        platform_display_name=name,
        platform=platform,
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=False,
            identity_provider="oauth_browser",
            oauth_provider="google",
            chrome_user_data_dir="/tmp/chrome-profile",
            chrome_cdp_url="",
        ),
        config=RegisterConfig(
            executor_type="headed",
            proxy="socks5://127.0.0.1:1080",
            extra={"identity_provider": "oauth_browser"},
        ),
        email="user@example.com",
        password=None,
        log_fn=lambda message: None,
    )


def test_browser_oauth_flow_inherits_lazy_captcha_phone_and_cleanup(monkeypatch):
    events: list[object] = []

    class FakeCaptcha:
        def solve_turnstile(self, page_url: str, site_key: str) -> str:
            events.append(("captcha", page_url, site_key))
            return "turnstile-token"

    class FakePlatform:
        mailbox = None

        def _make_captcha(self):
            events.append("captcha-created")
            return FakeCaptcha()

    def fake_build_phone_callbacks(ctx, *, service=None):
        events.append(("phone-build", service))
        return (lambda: "18885551234", lambda: events.append(("phone-cleanup", service)))

    monkeypatch.setattr(flows_module, "build_phone_callbacks", fake_build_phone_callbacks)
    ctx = _oauth_context(FakePlatform(), "kimi")

    def run_oauth(ctx, artifacts):
        assert callable(artifacts.phone_callback)
        assert artifacts.captcha_solver is not None
        assert "captcha-created" not in events
        assert artifacts.captcha_solver.solve_turnstile("https://example.test", "site-key") == "turnstile-token"
        assert artifacts.phone_callback() == "18885551234"
        return {"email": ctx.identity.email}

    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=""),
        oauth_runner_with_artifacts=run_oauth,
        use_captcha_for_oauth=True,
    )

    result = BrowserRegistrationFlow(adapter).run(ctx)

    assert result.email == "user@example.com"
    assert ("phone-build", "kimi") in events
    assert ("captcha", "https://example.test", "site-key") in events
    assert ("phone-cleanup", "kimi") in events


def test_kimi_oauth_adapter_passes_framework_artifacts_but_not_human_challenge_by_default(monkeypatch):
    from platforms.kimi import browser_auth
    from platforms.kimi.plugin import KimiPlatform

    captured = {}

    def fake_register_with_google(**kwargs):
        captured.update(kwargs)
        return {"email": "user@example.com", "cookies": "k=1", "storage_state": "{}"}

    monkeypatch.setattr(browser_auth, "register_with_google", fake_register_with_google)
    platform = KimiPlatform(
        config=RegisterConfig(
            executor_type="headed",
            extra={"identity_provider": "oauth_browser"},
        )
    )
    artifacts = RegistrationArtifacts(
        phone_callback=lambda: "18885551234",
        captcha_solver=object(),
        challenge_callback=lambda request: ChallengeResponse(completed=True),
    )
    ctx = SimpleNamespace(
        identity=SimpleNamespace(
            oauth_provider="google",
            email="user@example.com",
            chrome_user_data_dir="/tmp/chrome-profile",
            chrome_cdp_url="",
        ),
        proxy="socks5://127.0.0.1:1080",
        extra={},
        log=lambda message: None,
    )

    platform._run_google_oauth(ctx, artifacts)

    assert captured["proxy"] == "socks5://127.0.0.1:1080"
    assert captured["phone_callback"] is artifacts.phone_callback
    assert captured["captcha_solver"] is artifacts.captcha_solver
    assert captured["challenge_callback"] is None


def test_kimi_debug_mode_can_opt_in_human_challenge(monkeypatch):
    from platforms.kimi import browser_auth
    from platforms.kimi.plugin import KimiPlatform

    captured = {}
    monkeypatch.setattr(
        browser_auth,
        "register_with_google",
        lambda **kwargs: captured.update(kwargs) or {"email": "user@example.com", "cookies": "k=1", "storage_state": "{}"},
    )
    platform = KimiPlatform(config=RegisterConfig(executor_type="headed", extra={"identity_provider": "oauth_browser"}))
    challenge = lambda request: ChallengeResponse(completed=True)
    artifacts = RegistrationArtifacts(challenge_callback=challenge)
    ctx = SimpleNamespace(
        identity=SimpleNamespace(
            oauth_provider="google",
            email="user@example.com",
            chrome_user_data_dir="/tmp/chrome-profile",
            chrome_cdp_url="",
        ),
        proxy=None,
        extra={"allow_human_challenge": True},
        log=lambda message: None,
    )

    platform._run_google_oauth(ctx, artifacts)
    assert captured["challenge_callback"] is challenge


def test_zai_mailbox_and_oauth_adapters_pass_framework_artifacts_without_human_wait_by_default(monkeypatch):
    from platforms.zai import browser_register
    from platforms.zai.plugin import ZAIPlatform

    platform = ZAIPlatform(config=RegisterConfig(executor_type="headed"), mailbox=None)
    adapter = platform.build_browser_registration_adapter()

    assert adapter.use_captcha_for_mailbox is True
    assert adapter.use_captcha_for_oauth is True
    assert adapter.oauth_runner_with_artifacts is not None

    artifacts = RegistrationArtifacts(
        otp_callback=lambda: "123456",
        phone_callback=lambda: "18885551234",
        captcha_solver=object(),
        challenge_callback=lambda request: ChallengeResponse(completed=True),
    )
    worker = adapter.browser_worker_builder(SimpleNamespace(proxy="socks5://127.0.0.1:1080", extra={}, log=lambda message: None), artifacts)
    assert worker.otp_callback is artifacts.otp_callback
    assert worker.phone_callback is artifacts.phone_callback
    assert worker.captcha_solver is artifacts.captcha_solver
    assert worker.challenge_callback is None

    captured = {}

    def fake_register_with_oauth(**kwargs):
        captured.update(kwargs)
        return {"email": "user@example.com", "cookies": "z=1", "storage_state": "{}"}

    monkeypatch.setattr(browser_register, "register_with_oauth", fake_register_with_oauth)
    ctx = SimpleNamespace(
        identity=SimpleNamespace(
            oauth_provider="google",
            email="user@example.com",
            chrome_user_data_dir="/tmp/chrome-profile",
            chrome_cdp_url="",
        ),
        proxy="socks5://127.0.0.1:1080",
        extra={},
        log=lambda message: None,
    )
    platform._run_oauth(ctx, artifacts)

    assert captured["phone_callback"] is artifacts.phone_callback
    assert captured["captcha_solver"] is artifacts.captcha_solver
    assert captured["challenge_callback"] is None
