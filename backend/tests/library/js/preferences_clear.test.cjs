// Browser acceptance revision: 清除 under the earliest start, with its script, executed offline.
//
// The real app/library/static/preferences.js and leave_guard.js run, in the order the editor page loads them, in a
// fresh Node vm context for each case, against a small DOM double built like step 3 of preferences_form.html (the
// year and month lists, 清除 sent hidden, 其他要求, a stance list from step 1, the step buttons, the unsaved-changes
// line, the leave dialog). Without script the page never shows 清除 (library.css: the hidden attribute always hides;
// checked in a real browser, see test_ui_browser_revision.py); with it, 清除 must appear, empty the year and the month
// together, touch nothing else, and be reported like typing, so 有还没保存的修改 shows and saving still goes through.
// No browser, server, network or third-party package. Run with: node --test preferences_clear.test.cjs
// (tests/library/test_preferences_clear_script.py does, inside the isolated run).
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");

const STATIC = path.join(__dirname, "..", "..", "..", "app", "library", "static");
// PREFERENCES_SCRIPT names another copy of the script, only to show these cases fail on a broken one.
const PREFERENCES = fs.readFileSync(process.env.PREFERENCES_SCRIPT || path.join(STATIC, "preferences.js"), "utf8");
const LEAVE_GUARD = fs.readFileSync(path.join(STATIC, "leave_guard.js"), "utf8");
const WORDS = { "zh-CN": "有还没保存的修改。", en: "You have unsaved changes." };

// --- a small DOM: tags, ids, classes, [attribute] and [attribute=value], descendants, comma lists ------------------

