from __future__ import annotations


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_twocaptcha_geetest_v4_uses_api_v2_and_registration_proxy(monkeypatch):
    import requests
    import time
    from providers.captcha.twocaptcha import TwoCaptcha

    calls = []
    responses = iter([
        FakeResponse({"errorId": 0, "taskId": 123}),
        FakeResponse({
            "errorId": 0,
            "status": "ready",
            "solution": {
                "captcha_id": "captcha-id-1",
                "lot_number": "lot",
                "pass_token": "pass",
                "gen_time": "123",
                "captcha_output": "output",
            },
        }),
    ])

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    result = TwoCaptcha("key").solve_geetest(
        "https://www.kimi.com/",
        {"version": 4, "captcha_id": "captcha-id-1"},
        proxy="socks5://user:pass@127.0.0.1:1080",
    )

    assert result["lot_number"] == "lot"
    assert calls[0][0] == "https://api.2captcha.com/createTask"
    task = calls[0][1]["json"]["task"]
    assert task["type"] == "GeeTestTask"
    assert task["websiteURL"] == "https://www.kimi.com/"
    assert task["version"] == 4
    assert task["initParameters"] == {"captcha_id": "captcha-id-1"}
    assert task["proxyType"] == "socks5"
    assert task["proxyAddress"] == "127.0.0.1"
    assert task["proxyPort"] == 1080
    assert task["proxyLogin"] == "user"
    assert task["proxyPassword"] == "pass"
    assert calls[1][0] == "https://api.2captcha.com/getTaskResult"
