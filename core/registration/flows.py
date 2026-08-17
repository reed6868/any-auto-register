from __future__ import annotations

from contextlib import nullcontext

from .adapters import BrowserRegistrationAdapter, ProtocolMailboxAdapter, ProtocolOAuthAdapter
from .helpers import (
    build_link_callback,
    build_otp_callback,
    build_phone_callbacks,
    ensure_identity_email,
    ensure_mailbox_identity,
    ensure_oauth_browser_reuse,
    ensure_oauth_executor_allowed,
)
from .models import RegistrationArtifacts, RegistrationContext, RegistrationResult


class _LazyCaptchaSolver:
    """Resolve the configured captcha provider only when a page actually needs it."""

    def __init__(self, platform):
        self.platform = platform
        self._solver = None

    def _resolve(self):
        if self._solver is None:
            self._solver = self.platform._make_captcha()
        return self._solver

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        return self._resolve().solve_turnstile(page_url, site_key)


def _build_browser_artifacts(
    ctx: RegistrationContext,
    adapter: BrowserRegistrationAdapter,
    *,
    include_mailbox_callbacks: bool,
    use_captcha: bool,
) -> RegistrationArtifacts:
    artifacts = RegistrationArtifacts(challenge_callback=ctx.challenge_callback)
    if use_captcha:
        # Browser tasks must not fail at startup just because no captcha provider is
        # configured. The provider is resolved lazily when a supported captcha is
        # actually detected; the platform can still fall back to HumanChallenge.
        artifacts.captcha_solver = _LazyCaptchaSolver(ctx.platform)

    if include_mailbox_callbacks and adapter.otp_spec:
        artifacts.otp_callback = build_otp_callback(
            ctx,
            keyword=adapter.otp_spec.keyword,
            timeout=adapter.otp_spec.timeout,
            code_pattern=adapter.otp_spec.code_pattern,
            wait_message=adapter.otp_spec.wait_message,
            success_label=adapter.otp_spec.success_label,
        )
    if include_mailbox_callbacks and adapter.link_spec:
        artifacts.verification_link_callback = build_link_callback(
            ctx,
            keyword=adapter.link_spec.keyword,
            timeout=adapter.link_spec.timeout,
            wait_message=adapter.link_spec.wait_message,
            success_label=adapter.link_spec.success_label,
            preview_chars=adapter.link_spec.preview_chars,
        )

    # Phone providers are lazy: creating this callback does not rent a phone.
    # It only acquires a number when a browser worker encounters a phone step.
    artifacts.phone_callback, artifacts.phone_cleanup = build_phone_callbacks(
        ctx,
        service=ctx.platform_name,
    )
    return artifacts


class BrowserRegistrationFlow:
    def __init__(self, adapter: BrowserRegistrationAdapter):
        self.adapter = adapter

    def run(self, ctx: RegistrationContext) -> RegistrationResult:
        if self.adapter.preflight:
            self.adapter.preflight(ctx)

        if getattr(ctx.identity, "identity_provider", "") == "oauth_browser":
            capability = self.adapter.capability
            ensure_oauth_executor_allowed(
                ctx,
                capability.oauth_allowed_executor_types,
            )
            if capability.oauth_headless_requires_browser_reuse and ctx.executor_type == "headless":
                ensure_oauth_browser_reuse(
                    ctx,
                    f"{ctx.platform_display_name} 无头 OAuth 需要配置 chrome_user_data_dir 或 chrome_cdp_url，以便复用本机已登录的浏览器会话",
                )
            if self.adapter.oauth_runner_with_artifacts:
                artifacts = _build_browser_artifacts(
                    ctx,
                    self.adapter,
                    include_mailbox_callbacks=False,
                    use_captcha=self.adapter.use_captcha_for_oauth,
                )
                try:
                    raw = self.adapter.oauth_runner_with_artifacts(ctx, artifacts)
                    artifacts.raw_result = raw
                    return self.adapter.result_mapper(ctx, raw)
                finally:
                    if artifacts.phone_cleanup:
                        artifacts.phone_cleanup()
            if self.adapter.oauth_runner:
                # Backward-compatible path for existing platform adapters that do
                # not need framework browser artifacts.
                raw = self.adapter.oauth_runner(ctx)
                return self.adapter.result_mapper(ctx, raw)

        if self.adapter.capability.browser_mailbox_requires_email:
            ensure_identity_email(ctx, f"{ctx.platform_display_name} 浏览器模式需要邮箱地址")
        if self.adapter.capability.browser_mailbox_requires_mailbox:
            ensure_mailbox_identity(ctx, f"{ctx.platform_display_name} 浏览器邮箱注册依赖 mailbox provider")

        artifacts = _build_browser_artifacts(
            ctx,
            self.adapter,
            include_mailbox_callbacks=True,
            use_captcha=(
                self.adapter.use_captcha_for_mailbox
                and getattr(ctx.identity, "identity_provider", "") == "mailbox"
            ),
        )

        try:
            worker = self.adapter.browser_worker_builder(ctx, artifacts) if self.adapter.browser_worker_builder else None
            if worker is None or self.adapter.browser_register_runner is None:
                raise RuntimeError(f"{ctx.platform_display_name} 未实现浏览器注册适配器")
            raw = self.adapter.browser_register_runner(worker, ctx, artifacts)
            artifacts.raw_result = raw
            return self.adapter.result_mapper(ctx, raw)
        finally:
            if artifacts.phone_cleanup:
                artifacts.phone_cleanup()


