// U3-A3 R1 (F1): the running page's script, executed offline.
//
// The real app/library/static/job_analysis.js runs in a fresh Node vm context for each case, against small event and
// DOM doubles: timers run only when a case says so, every status request waits until the case answers or fails it,
// and the leave dialog closes the way browsers close it, its "close" event coming afterwards. No browser, server,
// network or third-party package. Run with: node --test running_page.test.cjs (tests/library/test_running_page_script.py
// does, inside the isolated run).
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");

// RUNNING_PAGE_SCRIPT names another copy of the script, only to show these cases fail on an earlier one.
const SCRIPT = fs.readFileSync(
  process.env.RUNNING_PAGE_SCRIPT || path.join(__dirname, "..", "..", "..", "app", "library", "static", "job_analysis.js"),
  "utf8"
);
const POLL_MS = 1500;
const STATUS = "/discover/tasks/synthetic/status";
const RUNNING = {
  stage: "running",
  status: "正在查找：已完成 1 步。本机模拟服务处理的请求：1 次（模拟，没有联网，不产生费用）。",
  steps: [{ title: "第 1 步 · 检索岗位 · 完成", params: "来源 synthetic-careers", summary: "合成招聘官网返回 10 个岗位。", note: "" }],
};
const LATE_RUNNING = { ...RUNNING, status: "迟到的运行中回答", steps: [...RUNNING.steps, { ...RUNNING.steps[0], title: "第 2 步" }] };
const ENDED = { stage: "cancelled", status: "已取消", steps: [] };

// Let every promise callback that can run, run.
const flush = () => new Promise((resolve) => setImmediate(resolve));

function element(fields = {}) {
  return {
    textContent: "",
    className: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    ...fields,
  };
}

// One page of a running task, with the script loaded. steps: the page lists steps (找岗位), or not (岗位分析).
// abortable: the browser can abort a request (AbortController), or not.
function load({ steps = true, abortable = true } = {}) {
  const log = [];
  const timers = [];
  const requests = [];
  const handlers = {};
  const on = (target) => (type, handler) => {
    (handlers[`${target}:${type}`] = handlers[`${target}:${type}`] || []).push(handler);
  };
  const fire = (target, type, event) => (handlers[`${target}:${type}`] || []).forEach((handler) => handler(event));

  const status = element({ textContent: "加载时的状态" });
  const list = element();
  const next = { value: "" };
  const cancelForm = {
    addEventListener: on("cancel"),
    querySelector: (selector) => (selector === "input[name=next]" ? next : null),
    submit() {
      log.push(`send cancel next=${next.value}`);
    },
  };
  const logout = {
    submit() {
      log.push("send logout");
    },
  };
  const dialog = {
    open: false,
    addEventListener: on("dialog"),
    showModal() {
      this.open = true;
      log.push("ask");
    },
    close() {
      if (this.open) {
        this.open = false;
        setImmediate(() => fire("dialog", "close", {}));
      }
    },
  };
  const main = {
    dataset: { taskStatus: STATUS },
    querySelector(selector) {
      return {
        "[data-task-step]": status,
        "[data-task-steps]": steps ? list : null,
        "form[data-task-cancel]": cancelForm,
      }[selector] || null;
    },
  };
  const document = {
    querySelector: (selector) => (selector === "main[data-task-running]" ? main : null),
    getElementById: (id) => (id === "task-leave-dialog" ? dialog : null),
    addEventListener: on("document"),
    createElement: () => element(),
  };
  const window = {
    location: {
      href: "http://127.0.0.1/discover/tasks/synthetic",
      origin: "http://127.0.0.1",
      reload() {
        log.push("reload");
      },
    },
    setTimeout(handler, delay) {
      timers.push({ handler, delay });
      return timers.length;
    },
    addEventListener: on("window"),
    fetch(url, options = {}) {
      log.push(`fetch ${url}`);

      return new Promise((resolve, reject) => {
        requests.push({
          options,
          answer: (body) => resolve({ json: () => Promise.resolve(body) }),
          fail: () => reject(new TypeError("Failed to fetch")),
        });

        if (options.signal) {
          options.signal.addEventListener("abort", () => {
            log.push("abort");
            reject(new Error("AbortError"));
          });
        }
      });
    },
  };
  const context = { document, window, URL, console };

  if (abortable) {
    context.AbortController = AbortController;
  }

  vm.runInNewContext(SCRIPT, context);

  return {
    log,
    timers,
    status,
    list,
    // Run the timers set so far, as their time comes.
    tick() {
      timers.splice(0).forEach((timer) => timer.handler());
    },
    async answer(body) {
      requests[requests.length - 1].answer(body);
      await flush();
    },
    async fail() {
      requests[requests.length - 1].fail();
      await flush();
    },
    fetches: () => log.filter((entry) => entry.startsWith("fetch")).length,
    // A same-site link clicked; whether the page held it back to ask first.
    clickLink(href = "http://127.0.0.1/job-analysis") {
      let held = false;
      const link = { href, target: "", hasAttribute: () => false };
      fire("document", "click", { target: { closest: () => link }, preventDefault: () => (held = true) });

      return held;
    },
    // The cancel button: its submit event passes the document's capture listener, then the form's own; then it is sent.
    cancel() {
      let held = false;
      const event = { target: cancelForm, preventDefault: () => (held = true) };
      fire("document", "submit", event);

      if (!held) {
        fire("cancel", "submit", event);
        log.push("send cancel");
      }

      return held;
    },
    // Another form, such as logging out.
    submitLogout() {
      let held = false;
      fire("document", "submit", { target: logout, preventDefault: () => (held = true) });

      if (!held) {
        logout.submit();
      }

      return held;
    },
    async choose(choice) {
      fire("dialog", "click", { target: { closest: () => ({ dataset: { taskChoice: choice } }) } });
      await flush();
    },
    pageshow(persisted) {
      fire("window", "pageshow", { persisted });
    },
    dialog,
  };
}

