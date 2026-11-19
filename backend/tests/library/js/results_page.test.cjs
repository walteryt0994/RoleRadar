// Owner redesign revision R2: the list-beside-one-item script, executed offline.
//
// The real app/library/static/results.js runs in a fresh Node vm context for each case, against a small DOM double
// built like the pages that load it (查找与分析记录, 我的岗位, a search's results): rows whose links the page wrote
// (?tab=searches&item=2 on the records page), 返回列表 links, the filters, a history with real entries and popstate,
// and the screen wide or narrow. The cases follow what Codex reproduced (a search record clicked, then the page
// reloaded, opened the analyses tab): the address after each step must name the tab and the item the page shows.
// No browser, server, network or third-party package. Run with: node --test results_page.test.cjs
// (tests/library/test_results_page_script.py does, inside the isolated run).
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");

// RESULTS_PAGE_SCRIPT names another copy of the script, only to show these cases fail on an earlier one.
const SCRIPT = fs.readFileSync(
  process.env.RESULTS_PAGE_SCRIPT || path.join(__dirname, "..", "..", "..", "app", "library", "static", "results.js"),
  "utf8"
);

// --- a small DOM: elements, attribute selectors, descendants -------------------------------------------------------

function parseSelector(selector) {
  return selector.trim().split(/\s+/).map((part) => {
    const tag = (part.match(/^[a-z][a-z0-9]*/i) || [""])[0].toLowerCase();
    const attributes = [];
    part.replace(/\[([^\]=]+)(?:="([^"]*)")?\]/g, (_, name, value) => {
      attributes.push([name, value]);
      return "";
    });
    return { tag, attributes };
  });
}

function matchesPart(element, part) {
  if (part.tag && element.tagName !== part.tag) {
    return false;
  }
  return part.attributes.every(([name, value]) => element.hasAttribute(name) && (value === undefined || element.getAttribute(name) === value));
}

function matchesChain(element, parts) {
  if (!matchesPart(element, parts[parts.length - 1])) {
    return false;
  }
  let at = parts.length - 2;
  let ancestor = element.parentNode;
  while (at >= 0 && ancestor) {
    if (matchesPart(ancestor, parts[at])) {
      at -= 1;
    }
    ancestor = ancestor.parentNode;
  }
  return at < 0;
}

class Element {
  constructor(tag, attributes = {}, children = []) {
    this.tagName = tag;
    this.attributes = { ...attributes };
    this.children = [];
    this.parentNode = null;
    this.hidden = "hidden" in attributes;
    delete this.attributes.hidden;
    this.value = attributes.value || "";
    this.listeners = {};
    this.focused = 0;
    const classes = new Set((attributes.class || "").split(/\s+/).filter(Boolean));
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on === undefined ? (classes.has(name) ? classes.delete(name) : classes.add(name)) : on ? classes.add(name) : classes.delete(name)),
    };
    children.forEach((child) => this.appendChild(child));
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
    const parts = parseSelector(selector);
    return this.descendants().filter((element) => matchesChain(element, parts));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  closest(selector) {
    const parts = parseSelector(selector);
    let element = this;
    while (element) {
      if (matchesChain(element, parts)) {
        return element;
      }
      element = element.parentNode;
    }
    return null;
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  dispatch(type, fields = {}) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...fields };
    (this.listeners[type] || []).forEach((handler) => handler(event));
    return event;
  }
  click() {
    return this.dispatch("click");
  }
  focus() {
    this.focused += 1;
  }
  scrollIntoView() {}
  get options() {
    return this.children.filter((child) => child.tagName === "option");
  }
}

const h = (tag, attributes, ...children) => new Element(tag, attributes || {}, children);

// --- the pages ------------------------------------------------------------------------------------------------------

// rowHref(n) and backHref: the links exactly as each template writes them (job_history.html, job_records.html,
// discover_task.html).
const PAGES = {
  searches: { path: "/job-records/history", results: "searches", rowHref: (n) => `?tab=searches&item=${n}#record-${n}`, backHref: "?tab=searches", detail: "record" },
  analyses: { path: "/job-records/history", results: "analyses", rowHref: (n) => `?tab=analyses&item=${n}#record-${n}`, backHref: "?tab=analyses", detail: "record" },
  myJobs: { path: "/job-records", results: "jobs", rowHref: (n) => `?item=${n}#job-${n}`, backHref: "?", detail: "job" },
  found: { path: "/discover/tasks/synthetic-task", results: "results", rowHref: (n) => `?item=${n}#result-${n}`, backHref: "?", detail: "result" },
};

