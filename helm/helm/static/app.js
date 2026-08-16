/* helm console — the living loop, the preferences, and the chat window.
 *
 * Everything here talks to the documented API. There is no privileged path.
 * Preferences are written BOTH to localStorage (so a JS-only reload is
 * instant) and to the server (so they follow the signet subject).
 */
(function () {
  "use strict";

  var PREF_KEYS = {
    theme: "nnd-theme",
    feeds: "nnd-feeds",
    rail: "nnd-rail",
    feedsWidth: "nnd-feeds-w",
    railWidth: "nnd-rail-w"
  };

  function boot() {
    return window.HELM || {};
  }

  function api(path, options) {
    var opts = options || {};
    opts.headers = opts.headers || {};
    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (data) {
        return { status: r.status, ok: r.ok, data: data };
      }).catch(function () {
        return { status: r.status, ok: r.ok, data: {} };
      });
    });
  }

  function savePrefs(patch) {
    if (!boot().authenticated) { return; }
    api("/prefs", { method: "PUT", body: patch }).catch(function () {});
  }

  /* ------------------------------------------------------------- theme */
  function applyTheme(value) {
    var root = document.documentElement;
    if (value === "system") { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", value); }
    document.querySelectorAll(".theme-toggle button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.t === value));
    });
  }
  window.setTheme = function (value) {
    localStorage.setItem(PREF_KEYS.theme, value);
    applyTheme(value);
    savePrefs({ theme: value });
  };

  /* ------------------------------------------------------- rail collapse */
  function applyRail(state) {
    document.body.classList.toggle("rail-collapsed", state === "collapsed");
    var b = document.querySelector(".rail-collapse");
    if (b) {
      b.innerHTML = state === "collapsed" ? "&#10216;" : "&#10217;";
      b.title = state === "collapsed" ? "expand NemoClerk" : "collapse NemoClerk";
      b.setAttribute("aria-expanded", String(state !== "collapsed"));
    }
  }
  window.toggleRail = function () {
    var next = localStorage.getItem(PREF_KEYS.rail) === "collapsed" ? "expanded" : "collapsed";
    localStorage.setItem(PREF_KEYS.rail, next);
    applyRail(next);
    savePrefs({ rail_collapsed: next === "collapsed" });
  };

  function applyFeeds(state) {
    document.body.classList.toggle("feeds-collapsed", state === "collapsed");
    var b = document.querySelector(".feeds-collapse");
    if (b) {
      b.innerHTML = state === "collapsed" ? "&#10217;" : "&#10216;";
      b.title = state === "collapsed" ? "expand feeds" : "collapse feeds";
      b.setAttribute("aria-expanded", String(state !== "collapsed"));
    }
  }
  window.toggleFeeds = function () {
    var next = localStorage.getItem(PREF_KEYS.feeds) === "collapsed" ? "expanded" : "collapsed";
    localStorage.setItem(PREF_KEYS.feeds, next);
    applyFeeds(next);
    savePrefs({ feeds_collapsed: next === "collapsed" });
  };

  /* --------------------------------------------------------- rail widths */
  function trackWidth(el, storageKey, prefKey) {
    if (!el || !window.ResizeObserver) { return; }
    var timer = null;
    new ResizeObserver(function () {
      if (document.body.classList.contains("rail-collapsed") &&
          el.classList.contains("copilot-rail")) { return; }
      if (document.body.classList.contains("feeds-collapsed") &&
          el.classList.contains("feeds")) { return; }
      var w = Math.round(el.getBoundingClientRect().width);
      if (w < 60) { return; }
      localStorage.setItem(storageKey, String(w));
      clearTimeout(timer);
      timer = setTimeout(function () {
        var patch = {};
        patch[prefKey] = w;
        savePrefs(patch);
      }, 400);
    }).observe(el);
  }

  /* ----------------------------------------------------- situation banner */
  window.dismissSituationBanner = function () {
    var el = document.getElementById("situation-banner");
    if (!el) { return; }
    var key = "nnd-sitbanner-" + (boot().page || "helm");
    localStorage.setItem(key, "dismissed");
    el.classList.add("sitbanner-hidden");
  };

  /* --------------------------------------------------------- chart toggle */
  function wireChartToggles() {
    document.querySelectorAll(".chart-toggle").forEach(function (strip) {
      var btns = strip.querySelectorAll("button");
      var panels = [];
      var n = strip.nextElementSibling;
      while (n && n.classList && n.classList.contains("visual-region")) {
        panels.push(n);
        n = n.nextElementSibling;
      }
      btns.forEach(function (b, i) {
        b.addEventListener("click", function () {
          btns.forEach(function (x) { x.setAttribute("aria-selected", "false"); });
          b.setAttribute("aria-selected", "true");
          panels.forEach(function (p, j) { p.style.display = (j === i) ? "" : "none"; });
        });
      });
    });
  }

  /* -------------------------------------------------------- scenario modal */
  window.openScenario = function () {
    var d = document.getElementById("scenario-modal");
    if (d && d.showModal) { d.showModal(); } else if (d) { d.setAttribute("open", ""); }
  };
  window.closeScenario = function () {
    var d = document.getElementById("scenario-modal");
    if (d && d.close) { d.close(); } else if (d) { d.removeAttribute("open"); }
  };

  /* ------------------------------------------------------------ user menu */
  function wireUserMenu() {
    var wrap = document.querySelector(".user-menu");
    if (!wrap) { return; }
    var btn = wrap.querySelector(".avatar-btn");
    var menu = wrap.querySelector(".menu");
    function set(open) {
      menu.setAttribute("data-open", open ? "true" : "false");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    set(false);
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      set(menu.getAttribute("data-open") !== "true");
    });
    menu.addEventListener("click", function (e) { e.stopPropagation(); });
    document.addEventListener("click", function () { set(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { set(false); btn.focus(); }
    });
  }

  /* ------------------------------------------------------------- NemoClerk */
  function chipNode(chip) {
    var span = document.createElement("span");
    span.className = "tool-chip" + (chip.refused ? " refused" : "");
    span.textContent = chip.chip + (chip.refused ? " · ledgered" : "");
    span.title = chip.summary || "";
    if (chip.refused) { span.setAttribute("data-coordinate", "scenario://helm/agent-refusal"); }
    return span;
  }

  function appendTurn(transcript, role, text, chips) {
    // The first real turn retires the empty state. Leaving it in place kept
    // the centred layout on a real conversation and stranded its copy above
    // the first exchange.
    if (transcript.classList.contains("is-empty")) {
      transcript.classList.remove("is-empty");
      var placeholder = transcript.querySelector(".empty");
      if (placeholder) { placeholder.remove(); }
    }
    var p = document.createElement("p");
    p.className = "turn";
    var who = document.createElement("span");
    who.className = "who";
    who.textContent = (role === "you" ? "you:" : "NemoClerk:") + " ";
    p.appendChild(who);
    p.appendChild(document.createTextNode(text));
    if (chips && chips.length) {
      var row = document.createElement("span");
      row.className = "chips";
      chips.forEach(function (c) { row.appendChild(chipNode(c)); });
      p.appendChild(row);
    }
    transcript.appendChild(p);
    transcript.scrollTop = transcript.scrollHeight;
    return p;
  }

  function wireNemoClerk() {
    var rail = document.getElementById("copilot-rail");
    if (!rail) { return; }
    var transcript = rail.querySelector(".transcript");
    var input = rail.querySelector(".composer input");
    var send = rail.querySelector(".composer .send");
    if (!transcript || !input || !send) { return; }

    function ask(question) {
      if (!question.trim()) { return; }
      appendTurn(transcript, "you", question, []);
      input.value = "";
      var thinking = document.createElement("p");
      thinking.className = "turn pending";
      thinking.textContent = "NemoClerk is calling its tools…";
      transcript.appendChild(thinking);
      transcript.scrollTop = transcript.scrollHeight;
      api("/nemoclerk/message", {
        method: "POST",
        body: { message: question, feature_area: boot().page || "helm" }
      }).then(function (r) {
        thinking.remove();
        var d = r.data || {};
        var node = appendTurn(transcript, "nemoclerk", d.text || "(no answer)", d.chips || []);
        if (d.refused) {
          node.classList.add("refusal-flash");
          refreshApprovals();
        }
      }).catch(function () {
        thinking.remove();
        appendTurn(transcript, "nemoclerk", "I could not reach my own API.", []);
      });
    }

    send.addEventListener("click", function () { ask(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); ask(input.value); }
    });
    rail.querySelectorAll(".prompt-chips button").forEach(function (b) {
      b.addEventListener("click", function () { ask(b.textContent); });
    });
  }

  /* -------------------------------------------------- approvals + the wave */
  function approvalWave() {
    var dots = document.querySelectorAll(".stage-timeline .stage .dot");
    if (!dots.length) { return; }
    ["gated", "approved", "executed", "ledgered"].forEach(function (_, i) {
      var dot = dots[i + 2];
      if (!dot) { return; }
      setTimeout(function () {
        dot.classList.remove("current", "pending");
        dot.classList.add("done", "wave-fill");
        var stage = dot.closest(".stage");
        if (stage) { stage.classList.remove("current"); stage.classList.add("done"); }
      }, 300 * i);
    });
  }

  function refreshApprovals() {
    api("/approvals").then(function (r) {
      var badge = document.querySelector("#approval-queue .hold-badge");
      var n = (r.data && r.data.pending) || 0;
      if (badge) { badge.textContent = n + " waiting"; }
    }).catch(function () {});
  }

  function decisionMessage(status, data) {
    var detail = (data && data.detail) || {};
    if (typeof detail === "string") { return detail; }
    if (detail.reason) { return detail.reason; }
    if (status === 409) { return "This approval was already decided by someone else."; }
    if (status === 404) { return "This approval is no longer in the queue."; }
    if (status === 422) { return "The substrate rejected the decision: a signet subject is required."; }
    if (status === 502 || status === 503) {
      return "throughline is not responding, so the decision was NOT recorded. Nothing was executed.";
    }
    return "The decision was refused.";
  }

  function wireDecide() {
    document.querySelectorAll("[data-decide]").forEach(function (btn) {
      // The label carries the operator's identity, and that label IS the
      // claim. It used to be overwritten with a generic "Approve dispatch"
      // on failure, dropping the one thing the product exists to show.
      var original = btn.textContent;
      var inflight = false;
      btn.addEventListener("click", function () {
        // `btn.disabled = true` used to run here. Disabling the element
        // that currently holds focus drops focus to <body>, so a keyboard
        // user pressing Approve was dumped to the top of the document at
        // the exact moment the page had something to tell them, and the
        // relabel — which a screen reader announces as a name change on
        // the FOCUSED control — was announced to nobody. aria-disabled
        // plus an in-flight guard refuses the second click just as firmly
        // and keeps the operator where they were.
        if (inflight) { return; }
        var id = btn.getAttribute("data-decide");
        var decision = btn.getAttribute("data-decision") || "approve";
        var box = document.getElementById("decision-result");
        inflight = true;
        btn.setAttribute("aria-disabled", "true");
        btn.textContent = (decision === "approve" ? "Approving" : "Rejecting") +
          " as " + boot().subject + "…";
        api("/approvals/" + encodeURIComponent(id) + "/decide", {
          method: "POST",
          body: { decision: decision, rationale: "decided from the console" }
        }).then(function (r) {
          btn.textContent = original;
          if (r.ok) {
            if (decision === "approve") { approvalWave(); }
            if (box) {
              box.className = "box";
              // An expected success does not interrupt.
              box.setAttribute("aria-live", "polite");
              box.innerHTML = "";
              var p = document.createElement("p");
              p.textContent = decision + "d by " + boot().subject +
                " — the effect executed and the ledger row arrived.";
              box.appendChild(p);
            }
            setTimeout(function () { window.location.reload(); }, 2000);
            return;
          }
          inflight = false;
          btn.removeAttribute("aria-disabled");
          if (box) {
            box.className = "box refuse refusal-flash";
            // "REFUSED", or "the decision was NOT recorded — nothing was
            // executed", is the one sentence on this page a user must not
            // miss, and it is raised BEFORE the content is written so the
            // level is already in force when the mutation lands.
            box.setAttribute("aria-live", "assertive");
            box.innerHTML = "";
            var h = document.createElement("h3");
            h.textContent = r.status === 403 ? "REFUSED" : "Not recorded";
            var msg = document.createElement("p");
            msg.textContent = decisionMessage(r.status, r.data);
            box.appendChild(h);
            box.appendChild(msg);
            var note = document.createElement("p");
            note.className = "muted";
            note.style.fontSize = "15px";
            note.textContent = "The stage timeline below reflects the substrate, " +
              "not this attempt. Reloading will show its true state.";
            box.appendChild(note);
          }
          // An already-decided approval must stop showing a running clock.
          if (r.status === 409 || r.status === 404) {
            setTimeout(function () { window.location.reload(); }, 2500);
          }
        });
      });
    });
  }

  /* ------------------------------------------------------ ledger row walk */
  function wireLedgerWalk() {
    document.querySelectorAll("tr[data-effect-id]").forEach(function (row) {
      var effect = row.getAttribute("data-effect-id");
      var target = "/walk/" + encodeURIComponent(effect);
      row.style.cursor = "pointer";

      // The whole-row affordance was mouse-only: the pointer cursor
      // advertised it and no key could take it up. The row is now a link
      // in its own right — focusable, Enter/Space activated, and named.
      // The per-row "cause walk" anchor inside it is left exactly as it
      // was and stays the labelled path; this ADDS a second way in.
      if (!row.hasAttribute("tabindex")) { row.setAttribute("tabindex", "0"); }
      if (!row.hasAttribute("role")) { row.setAttribute("role", "link"); }
      if (!row.hasAttribute("aria-label")) {
        row.setAttribute("aria-label", "Walk the cause of effect " + effect);
      }

      row.addEventListener("click", function () { window.location.href = target; });
      row.addEventListener("keydown", function (e) {
        // Let the real anchors keep their own behaviour rather than firing
        // twice: only the row itself answers here.
        if (e.target !== row) { return; }
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          window.location.href = target;
        }
      });
    });
  }

  /* ---------------------------------------------------------- class arrival */
  window.hotReloadClass = function (name) {
    // An action reports what it DID. This used to reload the page regardless,
    // so a reload that never ran looked exactly like one that did.
    api("/admin/classes/" + encodeURIComponent(name) + "/reload", { method: "POST" })
      .then(function (r) {
        var d = r.data || {};
        var row = document.querySelector("#classes tr[data-class='" + name + "']") ||
          document.querySelector("#classes");
        var note = document.createElement("div");
        note.className = d.performed ? "note arrival-banner" : "note refuse refusal-flash";
        note.textContent = d.summary || "the reload returned no result";
        if (row && row.parentNode) { row.parentNode.insertBefore(note, row.nextSibling); }
        // The note beside the row is where the eye looks; the live region
        // is where a screen reader listens. Both say the same sentence —
        // this ADDS the announcement, it does not move the note.
        var spoken = document.getElementById("class-reload-result");
        if (spoken) { spoken.textContent = note.textContent; }
        if (d.performed) { setTimeout(function () { window.location.reload(); }, 1800); }
      });
  };

  function markArrivals() {
    document.querySelectorAll("[data-arrived='true']").forEach(function (el) {
      el.classList.add("feed-arrived");
    });
    document.querySelectorAll("tr[data-arrived='true']").forEach(function (el) {
      el.classList.add("arrival-flash");
    });
  }

  /* -------------------------------------------------------------- startup */
  function init() {
    var prefs = boot().prefs || {};
    var theme = localStorage.getItem(PREF_KEYS.theme) || prefs.theme || "system";
    applyTheme(theme);
    var railState = localStorage.getItem(PREF_KEYS.rail) ||
      (prefs.rail_collapsed ? "collapsed" : "expanded");
    applyRail(railState);
    var feedsState = localStorage.getItem(PREF_KEYS.feeds) ||
      (prefs.feeds_collapsed ? "collapsed" : "expanded");
    applyFeeds(feedsState);

    var feeds = document.querySelector("aside.feeds");
    var rail = document.querySelector("aside.copilot-rail");
    var fw = localStorage.getItem(PREF_KEYS.feedsWidth) || prefs.feeds_width;
    var rw = localStorage.getItem(PREF_KEYS.railWidth) || prefs.rail_width;
    if (feeds && fw) { feeds.style.width = parseInt(fw, 10) + "px"; }
    if (rail && rw) { rail.style.width = parseInt(rw, 10) + "px"; }
    trackWidth(feeds, PREF_KEYS.feedsWidth, "feeds_width");
    trackWidth(rail, PREF_KEYS.railWidth, "rail_width");

    var banner = document.getElementById("situation-banner");
    if (banner) {
      var key = "nnd-sitbanner-" + (boot().page || "helm");
      if (localStorage.getItem(key) === "dismissed") {
        banner.classList.add("sitbanner-hidden");
      }
    }

    wireChartToggles();
    wireUserMenu();
    wireNemoClerk();
    wireDecide();
    wireLedgerWalk();
    markArrivals();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
