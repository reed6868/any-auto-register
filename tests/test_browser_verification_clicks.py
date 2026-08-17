from __future__ import annotations

import re


class _Candidate:
    def __init__(self, page, label: str, matched: bool):
        self.page = page
        self.label = label
        self.matched = matched

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.matched

    def click(self):
        if self.matched:
            self.page.clicked.append(self.label)


class _RolePage:
    """Mimic Playwright's first role match against DOM-order button names."""

    def __init__(self):
        self.buttons = ["Log in with phone number", "Log In"]
        self.clicked = []

    def get_by_role(self, role, name=None):
        if role != "button":
            return _Candidate(self, "", False)
        pattern = name if hasattr(name, "search") else re.compile(str(name or ""))
        for label in self.buttons:
            if pattern.search(label):
                return _Candidate(self, label, True)
        return _Candidate(self, "", False)


def test_click_first_prefers_exact_login_button_over_phone_mode_button():
    from core.registration.browser_verification import _click_first

    page = _RolePage()

    assert _click_first(page, ("Log In",)) is True
    assert page.clicked == ["Log In"]
