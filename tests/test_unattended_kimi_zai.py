from __future__ import annotations

import pytest


def test_phone_is_a_first_class_identity_provider():
    from core.base_identity import create_identity_provider, normalize_identity_provider

    assert normalize_identity_provider("phone") == "phone"
    assert normalize_identity_provider("sms") == "phone"

    identity = create_identity_provider("phone").resolve()
    assert identity.identity_provider == "phone"
    assert identity.email == ""


def test_kimi_exposes_phone_and_google_registration(client):
    response = client.get("/api/platforms")
    assert response.status_code == 200
    platform = next(item for item in response.json() if item["name"] == "kimi")

    assert platform["supported_executors"] == ["headed"]
    assert platform["supported_identity_modes"] == ["phone", "oauth_browser"]
    assert platform["supported_oauth_providers"] == ["google"]


def test_kimi_phone_adapter_uses_generic_browser_worker():
    from core.base_platform import RegisterConfig
    from platforms.kimi.plugin import KimiPlatform

    platform = KimiPlatform(
        config=RegisterConfig(
            executor_type="headed",
            extra={"identity_provider": "phone"},
        )
    )
    adapter = platform.build_browser_registration_adapter()

    assert adapter.browser_worker_builder is not None
    assert adapter.browser_register_runner is not None
    assert adapter.use_captcha_for_mailbox is True


def test_kimi_google_unattended_requires_reusable_browser_session(monkeypatch):
    from core.base_platform import RegisterConfig
    from platforms.kimi.plugin import KimiPlatform

    platform = KimiPlatform(
        config=RegisterConfig(
            executor_type="headed",
            extra={
                "identity_provider": "oauth_browser",
                "oauth_provider": "google",
            },
        )
    )

    with pytest.raises(RuntimeError, match="Chrome|CDP|复用"):
        platform.register(email="owner@example.com")


def test_zai_oauth_unattended_requires_reusable_browser_session():
    from core.base_platform import RegisterConfig
    from platforms.zai.plugin import ZAIPlatform

    platform = ZAIPlatform(
        config=RegisterConfig(
            executor_type="headed",
            extra={
                "identity_provider": "oauth_browser",
                "oauth_provider": "google",
            },
        )
    )

    with pytest.raises(RuntimeError, match="Chrome|CDP|复用"):
        platform.register(email="owner@example.com")