async function leaveBy(page, way) {
  if (way === "the cancel button") {
    assert.equal(page.cancel(), false);
  } else {
    assert.equal(page.clickLink(), true);
    await page.choose("cancel");
  }
}

const SENT = { "the cancel button": "send cancel", "cancel and leave": "send cancel next=/job-analysis" };

// --- while the task runs ------------------------------------------------------------------------------------------

test("a running search shows its new steps, and an older answer with fewer steps changes nothing", async () => {
  const page = load();
  assert.deepEqual(page.timers.map((timer) => timer.delay), [POLL_MS]);

  page.tick();
  await page.answer(RUNNING);

  assert.equal(page.status.textContent, RUNNING.status);
  assert.deepEqual(page.list.children.map((item) => item.children.map((part) => [part.className, part.textContent])), [
    [["step-title", "第 1 步 · 检索岗位 · 完成"], ["step-params", "来源 synthetic-careers"], ["step-summary", "合成招聘官网返回 10 个岗位。"]],
  ]);
  assert.deepEqual(page.timers.map((timer) => timer.delay), [POLL_MS]);

  page.tick();
  await page.answer({ ...RUNNING, status: "更早的回答", steps: [] });

  assert.equal(page.status.textContent, RUNNING.status);
  assert.equal(page.list.children.length, 1);
  assert.equal(page.fetches(), 2);
});

test("the job analysis page's own status still reads as before", async () => {
  const page = load({ steps: false });

  page.tick();
  await page.answer({ stage: "running", step: "解析岗位", sends: "计数 0 次、生成 1 次，合计 1 次" });

  assert.equal(page.status.textContent, "正在解析岗位。已交给服务的请求：计数 0 次、生成 1 次，合计 1 次。");
  assert.deepEqual(page.timers.map((timer) => timer.delay), [POLL_MS]);

  page.tick();
  await page.answer({ stage: "done" });

  assert.deepEqual(page.log.slice(-1), ["reload"]);
  assert.equal(page.timers.length, 0);
});

test("a request that failed while the task runs is tried again later", async () => {
  const page = load();

  page.tick();
  await page.fail();

  assert.deepEqual(page.timers.map((timer) => timer.delay), [POLL_MS * 2]);
  assert.equal(page.status.textContent, "加载时的状态");
});

test("an ended task is shown once, and nothing asks again", async () => {
  const page = load();

  page.tick();
  await page.answer({ stage: "done", status: "完成", steps: [] });

  assert.deepEqual(page.log, [`fetch ${STATUS}`, "reload"]);
  assert.equal(page.timers.length, 0);
  // While the page reloads, a link is not held back to ask about a task that ended.
  assert.equal(page.clickLink(), false);
});

