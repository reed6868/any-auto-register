from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, OtpSpec, RegistrationCapability, RegistrationResult
from core.registry import register


@register
class ZAIPlatform(BasePlatform):
    name = "zai"
    display_name = "Z.AI"
    version = "1.0.0"
    supported_executors = ["headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google", "github"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
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
            raise NotImplementedError("Z.AI 注册仅支持 headed 浏览器执行器")
        return super().register(email=email, password=password)

    @staticmethod
    def _map_result(ctx, result: dict) -> RegistrationResult:
        cookies = str(result.get("cookies") or "")
        storage_state = str(result.get("storage_state") or "")
        return RegistrationResult(
            email=str(result.get("email") or ctx.identity.email or ""),
            password=str(result.get("password") or ctx.password or ""),
            status=AccountStatus.REGISTERED,
            extra={
                "cookies": cookies,
                "storage_state": storage_state,
                "account_overview": {
                    "valid": bool(cookies or storage_state),
                    "chips": ["Browser Session"],
                },
            },
        )

    def _run_oauth(self, ctx) -> dict:
        from platforms.zai.browser_register import register_with_oauth

        return register_with_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            challenge_callback=ctx.challenge_callback,
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
            timeout=int(ctx.extra.get("browser_oauth_timeout") or 300),
            log_fn=ctx.log,
        )

    def build_browser_registration_adapter(self):
        def _build_worker(ctx, artifacts):
            from platforms.zai.browser_register import ZAIBrowserRegister

            return ZAIBrowserRegister(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                challenge_callback=artifacts.challenge_callback,
                timeout=int(ctx.extra.get("browser_register_timeout") or 300),
                log_fn=ctx.log,
            )

        return BrowserRegistrationAdapter(
            result_mapper=self._map_result,
            browser_worker_builder=_build_worker,
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(
                oauth_allowed_executor_types=("headed",),
            ),
            otp_spec=OtpSpec(wait_message="等待 Z.AI 邮箱验证码..."),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(str(extra.get("cookies") or "").strip() or str(extra.get("storage_state") or "").strip())