function buildPage(page, { chosen = "1", opened = false, count = 3 } = {}) {
  const rows = [];
  const details = [];
  for (let n = 1; n <= count; n += 1) {
    const link = h("a", { class: "md-item result-row", href: page.rowHref(n), "data-result-link": String(n), ...(String(n) === chosen ? { "aria-current": "true" } : {}) });
    rows.push(h("li", { "data-result-row": "", "data-words": `合成岗位 ${n} 合成公司`, "data-decision": n === 2 ? "favorite" : "none" }, link));
    details.push(
      h(
        "article",
        { id: `${page.detail}-${n}`, "data-result-detail": String(n), ...(String(n) === chosen ? {} : { hidden: "" }) },
        h("p", { class: "md-back" }, h("a", { href: page.backHref, "data-md-back": "" })),
        h("h2", {})
      )
    );
  }
  const words = h("input", { type: "search", "data-filter-words": "" });
  const decision = h("select", { "data-filter": "decision" }, h("option", { value: "" }), h("option", { value: "favorite" }), h("option", { value: "none" }));
  const filters = h("div", { "data-result-filters": "", hidden: "" }, words, decision);
  const area = h(
    "div",
    { class: `md results-md${opened ? " md-chosen" : ""}`, "data-results": page.results },
    h("nav", { class: "md-list" }, h("ul", { "data-result-list": "" }, ...rows), h("div", { "data-filter-empty": "", hidden: "" }), h("button", { "data-filter-clear": "", hidden: "" }), h("button", { "data-load-more": "", hidden: "" })),
    h("div", { class: "md-detail" }, ...details)
  );
  const body = h("body", {}, filters, area);
  return { body, area, filters, words, decision };
}

// One page loaded at address (path + query), wide or narrow, with the script run; storage is the tab's
// sessionStorage, shared between loads of the same case.
function load(page, { address = page.path, wide = true, chosen, opened, storage = new Map() } = {}) {
  const dom = buildPage(page, { chosen, opened });
  let current = new URL(address, "http://127.0.0.1:8842");
  const entries = [{ state: null, url: current.href }];
  let at = 0;
  const windowListeners = {};
  const log = [];

  const location = {
    get href() { return current.href; },
    get pathname() { return current.pathname; },
    get search() { return current.search; },
  };
  const history = {
    get state() { return entries[at].state; },
    replaceState(state, _title, url) {
      current = new URL(url, current.href);
      entries[at] = { state, url: current.href };
      log.push(`replace ${current.pathname}${current.search}`);
    },
    pushState(state, _title, url) {
      current = new URL(url, current.href);
      entries.splice(at + 1, entries.length, { state, url: current.href });
      at += 1;
      log.push(`push ${current.pathname}${current.search}`);
    },
    back() {
      move(-1);
    },
    forward() {
      move(1);
    },
  };
  function move(step) {
    if (!entries[at + step]) {
      return;
    }
    at += step;
    current = new URL(entries[at].url);
    log.push(`${step < 0 ? "back" : "forward"} ${current.pathname}${current.search}`);
    (windowListeners.popstate || []).forEach((handler) => handler({ state: entries[at].state }));
  }

  const window = {
    location,
    history,
    scrollY: 0,
    scrollTo(_x, y) { this.scrollY = y; },
    matchMedia: () => ({ matches: wide, addEventListener() {} }),
    sessionStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
    },
    addEventListener(type, handler) {
      (windowListeners[type] = windowListeners[type] || []).push(handler);
    },
  };
  const document = { querySelector: (selector) => dom.body.querySelector(selector) };
  const context = vm.createContext({ window, document, URL, JSON, Math, console });
  vm.runInContext(SCRIPT, context);

  const address_ = () => `${current.pathname}${current.search}`;
  const shown = () => dom.area.querySelectorAll("[data-result-detail]").filter((detail) => !detail.hidden).map((detail) => detail.getAttribute("data-result-detail"));
  const chosenRow = () => {
    const link = dom.area.querySelector("[data-result-link][aria-current]");
    return link ? link.getAttribute("data-result-link") : null;
  };
  return {
    ...dom,
    log,
    storage,
    address: address_,
    shown,
    chosenRow,
    listShown: () => !dom.area.classList.contains("md-chosen"),
    row: (n) => dom.area.querySelector(`[data-result-link="${n}"]`),
    backOf: (n) => dom.area.querySelector(`[data-result-detail="${n}"] [data-md-back]`),
    back: () => history.back(),
    forward: () => history.forward(),
  };
}