function parseCompound(text) {
  const tag = (text.match(/^[a-z][a-z0-9]*/i) || [""])[0].toLowerCase();
  const ids = [...text.matchAll(/#([\w-]+)/g)].map((m) => m[1]);
  const classes = [...text.matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
  const attributes = [...text.matchAll(/\[([^\]=]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/g)].map((m) => [m[1], m[2] !== undefined ? m[2] : m[3]]);
  return { tag, ids, classes, attributes };
}

function parseSelector(selector) {
  return selector.split(",").map((one) => one.trim().split(/\s+/).map(parseCompound));
}

function matchesCompound(element, part) {
  return (
    (!part.tag || element.tagName === part.tag) &&
    part.ids.every((id) => element.getAttribute("id") === id) &&
    part.classes.every((name) => element.classList.contains(name)) &&
    part.attributes.every(([name, value]) => element.hasAttribute(name) && (value === undefined || element.getAttribute(name) === value))
  );
}

function matchesChain(element, chain) {
  if (!matchesCompound(element, chain[chain.length - 1])) {
    return false;
  }
  let at = chain.length - 2;
  let ancestor = element.parentNode;
  while (at >= 0 && ancestor) {
    if (ancestor.tagName && matchesCompound(ancestor, chain[at])) {
      at -= 1;
    }
    ancestor = ancestor.parentNode;
  }
  return at < 0;
}

class Event {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.defaultPrevented = false;
    this.returnValue = undefined;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

class Element {
  constructor(tag, attributes = {}, children = []) {
    this.tagName = tag;
    this.attributes = { ...attributes };
    this.children = [];
    this.parentNode = null;
    // hidden is the attribute the server sends; the scripts set the property (the same thing in a browser).
    this.hidden = "hidden" in attributes;
    delete this.attributes.hidden;
    this.value = attributes.value || "";
    this.checked = "checked" in attributes;
    this.textContent = "";
    this.dataset = {};
    this.listeners = {};
    this.calls = [];
    const classes = new Set((attributes.class || "").split(/\s+/).filter(Boolean));
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on === undefined ? (classes.has(name) ? classes.delete(name) : classes.add(name)) : on ? classes.add(name) : classes.delete(name)),
    };
    children.forEach((child) => (typeof child === "string" ? (this.textContent += child) : this.appendChild(child)));
  }
  get name() {
    return this.getAttribute("name") || "";
  }
  get id() {
    return this.getAttribute("id") || "";
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name);
  }
  getAttribute(name) {
    return this.hasAttribute(name) ? this.attributes[name] : null;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }
  querySelectorAll(selector) {
    const chains = parseSelector(selector);
    return this.descendants().filter((element) => chains.some((chain) => matchesChain(element, chain)));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  closest(selector) {
    const chains = parseSelector(selector);
    for (let element = this; element && element.tagName; element = element.parentNode) {
      if (chains.some((chain) => matchesChain(element, chain))) {
        return element;
      }
    }
    return null;
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    for (let node = this; node; node = event.bubbles ? node.parentNode : null) {
      (node.listeners[event.type] || []).forEach((handler) => handler(event));
    }
    return !event.defaultPrevented;
  }
  click() {
    return this.dispatchEvent(new Event("click", { bubbles: true }));
  }
  focus() {
    this.calls.push("focus");
  }
  submit() {
    this.calls.push("submit");
  }
  requestSubmit() {
    this.calls.push("requestSubmit");
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
}

const h = (tag, attributes, ...children) => new Element(tag, attributes || {}, children);

function list(name, values, extra = {}) {
  return h("select", { name, ...extra }, h("option", { value: "" }), ...values.map((value) => h("option", { value })));
}

// Step 3 of preferences_form.html (and what the scripts read elsewhere in the form), with the year, month and other
// requirements as the server filled them in.
function editorPage({ language = "zh-CN", year = "2026", month = "7", other = "每周可到岗三天（合成）" } = {}) {
  const yearList = list("must_start_year", ["2027", "2026", "2025"], { "data-must-part": "" });
  const monthList = list("must_start_month", ["1", "2", "6", "7", "12"], { "data-must-part": "" });
  yearList.value = year;
  monthList.value = month;
  const clear = h("button", { type: "button", class: "link-button", "data-must-clear": "", hidden: "" }, "清除");
  const otherBox = h("textarea", { id: "must-other", name: "must_other" });
  otherBox.value = other;
  const stance = list("cities_r0_stance", ["ideal", "acceptable", "unacceptable"], { class: "stance-select" });
  stance.value = "ideal";
  const city = h("input", { type: "text", name: "cities_r0", class: "pref-value-input", value: "北京" });
  const status = h("p", { class: "dirty-status", id: "dirty-status", "aria-live": "polite" });
  const save = h("button", { type: "submit", "data-save-button": "" }, "保存偏好");
  const form = h(
    "form",
    { method: "post", action: "/job-preferences/synthetic/save", class: "editor pref-editor", "data-leave-guard": "", "data-pref-editor": "", "data-step": "1" },
    h("nav", { class: "stepper" }, ...[1, 2, 3].map((n) => h("a", { href: `#pref-step-${n}`, "data-step-link": String(n) }))),
    h("fieldset", { class: "pref-step", id: "pref-step-1", "data-step-panel": "1" }, h("legend", { class: "step-title" }), city, stance),
    h("fieldset", { class: "pref-step", id: "pref-step-2", "data-step-panel": "2" }, h("legend", { class: "step-title" })),
    h(
      "fieldset",
      { class: "pref-step", id: "pref-step-3", "data-step-panel": "3" },
      h("legend", { class: "step-title" }),
      h("input", { type: "hidden", name: "must_original", value: "最早入职：2026 年 7 月\n每周可到岗三天（合成）" }),
      h("div", { class: "req-row" }, h("span", { class: "req-label", id: "l-start" }), h("span", { class: "month-selects" }, yearList, monthList), clear),
      h("div", { class: "req-row" }, otherBox)
    ),
    h(
      "div",
      { class: "form-actions step-actions" },
      h("button", { type: "button", class: "secondary", "data-step-prev": "", hidden: "" }),
      h("button", { type: "button", class: "primary", "data-step-next": "", hidden: "" }),
      save,
      h("a", { class: "cancel-link", href: "/job-preferences" }),
      status
    )
  );
  const dialog = h("dialog", { id: "leave-dialog" });
  const html = h("html", { lang: language, "data-ui-text": JSON.stringify({ "js.unsaved": WORDS[language] }) }, h("body", {}, form, dialog));
  return { html, form, clear, yearList, monthList, otherBox, stance, city, status, save };
}

// Loads the page's two scripts, as the editor does (preferences.js, then leave_guard.js).
function load(page) {
  const windowListeners = {};
  const document = {
    documentElement: page.html,
    body: page.html.children[0],
    activeElement: null,
    querySelector: (selector) => page.html.querySelector(selector),
    querySelectorAll: (selector) => page.html.querySelectorAll(selector),
    getElementById: (id) => page.html.querySelector(`#${id}`),
    addEventListener: (type, handler) => page.html.addEventListener(type, handler),
    createElement: (tag) => new Element(tag),
  };
  const window = {
    location: { href: "http://127.0.0.1:8842/job-preferences/edit", origin: "http://127.0.0.1:8842" },
    addEventListener: (type, handler) => (windowListeners[type] = windowListeners[type] || []).push(handler),
  };
  const context = vm.createContext({ window, document, Event, URL, URLSearchParams, console });
  vm.runInContext(PREFERENCES, context, { filename: "preferences.js" });
  vm.runInContext(LEAVE_GUARD, context, { filename: "leave_guard.js" });
  const leaving = () => {
    const event = new Event("beforeunload");
    (windowListeners.beforeunload || []).forEach((handler) => handler(event));
    return event.defaultPrevented;
  };
  return { leaving };
}

// --- the cases ------------------------------------------------------------------------------------------------------

test("the page sends 清除 hidden and its script shows it", () => {
  const page = editorPage();
  assert.equal(page.clear.hidden, true);
  load(page);
  assert.equal(page.clear.hidden, false);
});

test("清除 empties the year and the month together, and nothing else", () => {
  const page = editorPage();
  load(page);
  page.clear.click();
  assert.equal(page.yearList.value, "");
  assert.equal(page.monthList.value, "");
  assert.equal(page.otherBox.value, "每周可到岗三天（合成）");
  assert.equal(page.stance.value, "ideal");
  assert.equal(page.city.value, "北京");
  assert.deepEqual(page.form.calls, []);
});

for (const language of ["zh-CN", "en"]) {
  test(`after 清除 the page says there are unsaved changes (${language}) and leaving asks first`, () => {
    const page = editorPage({ language });
    const { leaving } = load(page);
    assert.equal(page.status.textContent, "");
    assert.equal(page.form.dataset.dirty, "false");
    assert.equal(leaving(), false);
    page.clear.click();
    assert.equal(page.status.textContent, WORDS[language]);
    assert.equal(page.form.dataset.dirty, "true");
    assert.equal(leaving(), true);
  });
}

test("saving after 清除 goes through: the form's own send never asks to leave", () => {
  const page = editorPage();
  const { leaving } = load(page);
  page.clear.click();
  page.form.dispatchEvent(new Event("submit", { bubbles: true }));
  assert.equal(leaving(), false);
  assert.equal(page.yearList.value, "");
  assert.equal(page.monthList.value, "");
});

test("清除 on a start that is already empty changes nothing and leaves the form clean", () => {
  const page = editorPage({ year: "", month: "" });
  const { leaving } = load(page);
  page.clear.click();
  assert.equal(page.status.textContent, "");
  assert.equal(page.form.dataset.dirty, "false");
  assert.equal(leaving(), false);
});
