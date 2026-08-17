from __future__ import annotations

import pytest


def test_kimi_is_exposed_with_evidence_backed_google_oauth_capability(client):
    response = client.get("/api/platforms")
    assert response.status_code == 200
    platform = next(item for item in response.json() if item["name"] == "kimi")

    assert platform["display_name"] == "Kimi"
    assert platform["supported_executors"] == ["headed"]
    assert platform["supported_identity_modes"] == ["oauth_browser"]
    assert platform["supported_oauth_providers"] == ["google"]


def test_kimi_auth_surface_detection_distinguishes_login_and_authenticated():
    from platforms.kimi.browser_auth import AuthSurface, detect_auth_surface

    assert detect_auth_surface(
        url="https://www.kimi.com/",
        text="Log in to Chat with Kimi for Free Continue with Google Log in with phone number",
    ) is AuthSurface.LANDING

    assert detect_auth_surface(
        url="https://www.kimi.com/",
        text="New Chat Chats Kimi Claw",
    ) is AuthSurface.AUTHENTICATED

    assert detect_auth_surface(
        url="https://www.kimi.com/",
        text="Security verification required",
    ) is AuthSurface.SECURITY_CHALLENGE


def test_kimi_plugin_is_headed_oauth_only():
    from core.base_platform import RegisterConfig
    from platforms.kimi.plugin import KimiPlatform

    platform = KimiPlatform(config=RegisterConfig(executor_type="headed"))
    adapter = platform.build_browser_registration_adapter()

    assert adapter.oauth_runner is not None
    assert adapter.browser_worker_builder is None
    assert platform.build_protocol_mailbox_adapter() is None
    assert platform.build_protocol_oauth_adapter() is None


def test_kimi_default_instance_can_check_saved_account_but_cannot_register_protocol():
    from core.base_platform import Account, RegisterConfig
    from platforms.kimi.plugin import KimiPlatform

    platform = KimiPlatform(config=RegisterConfig())
    account = Account(
        platform="kimi",
        email="kimi@example.com",
        password="",
        extra={"cookies": "kimi_session=1"},
    )

    assert platform.check_valid(account) is True
    with pytest.raises(NotImplementedError, match="headed"):
        platform.register(email="kimi@example.com")
