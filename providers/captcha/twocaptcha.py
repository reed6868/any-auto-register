"""2Captcha — cloud Turnstile and GeeTest solver."""
from urllib.parse import urlparse

from core.base_captcha import BaseCaptcha
from providers.registry import register_provider


@register_provider("captcha", "twocaptcha_api")
class TwoCaptcha(BaseCaptcha):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.api = "https://2captcha.com"
        self.api_v2 = "https://api.2captcha.com"

    @classmethod
    def from_config(cls, config: dict) -> 'TwoCaptcha':
        api_key = str(config.get("twocaptcha_key", "") or "")
        if not api_key:
            raise RuntimeError("2Captcha Key 未配置")
        return cls(api_key)

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        import time
        import requests

        create = requests.post(
            f"{self.api}/in.php",
            data={
                "key": self.api_key,
                "method": "turnstile",
                "sitekey": site_key,
                "pageurl": page_url,
                "json": 1,
            },
            timeout=30,
        )
        create.raise_for_status()
        payload = create.json()
        if payload.get("status") != 1:
            raise RuntimeError(f"2Captcha 创建任务失败: {payload}")
        task_id = payload.get("request")
        if not task_id:
            raise RuntimeError(f"2Captcha 未返回任务 ID: {payload}")

        for _ in range(60):
            time.sleep(3)
            result = requests.get(
                f"{self.api}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                },
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            if data.get("status") == 1:
                return str(data.get("request") or "")
            if data.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                raise RuntimeError(f"2Captcha 错误: {data}")
        raise TimeoutError("2Captcha Turnstile 超时")

    @staticmethod
    def _proxy_task_fields(proxy: str) -> dict:
        raw = str(proxy or "").strip()
        if not raw:
            return {}
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https", "socks4", "socks5"} or not parsed.hostname or not parsed.port:
            return {}
        proxy_type = "http" if parsed.scheme in {"http", "https"} else parsed.scheme
        result = {
            "proxyType": proxy_type,
            "proxyAddress": parsed.hostname,
            "proxyPort": parsed.port,
        }
        if parsed.username:
            result["proxyLogin"] = parsed.username
        if parsed.password:
            result["proxyPassword"] = parsed.password
        return result

    def solve_geetest(self, page_url: str, params: dict, *, proxy: str = "") -> dict:
        """Solve standard GeeTest v3/v4 using 2Captcha API v2."""
        import time
        import requests

        version = int(params.get("version") or 4)
        proxy_fields = self._proxy_task_fields(proxy)
        task_type = "GeeTestTask" if proxy_fields else "GeeTestTaskProxyless"
        task: dict = {
            "type": task_type,
            "websiteURL": page_url,
            **proxy_fields,
        }
        if version == 4:
            captcha_id = str(params.get("captcha_id") or params.get("captchaId") or "").strip()
            if not captcha_id:
                raise RuntimeError("GeeTest v4 缺少 captcha_id")
            task.update({
                "version": 4,
                "initParameters": {"captcha_id": captcha_id},
            })
        else:
            gt = str(params.get("gt") or "").strip()
            challenge = str(params.get("challenge") or "").strip()
            if not gt or not challenge:
                raise RuntimeError("GeeTest v3 缺少 gt/challenge")
            task.update({"gt": gt, "challenge": challenge})
            api_server = str(params.get("api_server") or params.get("apiServer") or "").strip()
            if api_server:
                task["apiServer"] = api_server

        create = requests.post(
            f"{self.api_v2}/createTask",
            json={"clientKey": self.api_key, "task": task},
            timeout=30,
        )
        create.raise_for_status()
        payload = create.json()
        if int(payload.get("errorId") or 0) != 0 or not payload.get("taskId"):
            raise RuntimeError(f"2Captcha GeeTest 创建任务失败: {payload}")
        task_id = payload["taskId"]

        for _ in range(60):
            time.sleep(3)
            result = requests.post(
                f"{self.api_v2}/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            if int(data.get("errorId") or 0) != 0:
                raise RuntimeError(f"2Captcha GeeTest 错误: {data}")
            if data.get("status") == "ready":
                solution = dict(data.get("solution") or {})
                if version == 4:
                    required = ("lot_number", "pass_token", "gen_time", "captcha_output")
                    if not all(str(solution.get(key) or "").strip() for key in required):
                        raise RuntimeError(f"2Captcha GeeTest v4 返回字段不完整: {solution}")
                    return {"version": 4, **solution}
                return {
                    "version": 3,
                    "geetest_challenge": str(solution.get("challenge") or ""),
                    "geetest_validate": str(solution.get("validate") or ""),
                    "geetest_seccode": str(solution.get("seccode") or ""),
                }
        raise TimeoutError("2Captcha GeeTest 超时")

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError
