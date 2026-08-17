from __future__ import annotations

import pytest


def test_zai_is_exposed_with_browser_email_and_oauth_capabilities(client):
    response = client.get("/api/platforms")
    assert response.status_code == 200
    platform = next(item for item in response.json() if item["name"] == "zai")

    assert platform["display_name"] == "Z.AI"
    assert platform["supported_executors"] == ["headed"]
    assert platform["supported_identity_modes"] == ["mailbox", "oauth_browser"]
    assert platform["supported_oauth_providers"] == ["google", "github"]


def test_zai_auth_surface_detection_is_state_driven():
    from platforms.zai.browser_register import AuthSurface, detect_auth_surface

    assert detect_auth_surface(
        url="https://chat.z.ai/auth",
        text="Welcome to Z.ai Continue with Google Continue with Email Continue with Github",
        input_types=(),
    ) is AuthSurface.LANDING

    assert detect_auth_surface(
        url="https://chat.z.ai/auth",
        text="Continue",
        input_types=("email",),
    ) is AuthSurface.EMAIL

    assert detect_auth_surface(
        url="https://chat.z.ai/auth",
        text="Sign up",
        input_types=("password",),
    ) is AuthSurface.PASSWORD

    assert detect_auth_surface(
        url="https://chat.z.ai/auth",
        text="Enter verification code",
        input_types=("text",),
    ) is AuthSurface.OTP

    assert detect_auth_surface(
        url="https://chat.z.ai/auth",
        text="Verify you are human",
        input_types=(),
    ) is AuthSurface.SECURITY_CHALLENGE

    assert detect_auth_surface(
        url="https://chat.z.ai/",
        text="New Chat",
        input_types=(),
    ) is AuthSurface.AUTHENTICATED


def test_zai_plugin_builds_browser_adapter_without_protocol_registration():
    from core.base_platform import RegisterConfig
    from platforms.zai.plugin import ZAIPlatform

    platform = ZAIPlatform(config=RegisterConfig(executor_type="headed"), mailbox=None)
    adapter = platform.build_browser_registration_adapter()

    assert adapter.browser_worker_builder is not None
    assert adapter.browser_register_runner is not None
    assert adapter.oauth_runner_with_artifacts is not None
    assert adapter.oauth_runner is None
    assert adapter.use_captcha_for_mailbox is True
    assert adapter.use_captcha_for_oauth is True
    assert platform.build_protocol_mailbox_adapter() is None
    assert platform.build_protocol_oauth_adapter() is None


def test_zai_oauth_does_not_generate_synthetic_password_but_mailbox_does():
    from core.base_platform import RegisterConfig
    from platforms.zai.plugin import ZAIPlatform

    oauth = ZAIPlatform(
        config=RegisterConfig(
            executor_type="headed",
            extra={"identity_provider": "oauth_browser", "oauth_provider": "google"},
        )
    )
    assert oauth._prepare_registration_password(None) == ""

    mailbox = ZAIPlatform(
        config=RegisterConfig(
            executor_type="headed",
            extra={"identity_provider": "mailbox"},
        )
    )
    generated = mailbox._prepare_registration_password(None)
    assert isinstance(generated, str)
    assert generated


def test_zai_default_instance_can_check_saved_account_but_cannot_register_protocol():
    from core.base_platform import Account, RegisterConfig
    from platforms.zai.plugin import ZAIPlatform

    platform = ZAIPlatform(config=RegisterConfig())
    account = Account(
        platform="zai",
        email="zai@example.com",
        password="secret",
        extra={"cookies": "zai_session=1"},
    )

    assert platform.check_valid(account) is True
    with pytest.raises(NotImplementedError, match="headed"):
        platform.register(email="zai@example.com", password="secret")
