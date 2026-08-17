from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from sqlmodel import Session, select

from core.base_mailbox import create_mailbox
from core.base_platform import RegisterConfig
from core.base_sms import HeroSmsProvider, SmsBowerProvider
from core.db import AccountModel, engine, init_db, save_account
from platforms.kimi.plugin import KimiPlatform
from platforms.zai.plugin import ZAIPlatform


RESULT_PATH = Path("artifacts/live-e2e-result.json")


def _env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name, "") or "").strip()
        if value:
            return value
    return ""


def _log(prefix: str, message: str) -> None:
    text = str(message or "")
    # Never print configured secrets/proxy credentials from this script.
    print(f"[{prefix}] {text}", flush=True)


def _discover_kimi_sms(provider_name: str, api_key: str) -> tuple[str, str]:
    explicit_service = _env("E2E_KIMI_SMS_SERVICE")
    explicit_country = _env("E2E_KIMI_SMS_COUNTRY")
    proxy = _env("E2E_PROXY")

    provider = (
        HeroSmsProvider(api_key, proxy=proxy or None)
        if provider_name == "herosms"
        else SmsBowerProvider(api_key, proxy=proxy or None)
    )

    service = explicit_service
    if not service:
        services = provider.get_services(country=explicit_country or None)
        candidates: list[tuple[str, str]] = []
        for item in services:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("service") or item.get("id") or "").strip()
            label = " ".join(
                str(item.get(key) or "")
                for key in ("name", "title", "eng", "rus", "chn", "service")
            ).lower()
            if code and any(token in label for token in ("kimi", "moonshot", "月之暗面")):
                candidates.append((code, label))
        if not candidates:
            raise RuntimeError(
                "SMS provider 未自动发现 Kimi/Moonshot service code；"
                "请配置 repository secret E2E_KIMI_SMS_SERVICE"
            )
        service = candidates[0][0]

    country = explicit_country
    if not country:
        rows = provider.get_top_countries(service=service)
        usable = [
            row for row in rows
            if isinstance(row, dict)
            and str(row.get("country") or "").strip()
            and int(row.get("count") or 0) > 0
        ]
        if not usable:
            raise RuntimeError(
                "SMS provider 没有返回 Kimi service 的可用国家；"
                "请配置 E2E_KIMI_SMS_COUNTRY 或检查库存"
            )
        usable.sort(key=lambda row: float(row.get("price") or 10**9))
        country = str(usable[0].get("country") or "").strip()

    return service, country


def _sms_config() -> tuple[str, dict]:
    requested = _env("E2E_SMS_PROVIDER").lower()
    generic_key = _env("E2E_SMS_API_KEY")
    hero_key = _env("HEROSMS_API_KEY")
    bower_key = _env("SMSBOWER_API_KEY")

    if requested:
        if requested not in {"herosms", "smsbower"}:
            raise RuntimeError("E2E_SMS_PROVIDER 目前仅允许 herosms 或 smsbower")
        api_key = generic_key or (hero_key if requested == "herosms" else bower_key)
        if not api_key:
            raise RuntimeError(f"{requested} E2E API key 未配置")
        provider_name = requested
    elif hero_key:
        provider_name, api_key = "herosms", hero_key
    elif bower_key:
        provider_name, api_key = "smsbower", bower_key
    else:
        raise RuntimeError(
            "缺少真实接码 Secret：配置 HEROSMS_API_KEY / SMSBOWER_API_KEY，"
            "或 E2E_SMS_PROVIDER + E2E_SMS_API_KEY"
        )

    service, country = _discover_kimi_sms(provider_name, api_key)
    config = {
        "sms_provider": provider_name,
        "sms_service": service,
        "sms_country": country,
        "register_reuse_phone_to_max": False,
        "register_phone_success_max": 1,
    }
    if provider_name == "herosms":
        config.update(
            herosms_api_key=api_key,
            herosms_default_service=service,
            herosms_default_country=country,
        )
    else:
        config.update(
            smsbower_api_key=api_key,
            smsbower_default_service=service,
            smsbower_default_country=country,
        )
    return provider_name, config


def _assert_persisted(platform: str, email: str) -> None:
    with Session(engine) as session:
        row = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.email == email)
        ).first()
    if row is None:
        raise AssertionError(f"{platform} 注册成功但账号未持久化")


