from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlmodel import Session, select

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import RegisterConfig
from core.db import AccountModel, engine, init_db, save_account
from core.registration import browser_verification as browser_verification_module
from core.registration import helpers as registration_helpers
from platforms.kimi import browser_auth as kimi_browser
from platforms.kimi.plugin import KimiPlatform
from platforms.zai import browser_register as zai_browser
from platforms.zai.plugin import ZAIPlatform


RESULT_PATH = Path("artifacts/hermetic-e2e-result.json")
KIMI_PHONE = "+15550001111"
KIMI_SMS_CODE = "246810"
ZAI_EMAIL = "hermetic-e2e@example.test"
ZAI_MAIL_CODE = "135790"


KIMI_HTML = r"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Kimi Hermetic Fixture</title></head>
<body>
  <main id="root">
    <h1>Log in to chat with Kimi for free</h1>
    <button id="phone-mode">Log in with phone number</button>
    <div id="stage"></div>
  </main>
  <script>
    const stage = document.getElementById('stage');
    window.initGeetest4 = function(config, callback) {
      const successCallbacks = [];
      const instance = {
        onSuccess(fn) { successCallbacks.push(fn); return instance; },
        getValidate() { return {}; }
      };
      callback(instance);
      window.__fixtureGeetestSuccess = () => successCallbacks.forEach(fn => fn());
      return instance;
    };

    document.getElementById('phone-mode').addEventListener('click', () => {
      stage.innerHTML = `
        <input id="dial" aria-label="country dial code" value="+86" />
        <input id="phone" type="tel" name="phone" placeholder="Phone number" />
        <button id="send">Send</button>`;
      document.getElementById('send').addEventListener('click', () => {
        window.initGeetest4({captchaId: 'hermetic-kimi-geetest'}, (captcha) => {
          captcha.onSuccess(() => {
            stage.innerHTML = `
              <input id="otp" autocomplete="one-time-code" name="verification_code" placeholder="Verification code" />
              <button id="login">Log In</button>`;
            document.getElementById('login').addEventListener('click', () => {
              if (document.getElementById('otp').value !== '246810') return;
              localStorage.setItem('phone', '+15550001111');
              localStorage.setItem('session', 'hermetic-kimi-session');
              location.href = '/kimi/app';
            });
          });
        });
      });
    });
  </script>
</body>
</html>"""

KIMI_APP_HTML = r"""<!doctype html><html><body><h1>New Chat</h1><p>Kimi Claw</p></body></html>"""

ZAI_HTML = r"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Z.AI Hermetic Fixture</title></head>
<body>
  <main>
    <h1 id="heading">Welcome to Z.AI</h1>
    <div id="stage"><button id="email-mode">Continue with Email</button></div>
  </main>
  <script>
    const stage = document.getElementById('stage');
    const showEmail = () => {
      stage.innerHTML = `<input id="email" type="email" name="email" /><button id="email-next">Continue</button>`;
      document.getElementById('email-next').addEventListener('click', showPassword);
    };
    const showPassword = () => {
      stage.innerHTML = `<p>Create Account</p><input id="password" type="password" name="password" /><button id="create">Create Account</button>`;
      document.getElementById('create').addEventListener('click', showTurnstile);
    };
    const showTurnstile = () => {
      stage.innerHTML = `
        <div id="turnstile" data-sitekey="hermetic-zai-sitekey" data-callback="turnstileDone"></div>
        <input type="hidden" name="cf-turnstile-response" />`;
    };
    window.turnstileDone = () => {
      stage.innerHTML = `
        <p>Verification code</p>
        <input id="otp" autocomplete="one-time-code" name="code" inputmode="numeric" />
        <button id="verify">Verify</button>`;
      document.getElementById('verify').addEventListener('click', () => {
        if (document.getElementById('otp').value !== '135790') return;
        localStorage.setItem('email', 'hermetic-e2e@example.test');
        localStorage.setItem('session', 'hermetic-zai-session');
        location.href = '/zai/app';
      });
    };
    document.getElementById('email-mode').addEventListener('click', showEmail);
  </script>
</body>
</html>"""

