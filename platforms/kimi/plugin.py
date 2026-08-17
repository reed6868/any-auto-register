from __future__ import annotations

from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, RegistrationCapability, RegistrationResult
from core.registry import register


@register
class KimiPlatform(BasePlatform):
    name = "kimi"
    display_name = "Kimi"
    version = "1.0.0"
    supported_executors = ["headed"]
    supported_identity_modes = ["oauth_browser"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    @staticmethod
    def _map_result(ctx, result: dict) -> RegistrationResult:
        cookies = str(result.get("cookies") or "")
        storage_state = str(result.get("storage_state") or "")
        return RegistrationResult(
            email=str(result.get("email") or ctx.identity.email or ""),
            password="",
            status=AccountStatus.REGISTERED,
            extra={
                "cookies": cookies,
                "storage_state": storage_state,
                "account_overview": {
                    "valid": bool(cookies or storage_state),
                    "chips": ["Google OAuth", "Browser Session"],
                },
            },
        )

    def _run_google_oauth(self, ctx) -> dict:
        if ctx.identity.oauth_provider and ctx.identity.oauth_provider != "google":
            raise RuntimeError("Kimi 当前仅支持 Google OAuth")

        from platforms.kimi.browser_auth import register_with_google

        return register_with_google(
            proxy=ctx.proxy,
            email_hint=ctx.identity.email,
            challenge_callback=ctx.challenge_callback,
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
            timeout=int(ctx.extra.get("browser_oauth_timeout") or 300),
            log_fn=ctx.log,
        )

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return ""

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=self._map_result,
            oauth_runner=self._run_google_oauth,
            capability=RegistrationCapability(
                oauth_allowed_executor_types=("headed",),
                browser_mailbox_requires_email=False,
                browser_mailbox_requires_mailbox=False,
            ),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(str(extra.get("cookies") or "").strip() or str(extra.get("storage_state") or "").strip())
