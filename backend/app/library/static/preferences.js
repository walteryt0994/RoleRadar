// The job preferences editor in three steps (owner redesign, plan 3.3). Without this script the three steps follow one
// another on one page and each dimension offers one empty row; with it, one step shows at a time and rows can be added
// and removed. Moving between steps never saves anything: only 保存偏好 sends the form. Nothing is sent from here.
(function () {
  "use strict";

  var form = document.querySelector("form[data-pref-editor]");

  if (!form) {
    return;
  }

  var panels = Array.prototype.slice.call(form.querySelectorAll("[data-step-panel]"));
  var links = Array.prototype.slice.call(form.querySelectorAll("[data-step-link]"));
  var prev = form.querySelector("[data-step-prev]");
  var next = form.querySelector("[data-step-next]");
  var save = form.querySelector("[data-save-button]");
  var last = panels.length;
  var current = parseInt(form.getAttribute("data-step"), 10) || 1;
  var visited = {};

  function changed() {
    form.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function show(step, focus) {
    current = Math.min(Math.max(step, 1), last);
    visited[current] = true;
    panels.forEach(function (panel) {
      panel.hidden = parseInt(panel.getAttribute("data-step-panel"), 10) !== current;
    });
    links.forEach(function (link) {
      var number = parseInt(link.getAttribute("data-step-link"), 10);

      if (number === current) {
        link.setAttribute("aria-current", "step");
      } else {
        link.removeAttribute("aria-current");
      }

      link.classList.toggle("is-done", number < current);
    });
    prev.hidden = current === 1;
    next.hidden = current === last;
    save.hidden = current !== last;

    if (focus) {
      var title = form.querySelector('[data-step-panel="' + current + '"] .step-title');

      if (title) {
        title.setAttribute("tabindex", "-1");
        title.focus();
      }
    }
  }

  form.classList.add("is-stepped");
  links.forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      show(parseInt(link.getAttribute("data-step-link"), 10), true);
    });
  });
  prev.addEventListener("click", function () {
    show(current - 1, true);
  });
  next.addEventListener("click", function () {
    show(current + 1, true);
  });

  // Rows: add from the dimension's template, remove with its own button; at most data-most rows per dimension.
  function wireRow(row) {
    var remove = row.querySelector("[data-pref-remove]");

    if (remove) {
      remove.hidden = false;
      remove.addEventListener("click", function () {
        var list = row.parentNode;
        var dimension = row.closest("[data-pref-dim]");
        row.parentNode.removeChild(row);
        changed();
        refresh(dimension);
        var focusTarget = list.querySelector(".pref-value-input") || dimension.querySelector("[data-pref-add], [data-city-search]");

        if (focusTarget) {
          focusTarget.focus();
        }
      });
    }
  }

  function addRow(dimension, value) {
    var list = dimension.querySelector("[data-pref-rows]");
    var template = dimension.querySelector("[data-pref-template]");
    var most = parseInt(list.getAttribute("data-most"), 10);

    if (list.children.length >= most) {
      return null;
    }

    var index = parseInt(list.getAttribute("data-next-index"), 10);
    var holder = document.createElement("ul");
    holder.innerHTML = template.innerHTML.split("__N__").join(String(index)).split("__M__").join(String(index + 1));
    var row = holder.firstElementChild;
    list.setAttribute("data-next-index", String(index + 1));
    list.appendChild(row);
    wireRow(row);

    var input = row.querySelector(".pref-value-input");

    if (value && input) {
      input.value = value;
    }

    changed();
    refresh(dimension);
    return row;
  }

  function refresh(dimension) {
    var list = dimension.querySelector("[data-pref-rows]");
    var add = dimension.querySelector("[data-pref-add]");
    var most = parseInt(list.getAttribute("data-most"), 10);

    if (add) {
      add.hidden = list.children.length >= most;
    }

    // A row is read out by where it stands now ("row 3" for the third), however rows came and went; the field names
    // keep their own numbers and are never renumbered (the dimension names hold no digits).
    Array.prototype.slice.call(list.children).forEach(function (row, at) {
      Array.prototype.slice.call(row.querySelectorAll("[aria-label]")).forEach(function (part) {
        part.setAttribute("aria-label", part.getAttribute("aria-label").replace(/\d+/, String(at + 1)));
      });
    });
  }

  Array.prototype.slice.call(form.querySelectorAll("[data-pref-dim]")).forEach(function (dimension) {
    var list = dimension.querySelector("[data-pref-rows]");
    // The empty row offered without this script is not needed beside rows already there: "add" brings one back.
    // (This runs before the leave guard notes the form, so it is not counted as a change.)
    if (list && list.querySelectorAll("[data-pref-row]:not(.pref-row-new)").length) {
      Array.prototype.slice.call(list.querySelectorAll(".pref-row-new")).forEach(function (row) {
        var input = row.querySelector(".pref-value-input");

        if (input && !input.value) {
          row.parentNode.removeChild(row);
        }
      });
    }

    Array.prototype.slice.call(dimension.querySelectorAll("[data-pref-row]")).forEach(wireRow);
    var add = dimension.querySelector("[data-pref-add]");

    if (add) {
      add.addEventListener("click", function () {
        var row = addRow(dimension, "");
        var input = row && row.querySelector(".pref-value-input");

        if (input) {
          input.focus();
        }
      });
    }

    if (dimension.querySelector("[data-pref-rows]")) {
      refresh(dimension);
    }
  });

  // Cities: the country only narrows the suggestions; a city chosen or typed is added as its own row (once).
  var picker = form.querySelector("[data-city-picker]");

  if (picker) {
    var dimension = picker.closest("[data-pref-dim]");
    var country = picker.querySelector("[data-city-country]");
    var search = picker.querySelector("[data-city-search]");
    var add = picker.querySelector("[data-city-add]");
    var options = form.querySelector("[data-city-options]");
    var all = Array.prototype.slice.call(options.querySelectorAll("option")).map(function (option) {
      return { value: option.value, country: option.getAttribute("data-country") };
    });
    var narrowed = document.createElement("datalist");
    narrowed.id = "city-options-narrowed";
    picker.appendChild(narrowed);
    search.setAttribute("list", narrowed.id);

    var narrow = function () {
      narrowed.textContent = "";
      all.forEach(function (city) {
        if (city.country === country.value) {
          var option = document.createElement("option");
          option.value = city.value;
          narrowed.appendChild(option);
        }
      });
    };

    var addCity = function () {
      var value = search.value.trim();

      if (!value) {
        search.focus();
        return;
      }

      var inputs = Array.prototype.slice.call(dimension.querySelectorAll("[data-pref-rows] .pref-value-input"));
      var same = inputs.filter(function (input) {
        return input.value.trim() === value;
      })[0];

      if (same) {
        same.focus();
      } else {
        var empty = inputs.filter(function (input) {
          return !input.value.trim();
        })[0];

        if (empty) {
          empty.value = value;
          changed();
        } else {
          addRow(dimension, value);
        }
      }

      search.value = "";
      search.focus();
    };

    picker.hidden = false;
    narrow();
    country.addEventListener("change", narrow);
    add.addEventListener("click", addCity);
    search.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        addCity();
      }
    });
  }

  // The earliest start: 清除 empties both lists at once.
  var clear = form.querySelector("[data-must-clear]");

  if (clear) {
    clear.hidden = false;
    clear.addEventListener("click", function () {
      Array.prototype.slice.call(form.querySelectorAll("[data-must-part]")).forEach(function (part) {
        part.value = "";
      });
      changed();
    });
  }

  show(current, false);

  // A failed save opened the step of its first error (data-error-step, set by the server): its first field said to be
  // invalid gets the focus, so the error is where the eyes and the keyboard are.
  if (form.hasAttribute("data-error-step")) {
    var invalid = form.querySelector('[data-step-panel="' + current + '"] [aria-invalid="true"]');

    if (invalid) {
      invalid.focus();
    }
  }
})();