ZAI_APP_HTML = r"""<!doctype html><html><body><h1>Z.AI Chat</h1><p>Authenticated workspace</p></body></html>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib hook name
        path = urlparse(self.path).path
        if path in {"/kimi", "/kimi/"}:
            self._html(KIMI_HTML)
            return
        if path == "/kimi/app":
            self._html(KIMI_APP_HTML)
            return
        if path in {"/zai/auth", "/zai/auth/"}:
            self._html(ZAI_HTML)
            return
        if path == "/zai/app":
            self._html(ZAI_APP_HTML)
            return
        self.send_response(404)
        self.end_headers()

    def _html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return


class HermeticCaptcha:
    def __init__(self):
        self.geetest_calls = 0
        self.turnstile_calls = 0

    def solve_geetest(self, page_url: str, params: dict, *, proxy: str = "") -> dict:
        self.geetest_calls += 1
        assert str(params.get("captcha_id") or "") == "hermetic-kimi-geetest"
        return {
            "version": 4,
            "lot_number": "hermetic-lot",
            "captcha_output": "hermetic-output",
            "pass_token": "hermetic-pass",
            "gen_time": "1",
        }

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        self.turnstile_calls += 1
        assert site_key == "hermetic-zai-sitekey"
        return "hermetic-turnstile-token"


class HermeticPhoneController:
    def __init__(self):
        self.activation = SimpleNamespace(
            activation_id="hermetic-activation",
            phone_number=KIMI_PHONE,
            country="us",
            metadata={"country_phone_code": "1"},
        )
        self.calls = 0
        self.send_confirmed = False
        self.reported_success = False
        self.cleaned = False

    def __call__(self) -> str:
        self.calls += 1
        if self.calls == 1:
            return KIMI_PHONE
        if self.calls == 2:
            return KIMI_SMS_CODE
        raise AssertionError("phone callback called more than expected")

    def mark_send_succeeded(self) -> None:
        self.send_confirmed = True

    def report_success(self) -> None:
        self.reported_success = True


class HermeticMailbox(BaseMailbox):
    def __init__(self):
        self.wait_calls = 0

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=ZAI_EMAIL, account_id="hermetic-mailbox")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return {"before"}

    def wait_for_code(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        self.wait_calls += 1
        return ZAI_MAIL_CODE


def _patch_browser_targets(base_url: str) -> None:
    kimi_browser.KIMI_URL = f"{base_url}/kimi/"
    zai_browser.AUTH_URL = f"{base_url}/zai/auth"

    kimi_detect = kimi_browser.detect_auth_surface
    zai_detect = zai_browser.detect_auth_surface

    def detect_kimi(*, url: str, text: str):
        path = urlparse(str(url or "")).path
        return kimi_detect(url=f"https://www.kimi.com{path}", text=text)

    def detect_zai(*, url: str, text: str, input_types: tuple[str, ...]):
        path = urlparse(str(url or "")).path
        mapped_path = path.removeprefix("/zai") or "/"
        return zai_detect(url=f"https://chat.z.ai{mapped_path}", text=text, input_types=input_types)

    kimi_browser.detect_auth_surface = detect_kimi
    zai_browser.detect_auth_surface = detect_zai

    # Hermetic pages deliberately live on 127.0.0.1. Domain isolation is
    # covered by unit tests; this gate keeps the production verification state
    # machine but allows it to operate on the local fixture origin.
    browser_verification_module.BrowserVerificationSupport._is_allowed_page = lambda self, page: True


def _assert_persisted(platform: str, identity: str) -> None:
    with Session(engine) as session:
        row = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.email == identity)
        ).first()
        if row is None:
            raise AssertionError(f"{platform} account was not persisted")


def _run_kimi(captcha: HermeticCaptcha) -> dict:
    controller = HermeticPhoneController()

    def fake_phone_factory(provider_key, config, *, service, country="", log_fn=None):
        def cleanup():
            controller.cleaned = True
        return controller, cleanup

    registration_helpers.create_phone_callbacks = fake_phone_factory

    config = RegisterConfig(
        executor_type="headed",
        captcha_solver="hermetic",
        extra={
            "identity_provider": "phone",
            "sms_provider": "hermetic",
            "allow_human_challenge": False,
            "browser_register_timeout": 30,
        },
    )
    platform = KimiPlatform(config=config)
    platform._make_captcha = lambda **kwargs: captcha
    account = platform.register()

    if account.email != KIMI_PHONE:
        raise AssertionError(f"Kimi returned unexpected identity: {account.email}")
    if not controller.send_confirmed:
        raise AssertionError("Kimi did not confirm SMS send after reaching OTP stage")
    if not controller.reported_success:
        raise AssertionError("Kimi did not report SMS activation success")
    if not controller.cleaned:
        raise AssertionError("Kimi did not execute SMS cleanup")
    if captcha.geetest_calls != 1:
        raise AssertionError(f"Kimi expected one GeeTest solve, got {captcha.geetest_calls}")
    if not platform.check_valid(account):
        raise AssertionError("Kimi account is not structurally valid")

    save_account(account)
    _assert_persisted("kimi", KIMI_PHONE)
    return {
        "ok": True,
        "identity": "phone",
        "geetest_calls": captcha.geetest_calls,
        "sms_send_confirmed": True,
        "sms_success_reported": True,
        "sms_cleanup": True,
        "persisted": True,
    }


def _run_zai(captcha: HermeticCaptcha) -> dict:
    mailbox = HermeticMailbox()
    config = RegisterConfig(
        executor_type="headed",
        captcha_solver="hermetic",
        extra={
            "identity_provider": "mailbox",
            "allow_human_challenge": False,
            "browser_register_timeout": 30,
        },
    )
    platform = ZAIPlatform(config=config, mailbox=mailbox)
    platform._make_captcha = lambda **kwargs: captcha
    account = platform.register()

    if account.email != ZAI_EMAIL:
        raise AssertionError(f"Z.AI returned unexpected email: {account.email}")
    if mailbox.wait_calls != 1:
        raise AssertionError(f"Z.AI expected one mailbox OTP poll, got {mailbox.wait_calls}")
    if captcha.turnstile_calls != 1:
        raise AssertionError(f"Z.AI expected one Turnstile solve, got {captcha.turnstile_calls}")
    if not platform.check_valid(account):
        raise AssertionError("Z.AI account is not structurally valid")

    save_account(account)
    _assert_persisted("zai", ZAI_EMAIL)
    return {
        "ok": True,
        "identity": "mailbox",
        "mailbox_otp_calls": mailbox.wait_calls,
        "turnstile_calls": captcha.turnstile_calls,
        "persisted": True,
    }


def main() -> int:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_path = os.getenv("ACCOUNT_MANAGER_DATABASE_URL", "")
    result = {"ok": False, "database": bool(db_path), "kimi": {"ok": False}, "zai": {"ok": False}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        _patch_browser_targets(base_url)
        init_db()
        captcha = HermeticCaptcha()
        result["kimi"] = _run_kimi(captcha)
        result["zai"] = _run_zai(captcha)
        result["ok"] = bool(result["kimi"]["ok"] and result["zai"]["ok"])
        print("HERMETIC_E2E_GATE=PASS", flush=True)
        return 0
    except Exception as exc:
        result["error_type"] = exc.__class__.__name__
        result["error"] = str(exc)[:500]
        print(f"HERMETIC_E2E_GATE=FAIL: {exc}", flush=True)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