def _run_kimi(twocaptcha_key: str, proxy: str) -> dict:
    provider_name, sms_extra = _sms_config()
    messages: list[str] = []

    def logger(message: str) -> None:
        messages.append(str(message or ""))
        _log("KIMI", message)

    extra = {
        "identity_provider": "phone",
        "twocaptcha_key": twocaptcha_key,
        "allow_human_challenge": False,
        "browser_register_timeout": 240,
        **sms_extra,
    }
    config = RegisterConfig(
        executor_type="headed",
        captcha_solver="twocaptcha_api",
        proxy=proxy or None,
        extra=extra,
    )
    platform = KimiPlatform(config=config)
    platform.set_logger(logger)
    account = platform.register()
    if not str(account.email or "").strip():
        raise AssertionError("Kimi 未返回账号标识")
    account_extra = dict(account.extra or {})
    if str(account_extra.get("identity_mode") or "") != "phone":
        raise AssertionError("Kimi live E2E 未走 phone identity")
    if not (str(account_extra.get("cookies") or "").strip() or str(account_extra.get("storage_state") or "").strip()):
        raise AssertionError("Kimi 未捕获已认证 Web session")
    if any("human" in line.lower() and "challenge" in line.lower() for line in messages):
        raise AssertionError("Kimi live E2E 出现 HumanChallenge")

    save_account(account)
    _assert_persisted("kimi", account.email)
    return {
        "ok": True,
        "identity": "phone",
        "sms_provider": provider_name,
        "session_captured": True,
        "persisted": True,
        "human_challenge_count": 0,
    }


def _run_zai(twocaptcha_key: str, proxy: str) -> dict:
    messages: list[str] = []

    def logger(message: str) -> None:
        messages.append(str(message or ""))
        _log("ZAI", message)

    extra = {
        "identity_provider": "mailbox",
        "mail_provider": "tempmail_lol_api",
        "twocaptcha_key": twocaptcha_key,
        "allow_human_challenge": False,
        "browser_register_timeout": 240,
    }
    mailbox = create_mailbox("tempmail_lol_api", extra=extra, proxy=proxy or None)
    config = RegisterConfig(
        executor_type="headed",
        captcha_solver="twocaptcha_api",
        proxy=proxy or None,
        extra=extra,
    )
    platform = ZAIPlatform(config=config, mailbox=mailbox)
    platform.set_logger(logger)
    account = platform.register()
    if not str(account.email or "").strip():
        raise AssertionError("Z.AI 未返回邮箱")
    account_extra = dict(account.extra or {})
    if not (str(account_extra.get("cookies") or "").strip() or str(account_extra.get("storage_state") or "").strip()):
        raise AssertionError("Z.AI 未捕获已认证 Web session")
    if any("human" in line.lower() and "challenge" in line.lower() for line in messages):
        raise AssertionError("Z.AI live E2E 出现 HumanChallenge")

    save_account(account)
    _assert_persisted("zai", account.email)
    return {
        "ok": True,
        "identity": "mailbox",
        "mailbox_provider": "tempmail_lol_api",
        "session_captured": True,
        "persisted": True,
        "human_challenge_count": 0,
    }


def main() -> int:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result: dict = {"ok": False, "kimi": {"ok": False}, "zai": {"ok": False}}
    try:
        twocaptcha_key = _env("E2E_TWOCAPTCHA_KEY", "TWOCAPTCHA_KEY")
        if not twocaptcha_key:
            raise RuntimeError(
                "缺少 E2E_TWOCAPTCHA_KEY/TWOCAPTCHA_KEY；"
                "live gate 必须具备 GeeTest/Turnstile 无人值守能力"
            )
        proxy = _env("E2E_PROXY")

        init_db()
        result["kimi"] = _run_kimi(twocaptcha_key, proxy)
        result["zai"] = _run_zai(twocaptcha_key, proxy)
        result["ok"] = bool(result["kimi"].get("ok") and result["zai"].get("ok"))
        print("LIVE_E2E_GATE=PASS", flush=True)
        return 0
    except Exception as exc:
        result["error_type"] = exc.__class__.__name__
        result["error"] = str(exc)[:500]
        print(f"LIVE_E2E_GATE=FAIL: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1
    finally:
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
