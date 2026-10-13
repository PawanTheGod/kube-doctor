(function () {
  "use strict";
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Sticky nav shadow.
  var nav = document.getElementById("nav");
  var onScroll = function () {
    nav.classList.toggle("scrolled", window.scrollY > 8);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // Mobile menu.
  var btn = document.getElementById("menuBtn");
  var menu = document.getElementById("mobileMenu");
  btn.addEventListener("click", function () {
    var open = menu.hasAttribute("hidden");
    if (open) {
      menu.removeAttribute("hidden");
      btn.setAttribute("aria-expanded", "true");
    } else {
      menu.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", "false");
    }
  });
  menu.addEventListener("click", function (e) {
    if (e.target.tagName === "A") {
      menu.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", "false");
    }
  });

  // Scroll reveals (content stays visible if JS/IO fails: .in added only on intersect;
  // no-JS keeps .reveal hidden — so add a safety timeout fallback).
  var reveal = function () {
    var els = document.querySelectorAll(".reveal");
    if (reduced || !("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("in");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );
    els.forEach(function (el) { io.observe(el); });
  };
  reveal();

  // Showcase tabs.
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".tabpanel"));
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (t) {
        t.classList.remove("active");
        t.setAttribute("aria-selected", "false");
      });
      panels.forEach(function (p) { p.hidden = true; });
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      var panel = document.getElementById(tab.getAttribute("data-tab"));
      if (panel) panel.hidden = false;
    });
  });

  // Copy buttons with clipboard fallback.
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var el = document.getElementById(button.getAttribute("data-copy"));
      var text = el ? el.textContent : "";
      var done = function () {
        button.textContent = "Copied";
        button.classList.add("done");
        setTimeout(function () {
          button.textContent = "Copy";
          button.classList.remove("done");
        }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { done(); });
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) { /* best effort */ }
        document.body.removeChild(ta);
        done();
      }
    });
  });
})();
