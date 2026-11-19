// Two or more panels of one page as tabs (owner redesign: a job's 岗位原文 / 已保存分析, the history's 岗位分析 /
// 查找记录). Without this script each tab is a link (?tab=…) that the server answers with that panel shown. With it, a
// tab shows its panel in place and the address follows, so a reload or the back button lands on the same tab. Nothing
// is fetched or sent from here.
(function () {
  "use strict";

  Array.prototype.slice.call(document.querySelectorAll("[data-tab-area]")).forEach(function (area) {
    var links = Array.prototype.slice.call(area.querySelectorAll("[data-tab-link]"));
    var panels = Array.prototype.slice.call(area.querySelectorAll("[data-tab-panel]"));

    if (!links.length || area.hasAttribute("data-tabs-server")) {
      return;
    }

    function choose(name, focus) {
      links.forEach(function (link) {
        if (link.getAttribute("data-tab-link") === name) {
          link.setAttribute("aria-current", "page");
        } else {
          link.removeAttribute("aria-current");
        }
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.getAttribute("data-tab-panel") !== name;
      });

      if (focus) {
        var panel = area.querySelector('[data-tab-panel="' + name + '"]');
        var title = panel && panel.querySelector("h2, h3");

        if (title) {
          title.setAttribute("tabindex", "-1");
          title.focus();
        }
      }
    }

    var param = area.getAttribute("data-tab-param") || "tab";

    links.forEach(function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        var name = link.getAttribute("data-tab-link");
        choose(name, true);

        if (window.history && window.history.replaceState) {
          var url = new URL(window.location.href);
          url.searchParams.set(param, name);
          window.history.replaceState(null, "", url.pathname + url.search);
        }
      });
    });

    // The tab the page names as current (or the first) is the one shown; the others wait behind their tabs. A link to
    // something inside one (#c-… after saving a candidate) opens the tab that holds it, so it is never hidden.
    var current = area.querySelector("[data-tab-link][aria-current]") || links[0];
    var name = current.getAttribute("data-tab-link");
    var target = null;

    try {
      target = window.location.hash ? area.querySelector(window.location.hash) : null;
    } catch (error) {
      target = null;
    }

    var holder = target && target.closest("[data-tab-panel]");

    if (holder) {
      name = holder.getAttribute("data-tab-panel");
    }

    area.classList.add("is-tabbed");
    choose(name, false);

    if (holder) {
      target.scrollIntoView({ block: "start" });
    }
  });
})();
