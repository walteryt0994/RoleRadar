// Motion for the experience library: blocks that rise in when they first
// come into view, a backdrop that scrolls at half speed, and a skeleton while
// a workspace page is really on its way.
//
// All of it only changes how the page looks. Without JavaScript, or when the
// reader prefers reduced motion, every block is visible at once and the
// backdrop stays still; a skeleton, when one is needed, does not breathe.
// Loaded in <head>, so the blocks can be hidden before the first paint.
(function () {
  "use strict";

  const root = document.documentElement;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  // The backdrop moves at half the scrolling speed: 200px of scrolling moves it 100px.
  const PARALLAX_RATE = 0.5;
  // A page that arrives within this time never shows a skeleton.
  const SKELETON_DELAY_MS = 150;
  // A page still not there after this long gives the old content back, with a retry link.
  const SKELETON_GIVE_UP_MS = 10000;
  // The pages that show the workspace: the record list, a record, its form and the new form.
  const WORKSPACE_PAGE = /^\/experiences(?:\/new|\/[0-9a-f-]{36}(?:\/edit)?)?$/;

  if (!reduce.matches && "IntersectionObserver" in window) {
    root.classList.add("motion");
  }

  document.addEventListener("DOMContentLoaded", function () {
    [setupReveal, setupParallax, setupSkeleton].forEach(function (setup) {
      try {
        setup();
      } catch (error) {
        // A failed effect must never leave content hidden.
        root.classList.remove("motion");
      }
    });
  });

  function onReduceChange(listener) {
    if (reduce.addEventListener) {
      reduce.addEventListener("change", listener);
    } else if (reduce.addListener) {
      reduce.addListener(listener);
    }
  }

  // --- reveal on scroll -----------------------------------------------------

  function setupReveal() {
    if (!root.classList.contains("motion")) {
      return;
    }

    // Errors, conflicts and confirmations are read at once, with the blocks around them.
    if (document.querySelector('[role="alert"]')) {
      root.classList.remove("motion");
      return;
    }

    const blocks = Array.from(document.querySelectorAll("[data-reveal]"));
    const observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          reveal(entry.target, false);
        }
      });
    });

    // Each block rises in once; it never hides again.
    function reveal(block, instant) {
      observer.unobserve(block);

      if (!block.classList.contains("is-revealed")) {
        block.classList.toggle("is-instant", instant);
        block.classList.add("is-revealed");
      }
    }

    function revealTarget() {
      const id = decodeURIComponent(window.location.hash.slice(1));
      const target = id && document.getElementById(id);
      const block = target && target.closest("[data-reveal]");

      if (block) {
        reveal(block, true);
      }
    }

    blocks.forEach(function (block) {
      observer.observe(block);
    });

    // Keyboard focus and in-page links never land on a block that is still hidden.
    document.addEventListener("focusin", function (event) {
      const block = event.target.closest && event.target.closest("[data-reveal]");

      if (block) {
        reveal(block, true);
      }
    });
    window.addEventListener("hashchange", revealTarget);
    revealTarget();

    onReduceChange(function () {
      if (reduce.matches) {
        root.classList.remove("motion");
      }
    });
  }

  // --- the parallax backdrop -------------------------------------------------

  function setupParallax() {
    const layer = document.querySelector("[data-parallax]");

    if (!layer) {
      return;
    }

    // The bands repeat every `period` pixels (set in the stylesheet), so moving
    // by the remainder looks the same as moving the whole way, and the layer
    // never runs out at either end of a long page.
    const period = parseFloat(window.getComputedStyle(layer).getPropertyValue("--backdrop-period"));
    let queued = false;

    function place() {
      queued = false;

      if (reduce.matches || !(period > 0)) {
        layer.style.transform = "";
        return;
      }

      const shift = (window.scrollY * PARALLAX_RATE) % period;
      layer.style.transform = "translate3d(0, " + -((shift + period) % period) + "px, 0)";
    }

    window.addEventListener(
      "scroll",
      function () {
        if (!queued) {
          queued = true;
          window.requestAnimationFrame(place);
        }
      },
      { passive: true }
    );
    onReduceChange(place);
    place();
  }

  // --- a skeleton while a workspace page loads --------------------------------

  // The pages are rendered on the server, so the only real wait is between
  // following a link and the next page arriving. The skeleton covers that
  // wait and nothing else: not form submissions (saving keeps the form and
  // its own feedback), downloads, in-page links, links opened elsewhere, or a
  // link the leave guard stopped because there are unsaved changes.
  function setupSkeleton() {
    const pane = document.querySelector("main.pane");
    const body = pane && pane.querySelector("[data-pane-body]");
    const skeleton = pane && pane.querySelector("[data-pane-skeleton]");
    const status = pane && pane.querySelector("[data-pane-status]");
    let showTimer = 0;
    let giveUpTimer = 0;
    let followed = null;

    if (!body || !skeleton || !status) {
      return;
    }

    function restore() {
      window.clearTimeout(showTimer);
      window.clearTimeout(giveUpTimer);

      if (!skeleton.hidden) {
        skeleton.hidden = true;
        body.hidden = false;
        pane.removeAttribute("aria-busy");
        status.textContent = "";

        if (followed && document.activeElement === document.body) {
          followed.focus();
        }
      }
    }

    // The old page may only become editable again once the navigation that
    // is still on its way has been cancelled. Otherwise its late answer
    // replaces the page, and anything typed into it meanwhile, without asking:
    // the leave guard checks when a navigation starts, and the form was clean
    // then. Nothing is saved here; the retry link goes through the leave
    // guard like any other link.
    function stopOpening() {
      if (!skeleton.hidden) {
        window.stop();
      }

      restore();
    }

    function show(url) {
      body.hidden = true;
      skeleton.hidden = false;
      pane.setAttribute("aria-busy", "true");
      status.textContent = "正在打开…";
      giveUpTimer = window.setTimeout(function () {
        stopOpening();
        offerRetry(url);
      }, SKELETON_GIVE_UP_MS);
    }

    function offerRetry(url) {
      const note = document.createElement("p");
      const retry = document.createElement("a");

      note.className = "notice";
      note.setAttribute("role", "status");
      retry.href = url.pathname + url.search;
      retry.textContent = "重新打开";
      note.append("页面打开得比平时慢，已停止打开，下面仍是原来的内容。", retry);
      body.prepend(note);
    }

    document.addEventListener("click", function (event) {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }

      const link = event.target.closest && event.target.closest("a[href]");

      if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) {
        return;
      }

      const url = new URL(link.href, window.location.href);
      const here = url.pathname === window.location.pathname && url.search === window.location.search;

      if (url.origin !== window.location.origin || !WORKSPACE_PAGE.test(url.pathname) || (here && url.hash)) {
        return;
      }

      // Decide once every click handler has run, so a leave dialog that
      // cancels the navigation wins.
      window.setTimeout(function () {
        if (event.defaultPrevented) {
          return;
        }

        restore();
        followed = link;
        showTimer = window.setTimeout(function () {
          show(url);
        }, SKELETON_DELAY_MS);
      }, 0);
    });

    // Coming back to this page from the back/forward cache, or a load the
    // browser already stopped, gives the page back as it was. Escape stops
    // the opening first.
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        restore();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        stopOpening();
      }
    });

    if (window.navigation && window.navigation.addEventListener) {
      window.navigation.addEventListener("navigateerror", restore);
    }
  }
})();
