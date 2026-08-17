from __future__ import annotations

from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, RegistrationCapability, RegistrationResult
from core.registry import register


def _enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "是"}


@register
class KimiPlatform(BasePlatform):
    name = "kimi"
    display_name = "Kimi"
    version = "1.1.0"
    supported_executors = ["headed"]
    supported_identity_modes = ["phone", "oauth_browser"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox=None):
        requested = config or RegisterConfig()
        self._requested_executor_type = str(requested.executor_type or "protocol")
        init_config = requested
        if self._requested_executor_type not in type(self).supported_executors:
            init_config = RegisterConfig(
                executor_type="headed",
                captcha_solver=requested.captcha_solver,
                proxy=requested.proxy,
                extra=dict(requested.extra or {}),
            )
        super().__init__(init_config)
        self.mailbox = mailbox

    def register(self, email: str = None, password: str = None) -> Account:
        if self._requested_executor_type not in self.supported_executors:
            raise NotImplementedError("Kimi 注册仅支持 headed 浏览器执行器")
        account = super().register(email=email, password=password)
        # Runtime-only context for immediate CPA/device authorization. This is a
        # dynamic attribute, not part of the persisted Account schema.
        setattr(account, "_registration_proxy", str(self.config.proxy or ""))
        return account

    def _should_require_identity_email(self) -> bool:
        return self._get_identity_provider_name() not in {"phone", "oauth_browser"}

    def _browser_registration_label(self, identity) -> str:
        if getattr(identity, "identity_provider", "") == "phone":
            return "(phone provider)"
        return super()._browser_registration_label(identity)

    @staticmethod
    def _map_result(ctx, result: dict) -> RegistrationResult:
        cookies = str(result.get("cookies") or "")
        storage_state = str(result.get("storage_state") or "")
        identity_mode = str(result.get("identity_mode") or getattr(ctx.identity, "identity_provider", "") or "")
        phone_number = str(result.get("phone_number") or "")
        chips = ["Browser Session"]
        if identity_mode == "phone":
            chips.insert(0, "Phone OTP")
        else:
            chips.insert(0, "Google OAuth")
        return RegistrationResult(
            email=str(result.get("email") or ctx.identity.email or phone_number or ""),
            password="",
            status=AccountStatus.REGISTERED,
            extra={
                "cookies": cookies,
                "storage_state": storage_state,
                "phone_number": phone_number,
                "identity_mode": identity_mode,
                "account_overview": {
                    "valid": bool(cookies or storage_state),
                    "chips": chips,
                },
            },
        )

    def _challenge_callback(self, ctx, artifacts):
        if _enabled(ctx.extra.get("allow_human_challenge")):
            return artifacts.challenge_callback
        return None

    def _run_google_oauth(self, ctx, artifacts) -> dict:
        if ctx.identity.oauth_provider and ctx.identity.oauth_provider != "google":
            raise RuntimeError("Kimi 当前仅支持 Google OAuth")
        if not (str(ctx.identity.chrome_user_data_dir or "").strip() or str(ctx.identity.chrome_cdp_url or "").strip()):
            raise RuntimeError("Kimi 无人值守 Google OAuth 需要配置已登录的 Chrome Profile 或 Chrome CDP 以复用 Google 会话")
        from platforms.kimi.browser_auth import register_with_google
        return register_with_google(
            proxy=ctx.proxy,
            email_hint=ctx.identity.email,
            challenge_callback=self._challenge_callback(ctx, artifacts),
            captcha_solver=artifacts.captcha_solver,
            phone_callback=artifacts.phone_callback,
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
            timeout=int(ctx.extra.get("browser_oauth_timeout") or 300),
            log_fn=ctx.log,
        )

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return ""

    def build_browser_registration_adapter(self):
        def _build_worker(ctx, artifacts):
            from platforms.kimi.browser_auth import KimiPhoneRegister
            return KimiPhoneRegister(
                proxy=ctx.proxy,
                challenge_callback=self._challenge_callback(ctx, artifacts),
                captcha_solver=artifacts.captcha_solver,
                phone_callback=artifacts.phone_callback,
                timeout=int(ctx.extra.get("browser_register_timeout") or 300),
                log_fn=ctx.log,
            )

        return BrowserRegistrationAdapter(
            result_mapper=self._map_result,
            browser_worker_builder=_build_worker,
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(),
            oauth_runner_with_artifacts=self._run_google_oauth,
            use_captcha_for_mailbox=True,
            use_captcha_for_oauth=True,
            capability=RegistrationCapability(
                oauth_allowed_executor_types=("headed",),
                browser_mailbox_requires_email=False,
                browser_mailbox_requires_mailbox=False,
            ),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        overview = extra.get("account_overview") if isinstance(extra.get("account_overview"), dict) else {}
        legacy_extra = overview.get("legacy_extra") if isinstance(overview.get("legacy_extra"), dict) else {}
        return bool(
            str(extra.get("cookies") or "").strip()
            or str(extra.get("storage_state") or "").strip()
            or str(legacy_extra.get("storage_state") or "").strip()
        )