// What the server renders for an address after a reload (job_history.html and records_web.history_page): the tab from
// ?tab= (the analyses tab when it is missing), the item from ?item=.
function served(address) {
  const url = new URL(address, "http://127.0.0.1:8842");
  return { tab: url.searchParams.get("tab") || "analyses", item: url.searchParams.get("item") || "1" };
}

// --- cases ----------------------------------------------------------------------------------------------------------

test("a search record opened on a wide screen keeps its tab in the address, so a reload opens that record again", () => {
  const page = load(PAGES.searches, { address: "/job-records/history?tab=searches" });
  const event = page.row(2).click();

  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(page.shown(), ["2"]);
  assert.equal(page.chosenRow(), "2");
  assert.equal(page.address(), "/job-records/history?tab=searches&item=2");
  assert.deepEqual(served(page.address()), { tab: "searches", item: "2" });
});

test("the analyses tab keeps its own tab too", () => {
  const page = load(PAGES.analyses, { address: "/job-records/history?tab=analyses" });
  page.row(3).click();

  assert.equal(page.address(), "/job-records/history?tab=analyses&item=3");
  assert.deepEqual(served(page.address()), { tab: "analyses", item: "3" });
});

test("on a narrow screen a search record opens in place, and back to the list returns to the tab's list", () => {
  const page = load(PAGES.searches, { address: "/job-records/history?tab=searches", wide: false });
  page.row(2).click();

  assert.equal(page.listShown(), false);
  assert.deepEqual(page.shown(), ["2"]);
  assert.equal(page.address(), "/job-records/history?tab=searches&item=2");
  assert.deepEqual(page.log, ["push /job-records/history?tab=searches&item=2"]);

  page.backOf(2).click();
  assert.equal(page.listShown(), true);
  assert.equal(page.chosenRow(), "2");
  assert.equal(page.address(), "/job-records/history?tab=searches");
});

test("the browser's back and forward move between the list and the record, the address naming the tab each time", () => {
  const page = load(PAGES.searches, { address: "/job-records/history?tab=searches", wide: false });
  page.row(3).click();
  page.back();

  assert.equal(page.listShown(), true);
  assert.equal(page.address(), "/job-records/history?tab=searches");

  page.forward();
  assert.equal(page.listShown(), false);
  assert.deepEqual(page.shown(), ["3"]);
  assert.equal(page.address(), "/job-records/history?tab=searches&item=3");
});

test("a copied link to a search record, opened on a narrow screen, goes back to the searches list, never the analyses", () => {
  const page = load(PAGES.searches, { address: "/job-records/history?tab=searches&item=2", wide: false, chosen: "2", opened: true });

  assert.equal(page.listShown(), false);
  page.backOf(2).click();

  assert.equal(page.listShown(), true);
  assert.equal(page.address(), "/job-records/history?tab=searches");
  assert.deepEqual(served(page.address()), { tab: "searches", item: "1" });
});

test("my jobs and a search's results write their own addresses the same way", () => {
  const jobs = load(PAGES.myJobs, { address: "/job-records?notice=deleted" });
  jobs.row(2).click();
  assert.equal(jobs.address(), "/job-records?item=2");

  const narrow = load(PAGES.myJobs, { address: "/job-records?item=2", wide: false, chosen: "2", opened: true });
  narrow.backOf(2).click();
  assert.equal(narrow.address(), "/job-records");

  const found = load(PAGES.found, { address: "/discover/tasks/synthetic-task", wide: false });
  found.row(3).click();
  assert.equal(found.address(), "/discover/tasks/synthetic-task?item=3");
  found.backOf(3).click();
  assert.equal(found.address(), "/discover/tasks/synthetic-task");
});

test("the filters stay as they were across opening a record and coming back, and after a reload of the page", () => {
  const storage = new Map();
  const page = load(PAGES.myJobs, { address: "/job-records", wide: false, storage });
  page.words.value = "合成岗位 2";
  page.words.dispatch("input");
  assert.deepEqual(page.area.querySelectorAll("[data-result-row]").filter((row) => !row.hidden).length, 1);

  page.row(2).click();
  page.backOf(2).click();
  assert.equal(page.words.value, "合成岗位 2");
  assert.equal(page.area.querySelectorAll("[data-result-row]").filter((row) => !row.hidden).length, 1);

  const reloaded = load(PAGES.myJobs, { address: "/job-records", storage });
  assert.equal(reloaded.words.value, "合成岗位 2");
  assert.equal(reloaded.area.querySelectorAll("[data-result-row]").filter((row) => !row.hidden).length, 1);
});
