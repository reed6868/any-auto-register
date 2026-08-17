from __future__ import annotations


class _Field:
    def __init__(self, events):
        self.events = events

    def is_visible(self, timeout=0):
        return True

    def fill(self, value):
        self.events.append(("fill", value))


class _Fields:
    def __init__(self, events):
        self.events = events

    def count(self):
        return 1

    def nth(self, index):
        assert index == 0
        return _Field(self.events)


class _Button:
    def __init__(self, events, label):
        self.events = events
        self.label = label

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.label == "Create Account"

    def click(self):
        self.events.append(("click", self.label))


class _Page:
    def __init__(self):
        self.events = []

    def locator(self, selector):
        assert selector == 'input[type="password"]'
        return _Fields(self.events)

    def get_by_role(self, role, name=None):
        label = "Create Account"
        matched = bool(name and name.search(label))
        return _Button(self.events, label if matched else "")

    def get_by_text(self, label, exact=False):
        return _Button(self.events, label)


def test_submit_password_surface_fills_before_create_account_click():
    from platforms.zai.browser_register import _submit_password_surface

    page = _Page()

    _submit_password_surface(page, "Secret-123")

    assert page.events == [
        ("fill", "Secret-123"),
        ("click", "Create Account"),
    ]
