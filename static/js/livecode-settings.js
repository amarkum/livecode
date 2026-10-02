// LiveCode Settings: Appearance, Harness, Plan mode, Memory and Browser pages, settings search,
// import/export, and the client-side preferences they drive (editor options, chat text size,
// theme following the system, notifications). Loaded after livecode.js and uses its helpers.
(function() {
  "use strict";

  // ---------------------------------------------------------------- client preferences

  const EDITOR_FONT_DEFAULT = "'LivecodeMono', 'Prima Sans Mono W01 Roman', 'PrimaSansMonoW01-Roman', Consolas, 'Liberation Mono', 'Courier New', ui-monospace, SFMono-Regular, Menlo, Monaco, monospace";

  const CLIENT_DEFAULTS = {
    themeFollowSystem: false,
    themeDark: "dark",
    themeLight: "white",
    chatZoom: "100",
    reduceMotion: false,
    editorFontSize: 13,
    editorFontFamily: "",
    editorLineHeight: 0,
    editorWordWrap: "on",
    editorMinimap: false,
    editorTabSize: "4",
    editorRenderWhitespace: "boundary",
    editorCursorStyle: "line",
    editorCursorBlinking: "solid",
    editorLigatures: false,
    editorLineNumbers: "on",
    editorStickyScroll: false,
    editorBracketColors: true,
    editorSmoothScrolling: true,
    editorAutosaveMs: 1000,
    notifyOnFinish: false,
    notifyOnApproval: true,
    notifySound: false,
    confirmCloseWhileRunning: true,
    planAutoOpen: false,
  };
  Object.keys(CLIENT_DEFAULTS).forEach(function(key) {
    if (!(key in _LIVECODE_SETTING_DEFAULTS)) _LIVECODE_SETTING_DEFAULTS[key] = CLIENT_DEFAULTS[key];
  });

  function pref(key) {
    const value = _livecodeSettingsGet(key);
    return value === undefined || value === null ? CLIENT_DEFAULTS[key] : value;
  }

  function esc(text) { return _livecodeEscapeHtml(String(text == null ? "" : text)); }

  function attr(text) { return esc(text).replace(/"/g, "&quot;"); }

  function toast(message) { _livecodeShowIdeToast(message); }

  function postJson(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(function(resp) { return resp.json().catch(function() { return {}; }).then(function(data) { return { ok: resp.ok, data: data || {} }; }); })
      .then(function(res) {
        if (!res.ok || res.data.error || res.data.success === false) throw new Error(res.data.error || ("Request failed: " + url));
        return res.data;
      });
  }

  function rerender(sections) {
    if (typeof _livecodeSettingsVisible !== "function" || !_livecodeSettingsVisible()) return;
    if (_livecodeSettingsQuery || !sections || sections.indexOf(_livecodeSettingsSection) !== -1) _livecodeRenderSettingsPage();
  }

  // ---------------------------------------------------------------- appearance: apply

  function editorOptions() {
    const family = String(pref("editorFontFamily") || "").trim();
    return {
      fontSize: Number(pref("editorFontSize")) || 13,
      fontFamily: family ? family + ", " + EDITOR_FONT_DEFAULT : (window.LIVECODE_MONACO_FONT || EDITOR_FONT_DEFAULT),
      lineHeight: Number(pref("editorLineHeight")) || 0,
      wordWrap: pref("editorWordWrap"),
      minimap: { enabled: !!pref("editorMinimap") },
      renderWhitespace: pref("editorRenderWhitespace"),
      cursorStyle: pref("editorCursorStyle"),
      cursorBlinking: pref("editorCursorBlinking"),
      fontLigatures: !!pref("editorLigatures"),
      lineNumbers: pref("editorLineNumbers"),
      stickyScroll: { enabled: !!pref("editorStickyScroll") },
      bracketPairColorization: { enabled: !!pref("editorBracketColors") },
      smoothScrolling: !!pref("editorSmoothScrolling"),
    };
  }

  function applyTabSize(model) {
    const size = Number(pref("editorTabSize")) || 4;
    try { if (model && !model.isDisposed()) model.updateOptions({ tabSize: size, indentSize: size }); } catch (e) {}
  }

  let editorHooked = false;

  window._livecodeApplyEditorSettings = function() {
    const editor = window.ideEditor;
    if (!editor || !window.monaco) return;
    try { editor.updateOptions(editorOptions()); } catch (e) {}
    try { window.monaco.editor.getModels().forEach(applyTabSize); } catch (e) {}
    try { window.monaco.editor.remeasureFonts(); } catch (e) {}
    if (!editorHooked) {
      editorHooked = true;
      try { editor.onDidChangeModel(function() { applyTabSize(editor.getModel()); }); } catch (e) {}
      try { window.monaco.editor.onDidCreateModel(applyTabSize); } catch (e) {}
    }
  };

  window._livecodeEditorAutosaveDelay = function() {
    const ms = Number(pref("editorAutosaveMs"));
    return Number.isFinite(ms) ? Math.min(10000, Math.max(250, ms)) : 1000;
  };

  function applyInterface() {
    const body = document.body;
    if (!body) return;
    const zoom = Number(pref("chatZoom")) || 100;
    body.style.setProperty("--lc-chat-zoom", String(zoom / 100));
    body.classList.toggle("lc-chat-zoomed", zoom !== 100);
    body.classList.toggle("lc-reduce-motion", !!pref("reduceMotion"));
  }

  const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function systemTheme() {
    const dark = !darkQuery || darkQuery.matches;
    return dark ? pref("themeDark") : pref("themeLight");
  }

  function applySystemTheme() {
    if (!pref("themeFollowSystem")) return;
    const want = systemTheme();
    if (_livecodeCurrentThemeId() !== want) baseSetTheme(want);
  }

  const baseSetTheme = window.setLiveCodeTheme;
  const baseToggleTheme = window.toggleLiveCodeTheme;

  // Picking a theme by hand stops following the system.
  window.toggleLiveCodeTheme = function() {
    if (pref("themeFollowSystem")) _livecodeSettingsSet("themeFollowSystem", false);
    baseToggleTheme();
    rerender(["appearance"]);
  };

  if (darkQuery) {
    const onChange = function() { applySystemTheme(); rerender(["appearance"]); };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onChange);
    else if (darkQuery.addListener) darkQuery.addListener(onChange);
  }

  function applyAll() {
    applyInterface();
    applySystemTheme();
    window._livecodeApplyEditorSettings();
  }

  // ---------------------------------------------------------------- notifications

  const notified = {};
  let audioCtx = null;

  function chime() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const now = audioCtx.currentTime;
      [880, 1320].forEach(function(freq, i) {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + i * 0.12);
        gain.gain.exponentialRampToValueAtTime(0.12, now + i * 0.12 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.12 + 0.25);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now + i * 0.12);
        osc.stop(now + i * 0.12 + 0.3);
      });
    } catch (e) {}
  }

  function notify(key, title, body) {
    if (key && notified[key]) return;
    if (key) notified[key] = true;
    if (document.hasFocus && document.hasFocus() && document.visibilityState === "visible") return;
    if (pref("notifySound")) chime();
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, { body: body, icon: "/livecode/static/assets/favicon.png", tag: key || undefined });
      n.onclick = function() { window.focus(); n.close(); };
    } catch (e) {}
  }

  function tabTitle(sessionId) {
    const tab = (window.livecodeChatTabs || livecodeChatTabs || []).find(function(t) { return t.sessionId === sessionId; });
    return (tab && tab.title) || "your chat";
  }

  window._livecodeNotifyProgress = function(data) {
    if (!data) return;
    const sid = data.session_id || "";
    const turn = data.turn_id || "";
    if (data.status === "complete" && pref("notifyOnFinish")) {
      notify("done:" + sid + ":" + turn, "LiveCode: agent finished", "Finished working on " + tabTitle(sid) + ".");
    } else if (data.status === "progress" && pref("notifyOnApproval") && (data.type === "permission_request" || data.type === "question_request")) {
      notify("ask:" + sid + ":" + turn + ":" + (data.request_id || data.seq || ""), "LiveCode needs you", data.type === "permission_request" ? "The agent is waiting for your approval in " + tabTitle(sid) + "." : "The agent asked a question in " + tabTitle(sid) + ".");
    }
  };

  window.addEventListener("beforeunload", function(e) {
    if (!pref("confirmCloseWhileRunning")) return;
    const running = (livecodeChatTabs || []).some(function(t) { return t && t.agentRunning; });
    if (!running) return;
    e.preventDefault();
    e.returnValue = "";
  });

  // ---------------------------------------------------------------- server-backed settings

  let agent = null;
  let agentLoading = false;

  function loadAgent() {
    if (agentLoading) return;
    agentLoading = true;
    fetch("/livecode/agent/settings")
      .then(function(r) { return r.json(); })
      .then(function(data) { if (data && data.success) agent = data; })
      .catch(function() {})
      .finally(function() { agentLoading = false; rerender(["agent", "harness", "plan", "memory"]); });
  }

  function agentValue(key) {
    if (!agent) return undefined;
    return agent.settings[key];
  }

  function agentSpec(key) { return (agent && agent.schema[key]) || {}; }

  // Values whose save is still in flight. A reply to an earlier save carries the server's older value for
  // them, so they are laid over every reply until their own save has answered.
  const pendingAgent = new Map();

  function saveAgent(body, message) {
    const keys = Object.keys(body || {});
    const stamp = {};
    keys.forEach(function(key) {
      const token = {};
      stamp[key] = token;
      pendingAgent.set(key, { token: token, value: body[key] });
    });
    const settle = function() {
      keys.forEach(function(key) {
        const entry = pendingAgent.get(key);
        if (entry && entry.token === stamp[key]) pendingAgent.delete(key);
      });
    };
    const overlay = function(data) {
      if (!data || typeof data !== "object" || !pendingAgent.size) return data;
      const merged = Object.assign({}, data);
      pendingAgent.forEach(function(entry, key) { merged[key] = entry.value; });
      return merged;
    };
    return postJson("/livecode/agent/settings", body)
      .then(function(data) {
        settle();
        agent = overlay(data);
        rerender(["agent", "harness", "plan", "memory"]);
        if (message) toast(message);
      })
      .catch(function(err) { settle(); toast(err.message || String(err)); rerender(["agent", "harness", "plan", "memory"]); });
  }

  function resetAgentGroup(group, label) {
    _livecodeModalConfirm({ title: "Reset " + label + " settings?", message: "Every " + label + " setting goes back to its default.", confirmText: "Reset", danger: true }).then(function(ok) {
      if (!ok) return;
      postJson("/livecode/agent/settings/reset", { group: group })
        .then(function(data) { agent = data; rerender(["agent", "harness", "plan", "memory"]); toast(label + " settings reset"); })
        .catch(function(err) { toast(err.message || String(err)); });
    });
  }

  function modifiedBadge(key) {
    const spec = agentSpec(key);
    if (!agent || spec.default === undefined || agentValue(key) === spec.default) return "";
    return ' <button type="button" class="lc-btn livecode-settings-modified" data-settings-action="agent-reset-key" data-key="' + key + '" title="Reset to the default (' + attr(spec.default) + ')">Modified · reset</button>';
  }

  function loadingRow(title) {
    loadAgent();
    return _livecodeSettingsRowHtml(title, "Loading…", "");
  }

  function switchHtml(attrs, on, label, disabled) {
    return '<label class="lc-switch"><input type="checkbox" ' + attrs + (on ? " checked" : "") + (disabled ? " disabled" : "") + ' aria-label="' + attr(label) + '"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label>';
  }

  function segmentedHtml(label, options, current, actionAttrs) {
    return '<div class="lc-segmented" role="radiogroup" aria-label="' + attr(label) + '">' + options.map(function(o) {
      const on = String(o.value) === String(current);
      return '<button type="button" class="lc-btn' + (on ? " is-active" : "") + '" ' + actionAttrs + ' data-value="' + attr(o.value) + '" role="radio" aria-checked="' + on + '">' + esc(o.label) + "</button>";
    }).join("") + "</div>";
  }

  function agentSwitch(key, title, desc) {
    if (!agent) return loadingRow(title);
    return _livecodeSettingsRowHtml(title + modifiedBadge(key), desc, switchHtml('data-agent-setting="' + key + '"', !!agentValue(key), title));
  }

  function agentNumber(key, title, desc, unit, opts) {
    if (!agent) return loadingRow(title);
    const spec = agentSpec(key);
    const scale = (opts && opts.scale) || 1;
    const value = Number(agentValue(key)) * scale;
    const step = (opts && opts.step) || (spec.kind === "float" ? 0.05 * scale : 1);
    return _livecodeSettingsRowHtml(title + modifiedBadge(key), desc + ' <span class="livecode-settings-default">Default ' + esc(Number(spec.default) * scale) + (unit ? " " + esc(unit) : "") + ".</span>",
      '<div class="livecode-settings-number-wrap"><input type="number" class="livecode-settings-number" data-agent-setting="' + key + '" data-scale="' + scale + '" min="' + spec.min * scale + '" max="' + spec.max * scale + '" step="' + step + '" value="' + attr(value) + '" aria-label="' + attr(title) + '">' +
      (unit ? '<span class="livecode-settings-unit">' + esc(unit) + "</span>" : "") + "</div>");
  }

  function agentChoice(key, title, desc, options) {
    if (!agent) return loadingRow(title);
    return _livecodeSettingsRowHtml(title + modifiedBadge(key), desc, segmentedHtml(title, options, agentValue(key), 'data-settings-action="agent-choice" data-key="' + key + '"'));
  }

  function clientSwitch(key, title, desc) {
    return _livecodeSettingsRowHtml(title, desc, switchHtml('data-setting="' + key + '"', !!pref(key), title));
  }

  function clientChoice(key, title, desc, options) {
    return _livecodeSettingsRowHtml(title, desc, segmentedHtml(title, options, pref(key), 'data-settings-action="client-choice" data-key="' + key + '"'));
  }

  function clientNumber(key, title, desc, min, max, step, unit) {
    return _livecodeSettingsRowHtml(title, desc,
      '<div class="livecode-settings-number-wrap"><input type="number" class="livecode-settings-number" data-client-number="' + key + '" min="' + min + '" max="' + max + '" step="' + (step || 1) + '" value="' + attr(pref(key)) + '" aria-label="' + attr(title) + '">' +
      (unit ? '<span class="livecode-settings-unit">' + esc(unit) + "</span>" : "") + "</div>");
  }

  function clientText(key, title, desc, placeholder) {
    return _livecodeSettingsRowHtml(title, desc,
      '<input type="text" class="livecode-settings-text" data-client-text="' + key + '" value="' + attr(pref(key)) + '" placeholder="' + attr(placeholder || "") + '" spellcheck="false" autocomplete="off" aria-label="' + attr(title) + '">');
  }

  function headRow(title, lead, actionsHtml) {
    return '<div class="livecode-settings-head-row"><h2 class="livecode-settings-h">' + esc(title) + "</h2>" +
      (actionsHtml ? '<div class="livecode-settings-head-actions">' + actionsHtml + "</div>" : "") + "</div>" +
      (lead ? '<p class="livecode-settings-lead">' + lead + "</p>" : "");
  }

  function group(title, rows) {
    return (title ? '<h3 class="livecode-settings-subh">' + esc(title) + "</h3>" : "") + '<div class="livecode-settings-group">' + rows.join("") + "</div>";
  }

  // ---------------------------------------------------------------- pages

  window._livecodeSettingsAppearanceHtml = function() {
    const current = _livecodeCurrentThemeId();
    const follow = !!pref("themeFollowSystem");
    const cards = _LIVECODE_THEMES.map(function(t) {
      const on = !follow && t.id === current;
      return '<button type="button" class="lc-btn livecode-theme-card' + (on ? " is-active" : "") + (follow && t.id === current ? " is-system" : "") + '" data-settings-action="set-theme" data-theme="' + t.id + '" role="radio" aria-checked="' + on + '">' +
        '<span class="livecode-theme-card-swatch is-' + t.id + '"><span></span><span></span><span></span></span><span class="livecode-theme-card-label">' + esc(t.label) + "</span></button>";
    }).join("");
    let html = headRow("Appearance", "Theme, editor and conversation display. These are saved in this browser.");
    html += '<h3 class="livecode-settings-subh">Theme</h3><div class="livecode-theme-cards" role="radiogroup" aria-label="Theme">' + cards + "</div>";
    const themeRows = [clientSwitch("themeFollowSystem", "Match system appearance", "Switch between a dark and a light theme when your operating system does.")];
    if (follow) {
      themeRows.push(clientChoice("themeDark", "Dark theme", "Used while the system is dark.", [{ value: "dark", label: "Dark" }, { value: "black", label: "Black" }]));
      themeRows.push(clientChoice("themeLight", "Light theme", "Used while the system is light.", [{ value: "white", label: "Light" }, { value: "pink", label: "Pink" }]));
    }
    html += '<div class="livecode-settings-group livecode-settings-group-gap">' + themeRows.join("") + "</div>";

    const density = _livecodeConversationDensity();
    const grouping = _livecodeStepGrouping();
    html += group("Conversation", [
      clientChoice("chatZoom", "Text size", "Scales the conversation: messages, tool calls and diffs.", [{ value: "90", label: "S" }, { value: "100", label: "M" }, { value: "110", label: "L" }, { value: "125", label: "XL" }]),
      _livecodeSettingsRowHtml("Detail", "Detailed shows each file edit's diff and each command's output; Balanced shows them as one line each.",
        segmentedHtml("Conversation density", LIVECODE_CONVERSATION_DENSITIES, density, 'data-settings-action="set-density-ext"')),
      _livecodeSettingsRowHtml("Group tool calls", "Grouped folds consecutive reads, searches and other tool calls into one collapsible line.",
        segmentedHtml("Group tool calls", LIVECODE_STEP_GROUPINGS, grouping, 'data-settings-action="set-grouping-ext"')),
      clientSwitch("reduceMotion", "Reduce motion", "Turns off animations and transitions across LiveCode."),
    ]);

    html += group("Editor", [
      clientNumber("editorFontSize", "Font size", "Editor text size in pixels.", 9, 28, 1, "px"),
      clientText("editorFontFamily", "Font family", "A font installed on this computer, e.g. JetBrains Mono. Empty uses LiveCode's font.", "LiveCode default"),
      clientNumber("editorLineHeight", "Line height", "In pixels; 0 picks one from the font size.", 0, 48, 1, "px"),
      clientChoice("editorTabSize", "Tab size", "Spaces per indent level.", [{ value: "2", label: "2" }, { value: "4", label: "4" }, { value: "8", label: "8" }]),
      clientChoice("editorWordWrap", "Word wrap", "Wrap long lines to the editor width.", [{ value: "off", label: "Off" }, { value: "on", label: "On" }]),
      clientChoice("editorLineNumbers", "Line numbers", "", [{ value: "on", label: "On" }, { value: "relative", label: "Relative" }, { value: "off", label: "Off" }]),
      clientChoice("editorRenderWhitespace", "Show whitespace", "", [{ value: "none", label: "None" }, { value: "boundary", label: "Boundary" }, { value: "all", label: "All" }]),
      clientChoice("editorCursorStyle", "Cursor", "", [{ value: "line", label: "Line" }, { value: "block", label: "Block" }, { value: "underline", label: "Underline" }]),
      clientChoice("editorCursorBlinking", "Cursor blinking", "", [{ value: "solid", label: "Solid" }, { value: "blink", label: "Blink" }, { value: "smooth", label: "Smooth" }]),
      clientSwitch("editorMinimap", "Minimap", "Show a zoomed-out outline of the file beside the scrollbar."),
      clientSwitch("editorStickyScroll", "Sticky scroll", "Keep the enclosing function and class headers pinned while you scroll."),
      clientSwitch("editorBracketColors", "Colour matching brackets", "Give each nesting level of brackets its own colour."),
      clientSwitch("editorLigatures", "Font ligatures", "Join character pairs like => and != when the font supports it."),
      clientSwitch("editorSmoothScrolling", "Smooth scrolling", "Animate scrolling in the editor."),
      clientNumber("editorAutosaveMs", "Auto-save delay", "Edits are saved this long after you stop typing.", 250, 10000, 250, "ms"),
    ]);
    return html;
  };

  window._livecodeSettingsHarnessHtml = function() {
    if (!agent) loadAgent();
    let html = headRow("Harness", "How the agent loop runs: step budgets, parallel work, safety rails and context. Changes apply from the next message.",
      _livecodeSettingsButton("Reset to defaults", "agent-reset-group", ' data-group="harness" data-label="Harness"'));
    html += group("Step budget", [
      agentNumber("max_iterations", "Agent mode step limit", "Most model calls one Agent message may make before it must wrap up. Near the end the agent is told how many steps remain.", "steps"),
      agentNumber("read_only_max_iterations", "Plan and Ask step limit", "The same limit for Plan and Ask mode, which only read.", "steps"),
      agentNumber("subagent_max_iterations", "Subagent step limit", "Steps each subagent may take before it reports back.", "steps"),
    ]);
    html += group("Parallel work", [
      agentSwitch("subagents", "Subagents", "Let the agent hand independent parts of a task to subagents that work at the same time."),
      agentSwitch("parallel_tools", "Run tool calls in parallel", "Reads and searches the model asks for together run at once, and edits to separate files run side by side. Off runs every call one after another."),
      agentNumber("max_parallel_writers", "Parallel edits", "Most file-editing calls that run at the same time.", "calls"),
    ]);
    html += group("Reliability", [
      agentSwitch("loop_guard", "Loop guard", "Notices the agent repeating the same call with the same arguments, tells it to change course, and stops the turn if it keeps going."),
      agentNumber("loop_hard_stop", "Stop after repeats", "Identical calls in a row before the loop guard ends the turn.", "calls"),
      agentSwitch("nudges", "Course-correction hints", "Short reminders when the agent scatters searches, re-reads without editing, keeps failing a test, or forgets to finish after editing."),
      agentSwitch("verify_after_edit", "Check diagnostics after edits", "After the agent edits code, language-server errors in those files are fed back so it fixes them before finishing."),
      agentSwitch("auto_checks", "Run the project's checks", "When the agent finishes after editing code without running the tests itself, the project's own check (npm test, pytest, cargo test, make test...) runs as a command card and a failure holds the turn open."),
      agentSwitch("todo_gate", "Finish the to-do list", "The agent cannot end a turn while its own to-do list has unfinished items, unless it marks them cancelled."),
      agentNumber("model_retries", "Model retries", "How many times a failed model call (rate limit, timeout, server error) is retried with backoff.", "times"),
      agentNumber("command_timeout_s", "Command timeout", "How long a command may run before it is stopped, unless the agent asks for longer.", "s"),
    ]);
    html += group("Context", [
      agentNumber("auto_compact_percent", "Compact at", "When the conversation fills this share of the model's context window, older tool output is summarized to make room.", "%"),
    ]);
    return html;
  };

  let plansList = null;
  let plansLoading = false;

  function loadPlans() {
    if (plansLoading) return;
    plansLoading = true;
    fetch("/livecode/plans?limit=12" + (livecodeProjectPath ? "&project_path=" + encodeURIComponent(livecodeProjectPath) : ""))
      .then(function(r) { return r.json(); })
      .then(function(data) { plansList = data && data.ok ? data.plans || [] : []; })
      .catch(function() { plansList = []; })
      .finally(function() { plansLoading = false; rerender(["plan"]); });
  }

  function ago(epochSeconds) {
    const s = Math.max(0, Date.now() / 1000 - Number(epochSeconds || 0));
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }

  window._livecodeSettingsPlanHtml = function() {
    if (!agent) loadAgent();
    let html = headRow("Plan mode", "In Plan mode the agent explores the code and writes a plan for you to review before anything changes. Build hands the approved plan to Agent mode.",
      _livecodeSettingsButton("Reset to defaults", "agent-reset-group", ' data-group="plan" data-label="Plan mode"'));
    html += group("Writing plans", [
      agentChoice("plan_detail", "Plan detail", "Concise plans are short with a few to-dos; Detailed plans cover every file, edge case and migration step.",
        [{ value: "concise", label: "Concise" }, { value: "standard", label: "Standard" }, { value: "detailed", label: "Detailed" }]),
      agentSwitch("plan_ask_questions", "Ask clarifying questions", "Before writing the plan, the agent asks about open decisions in a questions card. Off: it picks sensible defaults and lists its assumptions in the plan."),
      agentSwitch("plan_diagrams", "Diagrams", "Include a Mermaid diagram when a flow or sequence is easier to see than read."),
      agentSwitch("plan_tests_section", "Tests section", "End each plan with the tests to add and the command to run them."),
      agentNumber("read_only_max_iterations", "Step limit", "Model calls a Plan or Ask message may make. Shared with Settings › Harness.", "steps"),
    ]);
    html += group("Building plans", [
      clientSwitch("planAutoOpen", "Open new plans automatically", "Open each plan in the editor as soon as the agent writes it, instead of waiting for you to click View Plan."),
      agentSwitch("plan_build_verify", "Run the plan's tests after building", "When building, the agent finishes by running the tests the plan's Tests section names."),
    ]);
    if (plansList === null) loadPlans();
    let rows = "";
    if (plansList === null) rows = '<div class="livecode-settings-empty">Loading…</div>';
    else if (!plansList.length) rows = '<div class="livecode-settings-empty">Plans you create in Plan mode appear here.</div>';
    else rows = plansList.map(function(p) {
      return '<div class="livecode-settings-row is-compact"><button type="button" class="lc-btn livecode-settings-link-row" data-settings-action="plan-open" data-file="' + attr(p.file) + '" title="Open ' + attr(p.title) + '">' +
        '<span class="livecode-settings-row-title">' + esc(p.title) + "</span>" +
        '<span class="livecode-settings-row-desc">' + esc(ago(p.mtime)) + " · " + esc(p.file) + "</span></button>" +
        '<button type="button" class="lc-btn livecode-settings-icon-btn" data-settings-action="plan-delete" data-file="' + attr(p.file) + '" title="Delete plan" aria-label="Delete ' + attr(p.title) + '">' + _LIVECODE_QUEUE_ICONS.remove + "</button></div>";
    }).join("");
    html += '<h3 class="livecode-settings-subh">Recent plans</h3><div class="livecode-settings-group">' + rows + "</div>";
    return html;
  };

  let memory = null;
  let memoryFor = "";
  let memoryLoading = false;

  function memoryBody(extra) {
    return Object.assign({ project_path: livecodeProjectPath, workspace: _livecodeCurrentWorkspacePayload() }, extra || {});
  }

  function loadMemory(force) {
    if (!livecodeProjectPath || (memoryLoading && !force)) return;
    memoryLoading = true;
    memoryFor = livecodeProjectPath;
    postJson("/livecode/memory/status", memoryBody())
      .then(function(data) { memory = data; })
      .catch(function(err) { memory = { error: err.message || String(err) }; })
      .finally(function() { memoryLoading = false; rerender(["memory"]); });
  }

  function bytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  window._livecodeSettingsMemoryHtml = function() {
    if (!agent) loadAgent();
    let html = headRow("Memory", "LiveCode keeps a searchable memory for each project: notes the agent saves, a log of past sessions, and a MEMORY.md digest. Relevant pieces are added to the agent's context on each message.",
      _livecodeSettingsButton("Reset to defaults", "agent-reset-group", ' data-group="memory" data-label="Memory"'));
    if (!livecodeProjectPath) {
      html += group("This project", ['<div class="livecode-settings-empty">Open a project to see and manage its memory.</div>']);
    } else {
      if (!memory || memoryFor !== livecodeProjectPath) loadMemory();
      const m = memory && memoryFor === livecodeProjectPath ? memory : null;
      const rows = [];
      if (!m) rows.push(_livecodeSettingsRowHtml("Project memory", "Loading…", ""));
      else if (m.error) rows.push(_livecodeSettingsRowHtml("Project memory", esc(m.error), _livecodeSettingsButton("Retry", "memory-refresh")));
      else {
        rows.push(_livecodeSettingsRowHtml("MEMORY.md",
          (m.memory_exists ? m.memory_chars.toLocaleString() + " characters" : "Empty") + _livecodeSettingsPathHtml(m.memory_path || ""),
          _livecodeSettingsButton(m.memory_exists ? "Open" : "Create", "memory-open", "", !m.memory_exists)));
        rows.push(_livecodeSettingsRowHtml("Session logs",
          m.session_logs.toLocaleString() + " log" + (m.session_logs === 1 ? "" : "s") + " · " + bytes(m.bytes) + " on disk",
          _livecodeSettingsButton("Consolidate now", "memory-consolidate") + _livecodeSettingsButton("Rebuild index", "memory-reindex")));
        (m.recent_logs || []).slice(0, 5).forEach(function(log) {
          rows.push('<div class="livecode-settings-row is-compact"><button type="button" class="lc-btn livecode-settings-link-row" data-settings-action="open-file" data-path="' + attr(log.path) + '">' +
            '<span class="livecode-settings-row-desc">' + esc(log.name) + "</span></button></div>");
        });
        rows.push(_livecodeSettingsRowHtml("Forget", "Delete this project's session logs, or all of its memory including MEMORY.md. This cannot be undone.",
          _livecodeSettingsButton("Clear logs", "memory-clear", ' data-scope="sessions"') + _livecodeSettingsButton("Clear all", "memory-clear", ' data-scope="all"')));
      }
      html += group("This project", rows);
    }
    html += group("Recall", [
      agentSwitch("memory_enabled", "Use memory", "Add relevant notes from past sessions to the agent's context. Off: the agent starts each chat without them and cannot save new notes."),
      agentNumber("memory_max_results", "Notes per message", "Most memory snippets added to each message.", "notes"),
      agentNumber("memory_min_score", "Relevance threshold", "Only snippets at least this relevant are added. 0 adds the best matches whatever their score.", "%", { scale: 100, step: 5 }),
    ]);
    html += group("Saving", [
      agentSwitch("memory_agent_writes", "Agent can save notes", "Let the agent record conventions and decisions with its update_memory tool when you ask it to remember something."),
      agentSwitch("memory_autosave", "Log sessions", "Write a short log of each chat (topics and counts) once it has a few messages."),
      agentSwitch("memory_auto_extract", "Extract notes automatically", "After a turn, a fast model pulls decisions, conventions and fixes out of the conversation into the session log. Uses a little of your model quota."),
      agentSwitch("memory_consolidate", "Consolidate into MEMORY.md", "Periodically fold recent session logs into one digest, resolving contradictions and dropping noise."),
      agentNumber("memory_consolidate_hours", "Consolidate at most every", "Minimum time between automatic consolidations.", "hours"),
    ]);
    return html;
  };

  // The engine choice: LiveCode's own browser without a window, with a window, or your Chrome over CDP.
  function engine() {
    const c = _livecodeBrowserConnection;
    if ((c && c.engine === "chrome") || cdpProbe) return "chrome";
    return _livecodeBrowserSettings && _livecodeBrowserSettings.headless === false ? "window" : "headless";
  }

  let cdpProbe = null;
  let cdpBusy = "";

  function cdpCardHtml() {
    const c = _livecodeBrowserConnection || {};
    const attached = c.engine === "chrome";
    const isEnv = c.source === "env";
    const value = attached ? c.endpoint : (cdpProbe && cdpProbe.endpoint) || "http://127.0.0.1:9222";
    let status = "";
    if (attached) {
      status = '<span class="livecode-settings-status ' + (c.connected === false ? "is-warn" : "is-ok") + '">' + (c.connected === false ? "Not answering" : "Attached") + "</span> " +
        esc(String(c.endpoint || "").replace(/^https?:\/\//, "")) + (c.version ? " · " + esc(c.version) : "") + (isEnv ? " · set by LIVECODE_BROWSER_CDP_URL" : "");
    } else if (cdpProbe) {
      status = cdpProbe.ok && cdpProbe.found !== false
        ? '<span class="livecode-settings-status is-ok">Found</span> ' + esc(cdpProbe.browser || "Chrome") + " at " + esc(String(cdpProbe.endpoint || "").replace(/^https?:\/\//, ""))
        : '<span class="livecode-settings-status is-warn">Not found</span> ' + esc(cdpProbe.error || "No Chrome is listening on ports 9222–9224 or 9229.");
    } else {
      status = "Start Chrome with <code>--remote-debugging-port=9222 --user-data-dir=&lt;profile folder&gt;</code>, or let LiveCode launch one for you.";
    }
    const busy = function(name) { return cdpBusy === name ? " disabled" : ""; };
    return '<div class="livecode-settings-row livecode-cdp-card"><div class="livecode-settings-row-text">' +
      '<div class="livecode-settings-row-title">Chrome debugging address</div>' +
      '<div class="livecode-settings-row-desc">' + status + "</div>" +
      '<div class="livecode-cdp-inline">' +
        '<input type="text" class="livecode-settings-text is-wide" data-cdp-url value="' + attr(value) + '" placeholder="http://127.0.0.1:9222" spellcheck="false" autocomplete="off" aria-label="Chrome debugging address"' + (attached || isEnv ? " disabled" : "") + ">" +
        (attached
          ? (isEnv ? "" : _livecodeSettingsButton("Disconnect", "cdp-disconnect", busy("disconnect")))
          : _livecodeSettingsButton(cdpBusy === "connect" ? "Attaching…" : "Attach", "cdp-connect", busy("connect"), true) +
            _livecodeSettingsButton(cdpBusy === "test" ? "Testing…" : "Test", "cdp-test", busy("test")) +
            _livecodeSettingsButton(cdpBusy === "detect" ? "Looking…" : "Detect", "cdp-detect", busy("detect")) +
            _livecodeSettingsButton(cdpBusy === "launch" ? "Launching…" : "Launch Chrome", "cdp-launch", busy("launch") + ' title="Start Chrome with remote debugging on a LiveCode profile and attach to it"')) +
      "</div></div></div>";
  }

  function launchRowsHtml() {
    const s = _livecodeBrowserSettings;
    if (!s) return "";
    const env = s.env_overrides || {};
    const envNote = function(key) { return env[key] ? ' <span class="livecode-settings-default">Set by ' + esc(env[key]) + " on the server.</span>" : ""; };
    const scale = Number(s.device_scale) || 2;
    return _livecodeSettingsRowHtml("Browser program", "Chrome, Chromium, Edge or Brave executable (or a .app on macOS). Empty uses Playwright's Chromium, then Chrome." + envNote("executable_path"),
        '<input type="text" class="livecode-settings-text is-wide" data-browser-text="executable_path" value="' + attr(s.executable_path || "") + '" placeholder="Playwright Chromium" spellcheck="false" autocomplete="off" aria-label="Browser program"' + (env.executable_path ? " disabled" : "") + ">") +
      _livecodeSettingsRowHtml("Proxy", "Send the built-in browser's traffic through a proxy, e.g. http://127.0.0.1:8080 or socks5://127.0.0.1:1080." + envNote("proxy"),
        '<input type="text" class="livecode-settings-text is-wide" data-browser-text="proxy" value="' + attr(s.proxy || "") + '" placeholder="No proxy" spellcheck="false" autocomplete="off" aria-label="Proxy"' + (env.proxy ? " disabled" : "") + ">") +
      _livecodeSettingsRowHtml("Pixel density", "Device pixel ratio pages render at. 2× is sharp on Retina screens; 1× is lighter." + envNote("device_scale"),
        env.device_scale ? "" : segmentedHtml("Pixel density", [{ value: "1", label: "1×" }, { value: "1.5", label: "1.5×" }, { value: "2", label: "2×" }, { value: "3", label: "3×" }], String(scale), 'data-settings-action="browser-scale"'));
  }

  window._livecodeSettingsBrowserHtml = function() {
    if (!_livecodeBrowserConnection) _livecodeLoadBrowserConnection();
    if (!_livecodeBrowserSettings) _livecodeLoadBrowserSettings();
    let html = headRow("Browser", "The agent's browser: where it runs, how it starts, and how it checks designs.");
    const ready = !!(_livecodeBrowserConnection && _livecodeBrowserSettings);
    const current = ready ? engine() : "";
    const env = (_livecodeBrowserSettings && _livecodeBrowserSettings.env_overrides) || {};
    const engineDesc = {
      headless: "LiveCode runs Chromium without a window and streams it into the Browser tab. Best for most work.",
      window: "LiveCode's Chromium opens in its own window as well, so you can watch it or use DevTools there.",
      chrome: (_livecodeBrowserConnection || {}).engine === "chrome"
        ? "The agent drives your own Chrome over the DevTools protocol, signed in with that profile's logins."
        : "Attach a Chrome running with remote debugging below. Until then the built-in browser is used.",
    }[current] || "Checking…";
    html += group("", [
      clientSwitch("browserTools", "Built-in browser", "Let the agent open pages in the Browser tab, click and type in them, run page scripts and take screenshots. Clicks, typing and scripts ask first when approvals are on."),
      _livecodeSettingsRowHtml("Run the browser", engineDesc + (env.headless && current !== "chrome" ? ' <span class="livecode-settings-default">Window mode is set by LIVECODE_BROWSER_HEADLESS on the server.</span>' : ""),
        ready ? segmentedHtml("Run the browser", [{ value: "headless", label: "Headless" }, { value: "window", label: "In a window" }, { value: "chrome", label: "Your Chrome" }], current, 'data-settings-action="browser-engine"') : ""),
    ]);
    if (current === "chrome" || cdpProbe) html += group("Attach to Chrome (CDP)", [cdpCardHtml()]);
    html += group("Built-in browser", [
      launchRowsHtml(),
      _livecodeSettingsRowHtml("Cookies", current === "chrome"
        ? "Your attached Chrome signs in with its own cookies; imported ones are for the built-in browser."
        : "Import cookies from your own browser so pages open signed in. They stay in this project's browser profile.",
        (current === "chrome" ? "" : _livecodeSettingsButton("Manage…", "browser-cookies")) + _livecodeSettingsButton("Open browser", "open-browser")),
      _livecodeSettingsBrowserViewRowsHtml(),
      _livecodeSettingsAutomationRowHtml(),
    ]);
    html += group("Design checks", [
      _livecodeSettingsMatchRowHtml(),
      _livecodeSettingsCompareContentRowHtml(),
      _livecodeSettingsDesignGateRowHtml(),
      _livecodeBrowserSwitchRowHtml("ui_verify", "Check UI changes in the browser",
        "After changing components, pages or styles, the agent asks whether to check the change in the Browser tab, and on Yes looks at just that element. It checks without asking when your request asks to see it."),
      _livecodeSettingsFigmaRowHtml(),
    ]);
    return html;
  };

  // Extra rows for the existing General and Agent pages.
  window._livecodeSettingsGeneralExtraHtml = function() {
    const perm = "Notification" in window ? Notification.permission : "unsupported";
    const permNote = perm === "denied" ? " Notifications are blocked for this site in your browser settings." : perm === "unsupported" ? " This browser does not support notifications." : "";
    return group("Notifications", [
      clientSwitch("notifyOnFinish", "When the agent finishes", "Show a desktop notification when a turn ends while LiveCode is in the background." + permNote),
      clientSwitch("notifyOnApproval", "When the agent needs you", "Notify when the agent waits for an approval or asks a question while LiveCode is in the background."),
      clientSwitch("notifySound", "Play a sound", "Chime with each notification."),
    ]) + group("Window", [
      clientSwitch("confirmCloseWhileRunning", "Warn before closing while the agent works", "Ask before reloading or closing the tab while a turn is still running."),
    ]) + group("Settings", [
      _livecodeSettingsRowHtml("Back up and restore", "Export every LiveCode setting (except API keys and tokens) to a file, or import one.",
        _livecodeSettingsButton("Export…", "settings-export") + _livecodeSettingsButton("Import…", "settings-import")),
      _livecodeSettingsRowHtml("Reset everything", "Return appearance, agent, harness, memory and plan settings to their defaults. API keys, MCP servers and browser settings are kept.",
        _livecodeSettingsButton("Reset all…", "settings-reset-all")),
    ]);
  };

  window._livecodeSettingsAgentExtraHtml = function() {
    if (!agent) return group("Custom instructions", [loadingRow("Custom instructions")]);
    const value = String(agentValue("custom_instructions") || "");
    const max = agentSpec("custom_instructions").max_chars || 8000;
    return '<h3 class="livecode-settings-subh">Custom instructions</h3><div class="livecode-settings-group"><div class="livecode-settings-row livecode-settings-row-stack">' +
      '<div class="livecode-settings-row-text"><div class="livecode-settings-row-title">Instructions for every chat</div>' +
      '<div class="livecode-settings-row-desc">Added to the agent\'s system prompt in every project, on top of each project\'s AGENTS.md / CLAUDE.md rules. Saved when you click away.</div></div>' +
      '<textarea class="livecode-settings-textarea" data-agent-setting="custom_instructions" rows="5" maxlength="' + max + '" placeholder="e.g. Prefer small focused commits. Use pnpm, not npm. Write tests with Vitest." spellcheck="true">' + esc(value) + "</textarea>" +
      '<div class="livecode-settings-counter" data-counter-for="custom_instructions">' + value.length.toLocaleString() + " / " + max.toLocaleString() + "</div></div></div>";
  };

  // ---------------------------------------------------------------- search

  window._livecodeSettingsQuery = "";

  window._livecodeSettingsSearchHtml = function(query) {
    const q = String(query || "").trim().toLowerCase();
    const scratch = document.createElement("div");
    let html = headRow("Search", "Settings matching “" + esc(query) + "”.");
    let found = 0;
    _LIVECODE_SETTINGS_SECTIONS.forEach(function(section) {
      const render = _livecodeSettingsRenderers()[section.id];
      if (!render) return;
      try { scratch.innerHTML = render(); } catch (e) { return; }
      const rows = Array.from(scratch.querySelectorAll(".livecode-settings-row, .livecode-lsp-card")).filter(function(row) {
        return (row.textContent || "").toLowerCase().indexOf(q) !== -1 && !row.closest(".livecode-settings-row .livecode-settings-row");
      });
      const sectionHit = section.label.toLowerCase().indexOf(q) !== -1;
      if (!rows.length && !sectionHit) return;
      found += rows.length || 1;
      html += '<div class="livecode-settings-search-head"><h3 class="livecode-settings-subh">' + esc(section.label) + "</h3>" +
        '<button type="button" class="lc-btn livecode-settings-link" data-settings-section="' + section.id + '">Open ' + esc(section.label) + " ›</button></div>";
      if (rows.length) html += '<div class="livecode-settings-group">' + rows.map(function(r) { return r.outerHTML; }).join("") + "</div>";
    });
    if (!found) html += '<div class="livecode-settings-group"><div class="livecode-settings-empty">No settings match. Try a shorter word, like “theme”, “memory” or “chrome”.</div></div>';
    return html;
  };

  // ---------------------------------------------------------------- import / export / reset

  const EXPORT_BROWSER_KEYS = ["design_accuracy", "design_gate", "agent_tabs", "view_quality", "default_viewport", "reduce_automation_signals", "headless", "proxy", "device_scale", "executable_path"];

  function exportSettings() {
    Promise.all([
      fetch("/livecode/agent/settings").then(function(r) { return r.json(); }).catch(function() { return {}; }),
      fetch("/livecode/browser/settings").then(function(r) { return r.json(); }).catch(function() { return {}; }),
    ]).then(function(results) {
      const browser = {};
      EXPORT_BROWSER_KEYS.forEach(function(k) { if (results[1] && results[1][k] !== undefined) browser[k] = results[1][k]; });
      const payload = {
        livecode_settings: 1,
        exported_at: new Date().toISOString(),
        theme: _livecodeCurrentThemeId(),
        client: _livecodeSettingsAll(),
        agent: (results[0] && results[0].settings) || {},
        browser: browser,
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "livecode-settings.json";
      document.body.appendChild(a);
      a.click();
      setTimeout(function() { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    });
  }

  function importSettings() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/json,.json";
    input.addEventListener("change", function() {
      const file = input.files && input.files[0];
      if (!file) return;
      file.text().then(function(text) {
        const data = JSON.parse(text);
        if (!data || data.livecode_settings !== 1) throw new Error("That file is not a LiveCode settings export.");
        const jobs = [];
        if (data.client && typeof data.client === "object") {
          Object.keys(data.client).forEach(function(k) { if (k in _LIVECODE_SETTING_DEFAULTS) _livecodeSettingsSet(k, data.client[k]); });
        }
        if (data.agent && typeof data.agent === "object") jobs.push(postJson("/livecode/agent/settings", data.agent).then(function(d) { agent = d; }));
        if (data.browser && typeof data.browser === "object") {
          const browser = {};
          EXPORT_BROWSER_KEYS.forEach(function(k) { if (data.browser[k] !== undefined) browser[k] = data.browser[k]; });
          jobs.push(postJson("/livecode/browser/settings", browser).then(function(d) { _livecodeBrowserSettings = d; }));
        }
        if (data.theme && !pref("themeFollowSystem")) baseSetTheme(data.theme);
        return Promise.all(jobs);
      }).then(function() {
        applyAll();
        _livecodeApplyConversationDensity();
        _livecodeRenderSettings();
        toast("Settings imported");
      }).catch(function(err) { toast(err.message || String(err)); });
    });
    input.click();
  }

  function resetAll() {
    _livecodeModalConfirm({ title: "Reset all settings?", message: "Appearance, agent, harness, memory and plan settings go back to their defaults. API keys, MCP servers and browser settings are kept.", confirmText: "Reset all", danger: true }).then(function(ok) {
      if (!ok) return;
      try { localStorage.removeItem(LIVECODE_SETTINGS_STORAGE_KEY); } catch (e) {}
      // Client preferences live in ~/.livecode/settings.json too; clear them there, keeping browser settings.
      if (typeof _livecodeServerSettings !== "undefined") { _livecodeServerSettings = {}; _livecodeSettingsPending = {}; }
      postJson("/livecode/settings/reset", { keep_browser: true }).catch(function() {});
      postJson("/livecode/agent/settings/reset", {})
        .then(function(d) { agent = d; })
        .catch(function(err) { toast(err.message || String(err)); })
        .finally(function() {
          applyAll();
          _livecodeApplyConversationDensity();
          _livecodeRenderSettings();
          toast("Settings reset");
        });
    });
  }

  // ---------------------------------------------------------------- events

  function setClient(key, value) {
    _livecodeSettingsSet(key, value);
    if (key.indexOf("editor") === 0) window._livecodeApplyEditorSettings();
    if (key === "chatZoom" || key === "reduceMotion") applyInterface();
    if (key === "themeFollowSystem" || key === "themeDark" || key === "themeLight") applySystemTheme();
    if ((key === "notifyOnFinish" || key === "notifyOnApproval") && value && "Notification" in window && Notification.permission === "default") {
      try { Notification.requestPermission().then(function() { rerender(["general"]); }); } catch (e) {}
    }
  }

  function cdpUrlInput() {
    const el = document.querySelector("#ide-settings-view [data-cdp-url]");
    return el ? String(el.value || "").trim() : "";
  }

  function cdpRun(name, promise, done) {
    cdpBusy = name;
    rerender(["browser"]);
    return promise
      .then(done)
      .catch(function(err) { toast(err.message || String(err)); })
      .finally(function() { cdpBusy = ""; rerender(["browser"]); });
  }

  function setEngine(value) {
    const c = _livecodeBrowserConnection || {};
    const attached = c.engine === "chrome";
    if (value === "chrome") {
      if (!attached && !cdpProbe) {
        cdpProbe = { ok: false, endpoint: "http://127.0.0.1:9222", error: "Looking for Chrome…" };
        cdpRun("detect", fetch("/livecode/browser/cdp/detect").then(function(r) { return r.json(); }), function(data) {
          cdpProbe = data.found ? data : { ok: false, endpoint: "http://127.0.0.1:9222", error: "No Chrome with remote debugging found. Launch one, or start yours and enter its address." };
        });
      }
      rerender(["browser"]);
      return;
    }
    cdpProbe = null;
    const headless = value !== "window";
    const after = function() {
      const s = _livecodeBrowserSettings || {};
      if (s.headless === headless) return Promise.resolve();
      return _livecodeSaveBrowserSetting("headless", headless).then(function() {
        toast(headless ? "The browser now runs headless" : "The browser now opens in its own window");
      });
    };
    if (attached) {
      if (c.source === "env") { toast("LIVECODE_BROWSER_CDP_URL is set on the server; remove it there to use the built-in browser."); return; }
      _livecodeSetBrowserConnection({ disconnect: true }).then(after).catch(function(err) { toast(err.message || String(err)); });
    } else {
      after();
    }
  }

  // ---------------------------------------------------------------- languages (LSP)

  let lsp = null;
  let lspLoading = false;

  function loadLsp() {
    if (lspLoading) return;
    lspLoading = true;
    fetch("/livecode/lsp/settings")
      .then(function(r) { return r.json(); })
      .then(function(data) { lsp = data && data.success ? data : { error: (data && data.error) || "Could not read language settings." }; })
      .catch(function(err) { lsp = { error: err.message || String(err) }; })
      .finally(function() { lspLoading = false; rerender(["languages"]); });
  }

  function saveLsp(body, message) {
    return postJson("/livecode/lsp/settings", body)
      .then(function(data) {
        lsp = data;
        rerender(["languages"]);
        if (window.WBLsp && typeof window.WBLsp.reload === "function") window.WBLsp.reload();
        if (message) toast(message);
      })
      .catch(function(err) { toast(err.message || String(err)); rerender(["languages"]); });
  }

  // Logo (Material file icons served from /asset) and brand colour for each language card.
  const LSP_BRAND = {
    python: ["python", "#3776ab"], typescript: ["typescript", "#3178c6"], java: ["java", "#e76f00"],
    kotlin: ["kotlin", "#7f52ff"], scala: ["scala", "#dc322f"], go: ["go", "#00add8"], rust: ["rust", "#ce422b"],
    cpp: ["cpp", "#00599c"], csharp: ["csharp", "#68217a"], swift: ["swift", "#f05138"], ruby: ["ruby", "#cc342d"],
    php: ["php", "#777bb4"], dart: ["dart", "#0175c2"], lua: ["lua", "#2c2d72"], elixir: ["elixir", "#6e4a7e"],
    haskell: ["haskell", "#5e5086"], zig: ["zig", "#f7a41d"], shell: ["console", "#4eaa25"], yaml: ["yaml", "#cb171e"],
    terraform: ["terraform", "#7b42bc"], dockerfile: ["docker", "#2496ed"], sql: ["database", "#e38c00"],
    html: ["html", "#e34f26"], css: ["css", "#1572b6"], json: ["json", "#cfa400"], markdown: ["markdown", "#4a8fe2"],
  };

  function lspBrand(lang) {
    const b = LSP_BRAND[lang.id] || ["file", "#64748b"];
    return { icon: "/asset/material-icons/" + b[0] + ".svg", color: b[1] };
  }

  function lspPill(kind, text) {
    return '<span class="livecode-lsp-pill is-' + kind + '">' + esc(text) + "</span>";
  }

  // A row per language: its logo and name, then Install, or its on/off switch once it is installed.
  // A status line shows only when something needs attention.
  function lspCardHtml(lang, masterOn) {
    const brand = lspBrand(lang);
    const job = lspJobs[lang.id];
    const installing = !!(job && job.status === "running");
    const installed = !!(lang.found.length || lang.command);
    const setupServer = lang.servers.filter(function(sv) { return sv.setup && !sv.setup_needs; })[0] || lang.servers.filter(function(sv) { return sv.setup; })[0];

    let right = "";
    if (installed) right = switchHtml('data-lsp-enabled="' + attr(lang.id) + '"', lang.enabled, lang.label + " language server", !masterOn);
    else if (!installing && setupServer && !setupServer.setup_needs) right = lspActionButton(brand, "Install", ' data-lang="' + attr(lang.id) + '" data-server="' + attr(setupServer.name) + '"');

    let body = "";
    if (installing) body += '<div class="livecode-lsp-status">' + lspPill("busy", "Installing…") + "</div>";
    else if (installed && lang.enabled && lang.problem) body += '<div class="livecode-lsp-status">' + lspPill("warn", "Not running") + "</div>" + '<p class="livecode-lsp-note is-warn">' + esc(lang.problem) + "</p>";
    else if (!installed && setupServer && setupServer.setup_needs) body += '<p class="livecode-lsp-note">Needs ' + esc(setupServer.setup_needs) + " on this machine first.</p>";
    else if (!installed && !setupServer && lang.servers[0] && lang.servers[0].install) body += '<p class="livecode-lsp-note">' + esc(lang.servers[0].install) + "</p>";
    if (lang.lint_plugins_missing && !(job && job.lint)) {
      body += '<p class="livecode-lsp-note is-warn">Python errors aren\'t checked yet.</p>' +
        '<div class="livecode-lsp-action">' + lspActionButton(brand, "Turn on error checking", ' data-lang="python" data-lint="1"') + "</div>";
    }

    return '<div class="livecode-lsp-card' + (lang.enabled || !installed ? "" : " is-off") + '" data-lsp-card="' + attr(lang.id) + '">' +
      '<div class="livecode-lsp-head"><span class="livecode-lsp-logo"><img src="' + brand.icon + '" alt="" width="22" height="22" loading="lazy"></span>' +
        '<div class="livecode-lsp-title"><div class="livecode-lsp-name">' + esc(lang.label) + "</div></div>" + right + "</div>" +
      body + (job ? lspJobHtml(job) : "") + "</div>";
  }

  function lspActionButton(brand, label, attrs) {
    return '<button type="button" class="lc-btn livecode-settings-btn livecode-lsp-install" data-settings-action="lsp-install"' + attrs + ">" + esc(label) + "</button>";
  }

  const lspJobs = {};

  // While installing, the status line says so; the installer's output only shows when it fails.
  function lspJobHtml(job) {
    if (job.status !== "failed") return "";
    return '<div class="livecode-lsp-job"><div class="livecode-lsp-job-head">' + lspPill("warn", "Install failed") +
      '<button type="button" class="lc-btn livecode-lsp-more" data-settings-action="lsp-job-dismiss" data-lang="' + attr(job.language) + '">Dismiss</button></div>' +
      '<details class="livecode-lsp-details"><summary>Details</summary><pre class="livecode-lsp-log">' + esc((job.log || []).slice(-20).join("\n")) + "</pre></details></div>";
  }

  function pollLspJob(langId) {
    const job = lspJobs[langId];
    if (!job || job.status !== "running") return;
    setTimeout(function() {
      fetch("/livecode/lsp/install/" + encodeURIComponent(job.id))
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (!data || !data.job) return;
          lspJobs[langId] = Object.assign(data.job, { lint: job.lint, onDone: job.onDone });
          if (data.job.status === "running") { rerender(["languages"]); pollLspJob(langId); return; }
          if (job.onDone) job.onDone(data.job.status === "ok");
          else toast(data.job.status === "ok" ? data.job.server + " installed" : "Installing " + data.job.server + " failed");
          lsp = null;
          loadLsp();
          if (window.WBLsp && window.WBLsp.reload) window.WBLsp.reload();
        })
        .catch(function() { pollLspJob(langId); });
    }, 1000);
  }

  // Every enabled language without a server whose installer is present, one command each
  // (HTML, CSS and JSON share one npm package), plus Python's linting plugins when missing.
  function lspInstallPlan() {
    const plan = [];
    const seen = {};
    ((lsp && lsp.languages) || []).forEach(function(lang) {
      if (!lang.enabled) return;
      if (lang.lint_plugins_missing) plan.push({ lang: lang.id, lint: true, label: "Python error checking", setup: "pip install 'python-lsp-server[all]'" });
      if (lang.found.length || lang.command) return;
      const server = lang.servers.filter(function(sv) { return sv.setup && !sv.setup_needs; })[0];
      if (!server || seen[server.setup]) return;
      seen[server.setup] = true;
      plan.push({ lang: lang.id, server: server.name, label: lang.label, setup: server.setup });
    });
    return plan;
  }

  let lspQueue = null;

  function runLspQueue() {
    if (!lspQueue || !lspQueue.items.length) {
      if (lspQueue) toast("Finished installing " + lspQueue.done + " of " + lspQueue.total + " language servers" + (lspQueue.failed ? " (" + lspQueue.failed + " failed)" : ""));
      lspQueue = null;
      rerender(["languages"]);
      return;
    }
    const item = lspQueue.items.shift();
    lspQueue.current = item.label;
    rerender(["languages"]);
    postJson("/livecode/lsp/install", { language: item.lang, server: item.server || "", lint_plugins: !!item.lint })
      .then(function(data) {
        lspJobs[item.lang] = Object.assign(data.job, { lint: !!item.lint, onDone: function(ok) {
          lspQueue.done += ok ? 1 : 0;
          lspQueue.failed += ok ? 0 : 1;
          runLspQueue();
        } });
        rerender(["languages"]);
        pollLspJob(item.lang);
      })
      .catch(function(err) {
        toast(item.label + ": " + (err.message || String(err)));
        lspQueue.failed += 1;
        runLspQueue();
      });
  }

  function startLspInstallAll() {
    const plan = lspInstallPlan();
    if (!plan.length) { toast("Nothing to install: every enabled language with an available installer has a server."); return; }
    _livecodeModalConfirm({
      title: "Install " + plan.length + " language server" + (plan.length === 1 ? "" : "s") + "?",
      message: plan.map(function(p) { return "• " + p.label; }).join("\n"),
      confirmText: "Install all",
    }).then(function(ok) {
      if (!ok) return;
      lspQueue = { items: plan.slice(), total: plan.length, done: 0, failed: 0, current: "" };
      runLspQueue();
    });
  }

  function startLspInstall(btn) {
    const langId = btn.getAttribute("data-lang") || "";
    const lint = btn.getAttribute("data-lint") === "1";
    btn.disabled = true;
    postJson("/livecode/lsp/install", { language: langId, server: btn.getAttribute("data-server") || "", lint_plugins: lint })
      .then(function(data) {
        lspJobs[langId] = Object.assign(data.job, { lint: lint });
        rerender(["languages"]);
        pollLspJob(langId);
      })
      .catch(function(err) { toast(err.message || String(err)); btn.disabled = false; });
  }

  window._livecodeSettingsLanguagesHtml = function() {
    if (!lsp) loadLsp();
    let html = headRow("Languages", "Language servers give the editor and the agent completions, hover docs, go to definition, references, rename and live errors. Installed servers are found automatically.",
      _livecodeSettingsButton("Rescan", "lsp-rescan"));
    if (!lsp) return html + group("", [_livecodeSettingsRowHtml("Language servers", "Loading…", "")]);
    if (lsp.error) return html + group("", [_livecodeSettingsRowHtml("Language servers", esc(lsp.error), _livecodeSettingsButton("Retry", "lsp-rescan"))]);
    const bridge = lsp.bridge || {};
    const ready = lsp.languages.filter(function(l) { return l.enabled && l.ready; }).length;
    const plan = lspInstallPlan();
    const blocked = lsp.languages.filter(function(l) {
      return l.enabled && !l.found.length && !l.command && !l.servers.some(function(sv) { return sv.setup && !sv.setup_needs; });
    });
    let hero = '<div class="livecode-lsp-hero"><div class="livecode-lsp-hero-main">' +
      '<div class="livecode-lsp-stat"><strong>' + ready + " of " + lsp.languages.length + "</strong> languages ready</div>" +
      '<div class="livecode-lsp-hero-text">';
    if (lspQueue) {
      const doneCount = lspQueue.total - lspQueue.items.length;
      hero += "<div><strong>Installing " + doneCount + " of " + lspQueue.total + "</strong> · " + esc(lspQueue.current) + "…</div>" +
        '<div class="livecode-lsp-progress"><span style="width:' + Math.round(100 * Math.max(0, doneCount - 1) / lspQueue.total) + '%"></span></div>';
    } else if (plan.length) {
      hero += "<div>" + plan.length + " ready to install</div>";
    } else {
      hero += "<div><strong>Everything installable is installed</strong></div>";
    }
    if (blocked.length) hero += '<div class="livecode-lsp-note">Installed outside LiveCode: ' + blocked.map(function(l) { return esc(l.label); }).join(", ") + ".</div>";
    hero += "</div></div>" +
      '<div class="livecode-lsp-hero-actions">' +
        (lspQueue ? "" : _livecodeSettingsButton("Install all", "lsp-install-all", plan.length ? "" : " disabled", true)) +
        '<span class="livecode-lsp-master">' + switchHtml("data-lsp-master", lsp.enabled, "Use language servers") + "<span>Enabled</span></span>" +
      "</div></div>";
    if (bridge.available === false) hero += '<p class="livecode-lsp-note is-warn livecode-lsp-bridge">' + esc(bridge.reason || "Editor bridge off.") + " The agent's tools still use the servers.</p>";
    html += hero;
    const order = function(l) { return l.enabled && l.ready ? 0 : (l.found.length || l.command) ? 1 : l.enabled ? 2 : 3; };
    const sorted = lsp.languages.slice().sort(function(a, b) { return order(a) - order(b); });
    const installed = sorted.filter(function(l) { return l.found.length || l.command; });
    const missing = sorted.filter(function(l) { return !l.found.length && !l.command; });
    if (installed.length) html += '<h3 class="livecode-settings-subh">Installed</h3><div class="livecode-lsp-grid">' + installed.map(function(l) { return lspCardHtml(l, lsp.enabled); }).join("") + "</div>";
    if (missing.length) html += '<h3 class="livecode-settings-subh">Available</h3><div class="livecode-lsp-grid">' + missing.map(function(l) { return lspCardHtml(l, lsp.enabled); }).join("") + "</div>";
    return html;
  };

  function handleLspAction(action, btn) {
    if (action === "lsp-rescan") { lsp = null; loadLsp(); if (window.WBLsp && window.WBLsp.reload) window.WBLsp.reload(); return true; }
    if (action === "lsp-install") { startLspInstall(btn); return true; }
    if (action === "lsp-install-all") { startLspInstallAll(); return true; }
    if (action === "lsp-job-dismiss") { delete lspJobs[btn.getAttribute("data-lang") || ""]; rerender(["languages"]); return true; }
    return false;
  }

  function handleLspChange(input) {
    if (input.hasAttribute("data-lsp-master")) { saveLsp({ enabled: !!input.checked }); return true; }
    const enabledFor = input.getAttribute("data-lsp-enabled");
    if (enabledFor) { saveLsp({ languages: { [enabledFor]: { enabled: !!input.checked } } }); return true; }
    return false;
  }

  window._livecodeHandleSettingsActionExt = function(action, btn) {
    if (handleLspAction(action, btn)) return true;
    const key = btn.getAttribute("data-key") || "";
    const value = btn.getAttribute("data-value");
    switch (action) {
      case "client-choice":
        if (key) { setClient(key, value); _livecodeRenderSettingsPage(); }
        return true;
      case "set-theme":
        _livecodeSettingsSet("themeFollowSystem", false);
        baseSetTheme(btn.getAttribute("data-theme") || "dark");
        _livecodeRenderSettingsPage();
        return true;
      case "set-density-ext":
        _livecodeSettingsSet("conversationDensity", value || "detailed");
        _livecodeApplyConversationDensity();
        _livecodeRenderSettingsPage();
        return true;
      case "set-grouping-ext":
        _livecodeSettingsSet("stepGrouping", value || "grouped");
        _livecodeApplyConversationDensity();
        _livecodeRenderSettingsPage();
        return true;
      case "agent-choice":
        if (key) saveAgent({ [key]: value });
        return true;
      case "agent-reset-key":
        if (key && agent) saveAgent({ [key]: agentSpec(key).default }, "Reset to default");
        return true;
      case "agent-reset-group":
        resetAgentGroup(btn.getAttribute("data-group") || "", btn.getAttribute("data-label") || "these");
        return true;
      case "plan-open":
        window.openLiveCodePlanTab(btn.getAttribute("data-file") || "", "");
        return true;
      case "plan-delete": {
        const file = btn.getAttribute("data-file") || "";
        _livecodeModalConfirm({ title: "Delete this plan?", message: file, confirmText: "Delete", danger: true }).then(function(ok) {
          if (!ok) return;
          postJson("/livecode/plan/delete", { file: file }).then(function() { plansList = null; loadPlans(); }).catch(function(err) { toast(err.message || String(err)); });
        });
        return true;
      }
      case "memory-refresh":
        loadMemory(true);
        return true;
      case "memory-open":
        postJson("/livecode/memory/file", memoryBody()).then(function(data) { openFileInEditorFromPath(data.path); loadMemory(true); }).catch(function(err) { toast(err.message || String(err)); });
        return true;
      case "memory-reindex":
        btn.disabled = true;
        btn.textContent = "Rebuilding…";
        postJson("/livecode/memory/reindex", memoryBody()).then(function(data) { memory = data; toast("Memory index rebuilt"); }).catch(function(err) { toast(err.message || String(err)); }).finally(function() { rerender(["memory"]); });
        return true;
      case "memory-consolidate":
        btn.disabled = true;
        btn.textContent = "Consolidating…";
        postJson("/livecode/memory/consolidate", memoryBody()).then(function(data) {
          memory = data;
          const r = data.result || {};
          toast(r.status === "written" ? "Folded " + (r.logs_consumed || 0) + " log(s) into MEMORY.md" : r.status === "failed" ? "Consolidation failed: " + (r.error || "") : "Nothing new to consolidate");
        }).catch(function(err) { toast(err.message || String(err)); }).finally(function() { rerender(["memory"]); });
        return true;
      case "memory-clear": {
        const scope = btn.getAttribute("data-scope") || "sessions";
        _livecodeModalConfirm({
          title: scope === "all" ? "Clear all of this project's memory?" : "Clear this project's session logs?",
          message: scope === "all" ? "MEMORY.md, every session log and the search index are deleted." : "Every session log is deleted. MEMORY.md is kept.",
          confirmText: "Clear", danger: true,
        }).then(function(ok) {
          if (!ok) return;
          postJson("/livecode/memory/clear", memoryBody({ scope: scope })).then(function(data) { memory = data; toast("Memory cleared"); rerender(["memory"]); }).catch(function(err) { toast(err.message || String(err)); });
        });
        return true;
      }
      case "browser-engine":
        setEngine(value || "headless");
        return true;
      case "browser-scale":
        _livecodeSaveBrowserSetting("device_scale", Number(value) || 2);
        return true;
      case "cdp-test":
        cdpRun("test", postJson("/livecode/browser/cdp/probe", { cdp_url: cdpUrlInput() }), function(data) { cdpProbe = data; });
        return true;
      case "cdp-detect":
        cdpRun("detect", fetch("/livecode/browser/cdp/detect").then(function(r) { return r.json(); }), function(data) {
          cdpProbe = data.found ? data : { ok: false, endpoint: cdpUrlInput(), error: "No Chrome with remote debugging is listening on " + (data.tried || []).map(function(u) { return u.replace(/^https?:\/\//, ""); }).join(", ") + "." };
        });
        return true;
      case "cdp-connect": {
        const url = cdpUrlInput();
        if (!url) { toast("Enter Chrome's debugging address."); return true; }
        cdpRun("connect", _livecodeSetBrowserConnection({ cdp_url: url }), function(data) { cdpProbe = null; toast("Attached to Chrome" + (data.version ? " " + data.version : "")); });
        return true;
      }
      case "cdp-launch":
        cdpRun("launch", postJson("/livecode/browser/cdp/launch", { port: 9222 }), function(data) {
          _livecodeBrowserConnection = data;
          cdpProbe = null;
          if (typeof _livecodeBrowserReset === "function") _livecodeBrowserReset();
          toast("Chrome launched and attached" + (data.version ? " (" + data.version + ")" : ""));
        });
        return true;
      case "cdp-disconnect":
        cdpRun("disconnect", _livecodeSetBrowserConnection({ disconnect: true }), function() { toast("Detached from Chrome"); });
        return true;
      case "settings-export":
        exportSettings();
        return true;
      case "settings-import":
        importSettings();
        return true;
      case "settings-reset-all":
        resetAll();
        return true;
    }
    return false;
  };

  // Returns true when it handled the change.
  window._livecodeHandleSettingsChangeExt = function(input) {
    if (handleLspChange(input)) return true;
    const agentKey = input.getAttribute("data-agent-setting");
    if (agentKey) {
      const spec = agentSpec(agentKey);
      if (spec.kind === "bool") {
        saveAgent({ [agentKey]: !!input.checked });
      } else if (spec.kind === "int" || spec.kind === "float") {
        const scale = Number(input.getAttribute("data-scale")) || 1;
        const number = Number(input.value) / scale;
        if (input.value === "" || !Number.isFinite(number) || number < spec.min || number > spec.max) {
          toast("Enter a number from " + spec.min * scale + " to " + spec.max * scale + ".");
          _livecodeRenderSettingsPage();
        } else {
          saveAgent({ [agentKey]: number }, "Saved");
        }
      } else {
        saveAgent({ [agentKey]: input.value }, "Saved");
      }
      return true;
    }
    const numberKey = input.getAttribute("data-client-number");
    if (numberKey) {
      const n = Number(input.value);
      const min = Number(input.min), max = Number(input.max);
      if (input.value === "" || !Number.isFinite(n) || n < min || n > max) {
        toast("Enter a number from " + min + " to " + max + ".");
        _livecodeRenderSettingsPage();
      } else {
        setClient(numberKey, n);
      }
      return true;
    }
    const textKey = input.getAttribute("data-client-text");
    if (textKey) {
      setClient(textKey, String(input.value || "").trim());
      return true;
    }
    const browserText = input.getAttribute("data-browser-text");
    if (browserText) {
      const v = String(input.value || "").trim();
      if (v !== String((_livecodeBrowserSettings || {})[browserText] || "")) _livecodeSaveBrowserSetting(browserText, v);
      return true;
    }
    const setting = input.getAttribute("data-setting");
    if (setting && setting in CLIENT_DEFAULTS) {
      setClient(setting, !!input.checked);
      _livecodeRenderSettingsPage();
      return true;
    }
    return false;
  };

  window._livecodeHandleSettingsInputExt = function(input) {
    if (input.getAttribute && input.getAttribute("data-agent-setting") === "custom_instructions") {
      const counter = document.querySelector('#ide-settings-view [data-counter-for="custom_instructions"]');
      if (counter) counter.textContent = input.value.length.toLocaleString() + " / " + (Number(input.getAttribute("maxlength")) || 8000).toLocaleString();
    }
    if (input.hasAttribute && input.hasAttribute("data-settings-search")) {
      window._livecodeSettingsQuery = String(input.value || "");
      // The page follows the search a beat after the last keystroke, not on every one.
      clearTimeout(window._livecodeSettingsSearchTimer);
      window._livecodeSettingsSearchTimer = setTimeout(_livecodeRenderSettingsPage, 120);
    }
  };

  // ---------------------------------------------------------------- boot

  function boot() {
    applyAll();
    loadAgent();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
