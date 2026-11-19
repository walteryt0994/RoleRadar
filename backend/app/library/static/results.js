// A list beside one item (owner redesign, plan 5.3): the search results, 我的岗位, the search and analysis records.
// Without this script every row is a link that opens its item (?item=) and every row shows. With it:
// - on a wide screen a row opens its item in place beside the list;
// - on a narrow one a row opens its item in place of the list, and 返回列表 (or the browser's back) brings the list
//   back as it was: the same filters, the same row chosen, the same place on the page (plan 2.4);
// - the list can be searched and filtered, ten rows at a time, all from what the page already holds: nothing is
//   fetched, sent or asked for. The filters are remembered for this page in this tab, so they are still set after a
//   decision made from the list reloads it.
// An item's two tabs (岗位概览 / 经历匹配) become tabs here too.
(function () {
  "use strict";

  var area = document.querySelector("[data-results]");

  if (!area) {
    return;
  }

  var wide = window.matchMedia("(min-width: 900px)");
  var rows = Array.prototype.slice.call(area.querySelectorAll("[data-result-row]"));
  var details = Array.prototype.slice.call(area.querySelectorAll("[data-result-detail]"));
  var filters = document.querySelector("[data-result-filters]");
  var PAGE = 10;
  var shownCount = PAGE;
  var memoryKey = "roleradar-list:" + window.location.pathname + ":" + (area.getAttribute("data-results") || "");
  var listPlace = null;

  function linkOf(number) {
    return area.querySelector('[data-result-link="' + number + '"]');
  }

  // Where a row or 返回列表 leads, as the page itself wrote it (the records page's ?tab= with it), less its #anchor:
  // the address then names the tab and the item the page shows, for a reload, the browser's back and forward, or a
  // copied link (owner redesign revision R2: writing ?item= alone sent a reload of a search back to the analyses tab).
  function addressOf(anchor) {
    var target = new URL(anchor.getAttribute("href"), window.location.href);

    return target.pathname + target.search;
  }

  function chosen() {
    var current = area.querySelector("[data-result-link][aria-current]");

    return current ? current.getAttribute("data-result-link") : null;
  }

  function open(number, focus) {
    details.forEach(function (detail) {
      detail.hidden = detail.getAttribute("data-result-detail") !== number;
    });
    rows.forEach(function (row) {
      var link = row.querySelector("[data-result-link]");

      if (link.getAttribute("data-result-link") === number) {
        link.setAttribute("aria-current", "true");
      } else {
        link.removeAttribute("aria-current");
      }
    });

    if (focus) {
      var title = area.querySelector('[data-result-detail="' + number + '"] h2');

      if (title) {
        title.setAttribute("tabindex", "-1");
        title.focus();
      }
    }
  }

  function showDetail(number) {
    listPlace = window.scrollY;
    area.classList.add("md-chosen");

    if (filters) {
      filters.classList.add("is-away");
    }

    open(number, true);
  }

  function showList() {
    area.classList.remove("md-chosen");

    if (filters) {
      filters.classList.remove("is-away");
    }

    var link = linkOf(chosen());

    if (listPlace !== null) {
      window.scrollTo(0, listPlace);
    } else if (link) {
      link.scrollIntoView({ block: "center" });
    }

    if (link) {
      link.focus({ preventScroll: true });
    }
  }

  rows.forEach(function (row) {
    var link = row.querySelector("[data-result-link]");

    link.addEventListener("click", function (event) {
      var number = link.getAttribute("data-result-link");
      event.preventDefault();

      if (wide.matches) {
        open(number, true);

        if (window.history && window.history.replaceState) {
          window.history.replaceState(null, "", addressOf(link));
        }

        return;
      }

      showDetail(number);

      if (window.history && window.history.pushState) {
        window.history.pushState({ item: number }, "", addressOf(link));
      }
    });
  });

  // 返回列表: back to the list as it was, never a reload that would lose its filters.
  Array.prototype.slice.call(area.querySelectorAll("[data-md-back]")).forEach(function (back) {
    back.addEventListener("click", function (event) {
      event.preventDefault();

      if (window.history && window.history.state && window.history.state.item) {
        window.history.back();
        return;
      }

      showList();

      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, "", addressOf(back));
      }
    });
  });

  window.addEventListener("popstate", function (event) {
    if (event.state && event.state.item) {
      showDetail(event.state.item);
    } else if (area.classList.contains("md-chosen")) {
      showList();
    }
  });

  // 岗位概览 / 经历匹配 as tabs inside each item.
  details.forEach(function (detail) {
    var tabs = detail.querySelector("[data-detail-tabs]");

    if (!tabs) {
      return;
    }

    var buttons = Array.prototype.slice.call(tabs.querySelectorAll("[data-tab]"));
    var panels = Array.prototype.slice.call(detail.querySelectorAll("[data-panel]"));

    function choose(name) {
      buttons.forEach(function (button) {
        button.setAttribute("aria-selected", button.getAttribute("data-tab") === name ? "true" : "false");
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.getAttribute("data-panel") !== name;
      });
    }

    tabs.hidden = false;
    detail.classList.add("is-tabbed");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        choose(button.getAttribute("data-tab"));
      });
    });
    choose(buttons[0].getAttribute("data-tab"));
  });

  if (!filters) {
    return;
  }

  // Filters over the rows already here: words in the title or company, and each select over the attribute it names.
  var words = filters.querySelector("[data-filter-words]");
  var selects = Array.prototype.slice.call(filters.querySelectorAll("select[data-filter]"));
  var empty = area.querySelector("[data-filter-empty]");
  var more = area.querySelector("[data-load-more]");
  var clear = area.querySelector("[data-filter-clear]");

  function fill(select) {
    var name = "data-" + select.getAttribute("data-filter");
    var seen = {};

    rows.forEach(function (row) {
      var value = row.getAttribute(name);

      if (value && !seen[value]) {
        seen[value] = true;
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      }
    });
  }

  function remember() {
    try {
      var said = { words: words.value, selects: {} };

      selects.forEach(function (select) {
        said.selects[select.getAttribute("data-filter")] = select.value;
      });
      window.sessionStorage.setItem(memoryKey, JSON.stringify(said));
    } catch (error) {
      // Nothing is lost but the memory of the filters.
    }
  }

  function recall() {
    try {
      var said = JSON.parse(window.sessionStorage.getItem(memoryKey) || "null");

      if (!said) {
        return;
      }

      words.value = said.words || "";
      selects.forEach(function (select) {
        var value = (said.selects || {})[select.getAttribute("data-filter")] || "";

        if (Array.prototype.some.call(select.options, function (option) { return option.value === value; })) {
          select.value = value;
        }
      });
    } catch (error) {
      // A memory that cannot be read is left alone: the list shows unfiltered.
    }
  }

  function apply() {
    var text = words.value.trim().toLowerCase();
    var matching = rows.filter(function (row) {
      return (
        (!text || row.getAttribute("data-words").toLowerCase().indexOf(text) !== -1) &&
        selects.every(function (select) {
          return !select.value || row.getAttribute("data-" + select.getAttribute("data-filter")) === select.value;
        })
      );
    });
    var current = linkOf(chosen());
    var place = current ? matching.indexOf(current.closest("[data-result-row]")) : -1;

    // The row chosen stays within the rows shown, however far down it is.
    if (place >= shownCount) {
      shownCount = Math.ceil((place + 1) / PAGE) * PAGE;
    }

    rows.forEach(function (row) {
      row.hidden = true;
    });
    matching.slice(0, shownCount).forEach(function (row) {
      row.hidden = false;
    });
    empty.hidden = matching.length !== 0;
    more.hidden = matching.length <= shownCount;
  }

  selects.forEach(function (select) {
    if (select.hasAttribute("data-fill")) {
      fill(select);
    }
  });
  recall();
  filters.hidden = false;

  // The filters belong to the list: on a narrow screen showing one item they step away (and come back with the list,
  // or when the screen widens).
  function placeFilters() {
    filters.classList.toggle("is-away", area.classList.contains("md-chosen") && !wide.matches);
  }

  placeFilters();

  if (wide.addEventListener) {
    wide.addEventListener("change", placeFilters);
  }

  [words].concat(selects).forEach(function (control) {
    control.addEventListener("input", function () {
      shownCount = PAGE;
      apply();
      remember();
    });
  });
  more.addEventListener("click", function () {
    shownCount += PAGE;
    apply();
  });

  if (clear) {
    clear.hidden = false;
    clear.addEventListener("click", function () {
      words.value = "";
      selects.forEach(function (select) {
        select.value = "";
      });
      shownCount = PAGE;
      apply();
      remember();
      words.focus();
    });
  }

  apply();

  // On a wide screen the row chosen is brought into the list's view (after a decision made from it, say).
  if (wide.matches && chosen() && linkOf(chosen())) {
    linkOf(chosen()).scrollIntoView({ block: "nearest" });
  }

})();
