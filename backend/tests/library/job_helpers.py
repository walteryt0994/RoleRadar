"""Shared pieces of the U2-A2 job analysis page tests.

Everything is synthetic. The simulated service of the app is replaced where a
test needs to see or steer what it is asked: RecordingProvider keeps each
prompt, and Gate holds an answer until the test lets it go, which is how a
test cancels a task or changes material while a request is out.
"""
import re
import threading
import time

from app.ai_provider import AIProvider, GenerationResult
from app.job_analysis.simulation import SIMULATION_MODEL, SimulatedProvider
from library_helpers import forms_in

POSTING = """数据分析实习生（合成岗位）
岗位要求：
- 熟练使用 Python 进行数据处理
- 掌握 SQL 查询
- 2027 年及以后毕业
加分项：
- 了解 Docker
工作地点：上海，每周可远程两天
"""


class InlineExecutor:
    """Runs a started task at once, in the request that started it."""

    def submit(self, function, *args):
        function(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class RecordingProvider(AIProvider):
    """The app's simulated service, keeping every prompt it is given."""

    def __init__(self, inner=None):
        self.inner = inner or SimulatedProvider()
        self.prompts = []

    def identity(self):
        return self.inner.identity()

    def generate_text(self, prompt, **options):
        self.prompts.append(prompt)

        return self.inner.generate_text(prompt, **options)


class Gate(RecordingProvider):
    """Holds each answer until the test releases it; tells the test when a request arrives."""

    def __init__(self, inner=None):
        super().__init__(inner)
        self.arrived = threading.Semaphore(0)
        self.release = threading.Semaphore(0)

    def generate_text(self, prompt, **options):
        self.prompts.append(prompt)
        self.arrived.release()
        assert self.release.acquire(timeout=10), "the test never released the answer"

        return self.inner.generate_text(prompt, **options)


class FailingProvider(RecordingProvider):
    """Raises the given error for the request at the given position (1 = the parse)."""

    def __init__(self, error, at=1):
        super().__init__()
        self.error, self.at = error, at

    def generate_text(self, prompt, **options):
        self.prompts.append(prompt)

        if len(self.prompts) == self.at:
            raise self.error

        return self.inner.generate_text(prompt, **options)


class AnsweringProvider(RecordingProvider):
    """Gives the simulated parse, then the given text as the analysis answer."""

    def __init__(self, answer: str):
        super().__init__()
        self.answer = answer

    def generate_text(self, prompt, **options):
        self.prompts.append(prompt)

        if len(self.prompts) == 1:
            return self.inner.generate_text(prompt, **options)

        return GenerationResult(
            text=self.answer, provider="simulation", requested_model=SIMULATION_MODEL,
            model=SIMULATION_MODEL, latency_seconds=0.0,
        )


def use_provider(app, provider):
    app.state.job_analysis.make_provider = lambda task: provider

    return provider


def confirmed(browser, category="project", **content) -> str:
    """Save and confirm a synthetic experience through the pages; return its id."""
    response = browser.submit(f"/experiences/new?kind={category}", "/experiences", **content)
    assert response.status_code == 303, response.text
    item_id = response.headers["location"].split("?")[0].rsplit("/", 1)[1]
    assert browser.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303

    return item_id


def offered(browser) -> dict[str, str]:
    """The experiences the input page offers: id -> the value of its checkbox."""
    page = browser.get("/job-analysis").text

    return {value.split(":")[0]: value for value in re.findall(r'name="source" value="([^"]+)"', page)}


def review(browser, item_ids, posting=POSTING, *, use_preferences=False, values=None):
    """Post the input page; returns the response (303 to the task, or the page again)."""
    values = values or offered(browser)
    data = {"csrf_token": browser.csrf_token(), "posting": posting, "source": [values[item] for item in item_ids]}

    if use_preferences:
        data["use_preferences"] = "yes"

    return browser.post("/job-analysis/review", data)


def task_of(response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/job-analysis/tasks/"), location

    return location.split("?")[0]


def confirm_form(browser, task_url):
    page = browser.get(task_url).text
    form = next(form for form in forms_in(page) if form.action == f"{task_url}/confirm")
    sends = re.findall(r'name="send" value="([^"]+)" checked', page)

    return form, sends, page


def start(browser, task_url, *, sends=None, action="start", fingerprint=None, csrf=None):
    form, ticked, _ = confirm_form(browser, task_url)
    data = {
        "csrf_token": csrf if csrf is not None else form.fields["csrf_token"],
        "fingerprint": fingerprint if fingerprint is not None else form.fields["fingerprint"],
        "action": action,
        "send": ticked if sends is None else sends,
    }

    return browser.post(f"{task_url}/confirm", data)


def wait_until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if condition():
            return

        time.sleep(0.01)

    raise AssertionError("timed out waiting")


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())