class ProtocolMailboxFlow:
    def __init__(self, adapter: ProtocolMailboxAdapter):
        self.adapter = adapter

    def run(self, ctx: RegistrationContext) -> RegistrationResult:
        if self.adapter.preflight:
            self.adapter.preflight(ctx)
        if self.adapter.capability.protocol_mailbox_requires_email:
            ensure_identity_email(ctx, f"{ctx.platform_display_name} 注册流程依赖 mailbox provider，当前未获取到邮箱账号")
        if self.adapter.capability.protocol_mailbox_requires_mailbox:
            ensure_mailbox_identity(ctx, f"{ctx.platform_display_name} 注册流程依赖 mailbox provider，当前未获取到邮箱账号")

        artifacts = RegistrationArtifacts(challenge_callback=ctx.challenge_callback)
        if self.adapter.use_captcha:
            artifacts.captcha_solver = ctx.platform._make_captcha()
        if self.adapter.otp_spec:
            artifacts.otp_callback = build_otp_callback(
                ctx,
                keyword=self.adapter.otp_spec.keyword,
                timeout=self.adapter.otp_spec.timeout,
                code_pattern=self.adapter.otp_spec.code_pattern,
                wait_message=self.adapter.otp_spec.wait_message,
                success_label=self.adapter.otp_spec.success_label,
            )
        if self.adapter.link_spec:
            artifacts.verification_link_callback = build_link_callback(
                ctx,
                keyword=self.adapter.link_spec.keyword,
                timeout=self.adapter.link_spec.timeout,
                wait_message=self.adapter.link_spec.wait_message,
                success_label=self.adapter.link_spec.success_label,
                preview_chars=self.adapter.link_spec.preview_chars,
            )

        executor_cm = ctx.platform._make_executor() if self.adapter.use_executor else nullcontext(None)
        with executor_cm as executor:
            artifacts.executor = executor
            worker = self.adapter.worker_builder(ctx, artifacts)
            raw = self.adapter.register_runner(worker, ctx, artifacts)
            artifacts.raw_result = raw
            return self.adapter.result_mapper(ctx, raw)


class ProtocolOAuthFlow:
    def __init__(self, adapter: ProtocolOAuthAdapter):
        self.adapter = adapter

    def run(self, ctx: RegistrationContext) -> RegistrationResult:
        if self.adapter.preflight:
            self.adapter.preflight(ctx)
        ensure_oauth_executor_allowed(
            ctx,
            self.adapter.capability.oauth_allowed_executor_types,
        )
        if self.adapter.capability.oauth_headless_requires_browser_reuse and ctx.executor_type == "headless":
            ensure_oauth_browser_reuse(
                ctx,
                f"{ctx.platform_display_name} 无头 OAuth 需要配置 chrome_user_data_dir 或 chrome_cdp_url，以便复用本机已登录的浏览器会话",
            )
        raw = self.adapter.oauth_runner(ctx)
        return self.adapter.result_mapper(ctx, raw)