test("staying keeps the page watching", async () => {
  const page = load();

  assert.equal(page.clickLink(), true);
  await page.choose("stay");
  page.tick();

  assert.equal(page.fetches(), 1);
  assert.equal(page.dialog.open, false);
  assert.ok(!page.log.some((entry) => entry.startsWith("send")));
});

// --- once the user cancels or leaves: nothing more is asked, changed or reloaded -------------------------------------

for (const way of ["the cancel button", "cancel and leave"]) {
  test(`after ${way}, a timer already set sends nothing`, async () => {
    const page = load();

    await leaveBy(page, way);
    page.tick();
    await flush();

    assert.equal(page.fetches(), 0);
    assert.deepEqual(page.log.filter((entry) => entry !== "ask"), [SENT[way]]);
  });

  for (const [late, settle] of [
    ["a running answer", (page) => page.answer(LATE_RUNNING)],
    ["an ended answer", (page) => page.answer(ENDED)],
    ["a failure", (page) => page.fail()],
  ]) {
    test(`after ${way}, ${late} that comes back late changes nothing, asks nothing and reloads nothing`, async () => {
      // The answer is already back while the page reads it, or the browser cannot abort it: only the page's own state stops it.
      const page = load({ abortable: false });

      page.tick();
      await leaveBy(page, way);
      await settle(page);

      assert.equal(page.status.textContent, "加载时的状态");
      assert.equal(page.list.children.length, 0);
      assert.equal(page.timers.length, 0);
      assert.deepEqual(page.log.filter((entry) => entry !== "ask"), [`fetch ${STATUS}`, SENT[way]]);
    });
  }

  test(`after ${way}, the request still out is aborted and its failure asks nothing again`, async () => {
    const page = load();

    page.tick();
    await leaveBy(page, way);
    await flush();

    assert.ok(page.log.includes("abort"));
    assert.equal(page.timers.length, 0);
    assert.ok(!page.log.includes("reload"));
    // Leaving, links are no longer held back.
    assert.equal(page.clickLink(), false);
  });
}

test("logging out from the dialog stops asking before the form is sent", async () => {
  const page = load({ abortable: false });

  page.tick();
  assert.equal(page.submitLogout(), true);
  await page.choose("cancel");
  await page.answer(LATE_RUNNING);
  page.tick();

  assert.deepEqual(page.log, [`fetch ${STATUS}`, "ask", "send logout"]);
  assert.equal(page.status.textContent, "加载时的状态");
});

// --- a task that ends while the user decides ---------------------------------------------------------------------------

test("a task that ends while the leave dialog is open is shown only once the user stays", async () => {
  const page = load();

  page.tick();
  assert.equal(page.clickLink(), true);
  await page.answer({ stage: "done", status: "完成", steps: [] });

  assert.ok(!page.log.includes("reload"));
  assert.equal(page.dialog.open, true);
  assert.equal(page.timers.length, 0);

  await page.choose("stay");

  assert.deepEqual(page.log.slice(-1), ["reload"]);
  assert.equal(page.log.filter((entry) => entry === "reload").length, 1);
});

test("a task that ends while the leave dialog is open is shown once the dialog is closed with Escape", async () => {
  const page = load();

  page.tick();
  assert.equal(page.clickLink(), true);
  await page.answer({ stage: "done", status: "完成", steps: [] });
  // Escape closes the dialog without a choice: the browser closes it, then fires "close".
  page.dialog.close();
  await flush();

  assert.deepEqual(page.log.slice(-1), ["reload"]);
  assert.ok(!page.log.some((entry) => entry.startsWith("send")));
});

test("a task that ends while the leave dialog is open does not take the user's leaving from them", async () => {
  const page = load();

  page.tick();
  assert.equal(page.clickLink(), true);
  await page.answer({ stage: "done", status: "完成", steps: [] });
  await page.choose("cancel");
  await flush();

  assert.ok(!page.log.includes("reload"));
  assert.deepEqual(page.log.slice(-1), ["send cancel next=/job-analysis"]);
});

// --- coming back ---------------------------------------------------------------------------------------------------

test("coming back from the back/forward cache loads the page again", () => {
  const page = load();

  page.pageshow(false);
  assert.ok(!page.log.includes("reload"));

  page.cancel();
  page.pageshow(true);
  assert.deepEqual(page.log.slice(-1), ["reload"]);
});
