// The search setup (owner redesign, plan 5.1). Without this script every chip is a checkbox and each dimension has one
// field to add a value. With it, values typed or chosen from a country's cities become chips at once (the country only
// narrows the suggestions and keeps every chip), and 开始查找 can be pressed once. Nothing is sent from here: the
// server checks everything when the form is sent.
(function () {
  "use strict";

  var form = document.querySelector("form[data-discover-setup]");

  if (!form) {
    return;
  }

  function changed() {
    form.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function addChip(fieldset, value) {
    value = value.trim();

    if (!value) {
      return;
    }

    var list = fieldset.querySelector("[data-chips]");
    var boxes = Array.prototype.slice.call(list.querySelectorAll("input[type=checkbox]"));
    var same = boxes.filter(function (box) {
      return box.value.trim() === value;
    })[0];

    if (same) {
      same.checked = true;
    } else {
      var template = list.querySelector("[data-chip-template]");
      var chip = template.content.firstElementChild.cloneNode(true);
      chip.querySelector("input").value = value;
      chip.querySelector("[data-chip-text]").textContent = value;
      list.insertBefore(chip, template);
    }

    changed();
  }

  Array.prototype.slice.call(form.querySelectorAll("[data-dimension]")).forEach(function (fieldset) {
    var adder = fieldset.querySelector("[data-add-value]");
    var picker = fieldset.querySelector("[data-city-picker]");

    if (picker) {
      // Cities: the picker replaces the plain field; its search is not sent (no name), only the chips are.
      var country = picker.querySelector("[data-city-country]");
      var search = picker.querySelector("[data-city-search]");
      var options = fieldset.querySelector("[data-city-options]");
      var all = Array.prototype.slice.call(options.querySelectorAll("option")).map(function (option) {
        return { value: option.value, country: option.getAttribute("data-country") };
      });
      var narrowed = document.createElement("datalist");
      narrowed.id = "discover-cities-narrowed";
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

      var take = function () {
        addChip(fieldset, search.value);
        search.value = "";
        search.focus();
      };

      picker.hidden = false;

      if (adder) {
        adder.hidden = true;
        adder.querySelector("[data-add-input]").disabled = true;
      }

      narrow();
      country.addEventListener("change", narrow);
      picker.querySelector("[data-city-add]").addEventListener("click", take);
      search.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          take();
        }
      });
      return;
    }

    if (adder) {
      var input = adder.querySelector("[data-add-input]");
      var button = adder.querySelector("[data-add-button]");
      // The typed value becomes a chip; the field itself is then empty, so it adds nothing twice.
      var add = function () {
        addChip(fieldset, input.value);
        input.value = "";
        input.focus();
      };

      button.hidden = false;
      button.addEventListener("click", add);
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          add();
        }
      });
    }
  });

  // 开始查找 once: a second press while the page is on its way does nothing (the server's one-time key says so too).
  var start = form.querySelector("[data-start-button]");
  var sending = false;

  form.addEventListener("submit", function (event) {
    if (sending) {
      event.preventDefault();
      return;
    }

    sending = true;
    window.setTimeout(function () {
      start.disabled = true;
    }, 0);
  });

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });
})();
