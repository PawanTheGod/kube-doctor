(function () {
  "use strict";

  var form = document.getElementById("form");
  var drop = document.getElementById("drop");
  var fileInput = document.getElementById("file");
  var dropIdle = document.getElementById("dropIdle");
  var dropFile = document.getElementById("dropFile");
  var preview = document.getElementById("preview");
  var fileName = document.getElementById("fileName");
  var fileInfo = document.getElementById("fileInfo");
  var fileErr = document.getElementById("fileErr");
  var logs = document.getElementById("logs");
  var logCount = document.getElementById("logCount");
  var signalCount = document.getElementById("signalCount");
  var nsInput = document.getElementById("ns");
  var backendSel = document.getElementById("backend");
  var backendPill = document.getElementById("backendPill");
  var runBtn = document.getElementById("runBtn");
  var progress = document.getElementById("progress");
  var stage = document.getElementById("stage");
  var formErr = document.getElementById("formErr");
  var empty = document.getElementById("empty");
  var out = document.getElementById("out");
  var copyReport = document.getElementById("copyReport");
  var runFlags = document.getElementById("runFlags");

  var ACCEPT = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"];
  var MAX_BYTES = 15 * 1024 * 1024;
  var SIGNAL = /error|fail|exception|oom|timeout|refused|unknown|denied|killed|backoff|pending|evict/i;
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var currentFile = null;
  var objectUrl = null;
  var lastResult = null;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // Remember workspace preferences (never secrets — names only).
  try {
    if (localStorage.getItem("ksd-ns")) nsInput.value = localStorage.getItem("ksd-ns");
    if (localStorage.getItem("ksd-backend") === "openai-compat") {
      backendSel.value = "openai-compat";
      backendPill.textContent = "openai-compat";
    }
  } catch (e) { /* private mode: carry on without memory */ }

  function setFile(file) {
    fileErr.hidden = true;
    if (!file) {
      currentFile = null;
      dropIdle.hidden = false;
      dropFile.hidden = true;
      fileInput.value = "";
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      return;
    }
    var suffix = "." + (file.name.split(".").pop() || "").toLowerCase();
    if (ACCEPT.indexOf(suffix) < 0) {
      fileErr.textContent = "Unsupported file type. Use: " + ACCEPT.join(", ") + ".";
      fileErr.hidden = false;
      return;
    }
    if (file.size > MAX_BYTES) {
      fileErr.textContent = "Screenshot exceeds 15 MB.";
      fileErr.hidden = false;
      return;
    }
    if (!file.size) {
      fileErr.textContent = "That file is empty.";
      fileErr.hidden = false;
      return;
    }
    currentFile = file;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(file);
    preview.src = objectUrl;
    fileName.textContent = file.name;
    fileInfo.textContent = (file.size / 1024).toFixed(1) + " KB · " + (file.type || suffix);
    dropIdle.hidden = true;
    dropFile.hidden = false;
  }

  drop.addEventListener("click", function (e) {
    if (e.target.id !== "removeFile") fileInput.click();
  });
  drop.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function () { setFile(fileInput.files[0] || null); });
  document.getElementById("removeFile").addEventListener("click", function (e) {
    e.stopPropagation();
    setFile(null);
  });
  // Whole panel is a drop target; the zone highlights.
  var panel = drop.parentElement;
  ["dragover", "dragenter"].forEach(function (ev) {
    panel.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    panel.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  panel.addEventListener("drop", function (e) {
    if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });
  document.addEventListener("paste", function (e) {
    var files = (e.clipboardData && e.clipboardData.files) || [];
    if (!files.length) return;
    if (files[0].type.indexOf("image/") === 0) { setFile(files[0]); return; }
    fileErr.textContent = "Clipboard has no image — copy a screenshot first.";
    fileErr.hidden = false;
  });

  function updateLogMeta() {
    var lines = logs.value.split("\n");
    var hits = lines.filter(function (l) { return SIGNAL.test(l); }).length;
    logCount.textContent = logs.value.length + " chars";
    signalCount.textContent = hits ? hits + " signal line" + (hits === 1 ? "" : "s") : "";
  }
  logs.addEventListener("input", updateLogMeta);
  updateLogMeta();

  backendSel.addEventListener("change", function () {
    backendPill.textContent = backendSel.value;
  });

  // Enter in single-line inputs must not fire the run; Ctrl+Enter runs it.
  form.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.id === "ns") e.preventDefault();
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  function copyText(text, btn, label) {
    var done = function () {
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = label || "Copy"; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, done);
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* best effort */ }
      document.body.removeChild(ta);
      done();
    }
  }

  function riskPill(risk) {
    return risk === "mutating"
      ? '<span class="risk mut">mutating</span>'
      : '<span class="risk ro">read-only</span>';
  }

  function meter(confidence) {
    var filled = { high: 3, medium: 2, low: 1 }[confidence] || 0;
    var bars = "";
    for (var i = 0; i < 3; i++) bars += '<i class="' + (i < filled ? "on" : "") + '"></i>';
    return '<span class="meter" role="img" aria-label="confidence ' + esc(confidence) + '">' + bars + "</span>";
  }

  function reportMarkdown(r) {
    var d = r.diagnosis;
    var out = ["# Diagnosis (" + d.causes[0].category + ", " + d.causes[0].confidence + " confidence)", ""];
    out.push(d.summary, "");
    d.causes.forEach(function (c, i) {
      out.push((i + 1) + ". " + c.title + " [" + c.category + "]");
      c.evidence.forEach(function (e) { out.push("   - " + e); });
    });
    out.push("", "Next steps:");
    d.next_steps.forEach(function (s) {
      out.push("- [" + s.risk + "] " + s.description + (s.command ? " `$ " + s.command + "`" : ""));
    });
    if (d.missing_info.length) out.push("", "Missing info:", d.missing_info.map(function (m) { return "- " + m; }).join("\n"));
    return out.join("\n");
  }

  function render(result) {
    lastResult = result;
    var d = result.diagnosis;
    var top = d.causes[0];
    var insufficient = top.category === "unknown";
    var banner = document.getElementById("banner");
    banner.className = "banner " + (insufficient ? "warn" : "ok");
    banner.innerHTML =
      '<div class="k">' + (insufficient ? "INSUFFICIENT EVIDENCE" :
        "TOP FINDING · " + esc(top.category) + " · " + esc(top.confidence).toUpperCase() + " CONFIDENCE") + "</div>" +
      "<h3>" + (insufficient ? "No reliable diagnosis possible" : esc(top.title)) + "</h3>" +
      "<p>" + esc(d.summary) + "</p>" +
      '<p class="meta">Model ' + esc(result.model_id || "unknown") + " · " + esc(result.backend || "") + "</p>";

    var flags = [];
    if (result.used_fallback) flags.push("answered by the fallback model");
    if (result.repaired) flags.push("first reply repaired once");
    if (flags.length) {
      runFlags.textContent = "Note: " + flags.join("; ") + ".";
      runFlags.hidden = false;
    } else { runFlags.hidden = true; }

    document.getElementById("summary").textContent = d.summary;

    document.getElementById("causes").innerHTML = d.causes.map(function (c, i) {
      var ev = c.evidence.map(function (e) { return "<li>" + esc(e) + "</li>"; }).join("");
      return '<li><details class="cause"' + (i === 0 ? " open" : "") + ">" +
        "<summary><span class='t'>" + (i + 1) + ". " + esc(c.title) + "</span> " +
        '<span class="pillcat">' + esc(c.category) + "</span> " + meter(c.confidence) + "</summary>" +
        '<ul class="ev">' + ev + "</ul></details></li>";
    }).join("");

    document.getElementById("steps").innerHTML = d.next_steps.length ? d.next_steps.map(function (s) {
      var cmd = s.command
        ? '<pre class="cmd"><code></code><button type="button">Copy</button></pre>'
        : (s.rationale.indexOf("command withheld") >= 0
          ? '<p class="withheld">Command withheld by the safety lint — see rationale.</p>'
          : "");
      return "<li class='step'><div>" + esc(s.description) + " " + riskPill(s.risk) + "</div>" +
        cmd + '<p class="why">Why: ' + esc(s.rationale) + "</p></li>";
    }).join("") : "<li class='muted'>No steps suggested.</li>";

    Array.prototype.forEach.call(document.querySelectorAll("#steps .cmd"), function (pre, i) {
      var withCmd = d.next_steps.filter(function (s) { return s.command; });
      pre.querySelector("code").textContent = withCmd[i].command;
      pre.querySelector("button").addEventListener("click", function (e) {
        copyText(withCmd[i].command, e.target);
      });
    });

    document.getElementById("missing").innerHTML = d.missing_info.length
      ? d.missing_info.map(function (m) { return "<li>" + esc(m) + "</li>"; }).join("")
      : "<li class='muted'>Nothing material — the evidence was sufficient.</li>";
    var rn = document.getElementById("redactNote");
    if (result.redactions) {
      rn.textContent = result.redactions + " secret value(s) redacted before the model call" +
        (result.redaction_summary ? " (" + result.redaction_summary + ")" : "") + ".";
      rn.hidden = false;
    } else { rn.hidden = true; }

    var obs = result.observations;
    document.getElementById("obs").innerHTML = (obs && obs.items.length)
      ? obs.items.map(function (it) {
        return '<li class="k-' + esc(it.kind) + '">[' + esc(it.kind) + "] " + esc(it.text) + "</li>";
      }).join("") + (obs.notes ? "<li>[notes] " + esc(obs.notes) + "</li>" : "")
      : "<li>No legible observations.</li>";
    var meta = document.getElementById("meta");
    meta.innerHTML = "";
    [["model", result.model_id], ["primary model", result.primary_model_id],
     ["backend", result.backend], ["stages", (result.stages_run || []).join(", ")],
     ["redactions", String(result.redactions)], ["repaired", String(result.repaired)],
     ["fallback used", String(result.used_fallback)]
    ].forEach(function (pair) {
      var dt = document.createElement("dt"); dt.textContent = pair[0];
      var dd = document.createElement("dd"); dd.textContent = pair[1] || "—";
      meta.appendChild(dt); meta.appendChild(dd);
    });
    document.getElementById("techBox").hidden = false;
    document.getElementById("techEmpty").hidden = true;

    copyReport.hidden = false;
    empty.hidden = true;
    out.hidden = false;
    document.getElementById("results").scrollIntoView({
      behavior: reducedMotion ? "auto" : "smooth", block: "start"
    });
  }

  copyReport.addEventListener("click", function (e) {
    if (lastResult) copyText(reportMarkdown(lastResult), e.target, "Copy full report");
  });

  function fail(message, hint) {
    formErr.innerHTML = "<strong>" + esc(message) + "</strong>" +
      (hint ? "<br>" + esc(hint) : "");
    formErr.hidden = false;
  }

  var ticker = null;
  function startElapsed() {
    var t0 = Date.now();
    stopElapsed();
    ticker = setInterval(function () {
      stage.textContent = "Awaiting model (observe → diagnose)… " +
        Math.round((Date.now() - t0) / 1000) + "s elapsed";
    }, 1000);
  }
  function stopElapsed() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    formErr.hidden = true;
    if (!currentFile && !logs.value.trim()) {
      fail("Nothing to diagnose.", "Upload a screenshot, or paste logs, then run again.");
      return;
    }
    try {
      localStorage.setItem("ksd-ns", nsInput.value);
      localStorage.setItem("ksd-backend", backendSel.value);
    } catch (err) { /* private mode */ }
    var fd = new FormData();
    if (currentFile) fd.append("image", currentFile, currentFile.name);
    fd.append("logs", logs.value);
    fd.append("namespace", nsInput.value);
    fd.append("context", document.getElementById("ctx").value);
    fd.append("backend", backendSel.value);
    runBtn.disabled = true;
    progress.hidden = false;
    stage.textContent = "Uploading evidence…";
    fetch("/api/diagnose", { method: "POST", body: fd }).then(function (resp) {
      stage.textContent = "Upload sent — awaiting model (observe → diagnose)…";
      startElapsed();
      return resp.json().then(function (payload) {
        return { status: resp.status, payload: payload };
      });
    }).then(function (r) {
      stopElapsed();
      stage.textContent = "Rendering report…";
      runBtn.disabled = false;
      progress.hidden = true;
      if (r.status === 200 && r.payload.diagnosis) { render(r.payload); return; }
      var msg = (r.payload && r.payload.error) || "Unexpected response from the server.";
      var hint = r.status === 400 ? "Check the inputs above and try again."
        : r.status === 502 ? "The model backend is unavailable. Wait a minute and retry — or ask the operator about a fallback model."
        : "Please retry. If it persists, re-run with a clearer screenshot.";
      fail(msg, hint);
    }).catch(function () {
      stopElapsed();
      runBtn.disabled = false;
      progress.hidden = true;
      fail("Could not reach the server.", "Check your connection and retry.");
    });
  });

  // Sidebar active section.
  var links = Array.prototype.slice.call(document.querySelectorAll(".side a"));
  window.addEventListener("scroll", function () {
    var current = links[0];
    ["run", "results", "technical"].forEach(function (id, i) {
      var el = document.getElementById(id);
      if (el && el.getBoundingClientRect().top < 140) current = links[i];
    });
    links.forEach(function (a) { a.classList.toggle("active", a === current); });
  }, { passive: true });
})();
