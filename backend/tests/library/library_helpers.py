"""Helpers for the experience library tests.

ManualClock moves time by hand; forms_in() reads the forms of a rendered page
the way a browser would submit them; Browser is one user's cookie jar on a
shared app, so two Browser objects behave like two separate people.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser

from fastapi.testclient import TestClient

LOCAL_BASE_URL = "http://127.0.0.1"


class ManualClock:
    def __init__(self, start: datetime):
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


@dataclass
class Form:
    action: str
    method: str
    fields: dict[str, str] = field(default_factory=dict)
    buttons: list[str] = field(default_factory=list)


class _FormReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._form: Form | None = None
        self._textarea: str | None = None
        self._button: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "form":
            self._form = Form(action=attrs.get("action", ""), method=attrs.get("method", "get"))
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input" and attrs.get("name"):
            if attrs.get("type") in ("checkbox", "radio") and "checked" not in attrs:
                return

            self._form.fields.setdefault(attrs["name"], attrs.get("value") or "")
        elif tag == "textarea" and attrs.get("name"):
            self._textarea = attrs["name"]
            self._form.fields[self._textarea] = ""
        elif tag == "button":
            self._button = []

    def handle_data(self, data):
        if self._form is not None and self._textarea is not None:
            self._form.fields[self._textarea] += data

        if self._button is not None:
            self._button.append(data)

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None
        elif tag == "button" and self._button is not None and self._form is not None:
            self._form.buttons.append("".join(self._button).strip())
            self._button = None
        elif tag == "form":
            self._form = None


def forms_in(html: str) -> list[Form]:
    reader = _FormReader()
    reader.feed(html)

    return reader.forms


def form_for(html: str, action: str) -> Form:
    """The form that posts to action. A page may also have a search form
    (method get) with the same action, so posting forms come first."""
    candidates = [form for form in forms_in(html) if form.action == action]
    matches = [form for form in candidates if form.method.lower() == "post"] or candidates

    if not matches:
        actions = [form.action for form in forms_in(html)]
        raise AssertionError(f"no form posts to {action!r}; forms on the page: {actions}")

    return matches[0]


def item_id_from(location: str) -> str:
    match = re.match(r"^/experiences/([0-9a-f-]{36})", location)
    assert match, location

    return match.group(1)


class Browser:
    """One person's browser: its own cookies, signed in as one synthetic user."""

    def __init__(self, app, client_host: str = "127.0.0.1"):
        self.client = TestClient(
            app,
            base_url=LOCAL_BASE_URL,
            client=(client_host, 50001),
            follow_redirects=False,
        )

    @classmethod
    def signed_in(cls, app, label: str) -> "Browser":
        browser = cls(app)
        browser.sign_in(label)

        return browser

    def sign_in(self, label: str) -> None:
        page = self.client.get("/login")
        assert page.status_code == 200
        chosen = [
            form
            for form in forms_in(page.text)
            if form.action == "/login/dev" and any(label in text for text in form.buttons)
        ]
        assert chosen, f"no dev login button for {label}"
        response = self.client.post("/login/dev", data=chosen[0].fields)
        assert response.status_code == 303, response.text

    def get(self, path: str, **kwargs):
        return self.client.get(path, **kwargs)

    def post(self, path: str, data: dict | None = None, **kwargs):
        return self.client.post(path, data=data or {}, **kwargs)

    def form(self, page_path: str, action: str) -> Form:
        page = self.get(page_path)
        assert page.status_code == 200, (page_path, page.status_code)

        return form_for(page.text, action)

    def submit(self, page_path: str, action: str, **changes):
        """Open a page, change some fields of one of its forms, and submit it."""
        form = self.form(page_path, action)

        return self.post(action, {**form.fields, **changes})

    def csrf_token(self) -> str:
        return self.form("/experiences", "/logout").fields["csrf_token"]

    def create(self, **content) -> str:
        response = self.submit("/experiences/new", "/experiences", **content)
        assert response.status_code == 303, response.text

        return item_id_from(response.headers["location"])

    def revision(self, item_id: str) -> int:
        form = self.form(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft")

        return int(form.fields["expected_revision"])


def all_routes(app) -> list:
    """Every route of the app, looking inside included routers."""
    found = []

    def walk(routes):
        for route in routes:
            included = getattr(route, "original_router", None)

            if included is not None:
                walk(included.routes)
            else:
                found.append(route)

    walk(app.routes)

    return found
