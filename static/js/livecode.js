let ideEditor = null;

let ideTerminal = null;

let ideFitAddon = null;

let ideTerminalInitialized = false;

let ideFileTree = {};

let ideOpenFiles = {};

let ideActiveFile = null;

let ideExpandedFolders = new Set;

let livecodeProjectPath = null;
window.getLiveCodeProjectPath = function() { return livecodeProjectPath; };

let livecodeProjectName = null;

let livecodeWorkspacePath = null;
let livecodeWorkspaceFolders = [];
let livecodeWorkspaceSettings = {};
let livecodeWorkspaceMcpServers = {};
let livecodeApplyingWorkspace = false;
let livecodeWorkspaceMissingFolders = [];

let livecodeMcpServers = [];
let livecodeMcpSelectedServers = new Set();
let _livecodeMcpStatusRequestSeq = 0;
let _livecodeMcpStatusLoadedWithoutProbe = false;

let livecodeAgentSessionId = null;
window.getLiveCodeSessionId = function() { return livecodeAgentSessionId; };

let livecodeAgentRunning = false;

let livecodeChatTabs = [];

let livecodeActiveChatTabId = null;

let livecodeChatTabCounter = 0;

let livecodeTabsByProject = {};

let _livecodeIdeSocket = null;

function _livecodeGetIdeSocket() {
  if (_livecodeIdeSocket) return _livecodeIdeSocket;
  _livecodeIdeSocket = (typeof socket !== "undefined" && socket)
    ? socket
    : io.connect(location.protocol + "//" + location.host, {
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 500,
        reconnectionDelayMax: 4000,
      });
  return _livecodeIdeSocket;
}

const LIVECODE_IDE_SOCKET_TIMEOUT_MS = 15000;

function _livecodeIdeSocketRequest(emitEvent, payload, responseEvent, matchFn, timeoutMs) {
  return new Promise(function(resolve) {
    const sock = _livecodeGetIdeSocket();
    let done = false;
    let timer = null;
    function cleanup() {
      sock.off(responseEvent, onResponse);
      sock.off("connect_error", onConnectError);
      if (timer) clearTimeout(timer);
    }
    function onResponse(data) {
      if (done || (matchFn && !matchFn(data))) return;
      done = true;
      cleanup();
      resolve({ data: data });
    }
    function onConnectError(err) {
      if (done) return;
      done = true;
      cleanup();
      resolve({ error: "connect_error", data: { error: "Connection error: " + ((err && err.message) || err) } });
    }
    sock.on(responseEvent, onResponse);
    sock.on("connect_error", onConnectError);
    timer = setTimeout(function() {
      if (done) return;
      done = true;
      cleanup();
      resolve({ error: "timeout", data: { error: responseEvent + " timed out" } });
    }, timeoutMs || LIVECODE_IDE_SOCKET_TIMEOUT_MS);
    sock.emit(emitEvent, payload);
  });
}

const LIVECODE_COMPOSER_PLACEHOLDER = "Plan, Build, / for skills, @ for context";
const LIVECODE_FOLLOWUP_PLACEHOLDER = "Add a follow-up";

const LIVECODE_CHAT_MODES = [
  { value: "agent", label: "Agent", placeholder: LIVECODE_COMPOSER_PLACEHOLDER },
  { value: "plan", label: "Plan", placeholder: LIVECODE_COMPOSER_PLACEHOLDER },
  { value: "ask", label: "Ask", placeholder: LIVECODE_COMPOSER_PLACEHOLDER },
];

const LIVECODE_CHAT_MODE_STORAGE_KEY = "livecode-chat-mode";

const _LIVECODE_MODE_ICON_PATHS = {
  agent: '<path fill-rule="evenodd" clip-rule="evenodd" d="M6.75 9C5.1393 9 3.75 10.1979 3.75 12C3.75 13.8021 5.1393 15 6.75 15C8.93215 15 9.96658 13.7213 11.0909 12.0197C9.9648 10.2963 8.94769 9 6.75 9ZM11.9886 10.6591C10.9022 9.07118 9.47531 7.5 6.75 7.5C4.37993 7.5 2.25 9.30208 2.25 12C2.25 14.6979 4.37993 16.5 6.75 16.5C9.45251 16.5 10.8909 14.9553 11.9845 13.3798C12.4189 14.0069 12.9091 14.6294 13.5048 15.1451C14.4451 15.9593 15.6342 16.5 17.25 16.5C19.6201 16.5 21.75 14.6979 21.75 12C21.75 9.30208 19.6201 7.5 17.25 7.5C14.5253 7.5 13.0855 9.07015 11.9886 10.6591ZM12.8809 12.023C13.3905 12.8006 13.8793 13.4853 14.4866 14.0111C15.1705 14.6032 16.0158 15 17.25 15C18.8607 15 20.25 13.8021 20.25 12C20.25 10.1979 18.8607 9 17.25 9C15.0496 9 14.0162 10.3002 12.8809 12.023Z" fill="currentColor"></path>',
  plan: '<line x1="8" y1="6" x2="21" y2="6"></line><line x1="8" y1="12" x2="21" y2="12"></line><line x1="8" y1="18" x2="21" y2="18"></line><circle cx="4" cy="6" r="1.5" fill="currentColor" stroke="none"></circle><circle cx="4" cy="12" r="1.5" fill="currentColor" stroke="none"></circle><circle cx="4" cy="18" r="1.5" fill="currentColor" stroke="none"></circle>',
  ask: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" fill="none"></path>',
};

const _LIVECODE_MODE_DROPDOWN_STYLE =
  "display:none;position:fixed;min-width:180px;max-height:320px;overflow:hidden;" +
  "border:1px solid rgba(71,85,105,0.4);border-radius:12px;box-shadow:0 -4px 20px rgba(0,0,0,0.3);" +
  "z-index:10050;flex-direction:column;";

function _livecodeGetModeIconHtml(modeValue, extraClass) {
  const paths = _LIVECODE_MODE_ICON_PATHS[modeValue] || _LIVECODE_MODE_ICON_PATHS.agent;
  const cls = "livecode-mode-icon-svg" + (extraClass ? " " + String(extraClass).trim() : "");
  const stroke = modeValue === "agent" ? "" : ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
  return '<svg class="' + cls + '" width="14" height="14" viewBox="0 0 24 24" fill="none"' + stroke + ' data-icon="mode-' + (modeValue || "agent") + '" aria-hidden="true">' + paths + '</svg>';
}

function _livecodeFileIcon(name) {
  return typeof window.getFileIcon === "function" ? window.getFileIcon(name) : "/asset/material-icons/file.svg";
}
window.livecodeFileIcon = _livecodeFileIcon;

function _livecodeFolderIcon(expanded, name) {
  if (typeof window.getFolderIcon === "function") return window.getFolderIcon(name || "", !!expanded);
  return expanded ? "/asset/material-icons/folder-open.svg" : "/asset/material-icons/folder.svg";
}

const _LIVECODE_ICON_SIZE_PX = { xs: 10, sm: 12, base: 14, lg: 16, xl: 20 };

const _LIVECODE_ICON_COLOR_VARS = {
  secondary: "var(--livecode-icon-secondary)", tertiary: "var(--livecode-icon-tertiary)",
  red: "var(--livecode-icon-red-primary)", green: "var(--livecode-icon-green-primary)",
};

const _LIVECODE_SVG_ICONS = {
  "arrow-up": '<line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline>',
  "arrow-left": '<line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline>',
  "arrow-right": '<line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline>',
  "at": '<circle cx="12" cy="12" r="4"></circle><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-5.5 8.28"></path>',
  "check": '<polyline points="20 6 9 17 4 12"></polyline>',
  "check-circle": '<circle cx="12" cy="12" r="9"></circle><polyline points="8.5 12.5 11 15 16 9"></polyline>',
  "chevron-down": '<polyline points="6 9 12 15 18 9"></polyline>',
  "chevron-right": '<polyline points="9 18 15 12 9 6"></polyline>',
  "chevron-up": '<polyline points="6 15 12 9 18 15"></polyline>',
  "clock": '<circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 15.5 14"></polyline>',
  "copy": '<rect x="9" y="9" width="12" height="12" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>',
  "ellipsis": '<circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none"></circle><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"></circle><circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none"></circle>',
  "error": '<circle cx="12" cy="12" r="9"></circle><line x1="12" y1="8" x2="12" y2="13"></line><line x1="12" y1="16.5" x2="12" y2="16.51"></line>',
  "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline>',
  "folder": '<path d="M3 7a2 2 0 0 1 2-2h4.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>',
  "folders": '<path d="M6 9V6a2 2 0 0 1 2-2h3l2 2h5a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-1"></path><path d="M2 10a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"></path>',
  "globe": '<circle cx="12" cy="12" r="9"></circle><line x1="3" y1="12" x2="21" y2="12"></line><path d="M12 3c2.6 2.6 4 5.9 4 9s-1.4 6.4-4 9c-2.6-2.6-4-5.9-4-9s1.4-6.4 4-9z"></path>',
  "history": '<circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 16 14"></polyline>',
  "image": '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path>',
  "list-todo": '<path d="M9 11l3 3L22 4"></path><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>',
  "loading": '<circle cx="12" cy="12" r="9" stroke-opacity="0.25"></circle><path d="M21 12a9 9 0 0 0-9-9"></path>',
  "mic": '<rect x="9" y="2" width="6" height="12" rx="3"></rect><path d="M5 10a7 7 0 0 0 14 0"></path><line x1="12" y1="17" x2="12" y2="21"></line><line x1="8" y1="21" x2="16" y2="21"></line>',
  "paperclip": '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path>',
  "pencil": '<path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path>',
  "plug": '<path d="M9 2v5M15 2v5"></path><path d="M6 7h12v4a6 6 0 0 1-12 0z"></path><path d="M12 17v5"></path>',
  "plus": '<line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line>',
  "search": '<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>',
  "sliders": '<line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line>',
  "square": '<rect x="4" y="4" width="16" height="16" rx="2"></rect>',
  "stop": '<rect x="5.5" y="5.5" width="13" height="13" rx="2.5" fill="currentColor"></rect>',
  "terminal": '<polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line>',
  "trash": '<polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>',
  "x": '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>',
  "arrows-cw": '<path d="M20 11a8 8 0 0 0-14.9-3.9L4 8.5"></path><path d="M4 4v4.5h4.5"></path><path d="M4 13a8 8 0 0 0 14.9 3.9l1.1-1.4"></path><path d="M20 20v-4.5h-4.5"></path>',
  "cookie": '<path d="M12 2a10 10 0 1 0 10 10 4 4 0 0 1-5-5 4 4 0 0 1-5-5z"></path><circle cx="8.5" cy="12.5" r="1" fill="currentColor" stroke="none"></circle><circle cx="12.5" cy="16.5" r="1" fill="currentColor" stroke="none"></circle>',
  "pointer-arrow": '<path d="M3 2l7.07 16.97 2.51-7.39 7.39-2.51z" fill="currentColor" stroke="currentColor" stroke-linejoin="round"></path>',
  "dots-3-vertical": '<circle cx="12" cy="5" r="1.4" fill="currentColor" stroke="none"></circle><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"></circle><circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none"></circle>',
  "arrows-expand": '<path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path>',
  "arrows-contract": '<polyline points="4 14 10 14 10 20"></polyline><polyline points="20 10 14 10 14 4"></polyline><line x1="14" y1="10" x2="21" y2="3"></line><line x1="10" y1="14" x2="3" y2="21"></line>',
  "layout-split-vertical": '<rect x="3" y="3" width="18" height="18" rx="2"></rect><line x1="12" y1="3" x2="12" y2="21"></line>',
  "layout-panel-bottom": '<rect x="3" y="3" width="18" height="18" rx="2"></rect><line x1="3" y1="15" x2="21" y2="15"></line>',
  "mobile": '<rect x="7" y="2" width="10" height="20" rx="2"></rect><line x1="11" y1="18" x2="13" y2="18"></line>',
  "displays": '<rect x="2" y="3" width="20" height="14" rx="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line>',
  "figma": '<path d="M8 3h5v5H8a2.5 2.5 0 0 1 0-5z"></path><path d="M8 8h5v5H8a2.5 2.5 0 0 1 0-5z"></path><path d="M8 13h5v5H8a2.5 2.5 0 0 1 0-5z"></path><circle cx="15.5" cy="10.5" r="2.5"></circle>',
  "inspect": '<rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M9 9h6v6H9z"></path>',
  "ruler": '<path d="M3 16.5L7.5 21 21 7.5 16.5 3z"></path><line x1="7.5" y1="7.5" x2="10" y2="10"></line><line x1="11" y1="11" x2="13.5" y2="13.5"></line>',
  "layers": '<polygon points="12 2 22 8.5 12 15 2 8.5"></polygon><polyline points="2 15.5 12 22 22 15.5"></polyline>',
  "play": '<path d="M8 5v14l11-7z" fill="currentColor" stroke="none"></path>',
  "settings-gear": '<circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>',
};
_LIVECODE_SVG_ICONS["infinity"] = _LIVECODE_MODE_ICON_PATHS.agent;
_LIVECODE_SVG_ICONS["chat-bubble"] = _LIVECODE_MODE_ICON_PATHS.ask;

function _livecodeIcon(name, opts) {
  const o = opts || {};
  const paths = _LIVECODE_SVG_ICONS[name] || _LIVECODE_SVG_ICONS["chevron-right"];
  const px = _LIVECODE_ICON_SIZE_PX[o.size] || _LIVECODE_ICON_SIZE_PX.sm;
  const cls = "ui-icon" + (o.spin ? " is-spinning" : "") + (o.className ? " " + o.className : "");
  const color = o.color && _LIVECODE_ICON_COLOR_VARS[o.color] ? ' style="color:' + _LIVECODE_ICON_COLOR_VARS[o.color] + '"' : "";
  return '<svg class="' + cls + '" width="' + px + '" height="' + px + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"' + color + ' data-icon="' + name + '" aria-hidden="true">' + paths + '</svg>';
}

function _livecodeDotGridHtml(size) {
  let dots = "";
  for (let i = 0; i < 9; i++) {
    dots += '<circle data-dot-index="' + (i + 1) + '" cx="' + (1.25 + (i % 3) * 4) + '" cy="' + (1.25 + Math.floor(i / 3) * 4) + '" r="1.125"></circle>';
  }
  return '<span class="ui-dot-grid-loader" data-shape="sine_3x3" data-size="' + (size || "xs") + '" aria-hidden="true">' +
    '<svg viewBox="0 0 10.5 10.5" focusable="false" role="presentation">' + dots + "</svg></span>";
}

const LIVECODE_CHAT_TAB_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
const _LIVECODE_TAB_MESSAGE_ICON = LIVECODE_CHAT_TAB_ICON;
const _LIVECODE_TAB_SPINNER_ICON = _livecodeDotGridHtml("xs");

function _livecodeGetTabIconHtml(tab) {
  if (tab && tab.agentRunning) return _LIVECODE_TAB_SPINNER_ICON;
  return _LIVECODE_TAB_MESSAGE_ICON;
}

function _livecodeClearTabUnread(tab) {
  if (tab) tab.hasUnread = false;
}

let livecodeIndexReady = false;
let livecodeIndexFileCount = 0;
let livecodeIndexSymbolCount = 0;
let livecodeIndexTruncated = false;

function _livecodeApplyIndexResult(data) {
  livecodeIndexReady = true;
  livecodeIndexFileCount = data.file_count || 0;
  livecodeIndexSymbolCount = data.symbol_count || 0;
  livecodeIndexTruncated = !!data.truncated;
}

const LIVECODE_LAST_PROJECT_KEY = "livecode_last_project";

const LIVECODE_BROWSER_WORKSPACE_KEY = "livecode_browser_workspace";

const LIVECODE_RECENT_PROJECTS_KEY = "livecode_recent_projects";

const LIVECODE_TABS_STORAGE_PREFIX = "livecode_tabs_v1:";

const LIVECODE_CHAT_STORAGE_PREFIX = "livecode_chat_v1:";

const LIVECODE_EDITOR_TABS_STORAGE_PREFIX = "livecode_editor_tabs_v1:";

const LIVECODE_MCP_SELECTED_STORAGE_PREFIX = "livecode_mcp_selected_v2:";

let livecodeWorkspaceRootOrder = [];

function _livecodeStorageGet(key) {
  if (!key) return null;
  try {
    const fromLocal = localStorage.getItem(key);
    if (fromLocal != null) return fromLocal;
    return sessionStorage.getItem(key);
  } catch (e) {
    return null;
  }
}

function _livecodeStorageSet(key, value) {
  if (!key) return;
  try {
    localStorage.setItem(key, value);
  } catch (e) {}
  try {
    sessionStorage.setItem(key, value);
  } catch (e) {}
}

function _livecodeStorageRemove(key) {
  if (!key) return;
  try {
    localStorage.removeItem(key);
  } catch (e) {}
  try {
    sessionStorage.removeItem(key);
  } catch (e) {}
}

function _livecodeTabHasConversation(tab) {
  return !!(tab && (tab.chatStarted || (tab.messagesHtml && String(tab.messagesHtml).trim())));
}

function _livecodeResetTabToNewChat(tab) {
  if (!tab) return;
  const newId = _livecodeNewChatSessionId();
  tab.sessionId = newId;
  tab.title = "New chat";
  tab.messagesHtml = "";
  tab.chatStarted = false;
  tab.hasUnread = false;
  tab.pendingQuestion = null;
  livecodeAgentSessionId = newId;
  _livecodeChatStarted = false;
  _livecodeLoadChatTabState(tab);
  _livecodeUpdateChatWelcome();
  _livecodeRenderChatTabs();
}

function _livecodeChatSnapshotKey(projectPath, sessionId, stateKey) {
  const project = stateKey || _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  const sid = String(sessionId || "").trim();
  if (!project || !sid) return "";
  return LIVECODE_CHAT_STORAGE_PREFIX + project + ":" + sid;
}

function _livecodePersistChatSnapshot(projectPath, tab, stateKey) {
  if (!tab || !projectPath || !tab.sessionId) return;
  if (_livecodeIsActiveTab(tab) && !tab.agentRunning) {
    const out = getLiveCodeChatOutput();
    if (out) {
      _livecodeFinalizeDomForSnapshot(out);
      tab.messagesHtml = out.innerHTML;
    }
  }
  if (!tab.messagesHtml) return;

  try {
    const tmp = document.createElement("div");
    tmp.innerHTML = tab.messagesHtml;
    _livecodeStripErrorActivityRows(tmp);
    tab.messagesHtml = tmp.innerHTML;
  } catch (e) {}

  const key = _livecodeChatSnapshotKey(projectPath, tab.sessionId, stateKey);
  if (!key) return;
  try {
    _livecodeStorageSet(key, JSON.stringify({
      messagesHtml: tab.messagesHtml,
      title: tab.title || "",
      chatStarted: !!tab.chatStarted,
      updatedAt: Date.now(),
    }));
  } catch (e) {}
}

const _LIVECODE_LEGACY_TRANSCRIPT_SELECTOR = [
  ".livecode-activity-line:not(.ui-tool-call-line):not(.ui-collapsible-header)",
  ".livecode-term-card:not(.ui-shell-tool-call)",
  ".livecode-diff-block:not(.ui-edit-tool-call)",
  ".livecode-agent-row:not([data-subagent-task-card])",
].join(", ");

function _livecodeIsLegacyTranscriptHtml(html) {
  const text = String(html || "");
  if (!/livecode-(?:activity-line|term-card|diff-block|agent-row)/.test(text)) return false;
  try {
    const tpl = document.createElement("template");
    tpl.innerHTML = text;
    if (tpl.content.querySelector(_LIVECODE_LEGACY_TRANSCRIPT_SELECTOR)) return true;
    return Array.from(tpl.content.querySelectorAll(".livecode-term-card")).some(function(card) {
      return !card.querySelector(".ui-shell-tool-call__description-row");
    }) || Array.from(tpl.content.querySelectorAll(".livecode-diff-block")).some(function(block) {
      return !block.querySelector(".ui-tool-call-card__expand-button");
    });
  } catch (e) {
    return false;
  }
}

function _livecodeLoadChatSnapshot(projectPath, sessionId) {
  const key = _livecodeChatSnapshotKey(projectPath, sessionId);
  if (!key) return null;
  try {
    const raw = _livecodeStorageGet(key);
    if (!raw) return null;
    const snap = JSON.parse(raw);
    if (snap && _livecodeIsLegacyTranscriptHtml(snap.messagesHtml)) {
      _livecodeStorageRemove(key);
      return null;
    }
    return snap;
  } catch (e) {
    return null;
  }
}

function _livecodePersistTabsStorage(projectPath) {
  const project = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!project) return;
  try {
    _livecodeStorageSet(LIVECODE_TABS_STORAGE_PREFIX + project, JSON.stringify({
      tabs: livecodeChatTabs.map(_livecodeSnapshotTab),
      activeTabId: livecodeActiveChatTabId,
      tabCounter: livecodeChatTabCounter,
    }));
  } catch (e) {}
}

function _livecodeLoadTabsStorage(projectPath) {
  const project = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!project) return null;
  try {
    const raw = _livecodeStorageGet(LIVECODE_TABS_STORAGE_PREFIX + project);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function _livecodePersistEditorTabs(projectPath) {
  const project = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!project) return;
  const openPaths = Object.keys(ideOpenFiles);
  const key = LIVECODE_EDITOR_TABS_STORAGE_PREFIX + project;
  if (!openPaths.length) {
    _livecodeStorageRemove(key);
    return;
  }
  try {
    _livecodeStorageSet(key, JSON.stringify({
      openPaths: openPaths,
      activeFile: ideActiveFile && ideOpenFiles[ideActiveFile] ? ideActiveFile : openPaths[openPaths.length - 1],
    }));
  } catch (e) {}
}

let _livecodePersistEditorTabsTimer = null;
let _livecodeEditorAutosaveBound = false;
let _livecodeEditorAutosaveTimer = null;

function _livecodeFlushPersistEditorTabs() {
  if (_livecodePersistEditorTabsTimer) {
    clearTimeout(_livecodePersistEditorTabsTimer);
    _livecodePersistEditorTabsTimer = null;
  }
  if (livecodeProjectPath) _livecodePersistEditorTabs(livecodeProjectPath);
}

function _livecodePersistEditorTabsDebounced(projectPath) {
  if (!projectPath) return;
  if (_livecodePersistEditorTabsTimer) clearTimeout(_livecodePersistEditorTabsTimer);
  _livecodePersistEditorTabsTimer = setTimeout(function() {
    _livecodePersistEditorTabsTimer = null;
    _livecodePersistEditorTabs(projectPath);
  }, 400);
}

function _livecodeBindEditorAutosaveOnce() {
  if (_livecodeEditorAutosaveBound || !window.ideEditor) return;
  _livecodeEditorAutosaveBound = true;
  window.ideEditor.onDidChangeModelContent(function() {
    if (!ideActiveFile || !ideOpenFiles[ideActiveFile]) return;
    const fileInfo = ideOpenFiles[ideActiveFile];
    if (fileInfo.isSettings || fileInfo.isBrowser) return;
    if (_livecodeFileUsesPlanSurface(fileInfo) && fileInfo.viewMode === "markdown") return;
    const currentContent = window.ideEditor.getValue();
    const originalContent = fileInfo.originalContent || "";
    const isModified = currentContent !== originalContent;
    if (fileInfo.modified !== isModified) {
      fileInfo.modified = isModified;
      _livecodeUpdateOpenFileTabBadge(ideActiveFile);
    }
    if (isModified) {
      clearTimeout(_livecodeEditorAutosaveTimer);
      _livecodeEditorAutosaveTimer = setTimeout(function() {
        autoSaveIDEFile(ideActiveFile);
      }, typeof window._livecodeEditorAutosaveDelay === "function" ? window._livecodeEditorAutosaveDelay() : 1e3);
    }
  });
}

function _livecodeLoadEditorTabsStorage(projectPath) {
  const project = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!project) return null;
  try {
    const raw = _livecodeStorageGet(LIVECODE_EDITOR_TABS_STORAGE_PREFIX + project);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.openPaths) || !parsed.openPaths.length) return null;
    return parsed;
  } catch (e) {
    return null;
  }
}

function _livecodeClearEditorFiles() {
  Object.keys(ideOpenFiles).forEach(function(filePath) {
    const info = ideOpenFiles[filePath];
    try { if (window.WBLsp) window.WBLsp.onFileClosed(filePath); } catch (e) {}
    if (info && info.model) {
      try {
        info.model.dispose();
      } catch (e) {}
    }
  });
  ideOpenFiles = {};
  ideActiveFile = null;
}

function _livecodeRestoreEditorTabs(projectPath) {
  const saved = _livecodeLoadEditorTabsStorage(projectPath);
  if (!saved || !saved.openPaths || !saved.openPaths.length) {
    showLiveCodeEditorIdle();
    return;
  }
  const paths = saved.openPaths.filter(function(p) { return !!p; });
  if (!paths.length) {
    showLiveCodeEditorIdle();
    return;
  }
  const active = saved.activeFile && paths.indexOf(saved.activeFile) !== -1
    ? saved.activeFile
    : paths[paths.length - 1];
  const backgroundPaths = paths.filter(function(p) { return p !== active; });

  function _livecodeLoadEditorTabFile(filePath, onDone) {
    if (ideOpenFiles[filePath]) {
      onDone();
      return;
    }
    if (_livecodeIsPlanTabKey(filePath)) {
      const planFile = filePath.slice(LIVECODE_PLAN_TAB_PREFIX.length);
      window.openLiveCodePlanTab(planFile, "", { activate: false }).then(onDone);
      return;
    }
    if (filePath === LIVECODE_SETTINGS_TAB_KEY) {
      ideOpenFiles[filePath] = { isSettings: true, path: filePath, name: "Settings", content: "", originalContent: "", modified: false };
      onDone();
      return;
    }
    if (filePath === LIVECODE_BROWSER_TAB_KEY) {
      ideOpenFiles[filePath] = { isBrowser: true, path: filePath, name: "Browser", content: "", originalContent: "", modified: false };
      onDone();
      return;
    }
    _livecodeIdeSocketRequest(
      "ide_read_file", { path: filePath },
      "ide_file_content",
      function(data) { return data && data.path === filePath; }
    ).then(function(result) {
      const data = result.data;
      if (!data.error) {
        const content = data.content || "";
        const fileName = filePath.split("/").pop();
        ideOpenFiles[filePath] = {
          content: content,
          path: filePath,
          name: fileName,
          modified: false,
          originalContent: content,
          readOnlyLarge: !!data.large_file,
        };
      } else if (result.error) {
        console.error("LiveCode: failed to restore tab for", filePath, data.error);
      }
      onDone();
    });
  }

  _livecodeLoadEditorTabFile(active, function() {
    if (ideOpenFiles[active]) {
      switchToFile(active);
    } else {
      const remaining = Object.keys(ideOpenFiles);
      if (remaining.length) switchToFile(remaining[remaining.length - 1]);
      else showLiveCodeEditorIdle();
    }
    updateOpenFilesList(true);
    updatePlayButtonVisibility();
    _livecodePersistEditorTabsDebounced(projectPath);
    backgroundPaths.forEach(function(filePath) {
      _livecodeLoadEditorTabFile(filePath, function() {});
    });
  });
}

function _livecodeSaveProjectUiState() {
  if (!livecodeProjectPath) return;
  _livecodeSaveTabsForProject(livecodeProjectPath);
  _livecodeFlushPersistEditorTabs();
}

let _livecodeProjectStateListenersBound = false;

function _livecodeBindProjectStateListenersOnce() {
  if (_livecodeProjectStateListenersBound) return;
  _livecodeProjectStateListenersBound = true;
  window.addEventListener("pagehide", _livecodeSaveProjectUiState);
  window.addEventListener("beforeunload", _livecodeSaveProjectUiState);
}

function _livecodeStripErrorActivityRows(root) {
  if (!root) return;
  root.querySelectorAll(".livecode-activity-line.is-error").forEach(function(line) {
    const outer = line.closest(".livecode-activity-wrap-outer") || line.closest(".livecode-activity-wrap");
    (outer || line).remove();
  });
  root.querySelectorAll(".chat-row.livecode-agent-steps-row").forEach(function(row) {
    const steps = row.querySelector(".livecode-agent-steps");
    if (steps && !steps.querySelector(".livecode-activity-wrap-outer, .livecode-activity-wrap")) {
      row.remove();
    }
  });
}

function _livecodePostProcessRestoredOutput(out) {
  _livecodeSyncPlanCardButtons(out);
  if (!out) return;
  _livecodeRegroupTranscript(out);
  if (out.querySelector(".livecode-agents-card")) _livecodeBindAgentsCardOnce();
  _livecodeScheduleStickyUserRows(out);
  _livecodeStripErrorActivityRows(out);
  out.querySelectorAll(".livecode-term-card").forEach(_livecodeTerminalSyncScroll);
  if (typeof window.rehydrateLivecodeChatMarkdown === "function") {
    window.rehydrateLivecodeChatMarkdown(out);
  }
  if (typeof decorateCodeBlocks === "function") {
    out.querySelectorAll(".livecode-code-card").forEach(function(card) {
      decorateCodeBlocks(card);
    });
  }
  if (typeof window._decorateChatLinksGlobal === "function") {
    out.querySelectorAll(".livecode-stream-msg, .chat-msg.assistant.livecode-plain-msg").forEach(function(el) {
      window._decorateChatLinksGlobal(el);
    });
  }
  out.querySelectorAll(".chat-msg.assistant").forEach(_livecodeDecorateFileCodeSpans);
}

function _livecodeApplySnapshotToTab(tab, sessionId, snap) {
  if (!tab || !snap || !snap.messagesHtml) return false;
  tab.sessionId = sessionId;
  tab.messagesHtml = snap.messagesHtml;
  tab.chatStarted = true;
  tab.title = _livecodeTruncateTabTitle(snap.title || tab.title || "New chat");
  livecodeAgentSessionId = sessionId;
  _livecodeChatStarted = true;
  _livecodeShowChatContainer();

  _livecodeLoadChatTabState(tab);
  _livecodePostProcessRestoredOutput(getLiveCodeChatOutput());
  _livecodeRenderChatTabs();
  return true;
}

function _livecodeClearChatSnapshot(projectPath, sessionId) {
  const key = _livecodeChatSnapshotKey(projectPath, sessionId);
  if (!key) return;
  _livecodeStorageRemove(key);
}

function _livecodeResolveSessionTitle(session) {
  const sid = (session && session.session_id) || "";
  if (!sid) return "Chat";
  if (session.title) return session.title;
  if (livecodeProjectPath) {
    const pending = _livecodeGetPendingSessionTitles(livecodeProjectPath);
    if (pending[sid]) return pending[sid];
  }
  const tab = livecodeChatTabs.find(function(t) { return t.sessionId === sid; });
  if (tab && tab.title && tab.title !== "New chat" && tab.title !== "Loading…") {
    return tab.title;
  }
  return session.first_user_preview || sid || "Chat";
}

function _livecodeUpsertPendingSession(sessionId, title) {
  if (!sessionId || !title || !livecodeProjectPath) return;
  const pending = _livecodeGetPendingSessionTitles(livecodeProjectPath);
  pending[sessionId] = title;
  let found = false;
  let cached = _livecodeGetCachedSessions(livecodeProjectPath).slice();
  cached = cached.map(function(s) {
    if (s.session_id !== sessionId) return s;
    found = true;
    return Object.assign({}, s, { title: title });
  });
  if (!found) {
    cached.unshift({
      session_id: sessionId,
      title: title,
      updated_at: Date.now() / 1000,
    });
  }
  _livecodeSetCachedSessions(livecodeProjectPath, cached);
}

function _livecodePurgeDeletedSession(sessionId) {
  if (!sessionId || !livecodeProjectPath) return;
  const pending = _livecodeGetPendingSessionTitles(livecodeProjectPath);
  delete pending[sessionId];
  _livecodeClearChatSnapshot(livecodeProjectPath, sessionId);
  _livecodeSetCachedSessions(livecodeProjectPath, _livecodeGetCachedSessions(livecodeProjectPath).filter(function(s) {
    return s.session_id !== sessionId;
  }));
  const tabsForSession = livecodeChatTabs.filter(function(t) { return t.sessionId === sessionId; });
  tabsForSession.forEach(function(tab) {
    if (tab.agentRunning) return;
    if (livecodeChatTabs.length <= 1) {
      _livecodeResetTabToNewChat(tab);
    } else {
      _livecodeCloseChatTab(tab.id);
    }
  });
}

function _livecodeFormatSessionTime(ts) {
  const n = Number(ts);
  if (!n || Number.isNaN(n)) return "";
  try {
    const d = new Date(n * 1000);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  } catch (e) {
    return "";
  }
}

var _livecodeCachedSessionsByProject = {};
var _livecodePendingSessionTitlesByProject = {};
var _livecodeSessionFetchToken = 0;
var _livecodeSessionMenuOpen = false;

function _livecodeGetPendingSessionTitles(projectPath) {
  const key = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!key) return {};
  if (!_livecodePendingSessionTitlesByProject[key]) {
    _livecodePendingSessionTitlesByProject[key] = {};
  }
  return _livecodePendingSessionTitlesByProject[key];
}

function _livecodeGetCachedSessions(projectPath) {
  const key = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!key) return [];
  return _livecodeCachedSessionsByProject[key] || [];
}

function _livecodeSetCachedSessions(projectPath, sessions) {
  const key = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!key) return;
  _livecodeCachedSessionsByProject[key] = sessions;
}

function _livecodeResetSessionMenuForProject(projectPath) {
  const key = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(projectPath);
  if (!key) return;
  delete _livecodeCachedSessionsByProject[key];
  delete _livecodePendingSessionTitlesByProject[key];
}

function _livecodeInvalidateSessionFetches() {
  _livecodeSessionFetchToken++;
}
var _livecodeSessionMenuPositionBound = false;

function _livecodeEnsureSessionMenuPortal() {
  const menu = document.getElementById("livecode-chat-session-menu");
  if (menu && menu.parentElement !== document.body) {
    document.body.appendChild(menu);
  }
}

function _livecodePositionSessionMenu() {
  const menu = document.getElementById("livecode-chat-session-menu");
  const btn = document.getElementById("livecode-chat-session-history");
  if (!menu || !btn) return;
  const rect = btn.getBoundingClientRect();
  const gap = 4;
  const viewportPad = 8;
  menu.style.top = Math.max(viewportPad, rect.bottom + gap) + "px";
  menu.style.right = Math.max(viewportPad, window.innerWidth - rect.right) + "px";
  menu.style.left = "auto";
  const maxH = Math.min(420, window.innerHeight - rect.bottom - gap - viewportPad);
  menu.style.maxHeight = Math.max(160, maxH) + "px";
}

function _livecodeOnSessionMenuReposition() {
  if (!_livecodeSessionMenuOpen) return;
  _livecodePositionSessionMenu();
}

function _livecodeBindSessionMenuReposition() {
  if (_livecodeSessionMenuPositionBound) return;
  _livecodeSessionMenuPositionBound = true;
  window.addEventListener("resize", _livecodeOnSessionMenuReposition, true);
  document.addEventListener("scroll", _livecodeOnSessionMenuReposition, true);
}

function _livecodeUnbindSessionMenuReposition() {
  if (!_livecodeSessionMenuPositionBound) return;
  _livecodeSessionMenuPositionBound = false;
  window.removeEventListener("resize", _livecodeOnSessionMenuReposition, true);
  document.removeEventListener("scroll", _livecodeOnSessionMenuReposition, true);
}

const _LIVECODE_SESSION_CHECK_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><polyline points="9 12 11 14 15 10"></polyline></svg>';
const _LIVECODE_SESSION_RUNNING_ICON = _livecodeDotGridHtml("xs");

const LIVECODE_SESSIONS_FETCH_TIMEOUT_MS = 20000;

function _livecodeMarkSessionsFetchFailed(list) {
  const result = list || [];
  result._livecodeFetchFailed = true;
  return result;
}

function _livecodeFetchSessions(projectPath) {
  if (!projectPath) return Promise.resolve([]);
  const hasAbort = typeof AbortController !== "undefined";
  const controller = hasAbort ? new AbortController() : null;
  const timeoutId = controller ? setTimeout(function() { controller.abort(); }, LIVECODE_SESSIONS_FETCH_TIMEOUT_MS) : null;
  const fetchOptions = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_path: projectPath,
      limit: 30,
      workspace: _livecodeCurrentWorkspacePayload(),
    }),
  };
  if (controller) fetchOptions.signal = controller.signal;
  return fetch(
    "/livecode/sessions",
    fetchOptions
  )
    .then(function(r) {
      if (timeoutId) clearTimeout(timeoutId);
      return r.json().then(function(data) {
        if (!r.ok || !data || !data.success) {
          console.error("LiveCode: failed to load sessions for", projectPath, (data && data.error) || r.status);
          return _livecodeMarkSessionsFetchFailed([]);
        }
        return data.sessions || [];
      });
    })
    .catch(function(err) {
      if (timeoutId) clearTimeout(timeoutId);
      console.error("LiveCode: failed to load sessions for", projectPath, err);
      return _livecodeMarkSessionsFetchFailed([]);
    });
}

function _livecodeGroupSessionsByDate(sessions) {
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const groups = { today: [], yesterday: [], older: [] };
  (sessions || []).forEach(function(s) {
    const ts = Number(s.updated_at);
    if (!ts || Number.isNaN(ts)) {
      groups.older.push(s);
      return;
    }
    const d = new Date(ts * 1000);
    if (d.toDateString() === today.toDateString()) groups.today.push(s);
    else if (d.toDateString() === yesterday.toDateString()) groups.yesterday.push(s);
    else groups.older.push(s);
  });
  return groups;
}

function _livecodeUnbindSessionItemMenuReposition(menu) {
  if (!menu || !menu._livecodeRepositionHandler) return;
  window.removeEventListener("resize", menu._livecodeRepositionHandler, true);
  document.removeEventListener("scroll", menu._livecodeRepositionHandler, true);
  menu._livecodeRepositionHandler = null;
}

function _livecodePositionSessionItemMenu(menu, anchor) {
  if (!menu || !anchor) return;
  const gap = 4;
  const viewportPad = 8;
  const rect = anchor.getBoundingClientRect();
  menu.style.display = "flex";
  const menuRect = menu.getBoundingClientRect();
  let top = rect.bottom + gap;
  let left = rect.right - menuRect.width;
  if (top + menuRect.height > window.innerHeight - viewportPad) {
    top = rect.top - menuRect.height - gap;
  }
  left = Math.max(viewportPad, Math.min(left, window.innerWidth - menuRect.width - viewportPad));
  top = Math.max(viewportPad, Math.min(top, window.innerHeight - menuRect.height - viewportPad));
  menu.style.top = top + "px";
  menu.style.left = left + "px";
}

function _livecodeBindSessionItemMenuReposition(menu, anchor) {
  _livecodeUnbindSessionItemMenuReposition(menu);
  const handler = function() {
    if (!menu.isConnected) {
      _livecodeUnbindSessionItemMenuReposition(menu);
      return;
    }
    _livecodePositionSessionItemMenu(menu, anchor);
  };
  menu._livecodeRepositionHandler = handler;
  window.addEventListener("resize", handler, true);
  document.addEventListener("scroll", handler, true);
}

function _livecodeCloseSessionItemMenus() {
  document.querySelectorAll(".livecode-chat-session-item-menu").forEach(function(menu) {
    _livecodeUnbindSessionItemMenuReposition(menu);
  });
  if (typeof window.closeChatMenus === "function") {
    window.closeChatMenus();
  }
}

function _livecodeBuildSessionMoreMenu(sessionId, currentTitle) {
  const menu = document.createElement("div");
  menu.className = "chat-history-menu livecode-chat-session-item-menu";
  const renameBtn = document.createElement("button");
  renameBtn.type = "button";
  renameBtn.className = "chat-history-menu-item";
  renameBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8.00012L4 16.0001V20.0001L8 20.0001L16 12.0001M12 8.00012L14.8686 5.13146L14.8704 5.12976C15.2652 4.73488 15.463 4.53709 15.691 4.46301C15.8919 4.39775 16.1082 4.39775 16.3091 4.46301C16.5369 4.53704 16.7345 4.7346 17.1288 5.12892L18.8686 6.86872C19.2646 7.26474 19.4627 7.46284 19.5369 7.69117C19.6022 7.89201 19.6021 8.10835 19.5369 8.3092C19.4628 8.53736 19.265 8.73516 18.8695 9.13061L18.8686 9.13146L16 12.0001M12 8.00012L16 12.0001"/></svg>Rename';
  renameBtn.onclick = function(ev) {
    ev.stopPropagation();
    window.renameLiveCodeSession(sessionId, currentTitle, ev);
    _livecodeCloseSessionItemMenus();
  };
  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "chat-history-menu-item danger";
  deleteBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9,3H7c0-1.7,1.3-3,3-3v2C9.4,2,9,2.4,9,3z"/><path d="M17,3h-2c0-0.6-0.4-1-1-1V0C15.7,0,17,1.3,17,3z"/><polygon points="17,6 7,6 7,3 9,3 9,4 15,4 15,3 17,3"/><rect x="10" width="4" height="2"/><path d="M21,6H3C2.4,6,2,5.6,2,5s0.4-1,1-1h18c0.6,0,1,0.4,1,1S21.6,6,21,6z"/><path d="M19,24H5c-0.6,0-1-0.4-1-1V9c0-0.6,0.4-1,1-1h14c0.6,0,1,0.4,1,1v14C20,23.6,19.6,24,19,24z M6,22h12V10H6V22z"/><path d="M10,20c-0.6,0-1-0.4-1-1v-6c0-0.6,0.4-1,1-1s1,0.4,1,1v6C11,19.6,10.6,20,10,20z"/><path d="M14,20c-0.6,0-1-0.4-1-1v-6c0-0.6,0.4-1,1-1s1,0.4,1,1v6C15,19.6,14.6,20,14,20z"/></svg>Delete';
  deleteBtn.onclick = function(ev) {
    ev.stopPropagation();
    window.deleteLiveCodeSession(sessionId, ev);
    _livecodeCloseSessionItemMenus();
  };
  menu.appendChild(renameBtn);
  menu.appendChild(deleteBtn);
  return menu;
}

function _livecodeCreateSessionMenuRow(session) {
  const sid = session.session_id || "";
  const titleText = _livecodeResolveSessionTitle(session);
  const isActive = sid === livecodeAgentSessionId;
  const isRunning = livecodeChatTabs.some(function(t) {
    return t.sessionId === sid && t.agentRunning;
  });
  const hasUnread = !isRunning && livecodeChatTabs.some(function(t) {
    return t.sessionId === sid && t.hasUnread;
  });

  const row = document.createElement("div");
  row.className = "livecode-chat-session-menu-item-row theme-transition" + (hasUnread ? " has-unread" : "");
  row.dataset.sessionId = sid;

  const itemBtn = document.createElement("button");
  itemBtn.type = "button";
  itemBtn.className = "livecode-chat-session-menu-item theme-transition" +
    (isActive ? " is-active" : "") +
    (isRunning ? " is-running" : "");
  const iconSpan = document.createElement("span");
  iconSpan.className = "livecode-chat-session-menu-item-icon";
  iconSpan.innerHTML = isRunning ? _LIVECODE_SESSION_RUNNING_ICON : _LIVECODE_SESSION_CHECK_ICON;
  const titleSpan = document.createElement("span");
  titleSpan.className = "livecode-chat-session-menu-item-title";
  titleSpan.textContent = titleText;
  itemBtn.appendChild(iconSpan);
  itemBtn.appendChild(titleSpan);
  itemBtn.onclick = function(e) {
    if (e.target.closest(".chat-history-menu") || e.target.closest(".livecode-chat-session-menu-item-more")) {
      return;
    }
    _livecodeCloseSessionItemMenus();
    window.resumeLiveCodeSession(sid);
    closeLiveCodeSessionMenu();
  };

  const moreBtn = document.createElement("button");
  moreBtn.type = "button";
  moreBtn.className = "livecode-chat-session-menu-item-more chat-history-item-more";
  moreBtn.title = "More";
  moreBtn.setAttribute("aria-label", "More options");
  moreBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="19" cy="12" r="1"></circle></svg>';
  moreBtn.onclick = function(e) {
    e.stopPropagation();
    const existing = document.querySelector(".livecode-chat-session-item-menu.is-portaled");
    _livecodeCloseSessionItemMenus();
    if (existing) return;
    const menu = _livecodeBuildSessionMoreMenu(sid, titleText);
    menu.classList.add("is-portaled");
    document.body.appendChild(menu);
    _livecodePositionSessionItemMenu(menu, moreBtn);
    _livecodeBindSessionItemMenuReposition(menu, moreBtn);
  };

  row.appendChild(itemBtn);
  row.appendChild(moreBtn);
  return row;
}

function _livecodePopulateSessionMenuList(list, sessions, fetchFailed, projectPath) {
  list.innerHTML = "";
  if (projectPath) {
    const name = projectPath.split("/").filter(Boolean).pop() || projectPath;
    const header = document.createElement("div");
    header.className = "livecode-chat-session-menu-section-label";
    header.textContent = name;
    header.title = projectPath;
    list.appendChild(header);
  }
  if (!sessions || !sessions.length) {
    if (fetchFailed) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "livecode-chat-session-menu-empty livecode-chat-session-menu-retry";
      retry.style.cssText = "width:100%;background:transparent;border:none;color:inherit;cursor:pointer;text-align:left;";
      retry.textContent = "Failed to load sessions — click to retry";
      retry.onclick = function() { renderLiveCodeSessionDropdown(); };
      list.appendChild(retry);
    } else {
      const empty = document.createElement("div");
      empty.className = "livecode-chat-session-menu-empty";
      empty.textContent = "No saved sessions";
      list.appendChild(empty);
    }
    return;
  }
  const groups = _livecodeGroupSessionsByDate(sessions);
  const sections = [
    { key: "today", label: "Today" },
    { key: "yesterday", label: "Yesterday" },
    { key: "older", label: "Older" },
  ];
  sections.forEach(function(sec) {
    const items = groups[sec.key] || [];
    if (!items.length) return;
    const label = document.createElement("div");
    label.className = "livecode-chat-session-menu-section-label";
    label.textContent = sec.label;
    list.appendChild(label);
    items.forEach(function(s) {
      list.appendChild(_livecodeCreateSessionMenuRow(s));
    });
  });
}

function renderLiveCodeSessionDropdown() {
  const list = document.getElementById("livecode-chat-session-menu-list");
  if (!list) return;
  const projectPath = livecodeProjectPath;
  if (!projectPath) {
    list.innerHTML = '<div class="livecode-chat-session-menu-empty">Open a project to view sessions</div>';
    return;
  }
  const fetchToken = ++_livecodeSessionFetchToken;
  list.innerHTML = '<div class="livecode-chat-session-menu-empty">Loading sessions…</div>';
  _livecodeFetchSessions(projectPath).then(function(sessions) {
    if (fetchToken !== _livecodeSessionFetchToken) return;
    if (_livecodeNormalizeProjectKey(projectPath) !== _livecodeNormalizeProjectKey(livecodeProjectPath)) return;
    const fetchFailed = !!sessions._livecodeFetchFailed;
    const merged = _livecodeMergeSessionSources(sessions, projectPath);
    _livecodeSetCachedSessions(projectPath, merged);
    _livecodePopulateSessionMenuList(list, merged, fetchFailed, projectPath);
    if (_livecodeSessionMenuOpen) _livecodePositionSessionMenu();
  });
}

function _livecodeMergeSessionSources(serverSessions, projectPath) {
  const byId = {};
  (serverSessions || []).forEach(function(s) {
    if (s && s.session_id) byId[s.session_id] = s;
  });
  const pendingTitles = _livecodeGetPendingSessionTitles(projectPath);
  Object.keys(pendingTitles).forEach(function(sid) {
    const pendingTitle = pendingTitles[sid];
    if (!byId[sid]) {
      byId[sid] = { session_id: sid, title: pendingTitle, updated_at: Date.now() / 1000 };
    } else if (!byId[sid].title && pendingTitle) {
      byId[sid].title = pendingTitle;
    }
  });
  livecodeChatTabs.forEach(function(tab) {
    if (!tab.sessionId || !tab.title || tab.title === "New chat" || tab.title === "Loading…") return;
    if (!byId[tab.sessionId]) {
      byId[tab.sessionId] = {
        session_id: tab.sessionId,
        title: tab.title,
        updated_at: Date.now() / 1000,
      };
    }
  });
  return Object.keys(byId).map(function(sid) { return byId[sid]; })
    .sort(function(a, b) { return Number(b.updated_at || 0) - Number(a.updated_at || 0); });
}

window.getLiveCodeKnownSessions = function() {
  const projectPath = livecodeProjectPath;
  if (!projectPath) return Promise.resolve([]);
  return _livecodeFetchSessions(projectPath).then(function(sessions) {
    const base = sessions && sessions._livecodeFetchFailed
      ? _livecodeGetCachedSessions(projectPath)
      : sessions;
    const merged = _livecodeMergeSessionSources(base, projectPath);
    _livecodeSetCachedSessions(projectPath, merged);
    return merged;
  }).catch(function() {
    return _livecodeMergeSessionSources(_livecodeGetCachedSessions(projectPath), projectPath);
  });
};

window.renameLiveCodeSession = function(sessionId, currentTitle, event) {
  if (event) event.stopPropagation();
  if (!sessionId || !livecodeProjectPath || typeof window.showRenameModal !== "function") return false;
  window.showRenameModal(currentTitle || "Chat", function(newTitle) {
    const title = (newTitle || "").trim();
    if (!title) return;
    fetch("/livecode/session/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_path: livecodeProjectPath,
        session_id: sessionId,
        title: title,
        workspace: _livecodeCurrentWorkspacePayload(),
      }),
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (!data || !data.success) return;
      const tab = livecodeChatTabs.find(function(t) { return t.sessionId === sessionId; });
      if (tab) {
        tab.title = title;
        _livecodeRenderChatTabs();
      }
      renderLiveCodeSessionDropdown();
    }).catch(function(err) {
      console.error("LiveCode rename session failed:", err);
    });
  });
  return false;
};

function _livecodeResetSessionIfDeleted(sessionId) {
  _livecodePurgeDeletedSession(sessionId);
}

window.deleteLiveCodeSession = function(sessionId, event) {
  if (event) event.stopPropagation();
  if (!sessionId || !livecodeProjectPath) return false;
  const runningTab = livecodeChatTabs.find(function(t) {
    return t.sessionId === sessionId && t.agentRunning;
  });
  if (runningTab) {
    if (typeof _livecodeShowIdeToast === "function") {
      _livecodeShowIdeToast("Can't delete a chat while its agent is running.");
    }
    return false;
  }
  const projectPath = livecodeProjectPath;
  _livecodePurgeDeletedSession(sessionId);
  _livecodeSaveTabsForProject(projectPath);
  renderLiveCodeSessionDropdown();
  fetch("/livecode/session/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_path: projectPath,
      session_id: sessionId,
      workspace: _livecodeCurrentWorkspacePayload(),
    }),
  }).then(function(r) {
    return r.json().catch(function() { return null; });
  }).then(function(data) {
    if (!data || !data.success) {
      console.error("LiveCode delete session failed:", (data && data.error) || "unknown error");
    }
  }).catch(function(err) {
    console.error("LiveCode delete session failed:", err);
  }).then(function() {
    if (_livecodeNormalizeProjectKey(projectPath) === _livecodeNormalizeProjectKey(livecodeProjectPath)) {
      renderLiveCodeSessionDropdown();
    }
  });
  return false;
};

window.closeLiveCodeSessionMenu = function() {
  const menu = document.getElementById("livecode-chat-session-menu");
  const btn = document.getElementById("livecode-chat-session-history");
  if (menu) menu.style.display = "none";
  if (btn) btn.setAttribute("aria-expanded", "false");
  _livecodeSessionMenuOpen = false;
  _livecodeUnbindSessionMenuReposition();
  _livecodeCloseSessionItemMenus();
};

window.toggleLiveCodeSessionMenu = function() {
  const menu = document.getElementById("livecode-chat-session-menu");
  const btn = document.getElementById("livecode-chat-session-history");
  if (!menu || !btn) return;
  if (_livecodeSessionMenuOpen) {
    closeLiveCodeSessionMenu();
    return;
  }
  if (typeof closeLiveCodeProjectMenu === "function") closeLiveCodeProjectMenu();
  _livecodeEnsureSessionMenuPortal();
  _livecodeSessionMenuOpen = true;
  _livecodePositionSessionMenu();
  menu.style.display = "flex";
  btn.setAttribute("aria-expanded", "true");
  renderLiveCodeSessionDropdown();
  _livecodeBindSessionMenuReposition();
};

var _livecodeProjectMenuOpen = false;
var _livecodeProjectMenuPositionBound = false;

function _livecodeEnsureProjectMenuPortal() {
  const menu = document.getElementById("livecode-project-menu");
  if (menu && menu.parentElement !== document.body) {
    document.body.appendChild(menu);
  }
}

function _livecodePositionProjectMenu() {
  const menu = document.getElementById("livecode-project-menu");
  const btn = document.getElementById("ide-activity-recent");
  if (!menu || !btn) return;
  const rect = btn.getBoundingClientRect();
  const gap = 4;
  const viewportPad = 8;
  menu.style.top = Math.max(viewportPad, rect.bottom + gap) + "px";
  const menuWidth = menu.offsetWidth || 320;
  const left = Math.min(
    Math.max(viewportPad, rect.left),
    window.innerWidth - menuWidth - viewportPad
  );
  menu.style.left = Math.max(viewportPad, left) + "px";
  menu.style.right = "auto";
  const maxH = Math.min(420, window.innerHeight - rect.bottom - gap - viewportPad);
  menu.style.maxHeight = Math.max(160, maxH) + "px";
}

function _livecodeOnProjectMenuReposition() {
  if (!_livecodeProjectMenuOpen) return;
  _livecodePositionProjectMenu();
}

function _livecodeBindProjectMenuReposition() {
  if (_livecodeProjectMenuPositionBound) return;
  _livecodeProjectMenuPositionBound = true;
  window.addEventListener("resize", _livecodeOnProjectMenuReposition, true);
  document.addEventListener("scroll", _livecodeOnProjectMenuReposition, true);
}

function _livecodeUnbindProjectMenuReposition() {
  if (!_livecodeProjectMenuPositionBound) return;
  _livecodeProjectMenuPositionBound = false;
  window.removeEventListener("resize", _livecodeOnProjectMenuReposition, true);
  document.removeEventListener("scroll", _livecodeOnProjectMenuReposition, true);
}

function renderLiveCodeProjectDropdown() {
  const list = document.getElementById("livecode-project-menu-list");
  if (!list) return;
  _livecodeCloseSessionItemMenus();
  list.innerHTML = "";
  const projects = getLiveCodeRecentProjects();
  if (!projects.length) {
    const empty = document.createElement("div");
    empty.className = "livecode-chat-session-menu-empty";
    empty.textContent = "No recent projects";
    list.appendChild(empty);
  } else {
    const label = document.createElement("div");
    label.className = "livecode-chat-session-menu-section-label";
    label.textContent = "Recent";
    list.appendChild(label);
    projects.forEach(function(path) {
      list.appendChild(_livecodeCreateProjectMenuRow(path));
    });
  }
  const sep = document.createElement("div");
  sep.className = "livecode-project-menu-sep";
  list.appendChild(sep);
  const openRow = document.createElement("div");
  openRow.className = "livecode-chat-session-menu-item-row theme-transition";
  const openBtn = document.createElement("button");
  openBtn.type = "button";
  openBtn.className = "livecode-chat-session-menu-item theme-transition";
  openBtn.onclick = function() {
    window.openLiveCodeFolderFromMenu();
  };
  const openTitle = document.createElement("span");
  openTitle.className = "livecode-chat-session-menu-item-title";
  openTitle.textContent = "Open Folder…";
  openBtn.appendChild(openTitle);
  openRow.appendChild(openBtn);
  list.appendChild(openRow);
  [
    ["Add Folder to Workspace…", window.addLiveCodeFolderToWorkspaceFromMenu],
    ["Manage Workspace…", window.openLiveCodeWorkspaceManager],
    ["Open Workspace File…", window.openLiveCodeWorkspaceFileFromMenu],
    ["Save Workspace As…", window.saveLiveCodeWorkspaceAsFromMenu]
  ].forEach(function(action) {
    const row = document.createElement("div");
    row.className = "livecode-chat-session-menu-item-row theme-transition";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "livecode-chat-session-menu-item theme-transition";
    btn.onclick = function() {
      if (typeof action[1] === "function") action[1]();
    };
    const title = document.createElement("span");
    title.className = "livecode-chat-session-menu-item-title";
    title.textContent = action[0];
    btn.appendChild(title);
    row.appendChild(btn);
    list.appendChild(row);
  });
}

function _livecodeBuildProjectMoreMenu(path) {
  const menu = document.createElement("div");
  menu.className = "chat-history-menu livecode-chat-session-item-menu";
  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "chat-history-menu-item danger";
  removeBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9,3H7c0-1.7,1.3-3,3-3v2C9.4,2,9,2.4,9,3z"/><path d="M17,3h-2c0-0.6-0.4-1-1-1V0C15.7,0,17,1.3,17,3z"/><polygon points="17,6 7,6 7,3 9,3 9,4 15,4 15,3 17,3"/><rect x="10" width="4" height="2"/><path d="M21,6H3C2.4,6,2,5.6,2,5s0.4-1,1-1h18c0.6,0,1,0.4,1,1S21.6,6,21,6z"/><path d="M19,24H5c-0.6,0-1-0.4-1-1V9c0-0.6,0.4-1,1-1h14c0.6,0,1,0.4,1,1v14C20,23.6,19.6,24,19,24z M6,22h12V10H6V22z"/><path d="M10,20c-0.6,0-1-0.4-1-1v-6c0-0.6,0.4-1,1-1s1,0.4,1,1v6C11,19.6,10.6,20,10,20z"/><path d="M14,20c-0.6,0-1-0.4-1-1v-6c0-0.6,0.4-1,1-1s1,0.4,1,1v6C15,19.6,14.6,20,14,20z"/></svg>Remove from recents';
  removeBtn.onclick = function(ev) {
    ev.stopPropagation();
    _livecodeCloseSessionItemMenus();
    removeLiveCodeRecentProject(path);
  };
  menu.appendChild(removeBtn);
  return menu;
}

function _livecodeCreateProjectMenuRow(path) {
  const name = path.split("/").filter(Boolean).pop() || path;
  const isActive = path === livecodeProjectPath;

  const row = document.createElement("div");
  row.className = "livecode-chat-session-menu-item-row theme-transition";
  row.dataset.projectPath = path;

  const itemBtn = document.createElement("button");
  itemBtn.type = "button";
  itemBtn.className = "livecode-chat-session-menu-item theme-transition" + (isActive ? " is-active" : "");
  const titleSpan = document.createElement("span");
  titleSpan.className = "livecode-chat-session-menu-item-title";
  titleSpan.title = path;
  titleSpan.textContent = name;
  itemBtn.appendChild(titleSpan);
  itemBtn.onclick = function(e) {
    if (e.target.closest(".chat-history-menu") || e.target.closest(".livecode-chat-session-menu-item-more")) {
      return;
    }
    _livecodeCloseSessionItemMenus();
    window.selectLiveCodeProjectFromMenu(path);
  };

  const moreBtn = document.createElement("button");
  moreBtn.type = "button";
  moreBtn.className = "livecode-chat-session-menu-item-more chat-history-item-more";
  moreBtn.title = "More";
  moreBtn.setAttribute("aria-label", "More options");
  moreBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="19" cy="12" r="1"></circle></svg>';
  moreBtn.onclick = function(e) {
    e.stopPropagation();
    const existing = document.querySelector(".livecode-chat-session-item-menu.is-portaled");
    _livecodeCloseSessionItemMenus();
    if (existing) return;
    const menu = _livecodeBuildProjectMoreMenu(path);
    menu.classList.add("is-portaled");
    document.body.appendChild(menu);
    _livecodePositionSessionItemMenu(menu, moreBtn);
    _livecodeBindSessionItemMenuReposition(menu, moreBtn);
  };

  row.appendChild(itemBtn);
  row.appendChild(moreBtn);
  return row;
}

window.selectLiveCodeProjectFromMenu = function(path) {
  closeLiveCodeProjectMenu();
  if (!path) return;
  _livecodeOpenProjectOrWorkspace(path);
  showIDEPanel("explorer");
};

window.openLiveCodeFolderFromMenu = function() {
  closeLiveCodeProjectMenu();
  openLiveCodeProjectBrowser();
};

function _livecodeFolderNameFromPath(path) {
  const normalized = String(path || "").replace(/\\/g, "/").replace(/\/$/, "");
  const parts = normalized.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "workspace");
}

function _livecodeWorkspaceRefForMcp() {
  return livecodeWorkspacePath || livecodeProjectPath;
}

function _livecodeStableStringHash(value) {
  let hash = 2166136261;
  const text = String(value || "");
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash.toString(36);
}

function _livecodeMcpWorkspaceIdentity() {
  const identityKey = _livecodeWorkspaceIdentityKey();
  return identityKey ? "browser:" + identityKey : "";
}

function _livecodeMcpSelectionStorageKey() {
  const key = _livecodeMcpWorkspaceIdentity();
  return key ? LIVECODE_MCP_SELECTED_STORAGE_PREFIX + key : "";
}

function _livecodeLoadMcpSelection() {
  const next = new Set();
  const key = _livecodeMcpSelectionStorageKey();
  if (key) {
    try {
      const parsed = JSON.parse(_livecodeStorageGet(key) || "[]");
      if (Array.isArray(parsed)) {
        parsed.forEach(function(name) {
          const value = String(name || "").trim();
          if (value) next.add(value);
        });
      }
    } catch (e) {}
  }
  livecodeMcpSelectedServers = next;
  _livecodeUpdateMcpToggle();
}

function _livecodePersistMcpSelection() {
  const key = _livecodeMcpSelectionStorageKey();
  if (!key) return;
  _livecodeStorageSet(key, JSON.stringify(Array.from(livecodeMcpSelectedServers).sort()));
}

function _livecodeIsWorkspaceFilePath(path) {
  const value = String(path || "").trim().toLowerCase();
  return value.endsWith(".livecode-workspace.json") || value.endsWith(".livecode-workspace") || value.endsWith(".code-workspace");
}

function _livecodeIsLivecodeWorkspaceFilePath(path) {
  const value = String(path || "").trim().toLowerCase();
  return value.endsWith(".livecode-workspace.json") || value.endsWith(".livecode-workspace");
}

var _livecodeWorkspaceAutoSaveTimer = null;
function _livecodeScheduleWorkspaceFileAutoSave() {
  if (!livecodeWorkspacePath || !_livecodeIsLivecodeWorkspaceFilePath(livecodeWorkspacePath)) return;
  if (_livecodeWorkspaceAutoSaveTimer) clearTimeout(_livecodeWorkspaceAutoSaveTimer);
  const targetPath = livecodeWorkspacePath;
  _livecodeWorkspaceAutoSaveTimer = setTimeout(function() {
    _livecodeWorkspaceAutoSaveTimer = null;
    _livecodeSaveWorkspaceFile(targetPath, { silent: true }).catch(function() {});
  }, 600);
}

function _livecodeOpenProjectOrWorkspace(path, options) {
  if (!path) return;
  const opts = options || {};
  if (_livecodeIsWorkspaceFilePath(path)) {
    _livecodeLoadWorkspaceFile(path, opts).catch(function(err) {
      _livecodeShowIdeToast("Couldn't open workspace: " + (err && err.message ? err.message : String(err)));
      renderLiveCodeExplorerEmpty();
      updateLiveCodeExplorerHeader();
      updateOpenFilesList();
    });
    return;
  }
  setLiveCodeProject(path);
}

function _livecodeNormalizeWorkspacePath(path) {
  let value = String(path || "").trim().replace(/\\+/g, "/");
  const unc = value.startsWith("//");
  value = value.replace(/\/+/g, "/");
  if (unc && !value.startsWith("//")) value = "/" + value;
  if (value.length > 1 && value !== "//") value = value.replace(/\/+$/, "");
  return value;
}

function _livecodeWorkspaceNameKey(name) {
  return String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function _livecodeUniqueWorkspaceName(baseName, usedNames) {
  const base = String(baseName || "workspace").trim() || "workspace";
  let name = base;
  let index = 2;
  while (usedNames.has(_livecodeWorkspaceNameKey(name))) {
    name = base + "-" + index;
    index += 1;
  }
  usedNames.add(_livecodeWorkspaceNameKey(name));
  return name;
}

function _livecodeNormalizeWorkspaceFolders(folders) {
  const usedPaths = new Set();
  const usedNames = new Set();
  const out = [];
  (Array.isArray(folders) ? folders : []).forEach(function(folder) {
    if (!folder) return;
    const path = _livecodeNormalizeWorkspacePath(folder.path || folder);
    if (!path || _livecodeIsWorkspaceFilePath(path)) return;
    const pathKey = _livecodeNormalizeProjectKey(path);
    if (usedPaths.has(pathKey)) return;
    usedPaths.add(pathKey);
    const rawName = String(folder.name || "").trim();
    const safeName = rawName && rawName !== "." && rawName !== ".." && !/[\\/]/.test(rawName)
      ? rawName
      : _livecodeFolderNameFromPath(path);
    out.push({ name: _livecodeUniqueWorkspaceName(safeName, usedNames), path: path });
  });
  return out;
}

function _livecodeCurrentWorkspacePayload() {
  const folders = _livecodeNormalizeWorkspaceFolders(livecodeWorkspaceFolders.length
    ? livecodeWorkspaceFolders
    : (livecodeProjectPath ? [{ name: _livecodeFolderNameFromPath(livecodeProjectPath), path: livecodeProjectPath }] : []));
  return {
    path: livecodeWorkspacePath || "",
    folders: folders,
    settings: livecodeWorkspaceSettings || {},
    mcpServers: livecodeWorkspaceMcpServers || {}
  };
}
window._livecodeCurrentWorkspacePayload = _livecodeCurrentWorkspacePayload;
function _livecodeWorkspaceStateKey() {
  const payload = _livecodeCurrentWorkspacePayload();
  const primary = (payload.folders || [])[0];
  return primary ? _livecodeNormalizeProjectKey(primary.path) : "";
}
window._livecodeWorkspaceStateKey = _livecodeWorkspaceStateKey;
function _livecodeWorkspaceIdentityKey() {
  const payload = _livecodeCurrentWorkspacePayload();
  const paths = (payload.folders || []).map(function(folder) {
    return _livecodeNormalizeProjectKey(folder.path);
  }).filter(Boolean).sort();
  if (!paths.length) return "";
  if (paths.length === 1) return paths[0];
  return "workspace:" + _livecodeStableStringHash(JSON.stringify(paths));
}
window._livecodeWorkspaceIdentityKey = _livecodeWorkspaceIdentityKey;

function _livecodePersistBrowserWorkspace() {
  const payload = _livecodeCurrentWorkspacePayload();
  if (!payload.folders.length) {
    try { localStorage.removeItem(LIVECODE_BROWSER_WORKSPACE_KEY); } catch (e) {}
    return;
  }
  try { localStorage.setItem(LIVECODE_BROWSER_WORKSPACE_KEY, JSON.stringify(payload)); } catch (e) {}
}

function _livecodeLoadBrowserWorkspace() {
  let raw = "";
  try { raw = localStorage.getItem(LIVECODE_BROWSER_WORKSPACE_KEY) || ""; } catch (e) {}
  if (!raw) return null;
  try {
    const payload = JSON.parse(raw);
    if (payload && Array.isArray(payload.folders) && payload.folders.length) {
      payload.folders = _livecodeNormalizeWorkspaceFolders(payload.folders);
      return payload.folders.length ? payload : null;
    }
  } catch (e) {}
  return null;
}

function _livecodeApplyWorkspaceFolderOrder(folders, message) {
  livecodeWorkspaceFolders = folders.map(function(folder) {
    return { name: folder.name || _livecodeFolderNameFromPath(folder.path), path: folder.path };
  }).filter(function(folder) { return !!folder.path; });
  _livecodePersistBrowserWorkspace();
  const primary = livecodeWorkspaceFolders.length ? livecodeWorkspaceFolders[0].path : "";
  const primaryChanged = _livecodeNormalizeProjectKey(primary) !== _livecodeNormalizeProjectKey(livecodeProjectPath);
  if (primary && primaryChanged) {
    livecodeApplyingWorkspace = true;
    try {
      setLiveCodeProject(primary);
    } finally {
      livecodeApplyingWorkspace = false;
    }
  } else {
    ideHomePath = primary || ideHomePath;
    ideFileTreeData = {};
    ideExpandedFolders.clear();
    _livecodeLoadWorkspaceFolderTrees();
    _livecodeMcpStatusLoadedWithoutProbe = false;
    refreshLiveCodeMcpStatus({ probeServers: [], hydrateSelected: true });
    renderLiveCodeRecentSessions();
    if (primary) _livecodeWarmWorkspaceIndex(primary);
  }
  _livecodeScheduleWorkspaceFileAutoSave();
  if (_livecodeProjectMenuOpen) renderLiveCodeProjectDropdown();
  _livecodeRenderWorkspaceManager();
  _livecodeSyncActiveProjectTab();
  if (message) _livecodeShowIdeToast(message);
}

function _livecodeSetWorkspaceFromPayload(workspace) {
  if (!workspace || !Array.isArray(workspace.folders) || !workspace.folders.length) return false;
  livecodeWorkspacePath = workspace.path || null;
  livecodeWorkspaceFolders = _livecodeNormalizeWorkspaceFolders(workspace.folders);
  livecodeWorkspaceSettings = workspace.settings || {};
  livecodeWorkspaceMcpServers = workspace.mcpServers || {};
  if (!livecodeWorkspaceFolders.length) return false;
  _livecodePersistBrowserWorkspace();
  livecodeApplyingWorkspace = true;
  try {
    setLiveCodeProject(livecodeWorkspaceFolders[0].path);
  } finally {
    livecodeApplyingWorkspace = false;
  }
  return true;
}

async function _livecodeLoadWorkspaceFile(path, options) {
  const opts = options || {};
  const resp = await fetch("/livecode/workspace/load", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_path: path, allow_partial: !!opts.allowPartial })
  });
  const payload = await resp.json();
  if (resp.status === 409 && payload.code === "workspace_folders_missing" && !opts.allowPartial) {
    const missing = Array.isArray(payload.missing) ? payload.missing : [];
    const names = missing.map(function(f) { return f.name || f.path; }).join(", ");
    const remaining = typeof payload.remaining === "number" ? payload.remaining : 0;
    if (!remaining) throw new Error(payload.error || "Workspace has no valid folders");
    const ok = await _livecodeModalConfirm({
      title: "Folders missing",
      message: `${missing.length} folder(s) in this workspace could not be found (${names}). Open the remaining ${remaining} folder(s)?`,
      confirmText: "Open remaining folders",
    });
    if (!ok) throw new Error("Workspace open cancelled");
    return _livecodeLoadWorkspaceFile(path, Object.assign({}, opts, { allowPartial: true }));
  }
  if (!resp.ok || !payload.workspace) throw new Error(payload.error || "Unable to open workspace file");
  if (!_livecodeSetWorkspaceFromPayload(payload.workspace)) throw new Error("Workspace has no folders");
  saveLiveCodeRecentProject(payload.workspace.path || path);
  renderLiveCodeRecentProjects();
  if (_livecodeProjectMenuOpen) renderLiveCodeProjectDropdown();
  if (!opts.silent) _livecodeShowIdeToast("Workspace opened");
}

async function _livecodeSaveWorkspaceFile(path, options) {
  const opts = options || {};
  const folders = _livecodeNormalizeWorkspaceFolders(livecodeWorkspaceFolders.length
    ? livecodeWorkspaceFolders
    : (livecodeProjectPath ? [{ name: _livecodeFolderNameFromPath(livecodeProjectPath), path: livecodeProjectPath }] : []));
  if (!folders.length) {
    if (!opts.silent) _livecodeShowIdeToast("Open a folder before saving a workspace.");
    return;
  }
  const resp = await fetch("/livecode/workspace/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_path: path,
      folders: folders,
      settings: livecodeWorkspaceSettings || {},
      mcpServers: livecodeWorkspaceMcpServers || {}
    })
  });
  const payload = await resp.json();
  if (!resp.ok || !payload.workspace) throw new Error(payload.error || "Unable to save workspace");
  if (!opts.silent) {
    _livecodeSetWorkspaceFromPayload(payload.workspace);
  } else {
    livecodeWorkspacePath = payload.workspace.path || livecodeWorkspacePath;
  }
  saveLiveCodeRecentProject(payload.workspace.path || path);
  renderLiveCodeRecentProjects();
  if (_livecodeProjectMenuOpen) renderLiveCodeProjectDropdown();
  if (!opts.silent) _livecodeShowIdeToast("Workspace saved");
}

window.addLiveCodeFolderToWorkspaceFromMenu = function() {
  closeLiveCodeProjectMenu();
  openLiveCodeFolderBrowser(function(path) {
    const normalizedPath = _livecodeNormalizeWorkspacePath(path);
    const existingKeys = new Set(livecodeWorkspaceFolders.map(function(folder) { return _livecodeNormalizeProjectKey(folder.path); }));
    const exists = existingKeys.has(_livecodeNormalizeProjectKey(normalizedPath));
    if (exists) {
      _livecodeShowIdeToast("Folder already in workspace");
      return;
    }
    const next = livecodeWorkspaceFolders.concat([{ name: _livecodeFolderNameFromPath(normalizedPath), path: normalizedPath }]);
    _livecodeApplyWorkspaceFolderOrder(next, "Folder added to workspace");
  }, livecodeProjectPath || "~");
};

function _livecodeEnsureWorkspaceManager() {
  let dialog = document.getElementById("livecode-workspace-manager");
  if (dialog) return dialog;
  dialog = document.createElement("div");
  dialog.id = "livecode-workspace-manager";
  dialog.className = "livecode-workspace-manager theme-transition";
  dialog.style.display = "none";
  dialog.innerHTML = '<div class="livecode-workspace-manager-modal theme-transition" role="dialog" aria-modal="true" aria-labelledby="livecode-workspace-manager-title"><div class="livecode-workspace-manager-header"><div><div id="livecode-workspace-manager-title" class="livecode-workspace-manager-title">Workspace folders</div><div id="livecode-workspace-manager-subtitle" class="livecode-workspace-manager-subtitle"></div></div><button type="button" class="livecode-workspace-manager-close theme-transition" onclick="closeLiveCodeWorkspaceManager(); return false;" aria-label="Close workspace manager">Close</button></div><div id="livecode-workspace-manager-list" class="livecode-workspace-manager-list"></div><div class="livecode-workspace-manager-footer"><button type="button" class="livecode-workspace-manager-action theme-transition" onclick="addLiveCodeFolderToWorkspaceFromManager(); return false;">Add Folder</button><button type="button" class="livecode-workspace-manager-action theme-transition" onclick="saveLiveCodeWorkspaceAsFromManager(); return false;">Save Workspace As</button></div></div>';
  document.body.appendChild(dialog);
  dialog.addEventListener("click", function(e) {
    if (e.target === dialog) closeLiveCodeWorkspaceManager();
    e.stopPropagation();
  });
  return dialog;
}

function _livecodeWorkspaceFoldersForManager() {
  if (livecodeWorkspaceFolders.length) return livecodeWorkspaceFolders.slice();
  return livecodeProjectPath ? [{ name: _livecodeFolderNameFromPath(livecodeProjectPath), path: livecodeProjectPath }] : [];
}

function _livecodeWorkspaceFolderIndexByPath(path) {
  const key = _livecodeNormalizeProjectKey(path);
  return _livecodeWorkspaceFoldersForManager().findIndex(function(folder) {
    return _livecodeNormalizeProjectKey(folder.path) === key;
  });
}

function _livecodeMakeWorkspaceFolderPrimary(path) {
  const next = _livecodeWorkspaceFoldersForManager();
  const index = _livecodeWorkspaceFolderIndexByPath(path);
  if (index <= 0) return;
  const item = next.splice(index, 1)[0];
  if (item) next.unshift(item);
  _livecodeApplyWorkspaceFolderOrder(next, "Primary workspace folder changed");
}

function _livecodeWorkspaceFolderOrderChanged(current, next) {
  if (current.length !== next.length) return true;
  return current.some(function(folder, index) {
    return _livecodeNormalizeProjectKey(folder.path) !== _livecodeNormalizeProjectKey(next[index] && next[index].path);
  });
}

function _livecodeWorkspacePrimaryChanged(current, next) {
  const currentPrimary = current.length ? current[0].path : "";
  const nextPrimary = next.length ? next[0].path : "";
  return _livecodeNormalizeProjectKey(currentPrimary) !== _livecodeNormalizeProjectKey(nextPrimary);
}

function _livecodeWorkspaceFolderLabel(folder) {
  return folder ? (folder.name || _livecodeFolderNameFromPath(folder.path)) : "";
}

function _livecodeMoveWorkspaceFolderToIndex(path, targetIndex) {
  const current = _livecodeWorkspaceFoldersForManager();
  const index = _livecodeWorkspaceFolderIndexByPath(path);
  if (index < 0 || targetIndex < 0 || targetIndex > current.length) return Promise.resolve(false);
  let insertionIndex = targetIndex;
  if (index < insertionIndex) insertionIndex -= 1;
  const next = current.slice();
  const item = next.splice(index, 1)[0];
  insertionIndex = Math.max(0, Math.min(next.length, insertionIndex));
  next.splice(insertionIndex, 0, item);
  if (!_livecodeWorkspaceFolderOrderChanged(current, next)) return Promise.resolve(false);
  const primaryChanged = _livecodeWorkspacePrimaryChanged(current, next);
  const title = primaryChanged ? "Change primary workspace folder?" : "Reorder workspace folders?";
  const nextPrimary = _livecodeWorkspaceFolderLabel(next[0]);
  const message = primaryChanged
    ? nextPrimary + " will become the primary folder for MCP priority and workspace-relative actions."
    : "Apply this workspace folder order?";
  return _livecodeModalConfirm({ title: title, message: message, confirmText: "Apply order" }).then(function(ok) {
    if (!ok) return false;
    _livecodeApplyWorkspaceFolderOrder(next, primaryChanged ? "Primary workspace folder changed" : "Workspace folders reordered");
    return true;
  });
}

function _livecodeRemoveWorkspaceFolder(path) {
  const next = _livecodeWorkspaceFoldersForManager();
  const index = _livecodeWorkspaceFolderIndexByPath(path);
  if (index < 0 || next.length <= 1) return;
  next.splice(index, 1);
  _livecodeApplyWorkspaceFolderOrder(next, "Folder removed from workspace");
}

function _livecodeRenderWorkspaceManager() {
  const dialog = document.getElementById("livecode-workspace-manager");
  if (!dialog || dialog.style.display === "none") return;
  const folders = _livecodeWorkspaceFoldersForManager();
  const subtitle = dialog.querySelector("#livecode-workspace-manager-subtitle");
  const list = dialog.querySelector("#livecode-workspace-manager-list");
  if (subtitle) subtitle.textContent = folders.length ? folders.length + " folder" + (folders.length === 1 ? "" : "s") + " in this workspace" : "Open or add folders to build a workspace";
  if (!list) return;
  if (!folders.length) {
    list.innerHTML = '<div class="livecode-workspace-manager-empty">No workspace folders yet.</div>';
    return;
  }
  list.innerHTML = folders.map(function(folder, index) {
    const name = _livecodeEscapeHtml(folder.name || _livecodeFolderNameFromPath(folder.path));
    const path = _livecodeEscapeHtml(folder.path || "");
    const primary = index === 0 ? '<span class="livecode-workspace-primary-badge">Primary</span>' : '';
    return '<div class="livecode-workspace-folder-row theme-transition" draggable="true" data-livecode-workspace-row="' + index + '"><div class="livecode-workspace-folder-drag" aria-hidden="true"></div><div class="livecode-workspace-folder-main"><div class="livecode-workspace-folder-name">' + name + '</div><div class="livecode-workspace-folder-path">' + path + '</div></div><div class="livecode-workspace-folder-actions">' + primary + '<button type="button" class="livecode-workspace-folder-btn is-danger theme-transition" data-livecode-workspace-remove="' + index + '" ' + (folders.length === 1 ? 'disabled' : '') + '>Remove</button></div></div>';
  }).join("");
  _livecodeBindWorkspaceManagerDrag(list);
  list.querySelectorAll("[data-livecode-workspace-remove]").forEach(function(button) {
    button.addEventListener("click", function() {
      const index = Number(button.getAttribute("data-livecode-workspace-remove"));
      const target = _livecodeWorkspaceFoldersForManager()[index];
      if (target) _livecodeRemoveWorkspaceFolder(target.path);
    });
  });
}

window.openLiveCodeWorkspaceManager = function() {
  closeLiveCodeProjectMenu();
  const dialog = _livecodeEnsureWorkspaceManager();
  _livecodeRenderWorkspaceManager();
  dialog.style.display = "flex";
  _livecodeRenderWorkspaceManager();
};

window.closeLiveCodeWorkspaceManager = function() {
  const dialog = document.getElementById("livecode-workspace-manager");
  if (dialog) dialog.style.display = "none";
};

window.addLiveCodeFolderToWorkspaceFromManager = function() {
  closeLiveCodeWorkspaceManager();
  window.addLiveCodeFolderToWorkspaceFromMenu();
};

window.saveLiveCodeWorkspaceAsFromManager = function() {
  closeLiveCodeWorkspaceManager();
  window.saveLiveCodeWorkspaceAsFromMenu();
};

window.openLiveCodeWorkspaceFileFromMenu = function() {
  closeLiveCodeProjectMenu();
  openLiveCodeFileBrowser(function(path) {
    _livecodeLoadWorkspaceFile(path).catch(function(err) {
      _livecodeShowIdeToast("Couldn't open workspace: " + (err && err.message ? err.message : String(err)));
    });
  }, livecodeProjectPath || "~", [".livecode-workspace.json", ".livecode-workspace", ".code-workspace"]);
};

window.saveLiveCodeWorkspaceAsFromMenu = function() {
  closeLiveCodeProjectMenu();
  const initial = livecodeWorkspacePath || ((livecodeProjectPath || "~/workspace") + ".livecode-workspace.json");
  _livecodeModalPrompt({
    title: "Save workspace as",
    message: "Enter a path for the LiveCode workspace file.",
    value: initial,
    placeholder: "~/my-project.livecode-workspace.json",
    confirmText: "Save",
    wide: true
  }).then(function(path) {
    if (!path) return;
    _livecodeSaveWorkspaceFile(path).catch(function(err) {
      _livecodeShowIdeToast("Couldn't save workspace: " + (err && err.message ? err.message : String(err)));
    });
  });
};

window.closeLiveCodeProjectMenu = function() {
  const menu = document.getElementById("livecode-project-menu");
  const btn = document.getElementById("ide-activity-recent");
  if (menu) menu.style.display = "none";
  if (btn) {
    btn.setAttribute("aria-expanded", "false");
    btn.classList.remove("active");
    btn.setAttribute("aria-pressed", "false");
  }
  _livecodeProjectMenuOpen = false;
  _livecodeUnbindProjectMenuReposition();
  _livecodeCloseSessionItemMenus();
};

window.toggleLiveCodeProjectMenu = function() {
  const menu = document.getElementById("livecode-project-menu");
  const btn = document.getElementById("ide-activity-recent");
  if (!menu || !btn) return;
  if (_livecodeProjectMenuOpen) {
    closeLiveCodeProjectMenu();
    return;
  }
  if (typeof closeLiveCodeSessionMenu === "function") closeLiveCodeSessionMenu();
  _livecodeEnsureProjectMenuPortal();
  _livecodeProjectMenuOpen = true;
  menu.style.display = "flex";
  _livecodePositionProjectMenu();
  btn.setAttribute("aria-expanded", "true");
  btn.classList.add("active");
  btn.setAttribute("aria-pressed", "true");
  renderLiveCodeProjectDropdown();
  _livecodeBindProjectMenuReposition();
};

function _livecodeRefreshSessionMenuIfOpen() {
  if (!_livecodeSessionMenuOpen) return;
  renderLiveCodeSessionDropdown();
  _livecodePositionSessionMenu();
}

function renderLiveCodeRecentSessions() {
  _livecodeRefreshSessionMenuIfOpen();
}

function _livecodeRenderSessionMessages(messages, out) {
  if (!out) return { firstUser: "" };
  _livecodeResetTurnState();
  let firstUser = "";
  const list = messages || [];
  list.forEach(function(msg, idx) {
    const role = msg.role;
    if (role === "diff") {
      appendLiveCodeDiffBlock({
        file_name: msg.file_name,
        diff_html: msg.diff_html,
        additions: msg.additions || 0,
        deletions: msg.deletions || 0,
        absolute_path: msg.absolute_path || "",
        created: !!msg.created,
      }, out);
      return;
    }
    if (role === "tool_artifact") {
      _livecodeAppendLoadedToolArtifact(msg, out);
      return;
    }
    if (role === "tool") {
      const text = String(msg.content || "");
      if (!text) return;
      try {
        const parsed = JSON.parse(text);
        if (parsed && (parsed.command || parsed.output !== undefined)) {
          _livecodeAppendLoadedCommandBlock(
            parsed.command || "",
            parsed.output || parsed.error || "",
            parsed.exit_code,
            out
          );
          return;
        }
      } catch (_) {}
      return;
    }
    const text = String(msg.content || "");
    const hasDisplay = msg.display && (
      msg.display.text || msg.display.segments || msg.display.images
    );
    if (!text && !hasDisplay && role !== "activity") return;
    if (role === "user") {
      const displayText = (msg.display && msg.display.text) ? String(msg.display.text) : text;
      firstUser = firstUser || displayText || text;
      let urow = msg.display ? _livecodeRenderPlanBuildRow(out, msg.display) : null;
      if (!urow && msg.display && typeof window.renderLivecodeUserMessage === "function") {
        urow = window.renderLivecodeUserMessage(out, msg.display);
      }
      if (!urow) {
        urow = document.createElement("div");
        urow.className = "chat-row livecode-user-row";
        urow.innerHTML = `<div class="chat-msg user"><span class="livecode-user-text">${_livecodeEscapeHtml(text)}</span></div>`;
        out.appendChild(urow);
      }
      _livecodeCurrentUserRow = urow;
      _livecodeScheduleUserMessageCollapseState(out);
    } else if (role === "assistant") {
      if (msg.narration) {
        _livecodeAppendNarration(text, out);
        return;
      }
      const arow = document.createElement("div");
      arow.className = "chat-row livecode-assistant-row";
      const msgEl = document.createElement("div");
      msgEl.className = "chat-msg assistant livecode-stream-msg livecode-plain-msg";
      arow.appendChild(msgEl);
      out.appendChild(arow);
      _livecodeRenderAssistantMarkdown(msgEl, text);
    } else if (role === "activity") {
      if (msg.thought_only) {
        const thought = String(msg.thought_content != null ? msg.thought_content : (msg.content || "")).trim();
        const secs = Number(msg.thinking_s) || 0;
        const ms = Number(msg.thinking_ms) || 0;
        if (thought || secs > 0 || ms > 0) {
          const wrap = _livecodeAppendActivityParts({
            verb: "Thought",
            detail: _livecodeThoughtDurationLabel(secs, ms),
            meta: "",
            thoughtContent: thought,
          }, false, out);
          _livecodeFillThoughtBody(wrap, thought);
        }
      } else if (msg.tool_calls && msg.tool_calls.length) {
        _livecodeAppendLoadedToolActivity(msg, out, list[idx + 1]);
      } else if (text) {
        _livecodeAppendLoadedActivitySummary(text, out);
      }
    }
  });
  _livecodeRegroupTranscript(out);
  _livecodeSyncTurnPointersFromDom();
  return { firstUser: firstUser };
}

async function _livecodeFetchSessionIntoTab(tab, sessionId, options) {
  options = options || {};
  if (!tab || !sessionId || !livecodeProjectPath) return;
  _livecodeClearTabUnread(tab);
  tab.sessionId = sessionId;
  if (_livecodeIsActiveTab(tab)) {
    livecodeAgentSessionId = sessionId;
  }

  if (!options.forceServer && tab.agentRunning && tab.messagesHtml && tab.chatStarted) {
    _livecodeChatStarted = true;
    _livecodeShowChatContainer();
    _livecodeLoadChatTabState(tab);
    _livecodePostProcessRestoredOutput(getLiveCodeChatOutput());
    _livecodeRenderChatTabs();
    toggleLiveCodeAgentPane(true);
    return;
  }

  if (!options.forceServer && tab.agentRunning) {
    const snap = _livecodeLoadChatSnapshot(livecodeProjectPath, sessionId);
    if (_livecodeApplySnapshotToTab(tab, sessionId, snap)) {
      toggleLiveCodeAgentPane(true);
      return;
    }
  }

  if (!options.forceServer && !tab.agentRunning) {
    if (!(tab.messagesHtml && String(tab.messagesHtml).trim())) {
      const saved = _livecodeLoadChatSnapshot(livecodeProjectPath, sessionId);
      if (saved && saved.messagesHtml) {
        tab.messagesHtml = saved.messagesHtml;
        if (saved.title) tab.title = _livecodeTruncateTabTitle(saved.title);
      }
    }
    if (tab.messagesHtml && String(tab.messagesHtml).trim()) {
      tab.chatStarted = true;
      if (_livecodeIsActiveTab(tab) && !options.silent) {
        _livecodeChatStarted = true;
        _livecodeShowChatContainer();
        _livecodeLoadChatTabState(tab);
        toggleLiveCodeAgentPane(true);
      }
      _livecodeRenderChatTabs();
      return;
    }
  }

  const silent = !!options.silent;
  const hadCachedContent = !!(tab.messagesHtml && String(tab.messagesHtml).trim());
  const wasActiveAtStart = _livecodeIsActiveTab(tab);

  if (!silent) {
    tab.title = "Loading…";
    if (wasActiveAtStart) {
      _livecodeLoadChatTabState(tab);
      toggleLiveCodeAgentPane(true);
      _livecodeShowChatContainer();
      const liveOut = getLiveCodeChatOutput();
      _livecodeMarkChatOutputForTab(liveOut, tab);
      if (liveOut) {
        if (hadCachedContent) {
          liveOut.innerHTML = tab.messagesHtml;
          _livecodePostProcessRestoredOutput(liveOut);
        } else {
          liveOut.innerHTML = '<div class="chat-row"><div class="chat-msg assistant livecode-plain-msg" style="opacity:0.7;font-size:13px;">Loading session…</div></div>';
        }
      }
    }
    _livecodeRenderChatTabs();
  }
  try {
    const resp = await fetch("/livecode/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_path: livecodeProjectPath,
        session_id: sessionId,
        workspace: _livecodeCurrentWorkspacePayload(),
      }),
    });
    const data = await resp.json();
    const messages = data && data.success ? (data.messages || []) : [];
    if (!messages.length) {

      if (silent) return;
      if (_livecodeIsActiveTab(tab)) {
        _livecodeResetTabToNewChat(tab);
      } else {
        tab.title = "New chat";
        tab.messagesHtml = "";
        tab.chatStarted = false;
        tab.sessionId = _livecodeNewChatSessionId();
        tab.hasUnread = false;
        _livecodeRenderChatTabs();
      }
      return;
    }
    const isStillActive = _livecodeIsActiveTab(tab);

    const renderOut = (isStillActive && !silent) ? getLiveCodeChatOutput() : document.createElement("div");
    if (!renderOut) return;
    renderOut.innerHTML = "";
    const rendered = _livecodeRenderSessionMessages(messages, renderOut);
    _livecodeStripStaleWelcome(renderOut);
    _livecodeSanitizeLoadedActivityHtml(renderOut);
    _livecodePostProcessRestoredOutput(renderOut);
    const newHtml = renderOut.innerHTML;
    const changed = newHtml !== tab.messagesHtml;
    tab.messagesHtml = newHtml;
    tab.chatStarted = true;
    tab.restoredFromServer = true;
    if (isStillActive) {
      livecodeAgentSessionId = tab.sessionId;
      _livecodeChatStarted = true;
      _livecodeShowChatContainer();
      if (silent && changed) {
        const liveOut = getLiveCodeChatOutput();
        if (liveOut) {
          liveOut.innerHTML = newHtml;
          _livecodePostProcessRestoredOutput(liveOut);
        }
      }
    }
    tab.title = _livecodeTruncateTabTitle(
      (data.summary && data.summary.title) || rendered.firstUser || "Chat"
    );
    _livecodePersistChatSnapshot(livecodeProjectPath, tab);
    if (!silent) _livecodeRenderChatTabs();
    renderLiveCodeRecentSessions();
    if (isStillActive && !silent) renderOut.scrollTop = renderOut.scrollHeight;
  } catch (e) {
    if (silent) {
      console.error("LiveCode: background session refresh failed", e);
      return;
    }
    if (_livecodeIsActiveTab(tab)) {
      _livecodeResetTabToNewChat(tab);
    }
  }
}

window.resumeLiveCodeSession = async function(sessionId) {
  if (!sessionId || !livecodeProjectPath) return;
  closeLiveCodeSessionMenu();
  let tab = livecodeChatTabs.find(function(t) { return t.sessionId === sessionId; });
  if (tab) {
    if (!_livecodeIsActiveTab(tab)) {
      _livecodeSwitchChatTab(tab.id);
    } else {
      _livecodeClearTabUnread(tab);
    }
  } else {
    _livecodeSaveActiveChatTabState();
    let activeTab = _livecodeGetActiveChatTab();
    if (!activeTab) {
      _livecodeInitChatTabs();
      activeTab = _livecodeGetActiveChatTab();
    }
    if (activeTab && activeTab.chatStarted) {
      _livecodeCreateChatTab("Loading…", { activate: true });
      tab = _livecodeGetActiveChatTab();
    } else {
      tab = activeTab;
    }
  }
  if (!tab) return;
  await _livecodeFetchSessionIntoTab(tab, sessionId, {});
};

const LIVECODE_PROJECT_TAB = "__livecode_project__";

window.inferLangFromPath = function(path) {
  const p = (path || "").toLowerCase();
  if (p.endsWith(".py") || p.endsWith(".pyi")) return "python";
  if (p.endsWith(".html") || p.endsWith(".htm")) return "html";
  if (p.endsWith(".tsx")) return "typescriptreact";
  if (p.endsWith(".jsx")) return "javascriptreact";
  if (p.endsWith(".mjs") || p.endsWith(".cjs")) return "javascript";
  if (p.endsWith(".mts") || p.endsWith(".cts")) return "typescript";
  if (p.endsWith(".js")) return "javascript";
  if (p.endsWith(".ts")) return "typescript";
  if (p.endsWith(".go")) return "golang";
  if (p.endsWith(".md")) return "markdown";
  if (p.endsWith(".json")) return "json";
  if (p.endsWith(".xml")) return "xml";
  if (p.endsWith(".css")) return "css";
  if (p.endsWith(".sql")) return "sql";
  if (p.endsWith(".sh") || p.endsWith(".bash")) return "sh";
  if (p.endsWith(".yaml")) return "yaml";
  if (p.endsWith(".yml")) return "yml";
  if (p.endsWith(".toml")) return "toml";
  if (p.endsWith(".ini")) return "ini";
  if (p.endsWith(".properties")) return "ini";
  if (p.endsWith(".dockerfile")) return "text";
  if (p.endsWith(".txt")) return "text";
  return "text";
};

window.mapToMonacoLang = function(lang) {
  const langMap = {
    python: "python",
    html: "html",
    javascript: "javascript",
    typescript: "typescript",
    typescriptreact: "typescript",
    javascriptreact: "javascript",
    json: "json",
    golang: "go",
    markdown: "markdown",
    text: "plaintext",
    xml: "xml",
    css: "css",
    sql: "sql",
    sh: "shell",
    yaml: "yaml",
    yml: "yaml",
    toml: "plaintext",
    ini: "plaintext"
  };
  return langMap[lang] || "plaintext";
};

let _livecodeMonacoThemes = null;
let _livecodeLastMonacoTheme = null;
fetch("/livecode/assets/monaco-themes.json")
  .then(function(r) { return r.ok ? r.json() : null; })
  .then(function(themes) {
    if (!themes) return;
    _livecodeMonacoThemes = themes;
    if (_livecodeLastMonacoTheme && window.monaco) window.applyIDEDynamicTheme(_livecodeLastMonacoTheme);
  })
  .catch(function() {});

const _LIVECODE_THEME_ICONS =
  '<svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"></path></svg>';

const _LIVECODE_THEMES = [
  { id: "dark", label: "Dark", swatch: "#1e293b" },
  { id: "white", label: "Light", swatch: "#f1f5f9" },
  { id: "black", label: "Black", swatch: "#0a0a0a" },
  { id: "pink", label: "Pink", swatch: "#fbcfe8" },
];

function _livecodeCurrentThemeId() {
  const match = _LIVECODE_THEMES.filter(function(t) { return document.body.classList.contains(t.id + "-theme"); })[0];
  return match ? match.id : "dark";
}

// The theme is a body class ("dark-theme", "white-theme", "black-theme", "pink-theme").
window.setLiveCodeTheme = function(theme) {
  const next = _LIVECODE_THEMES.some(function(t) { return t.id === theme; }) ? theme : "dark";
  _LIVECODE_THEMES.forEach(function(t) { document.body.classList.remove(t.id + "-theme"); });
  document.body.classList.add(next + "-theme");
  try { localStorage.setItem("livecode-theme", next); } catch (e) {}
  if (typeof window.applyIDEDynamicTheme === "function") window.applyIDEDynamicTheme(next);
  _livecodeSyncThemeButton();
};

function _livecodeSyncThemeButton() {
  const btn = document.getElementById("livecode-theme-toggle");
  if (!btn) return;
  const current = _livecodeCurrentThemeId();
  const idx = _LIVECODE_THEMES.findIndex(function(t) { return t.id === current; });
  const cur = _LIVECODE_THEMES[idx < 0 ? 0 : idx];
  const next = _LIVECODE_THEMES[(idx + 1) % _LIVECODE_THEMES.length];
  btn.title = "Theme: " + cur.label + " (click for " + next.label + ")";
  btn.setAttribute("aria-label", btn.title);
}

// One button cycles Dark -> Light -> Black -> Pink.
window.toggleLiveCodeTheme = function() {
  const current = _livecodeCurrentThemeId();
  const idx = _LIVECODE_THEMES.findIndex(function(t) { return t.id === current; });
  window.setLiveCodeTheme(_LIVECODE_THEMES[(idx + 1) % _LIVECODE_THEMES.length].id);
};

window.applyIDEDynamicTheme = function(themeName) {
  _livecodeLastMonacoTheme = themeName;
  if (!window.monaco) return;
  const livecodeTheme = _livecodeMonacoThemes && (_livecodeMonacoThemes[themeName] || _livecodeMonacoThemes.black);
  if (livecodeTheme) {
    monaco.editor.defineTheme("livecode-dynamic-theme", livecodeTheme);
    monaco.editor.setTheme("livecode-dynamic-theme");
    return "livecode-dynamic-theme";
  }
  const isPink = themeName === "pink";
  const isDark = themeName === "dark";
  const isBlack = themeName === "black";
  const bg = isPink ? "#fdf2f8" : isBlack ? "#1a1a1a" : isDark ? "#1e293b" : themeName === "white" ? "#f1f5f9" : "#ffffff";
  const fg = isPink ? "#831843" : isBlack ? "#d4d4d4" : isDark ? "#e2e8f0" : "#1e293b";
  const themeId = "livecode-dynamic-theme";
  monaco.editor.defineTheme(themeId, {
    base: isDark || isBlack ? "vs-dark" : "vs",
    inherit: true,
    rules: [],
    colors: {
      "editor.background": bg,
      "editor.foreground": fg,
      "editorGutter.background": bg,
      "editorLineNumber.foreground": isPink ? "#be185d" : isBlack ? "#737373" : isDark ? "#94a3b8" : "#64748b",
      "editorLineNumber.activeForeground": isPink ? "#831843" : isBlack || isDark ? "#ffffff" : "#111827",
      "editor.lineHighlightBackground": bg,
      "editor.selectionBackground": isPink ? "#fbcfe8" : isBlack ? "#1f2937" : isDark ? "#334155" : "#cfe8ff",
      "editorIndentGuide.background": isPink ? "#f9a8d4" : isBlack ? "#1a1a1a" : isDark ? "#334155" : "#e5e7eb",
      "editorIndentGuide.activeBackground": isPink ? "#ec4899" : isBlack ? "#404040" : isDark ? "#64748b" : "#9ca3af",
      "editorSuggestWidget.background": isPink ? "#fdf2f8" : isBlack ? "#1a1a1a" : isDark ? "#1e293b" : "#ffffff",
      "editorSuggestWidget.border": isPink ? "#f9a8d4" : isBlack ? "#333333" : isDark ? "#334155" : "#e2e8f0",
      "editorSuggestWidget.foreground": fg,
      "editorSuggestWidget.selectedBackground": isPink ? "#fbcfe8" : isBlack ? "#2a2a2a" : isDark ? "#334155" : "#eff6ff",
      "editorSuggestWidget.selectedForeground": fg,
      "editorSuggestWidget.highlightForeground": isPink ? "#be185d" : isBlack || isDark ? "#93c5fd" : "#2563eb",
      "editorSuggestWidget.focusHighlightForeground": isPink ? "#be185d" : isBlack || isDark ? "#93c5fd" : "#2563eb",
      "editorHoverWidget.background": isPink ? "#fdf2f8" : isBlack ? "#1a1a1a" : isDark ? "#1e293b" : "#ffffff",
      "editorHoverWidget.border": isPink ? "#f9a8d4" : isBlack ? "#333333" : isDark ? "#334155" : "#e2e8f0",
      "list.activeSelectionBackground": isPink ? "#fbcfe8" : isBlack ? "#2a2a2a" : isDark ? "#334155" : "#eff6ff",
      "list.activeSelectionForeground": fg,
      "list.inactiveSelectionBackground": isPink ? "#fbcfe8" : isBlack ? "#2a2a2a" : isDark ? "#334155" : "#eff6ff",
      "list.inactiveSelectionForeground": fg,
      "list.focusBackground": isPink ? "#fbcfe8" : isBlack ? "#2a2a2a" : isDark ? "#334155" : "#eff6ff",
      "list.focusForeground": fg,
      "list.focusHighlightForeground": isPink ? "#be185d" : isBlack || isDark ? "#93c5fd" : "#2563eb",
      "list.hoverBackground": isPink ? "#fce7f3" : isBlack ? "#242424" : isDark ? "#293548" : "#f8fafc",
      "list.hoverForeground": fg
    }
  });
  monaco.editor.setTheme(themeId);
  return themeId;
};

function initializeIDEEditor() {
  if (window.ideEditor) return;
  const container = document.getElementById("ide-monaco");
  if (!container) return;
  const wasHidden = container.style.display === "none";
  try {
    container.style.display = "block";
  } catch (_) {}
  function createEditor() {
    const currentTheme = localStorage.getItem("livecode-theme") || "dark";
    if (window.monaco && window.applyIDEDynamicTheme) {
      window.applyIDEDynamicTheme(currentTheme);
    }
    console.log("Creating Monaco editor for IDE section...");
    window.ideEditor = monaco.editor.create(container, {
      value: "",
      language: "plaintext",
      theme: "livecode-dynamic-theme",
      automaticLayout: true,
      fixedOverflowWidgets: true,
      minimap: {
        enabled: false
      },
      scrollBeyondLastLine: false,
      wordWrap: "on",
      fontSize: 13,
      fontLigatures: false,
      fontWeight: "400",
      fontFamily: window.LIVECODE_MONACO_FONT || "'LivecodeMono', 'Prima Sans Mono W01 Roman', 'PrimaSansMonoW01-Roman', Consolas, 'Liberation Mono', 'Courier New', ui-monospace, SFMono-Regular, Menlo, Monaco, monospace",
      tabSize: 4,
      insertSpaces: true,
      renderWhitespace: "boundary",
      cursorBlinking: "solid",
      cursorStyle: "line",
      smoothScrolling: true,
      roundedSelection: false,
      renderLineHighlight: "line",
      lineDecorationsWidth: 16,
      lineNumbersMinChars: 3,
      glyphMargin: true,
      folding: true,
      foldingHighlight: true,
      showFoldingControls: "always",
      scrollbar: {
        horizontal: "auto",
        vertical: "auto",
        horizontalScrollbarSize: 10,
        verticalScrollbarSize: 10,
        horizontalSliderSize: 10,
        verticalSliderSize: 10,
        useShadows: false,
        handleMouseWheel: true
      }
    });
    try {
      monaco.editor.remeasureFonts();
    } catch (e) {}
    window.addEventListener("resize", function() {
      if (window.ideEditor) {
        try {
          window.ideEditor.layout();
        } catch (_) {}
      }
    });
    ideEditor = window.ideEditor;
    window.ideEditorReady = true;
    if (typeof window._livecodeApplyEditorSettings === "function") window._livecodeApplyEditorSettings();
    _livecodeBindEditorAutosaveOnce();
    _livecodeBindPlanMonacoAutosave();
    try { if (typeof window.installLivecodeTsIntel === "function") window.installLivecodeTsIntel(); } catch (e) {}
    try {
      if (window.WBLsp && livecodeProjectPath) {
        window.WBLsp.onProjectOpen(livecodeProjectPath, null);
        if (window.WBTsIntel) window.WBTsIntel.onProjectOpen(livecodeProjectPath, null);
      }
    } catch (e) {}
    console.log("Monaco editor created successfully for IDE section");
    if (wasHidden || !ideActiveFile) {
      container.style.display = "none";
      const placeholder = document.getElementById("ide-editor-placeholder");
      if (placeholder) {
        updateLiveCodeEditorPlaceholder();
        placeholder.style.display = "flex";
      }
    }
  }
  if (typeof require !== "undefined") {
    require.config({
      paths: {
        vs: window.LIVECODE_MONACO_VS || "/templates/js/monaco-editor/vs"
      }
    });
    if (window.monaco && monaco.editor) {
      if (typeof window.installLivecodeMonacoDefaults === "function") {
        window.installLivecodeMonacoDefaults();
      }
      createEditor();
    } else {
      require([ "vs/editor/editor.main" ], function() {
        if (typeof window.installLivecodeMonacoDefaults === "function") {
          window.installLivecodeMonacoDefaults();
        }
        createEditor();
      });
    }
  }
}

window.createAndShowIDEEditor = function(showImmediately = true) {
  var ideSection = document.getElementById("ide-editor-section");
  var ideBtn = document.getElementById("ideEditorToggleBtn");
  if (!ideSection) return false;
  if (ideSection.style.display === "none" || !ideSection.style.display) {
    closeAllSectionsExcept("ideEditor");
    ideSection.style.display = "block";
    if (typeof updateDockIndicators === "function") {
      updateDockIndicators("ideEditor");
    }
    if (window.ideIsFullscreen) {
      exitIDEFullscreen();
    }
    initializeIDEEditor();
    const panelContent = document.getElementById("ide-panel-content");
    if (panelContent) {
      panelContent.style.overflowX = "auto";
      panelContent.style.overflowY = "auto";
    }
    setTimeout(function() {
      initLiveCodeProjectState();
      if (!ideTerminalInitialized) {
        initializeIDETerminal();
      }
      if (typeof window.initChatbotModelSelectors === "function") {
        window.initChatbotModelSelectors();
      }
      if (typeof window.initLivecodeModeSelector === "function") {
        window.initLivecodeModeSelector();
      }
      if (typeof window.refreshLivecodeShimmerStyle === "function") {
        window.refreshLivecodeShimmerStyle();
      }
    }, 100);
    if (showImmediately && ideSection) {}
  } else {
    window.livecodeRecordToolOpen("ideEditor");
    return false;
  }
  return true;
};

window.closeIDEEditor = function() {
  var ideSection = document.getElementById("ide-editor-section");
  var ideBtn = document.getElementById("ideEditorToggleBtn");
  if (ideSection) {
    if (window.ideIsFullscreen) {
      exitIDEFullscreen();
    }
    ideSection.style.display = "none";
    if (ideBtn) ideBtn.classList.remove("selected");
    var openSections = parseLivecodeOpenSectionsList();
    openSections = openSections.filter(s => s !== "ideEditor");
    if (typeof saveOpenSections === "function") {
      saveOpenSections(openSections);
    } else {
      localStorage.setItem("livecode-open-sections", JSON.stringify(openSections));
    }
  }
  closeAllSectionsExcept(null);
  updateNotesVisibility();
};

window.toggleIDEFullscreen = function() {
  if (window.ideIsFullscreen) {
    exitIDEFullscreen();
  } else {
    enterIDEFullscreen();
  }
};

function _livecodeRelayoutAfterFullscreen() {
  setTimeout(function() {
    if (window.ideEditor) {
      try { window.ideEditor.layout(); } catch (e) {}
    }
    if (ideFitAddon) {
      try { ideFitAddon.fit(); } catch (e) {}
    }
  }, 100);
}

function enterIDEFullscreen() {
  const ideSection = document.getElementById("ide-editor-section");
  const ideInner = document.getElementById("ide-editor-inner");
  if (!ideSection || !ideInner) return;
  if (!window.ideOriginalStates) window.ideOriginalStates = {};
  window.ideOriginalStates["ideInnerHeight"] = ideInner.style.height || "";
  ideSection.classList.add("fullscreen-section");
  document.body.classList.add("ide-editor-fullscreen");
  document.documentElement.classList.add("ide-editor-fullscreen");
  ideInner.style.height = "100%";
  const enterIcon = document.getElementById("ide-fullscreen-enter-icon");
  const exitIcon = document.getElementById("ide-fullscreen-exit-icon");
  if (enterIcon) enterIcon.style.display = "none";
  if (exitIcon) exitIcon.style.display = "";
  window.ideIsFullscreen = true;
  _livecodeRelayoutAfterFullscreen();
}

function exitIDEFullscreen() {
  const ideSection = document.getElementById("ide-editor-section");
  const ideInner = document.getElementById("ide-editor-inner");
  if (!ideSection || !ideInner) return;
  ideSection.classList.remove("fullscreen-section");
  document.body.classList.remove("ide-editor-fullscreen");
  document.documentElement.classList.remove("ide-editor-fullscreen");
  const originalHeight = (window.ideOriginalStates && window.ideOriginalStates["ideInnerHeight"]) || "";
  ideInner.style.height = originalHeight || "";
  const enterIcon = document.getElementById("ide-fullscreen-enter-icon");
  const exitIcon = document.getElementById("ide-fullscreen-exit-icon");
  if (enterIcon) enterIcon.style.display = "";
  if (exitIcon) exitIcon.style.display = "none";
  window.ideIsFullscreen = false;
  _livecodeRelayoutAfterFullscreen();
}

let ideTerminalTabs = [];

let ideActiveTabId = null;

let ideTerminalTabCounter = 0;

let _ideTerminalIdSeq = 0;
const _ideTerminalSetsByProject = {};
const _LIVECODE_TERMINAL_BUFFER_MAX = 512 * 1024;

function _livecodeWaitForNonZeroSize(el, { timeoutMs = 1500 } = {}) {
  return new Promise((resolve, reject) => {
    const start = performance.now();
    function tick() {
      if (!el || !el.isConnected) {
        resolve(false);
        return;
      }
      const rect = el.getBoundingClientRect();
      const ok = rect && rect.width > 2 && rect.height > 2;
      if (ok) {
        resolve(true);
        return;
      }
      if (performance.now() - start > timeoutMs) {
        resolve(false);
        return;
      }
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  });
}

function _livecodeFlushTerminalOutput(tabData) {
  if (!tabData) return;
  if (!tabData._pendingOutput || !tabData.terminal) return;
  try {
    tabData.terminal.write(tabData._pendingOutput);
  } catch (e) {}
  tabData._pendingOutput = "";
}

function _livecodeFitTerminalTab(tabData, { emitResize = false } = {}) {
  if (!tabData) return;
  const wrapper = document.getElementById(`ide-terminal-wrapper-${tabData.id}`);
  if (!wrapper) return;

  const applyViewportPadding = () => {
    const viewport = wrapper.querySelector(".xterm-viewport");
    if (viewport) {
      viewport.style.position = "absolute";
      viewport.style.top = "0";
      viewport.style.left = "0";
      viewport.style.right = "0";
      viewport.style.bottom = "0";
    }
  };

  applyViewportPadding();

  if (tabData.fitAddon) {
    try {
      tabData.fitAddon.fit();
    } catch (e) {}
  }

  if (tabData.terminal) {
    try {
      tabData.terminal.scrollToBottom();
    } catch (e) {}
  }

  if (emitResize && tabData.socket && tabData.fitAddon) {
    try {
      const dims = tabData.fitAddon.proposeDimensions();
      if (dims) {
        tabData.socket.emit("terminal_resize", {
          cols: dims.cols,
          rows: dims.rows
        });
      }
    } catch (e) {}
  }
}

function createTerminalTab(name = null, options = null) {
  ++ideTerminalTabCounter;
  const tabId = `terminal-${++_ideTerminalIdSeq}`;
  let tabName = name;
  if (!tabName) {
    if (ideTerminalTabCounter === 1) {
      tabName = "Local";
    } else {
      tabName = `Local (${ideTerminalTabCounter})`;
    }
  }
  const tabsList = document.getElementById("ide-terminal-tabs-list");
  if (!tabsList) return null;
  const tabElement = document.createElement("button");
  tabElement.className = "ide-terminal-tab";
  tabElement.id = `ide-terminal-tab-${tabId}`;
  tabElement.dataset.tabId = tabId;
  const icon = document.createElement("span");
  icon.className = "ide-terminal-tab-icon";
  icon.innerHTML = _livecodeIcon("terminal", { size: "base" });
  const label = document.createElement("span");
  label.className = "ide-terminal-tab-label";
  label.textContent = tabName;
  const closeBtn = document.createElement("button");
  closeBtn.className = "ide-terminal-tab-close";
  closeBtn.title = "Kill terminal";
  closeBtn.innerHTML = _livecodeIcon("x", { size: "base" });
  closeBtn.onclick = e => {
    e.stopPropagation();
    closeTerminalTab(tabId);
  };
  tabElement.appendChild(icon);
  tabElement.appendChild(label);
  tabElement.appendChild(closeBtn);
  tabElement.onclick = () => switchTerminalTab(tabId);
  tabsList.appendChild(tabElement);
  const tabData = {
    id: tabId,
    name: tabName,
    element: tabElement,
    terminal: null,
    fitAddon: null,
    socket: null,
    initialized: false,
    _pendingOutput: ""
  };
  ideTerminalTabs.push(tabData);
  switchTerminalTab(tabId, options);
  return tabId;
}

function switchTerminalTab(tabId, options) {
  if (ideActiveTabId === tabId) return;
  const tabData = ideTerminalTabs.find(t => t.id === tabId);
  if (!tabData) return;
  tabData._noFocus = !!(options && options.focus === false);
  const container = document.getElementById("ide-terminal-container");
  if (!container) return;
  const allWrappers = container.querySelectorAll('[id^="ide-terminal-wrapper-"]');
  allWrappers.forEach(wrapper => {
    wrapper.style.display = "none";
  });
  ideTerminalTabs.forEach(tab => {
    const tabElement = document.querySelector(`#ide-terminal-tab-${tab.id}`);
    if (tabElement) {
      tabElement.classList.remove("active");
    }
  });
  ideActiveTabId = tabId;
  const tabElement = document.querySelector(`#ide-terminal-tab-${tabId}`);
  if (tabElement) {
    tabElement.classList.add("active");
  }
  if (!tabData.initialized) {
    initializeTerminalForTab(tabData, container);
  } else {
    const wrapper = document.getElementById(`ide-terminal-wrapper-${tabId}`);
    if (wrapper) {
      wrapper.style.display = "block";
      _livecodeWaitForNonZeroSize(wrapper, { timeoutMs: 1000 }).finally(() => {
        _livecodeFitTerminalTab(tabData, { emitResize: true });
        _livecodeFlushTerminalOutput(tabData);
        if (tabData.terminal && !tabData._noFocus) {
          tabData.terminal.focus();
        }
        tabData._noFocus = false;
        ideTerminal = tabData.terminal;
        ideFitAddon = tabData.fitAddon;
        window.ideTerminalSocket = tabData.socket;
      });
    }
  }
}

function _livecodeTeardownTerminalTab(tabData) {
  if (!tabData) return;
  if (tabData._terminalResizeObserver) {
    try {
      tabData._terminalResizeObserver.disconnect();
    } catch (e) {}
    tabData._terminalResizeObserver = null;
  }
  if (tabData._windowResizeHandler) {
    window.removeEventListener("resize", tabData._windowResizeHandler);
    tabData._windowResizeHandler = null;
  }
  if (tabData.socket) {
    try {
      tabData.socket.disconnect();
    } catch (e) {}
    tabData.socket = null;
  }
  if (tabData.terminal) {
    try {
      tabData.terminal.dispose();
    } catch (e) {}
    tabData.terminal = null;
  }
  const wrapper = document.getElementById(`ide-terminal-wrapper-${tabData.id}`);
  if (wrapper && wrapper.parentNode) {
    wrapper.parentNode.removeChild(wrapper);
  }
  if (tabData.element && tabData.element.parentNode) {
    tabData.element.parentNode.removeChild(tabData.element);
  }
}

function _livecodeResetTerminalsForProject() {
  ideTerminalTabs.slice().forEach(_livecodeTeardownTerminalTab);
  ideTerminalTabs = [];
  ideActiveTabId = null;
  ideTerminalTabCounter = 0;
  ideTerminal = null;
  ideFitAddon = null;
  window.ideTerminalSocket = null;
  ideTerminalInitialized = false;
}

function _livecodeSwapTerminalsForProject(previousPath, nextPath) {
  const prevKey = _livecodeNormalizeProjectKey(previousPath);
  const nextKey = _livecodeNormalizeProjectKey(nextPath);
  if (prevKey === nextKey) return;
  if (prevKey && ideTerminalTabs.length) {
    ideTerminalTabs.forEach(function(tab) {
      if (tab.element) tab.element.hidden = true;
      const wrapper = document.getElementById(`ide-terminal-wrapper-${tab.id}`);
      if (wrapper) wrapper.style.display = "none";
    });
    _ideTerminalSetsByProject[prevKey] = {
      tabs: ideTerminalTabs,
      activeTabId: ideActiveTabId,
      counter: ideTerminalTabCounter,
    };
  } else {
    ideTerminalTabs.slice().forEach(_livecodeTeardownTerminalTab);
  }
  const next = nextKey ? _ideTerminalSetsByProject[nextKey] : null;
  if (next) delete _ideTerminalSetsByProject[nextKey];
  ideTerminalTabs = next ? next.tabs : [];
  ideTerminalTabCounter = next ? next.counter : 0;
  ideActiveTabId = null;
  ideTerminal = null;
  ideFitAddon = null;
  window.ideTerminalSocket = null;
  ideTerminalTabs.forEach(function(tab) {
    if (tab.element) tab.element.hidden = false;
  });
  const showId = next && ideTerminalTabs.some(function(t) { return t.id === next.activeTabId; })
    ? next.activeTabId
    : (ideTerminalTabs[0] && ideTerminalTabs[0].id);
  if (showId) {
    switchTerminalTab(showId, { focus: false });
  } else if (ideTerminalVisible && nextKey) {
    createTerminalTab(null, { focus: false });
  }
}

function _livecodeCloseProjectTerminals(key) {
  const set = _ideTerminalSetsByProject[key];
  if (!set) return;
  delete _ideTerminalSetsByProject[key];
  set.tabs.forEach(_livecodeTeardownTerminalTab);
}

function _livecodeBufferTerminalOutput(tabData, text) {
  let buffered = (tabData._pendingOutput || "") + text;
  if (buffered.length > _LIVECODE_TERMINAL_BUFFER_MAX) {
    buffered = buffered.slice(buffered.length - _LIVECODE_TERMINAL_BUFFER_MAX);
  }
  tabData._pendingOutput = buffered;
}

function closeTerminalTab(tabId) {
  const tabIndex = ideTerminalTabs.findIndex(t => t.id === tabId);
  if (tabIndex === -1) return;
  const tabData = ideTerminalTabs[tabIndex];
  const isLastTab = ideTerminalTabs.length === 1;
  _livecodeTeardownTerminalTab(tabData);
  ideTerminalTabs.splice(tabIndex, 1);
  if (isLastTab) {
    ideActiveTabId = null;
    hideIDETerminal();
    return;
  }
  if (ideActiveTabId === tabId) {
    if (ideTerminalTabs.length > 0) {
      switchTerminalTab(ideTerminalTabs[ideTerminalTabs.length - 1].id);
    } else {
      ideActiveTabId = null;
    }
  }
}

function _livecodeGetTerminalTheme() {

  const styles = getComputedStyle(document.body);
  const read = (name, fallback) => {
    const value = styles.getPropertyValue(name).trim();
    return value || fallback;
  };

  const bg = read("--ide-terminal-bg", "#141414");
  const fg = read("--ide-terminal-fg", "#ffffff");
  const selectionBackground = read("--ide-terminal-selection-bg", "rgba(148, 163, 184, 0.35)");
  const selectionForeground = read("--ide-terminal-selection-fg", fg);

  return {
    background: bg,
    foreground: fg,
    cursor: fg,
    cursorAccent: bg,

    selection: selectionBackground,
    selectionBackground: selectionBackground,
    selectionForeground: selectionForeground
  };
}

function _livecodeApplyTerminalTheme() {
  const theme = _livecodeGetTerminalTheme();
  ideTerminalTabs.forEach((t) => {
    if (!t || !t.terminal) return;
    try {
      t.terminal.options.theme = theme;
    } catch (e) {}
  });
}
window._livecodeApplyTerminalTheme = _livecodeApplyTerminalTheme;

function _livecodeTerminalContentEnd(line, cell, cols) {
  for (let x = Math.min(cols, line.length) - 1; x >= 0; x--) {
    const current = line.getCell(x, cell);
    if (!current) continue;
    const chars = current.getChars();
    if ((chars && chars !== " ") || !current.isBgDefault() || current.isInverse()) {
      return x + Math.max(1, current.getWidth());
    }
  }
  return 0;
}

function _livecodeTrimTerminalSelection(terminal) {
  const layer = terminal.element && terminal.element.querySelector(".xterm-screen .xterm-selection");
  if (!layer) return;
  const observer = new MutationObserver(function() {
    if (!layer.firstElementChild) return;
    const sampleRow = terminal.element.querySelector(".xterm-rows > div");
    const cellHeight = sampleRow ? parseFloat(sampleRow.style.height) : 0;
    const cellWidth = sampleRow ? parseFloat(sampleRow.style.width) / terminal.cols : 0;
    if (!(cellHeight > 0 && cellWidth > 0)) return;
    const buffer = terminal.buffer.active;
    const cell = buffer.getNullCell();
    const spans = [];
    Array.from(layer.children).forEach(function(block) {
      const left = parseFloat(block.style.left);
      const firstRow = Math.round(parseFloat(block.style.top) / cellHeight);
      const rowCount = Math.round(parseFloat(block.style.height) / cellHeight);
      const startCol = Math.round(left / cellWidth);
      const endCol = Math.round((left + parseFloat(block.style.width)) / cellWidth);
      for (let row = firstRow; row < firstRow + rowCount; row++) {
        const line = buffer.getLine(buffer.viewportY + row);
        const stopCol = Math.min(endCol, line ? _livecodeTerminalContentEnd(line, cell, terminal.cols) : 0);
        if (stopCol <= startCol) continue;
        const previous = spans[spans.length - 1];
        if (previous && previous.startCol === startCol && previous.stopCol === stopCol && previous.row + previous.rows === row) {
          previous.rows++;
        } else {
          spans.push({ row: row, rows: 1, startCol: startCol, stopCol: stopCol });
        }
      }
    });
    const fragment = document.createDocumentFragment();
    spans.forEach(function(span) {
      const element = document.createElement("div");
      element.style.top = span.row * cellHeight + "px";
      element.style.height = span.rows * cellHeight + "px";
      element.style.left = span.startCol * cellWidth + "px";
      element.style.width = (span.stopCol - span.startCol) * cellWidth + "px";
      fragment.appendChild(element);
    });
    layer.replaceChildren(fragment);
    observer.takeRecords();
  });
  observer.observe(layer, { childList: true });
}

function initializeTerminalForTab(tabData, container) {

  const fontStack = "'Menlo', 'Monaco', 'Consolas', 'Liberation Mono', 'Courier New', monospace";

  const terminalWrapper = document.createElement("div");
  terminalWrapper.id = `ide-terminal-wrapper-${tabData.id}`;
  terminalWrapper.style.cssText = "width:100%;height:100%;position:relative;";
  terminalWrapper.style.padding = "0";
  terminalWrapper.style.boxSizing = "border-box";
  container.appendChild(terminalWrapper);

  const isActive = () => ideActiveTabId === tabData.id;

  document.fonts.ready.then(() => {
    Promise.resolve().finally(() => {

      const terminal = new Terminal({
        cursorBlink: true,
        cursorStyle: "bar",
        fontFamily: fontStack,
        fontSize: 13,
        lineHeight: 1.2,
        fontWeight: "normal",
        letterSpacing: 0,
        theme: _livecodeGetTerminalTheme(),
        allowProposedApi: true,
        rightClickSelectsWord: true
      });

      let fitAddon = null;
      if (window.FitAddon) {
        fitAddon = new window.FitAddon.FitAddon;
        terminal.loadAddon(fitAddon);
      }

      terminal.open(terminalWrapper);
      _livecodeTrimTerminalSelection(terminal);

      const socket = io.connect(location.protocol + "//" + location.host);
      let terminalInitEmittedForSid = null;
      socket.on("connect_error", function() {
        try {
          socket.disconnect();
        } catch (e) {}
      });
      socket.on("disconnect", () => {
        terminalInitEmittedForSid = null;
      });

      function emitInitWithBestDims() {
        if (!socket || !socket.connected) return;
        if (terminalInitEmittedForSid === socket.id) return;
        terminalInitEmittedForSid = socket.id;
        let cols = 80;
        let rows = 24;
        if (fitAddon) {
          try {
            const dims = fitAddon.proposeDimensions();
            if (dims && dims.cols && dims.rows) {
              cols = dims.cols;
              rows = dims.rows;
            }
          } catch (e) {}
        }
        socket.emit("terminal_init", {
          cols,
          rows,
          cwd: livecodeProjectPath || undefined
        });
      }

      function ensureFitAndInit() {
        return _livecodeWaitForNonZeroSize(terminalWrapper, { timeoutMs: 2000 }).finally(() => {
          _livecodeFitTerminalTab(tabData);
          if (socket && socket.connected) {
            emitInitWithBestDims();
          }
          if (isActive()) {
            _livecodeFlushTerminalOutput(tabData);
            if (!tabData._noFocus) {
              try {
                terminal.focus();
              } catch (e) {}
            }
            tabData._noFocus = false;
          }
        });
      }

      socket.on("connect", () => {
        ensureFitAndInit();
      });

      socket.on("terminal_output", data => {
        if (!data || !data.output) return;
        if (isActive() && ideTerminalVisible) {
          _livecodeFlushTerminalOutput(tabData);
          try {
            terminal.write(data.output);
          } catch (e) {}
          setTimeout(() => {
            try {
              terminal.scrollToBottom();
            } catch (e) {}
          }, 10);
        } else {
          _livecodeBufferTerminalOutput(tabData, data.output);
        }
      });

      terminal.onData(data => {
        socket.emit("terminal_input", {
          input: data
        });
      });

      terminal.onResize(size => {
        socket.emit("terminal_resize", {
          cols: size.cols,
          rows: size.rows
        });
      });

      tabData.terminal = terminal;
      tabData.fitAddon = fitAddon;
      tabData.socket = socket;
      tabData.initialized = true;
      tabData.terminal.element = terminalWrapper;

      if (isActive()) {
        terminalWrapper.style.display = "block";
        ideTerminal = terminal;
        ideFitAddon = fitAddon;
        window.ideTerminalSocket = socket;
      } else {
        terminalWrapper.style.display = "none";
      }

      const windowResizeHandler = () => {
        if (fitAddon && isActive()) {
          _livecodeFitTerminalTab(tabData, { emitResize: true });
        }
      };
      window.addEventListener("resize", windowResizeHandler);
      tabData._windowResizeHandler = windowResizeHandler;

      if (fitAddon && typeof ResizeObserver !== "undefined") {
        const resizeObserver = new ResizeObserver(() => {
          if (!isActive()) return;
          _livecodeFitTerminalTab(tabData, { emitResize: true });
        });
        resizeObserver.observe(terminalWrapper);
        tabData._terminalResizeObserver = resizeObserver;
      }

      ensureFitAndInit();
    });
  });
}

function initializeIDETerminal() {
  const container = document.getElementById("ide-terminal-container");
  if (!container) return;
  const addBtn = document.getElementById("ide-terminal-tab-add");
  if (addBtn) {
    addBtn.onclick = () => createTerminalTab();
  }
  if (ideTerminalTabs.length === 0 && livecodeProjectPath) {
    createTerminalTab();
  }
  ideTerminalInitialized = true;
}

let ideTerminalVisible = false;

function showIDETerminal() {
  const terminalPanel = document.getElementById("ide-terminal-panel");
  const terminalDivider = document.getElementById("ide-terminal-divider");
  if (terminalPanel && terminalDivider) {
    terminalPanel.style.display = "flex";
    terminalPanel.style.opacity = "";
    terminalPanel.style.overflow = "";
    terminalPanel.style.transition = "";
    terminalDivider.style.display = "block";
    const ideEditorInner = document.getElementById("ide-editor-inner");
    if (ideEditorInner) {
      const sectionHeight = ideEditorInner.getBoundingClientRect().height || 499;
      const terminalHeight = Math.round(sectionHeight * .3);
      if (!terminalPanel.style.height || terminalPanel.style.height === "100px" || terminalPanel.style.height === "150px") {
        terminalPanel.style.height = terminalHeight + "px";
      }
    }
    if (ideTerminalTabs.length === 0) {
      createTerminalTab();
    }
    ideTerminalVisible = true;
    const toggleBtn = document.getElementById("ide-terminal-toggle");
    if (toggleBtn) {
      toggleBtn.innerHTML = `\n                 <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n                    <polyline points="20 6 9 17 4 12"></polyline>\n                 </svg>\n                 <span>Terminal</span>\n               `;
    }
    const activityTerminalBtn = document.getElementById("ide-activity-terminal");
    if (activityTerminalBtn) {
      activityTerminalBtn.classList.add("active");
      activityTerminalBtn.setAttribute("aria-pressed", "true");
    }
    function scheduleFits() {
      [ 100, 280, 550 ].forEach(delay => {
        setTimeout(() => {
          if (ideFitAddon) {
            try {
              ideFitAddon.fit();
            } catch (e) {}
          }
        }, delay);
      });
    }
    scheduleFits();
    const shown = ideTerminalTabs.find(t => t.id === ideActiveTabId);
    if (shown) setTimeout(() => _livecodeFlushTerminalOutput(shown), 120);
  }
}

function hideIDETerminal() {
  const terminalPanel = document.getElementById("ide-terminal-panel");
  const terminalDivider = document.getElementById("ide-terminal-divider");
  if (terminalPanel && terminalDivider) {
    terminalPanel.style.display = "none";
    terminalPanel.style.opacity = "";
    terminalPanel.style.overflow = "";
    terminalPanel.style.transition = "";
    terminalDivider.style.display = "none";
    ideTerminalVisible = false;
    const toggleBtn = document.getElementById("ide-terminal-toggle");
    if (toggleBtn) {
      toggleBtn.innerHTML = `\n                 <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n                    <polyline points="4 17 10 11 4 5"></polyline>\n                    <line x1="12" y1="19" x2="20" y2="19"></line>\n                 </svg>\n                 <span>Terminal</span>\n               `;
    }
    const activityTerminalBtn = document.getElementById("ide-activity-terminal");
    if (activityTerminalBtn) {
      activityTerminalBtn.classList.remove("active");
      activityTerminalBtn.setAttribute("aria-pressed", "false");
    }
  }
}

function toggleIDETerminal() {
  if (ideTerminalVisible) {
    hideIDETerminal();
  } else {
    showIDETerminal();
  }
}
window.toggleIDETerminal = toggleIDETerminal;

document.addEventListener("DOMContentLoaded", function() {
  const toggleBtn = document.getElementById("ide-terminal-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", toggleIDETerminal);
  }
});

function updatePlayButtonVisibility() {
  const playButton = document.getElementById("ide-play-button");
  updateIdePlanChrome();
  if (!playButton) return;
  if (!ideActiveFile || !ideOpenFiles[ideActiveFile]) {
    playButton.style.display = "none";
    return;
  }
  const fileName = ideOpenFiles[ideActiveFile].name;
  if (fileName.toLowerCase().endsWith(".py")) {
    playButton.style.display = "inline-flex";
  } else {
    playButton.style.display = "none";
  }
}

function runCurrentPythonFile() {
  if (!ideActiveFile || !ideOpenFiles[ideActiveFile]) {
    return;
  }
  const fileName = ideOpenFiles[ideActiveFile].name;
  if (!fileName.toLowerCase().endsWith(".py")) {
    return;
  }
  runPythonFile(ideActiveFile);
}

function runPythonFile(filePath) {
  showIDETerminal();
  if (!ideTerminalInitialized) {
    initializeIDETerminal();
    setTimeout(() => {
      executePythonFile(filePath);
    }, 500);
  } else {
    executePythonFile(filePath);
  }
}

function executePythonFile(filePath) {
  const command = `python3 "${filePath}"\n`;
  if (!window.ideTerminalSocket) {
    window.ideTerminalSocket = io.connect(location.protocol + "//" + location.host);
    window.ideTerminalSocket.on("connect", () => {
      window.ideTerminalSocket.emit("terminal_input", {
        input: command
      });
      setTimeout(() => {
        if (ideTerminal) {
          ideTerminal.focus();
        }
      }, 100);
    });
  } else {
    window.ideTerminalSocket.emit("terminal_input", {
      input: command
    });
    setTimeout(() => {
      if (ideTerminal) {
        ideTerminal.focus();
      }
    }, 100);
  }
}

let ideFileTreeData = {};

let ideHomePath = "~";

function _livecodeRootFolderName(path) {
  return (path || "").split("/").filter(Boolean).pop() || path || "Project";
}

function _livecodeUniqueWorkspaceRootName(baseName, usedNames) {
  let name = baseName || "Project";
  let index = 2;
  while (usedNames.has(name)) {
    name = (baseName || "Project") + " " + index;
    index += 1;
  }
  usedNames.add(name);
  return name;
}

function _livecodeWrapRootTreeChildren(actualPath, children) {
  const rootName = _livecodeRootFolderName(actualPath);
  ideExpandedFolders.add(actualPath);
  return {
    [rootName]: {
      type: "folder",
      path: actualPath,
      is_dir: true,
      children: children,
      expanded: true,
      loaded: true
    }
  };
}

function _livecodeWorkspaceRenderRootPath() {
  return livecodeWorkspaceFolders.length > 1 ? "__livecode_workspace__" : ideHomePath;
}

function _livecodeRenderWorkspaceTree() {
  const fileTreeEl = document.getElementById("ide-file-tree");
  if (!fileTreeEl) return;
  renderFileTree(ideFileTreeData, fileTreeEl, _livecodeWorkspaceRenderRootPath());
}

function _livecodeIsWorkspaceRootNode(rootPath, level, nodePath) {
  if (rootPath !== "__livecode_workspace__" || level !== 0 || !nodePath) return false;
  return _livecodeWorkspaceFolderIndexByPath(nodePath) !== -1;
}

function _livecodeAppendWorkspaceRootControls(item, nodePath) {
  const index = _livecodeWorkspaceFolderIndexByPath(nodePath);
  if (index < 0) return;
  item.dataset.livecodeWorkspaceRootPath = nodePath;
  item.dataset.livecodeWorkspaceRootIndex = String(index);
  if (index === 0) {
    const badge = document.createElement("span");
    badge.className = "livecode-workspace-root-badge theme-transition";
    badge.textContent = "Primary";
    item.appendChild(badge);
  }
  if (_livecodeWorkspaceFolderIsMissing(nodePath)) {
    const missingBadge = document.createElement("span");
    missingBadge.className = "livecode-workspace-missing-badge theme-transition";
    missingBadge.textContent = "Missing";
    item.appendChild(missingBadge);
  }
}

function _livecodeWorkspaceFolderIsMissing(nodePath) {
  const norm = _livecodeNormalizeProjectKey(nodePath);
  return livecodeWorkspaceMissingFolders.some(function(p) { return _livecodeNormalizeProjectKey(p) === norm; });
}

function _livecodeRefreshWorkspaceMissingBadges() {
  document.querySelectorAll("#ide-file-tree .livecode-workspace-root-item").forEach(function(item) {
    const nodePath = item.dataset.livecodeWorkspaceRootPath;
    const existing = item.querySelector(".livecode-workspace-missing-badge");
    if (_livecodeWorkspaceFolderIsMissing(nodePath)) {
      if (!existing) {
        const missingBadge = document.createElement("span");
        missingBadge.className = "livecode-workspace-missing-badge theme-transition";
        missingBadge.textContent = "Missing";
        item.appendChild(missingBadge);
      }
    } else if (existing) {
      existing.remove();
    }
  });
}

function _livecodeWorkspaceDragIndexFromEvent(row, event) {
  const rawIndex = Number(row.getAttribute("data-livecode-workspace-row") || row.dataset.livecodeWorkspaceRootIndex || "-1");
  if (rawIndex < 0) return -1;
  const rect = row.getBoundingClientRect();
  return event.clientY > rect.top + rect.height / 2 ? rawIndex + 1 : rawIndex;
}

function _livecodeClearWorkspaceDragClasses(container) {
  if (!container) return;
  container.querySelectorAll(".livecode-workspace-drag-over-before, .livecode-workspace-drag-over-after, .livecode-workspace-dragging").forEach(function(row) {
    row.classList.remove("livecode-workspace-drag-over-before", "livecode-workspace-drag-over-after", "livecode-workspace-dragging");
  });
}

function _livecodeUpdateWorkspaceDropIndicator(row, event) {
  row.classList.remove("livecode-workspace-drag-over-before", "livecode-workspace-drag-over-after");
  const rect = row.getBoundingClientRect();
  row.classList.add(event.clientY > rect.top + rect.height / 2 ? "livecode-workspace-drag-over-after" : "livecode-workspace-drag-over-before");
}

function _livecodeWorkspaceDragPayload(event) {
  if (!event.dataTransfer) return null;
  try {
    const raw = event.dataTransfer.getData("application/x-livecode-workspace-root");
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

function _livecodeStartWorkspaceRowDrag(row, event) {
  const index = Number(row.getAttribute("data-livecode-workspace-row") || row.dataset.livecodeWorkspaceRootIndex || "-1");
  const folders = _livecodeWorkspaceFoldersForManager();
  const folder = folders[index];
  if (!folder || !event.dataTransfer) return false;
  event.stopPropagation();
  row.classList.add("livecode-workspace-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("application/x-livecode-workspace-root", JSON.stringify({ path: folder.path }));
  event.dataTransfer.setData("text/plain", _livecodeWorkspaceFolderLabel(folder));
  return true;
}

function _livecodeBindWorkspaceRowDrag(row, container) {
  row.addEventListener("dragstart", function(event) {
    _livecodeStartWorkspaceRowDrag(row, event);
  });
  row.addEventListener("dragover", function(event) {
    const types = event.dataTransfer ? Array.prototype.slice.call(event.dataTransfer.types || []) : [];
    if (types.indexOf("application/x-livecode-workspace-root") === -1) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    _livecodeUpdateWorkspaceDropIndicator(row, event);
  });
  row.addEventListener("dragleave", function(event) {
    if (row.contains(event.relatedTarget)) return;
    row.classList.remove("livecode-workspace-drag-over-before", "livecode-workspace-drag-over-after");
  });
  row.addEventListener("drop", function(event) {
    const payload = _livecodeWorkspaceDragPayload(event);
    if (!payload || !payload.path) return;
    event.preventDefault();
    event.stopPropagation();
    const targetIndex = _livecodeWorkspaceDragIndexFromEvent(row, event);
    _livecodeClearWorkspaceDragClasses(container);
    _livecodeMoveWorkspaceFolderToIndex(payload.path, targetIndex);
  });
  row.addEventListener("dragend", function() {
    _livecodeClearWorkspaceDragClasses(container);
  });
}

function _livecodeBindWorkspaceManagerDrag(list) {
  list.querySelectorAll("[data-livecode-workspace-row]").forEach(function(row) {
    _livecodeBindWorkspaceRowDrag(row, list);
  });
}

function _livecodeLoadWorkspaceFolderTrees() {
  if (!livecodeWorkspaceFolders.length) return;
  const rootPath = _livecodeWorkspaceRenderRootPath();
  if (livecodeWorkspaceFolders.length > 1) {
    ideFileTreeData[rootPath] = {};
    livecodeWorkspaceRootOrder = [];
    const usedNames = new Set();
    livecodeWorkspaceFolders.forEach(function(folder) {
      if (!folder || !folder.path) return;
      const baseName = folder.name || _livecodeRootFolderName(folder.path);
      const name = _livecodeUniqueWorkspaceRootName(baseName, usedNames);
      livecodeWorkspaceRootOrder.push(name);
      ideFileTreeData[rootPath][name] = {
        type: "folder",
        path: folder.path,
        is_dir: true,
        children: {},
        expanded: true,
        loaded: false
      };
      ideExpandedFolders.add(folder.path);
      loadIDEFileTree(folder.path, ideFileTreeData[rootPath][name]);
    });
    _livecodeRenderWorkspaceTree();
    return;
  }
  loadIDEFileTree(livecodeWorkspaceFolders[0].path);
}

function _livecodeTreeNodeFromListItem(item) {
  return {
    type: item.type || (item.is_dir ? "folder" : "file"),
    path: item.path,
    is_dir: item.is_dir || false,
    children: item.is_dir ? {} : undefined,
    expanded: false,
    loaded: false
  };
}

function loadIDEFileTree(path, parentNode = null) {
  const fileTreeEl = document.getElementById("ide-file-tree");
  if (!fileTreeEl) return;
  if (!path && !livecodeProjectPath) {
    renderLiveCodeExplorerEmpty();
    return;
  }
  const listPath = path || livecodeProjectPath;
  if (!listPath) {
    renderLiveCodeExplorerEmpty();
    return;
  }
  const emptyEl = document.getElementById("ide-explorer-empty");
  if (emptyEl) emptyEl.style.display = "none";
  const requestedProject = livecodeProjectPath;
  _livecodeIdeSocketRequest(
    "ide_list_files", { path: listPath },
    "ide_files_list",
    function(data) {
      return data && (data.requested_path !== undefined ? data.requested_path === listPath : data.path === listPath);
    }
  ).then(function(result) {
    if (livecodeProjectPath !== requestedProject) return;
    const data = result.data;
    if (data.error) {
      if (result.error) console.error("LiveCode: failed to list", listPath, data.error);
      if (!parentNode) {
        fileTreeEl.innerHTML = `<div style="padding:4px 8px;color:#ef4444;font-size:12px;">Error: ${data.error}</div>`;
      }
      return;
    }
    const actualPath = data.path || listPath;
    if (!parentNode) {
      ideHomePath = actualPath;
      livecodeProjectPath = actualPath;
      livecodeProjectName = actualPath.split("/").filter(Boolean).pop() || actualPath;
    }
    const children = {};
    if (data.files && Array.isArray(data.files)) {
      data.files.forEach(item => {
        children[item.name] = _livecodeTreeNodeFromListItem(item);
      });
    }
    if (parentNode) {
      parentNode.loaded = true;
      parentNode.children = children;
      _livecodeRenderWorkspaceTree();
    } else {
      ideFileTreeData[actualPath] = _livecodeWrapRootTreeChildren(actualPath, children);
      _livecodeRenderWorkspaceTree();
    }
  });
}

let _livecodeFileTreeRenderRaf = null;
let _livecodeFileTreeRenderArgs = null;

function _livecodeToggleFolderChevron(item, expanded) {
  const chevron = item.querySelector(".ide-chevron-expanded, .ide-chevron-collapsed");
  if (!chevron) return;
  chevron.classList.toggle("ide-chevron-expanded", expanded);
  chevron.classList.toggle("ide-chevron-collapsed", !expanded);
  const folderImg = item.querySelector("img");
  const folderName = String(item.dataset.idePath || "").split("/").filter(Boolean).pop() || "";
  if (folderImg) folderImg.setAttribute("src", _livecodeFolderIcon(expanded, folderName));
}

function _livecodeScheduleFileTreeRender(treeData, container, rootPath) {
  _livecodeFileTreeRenderArgs = { treeData: treeData, container: container, rootPath: rootPath };
  if (_livecodeFileTreeRenderRaf != null) return;
  _livecodeFileTreeRenderRaf = requestAnimationFrame(function() {
    _livecodeFileTreeRenderRaf = null;
    const args = _livecodeFileTreeRenderArgs;
    _livecodeFileTreeRenderArgs = null;
    if (args) _livecodeRenderFileTreeNow(args.treeData, args.container, args.rootPath);
  });
}

function renderFileTree(treeData, container, rootPath) {
  _livecodeScheduleFileTreeRender(treeData, container, rootPath);
}

function _livecodeRenderFileTreeNow(treeData, container, rootPath) {
  container.innerHTML = "";
  function renderNode(node, name, fullPath, level, parentPath, parentContainer) {
    const isFolder = node.is_dir || node.type === "folder";
    const indent = level * 16;
    const isExpanded = node.expanded && ideExpandedFolders.has(node.path);
    const icon = isFolder ? _livecodeFolderIcon(isExpanded, name) : _livecodeFileIcon(name);
    const hasChildren = isFolder && node.children && Object.keys(node.children).length > 0;
    const item = document.createElement("div");
    item.className = isFolder ? "folder-item theme-transition" : "file-item theme-transition";
    item.style.cssText = `padding:2px 4px;padding-left:${4 + indent}px;cursor:pointer;display:flex;align-items:center;gap:4px;font-size:13px;border-radius:0;min-width:max-content;`;
    let chevron = "";
    if (isFolder) {
      const chevronClass = isExpanded ? "ide-chevron-expanded" : "ide-chevron-collapsed";
      chevron = `<span class="${chevronClass}" style="width:12px;height:12px;display:inline-block;margin-right:6px;vertical-align:middle;"></span>`;
    } else {
      chevron = '<span style="width:12px;display:inline-block;margin-right:6px;"></span>';
    }
    item.innerHTML = `\n              ${chevron}\n              <img src="${icon}" alt="${isFolder ? "Folder" : "File"}" style="width: 16px; height: 16px; flex-shrink: 0;" />\n              <span style="white-space:nowrap;min-width:0;">${name}</span>\n            `;
    const isWorkspaceRootNode = _livecodeIsWorkspaceRootNode(rootPath, level, node.path);
    if (isWorkspaceRootNode) {
      item.classList.add("livecode-workspace-root-item");
      _livecodeAppendWorkspaceRootControls(item, node.path);
      _livecodeBindWorkspaceRowDrag(item, container);
    }
    item.dataset.idePath = node.path;
    if (_livecodeFsClipboard && _livecodeFsClipboard.mode === "move" && _livecodeFsClipboard.path === node.path) {
      item.classList.add("ide-cut-pending");
    }
    item.draggable = true;
    item.addEventListener("dragstart", function(e) {
      if (isWorkspaceRootNode) return;
      if (!e.dataTransfer) return;
      e.stopPropagation();
      const repoPath = _livecodeWorkspaceRepoPath(node.path);
      const payload = {
        repoPath: repoPath,
        kind: isFolder ? "folder" : "file",
        name: name
      };
      window._livecodeRepoDragPayload = payload;
      e.dataTransfer.setData("application/x-livecode-repo-context", JSON.stringify(payload));
      e.dataTransfer.setData("text/plain", name);
      e.dataTransfer.effectAllowed = "copy";
    });
    item.addEventListener("dragend", function() {
      setTimeout(function() {
        window._livecodeRepoDragPayload = null;
      }, 0);
    });
    const dragImg = item.querySelector("img");
    if (dragImg) dragImg.draggable = false;
    if (!isFolder) {
      item.onclick = e => {
        e.stopPropagation();
        openFileInEditorFromPath(node.path);
      };
      item.oncontextmenu = e => {
        showIDEContextMenu(e, node.path, name, false);
      };
      parentContainer.appendChild(item);
    } else {
      item.oncontextmenu = e => {
        showIDEContextMenu(e, node.path, name, true);
      };
      const wrap = document.createElement("div");
      wrap.className = "ide-tree-folder-wrap";
      wrap.dataset.folderPath = node.path;
      wrap.appendChild(item);
      const childContainer = document.createElement("div");
      childContainer.className = "ide-tree-children";
      childContainer.dataset.treeChildren = node.path;
      childContainer.style.setProperty("--ide-tree-guide-x", (indent + 8) + "px");
      childContainer.style.display = isExpanded ? "" : "none";
      wrap.appendChild(childContainer);
      parentContainer.appendChild(wrap);
      item.onclick = e => {
        e.stopPropagation();
        node.expanded = !node.expanded;
        if (node.expanded) {
          ideExpandedFolders.add(node.path);
        } else {
          ideExpandedFolders.delete(node.path);
        }
        if (!node.loaded) {
          loadIDEFileTree(node.path, node);
          return;
        }
        childContainer.style.display = node.expanded ? "" : "none";
        _livecodeToggleFolderChevron(item, node.expanded);
      };
      if (isExpanded && hasChildren) {
        Object.keys(node.children).sort((a, b) => {
          const aIsFolder = node.children[a].is_dir || node.children[a].type === "folder";
          const bIsFolder = node.children[b].is_dir || node.children[b].type === "folder";
          if (aIsFolder && !bIsFolder) return -1;
          if (!aIsFolder && bIsFolder) return 1;
          return a.localeCompare(b);
        }).forEach(childName => {
          const childNode = node.children[childName];
          if (childNode.is_dir && !childNode.loaded && ideExpandedFolders.has(childNode.path)) {
            loadIDEFileTree(childNode.path, childNode);
          }
          renderNode(childNode, childName, fullPath + "/" + childName, level + 1, node.path, childContainer);
        });
      }
    }
  }
  const rootTree = treeData[rootPath] || {};
  if (!rootTree || Object.keys(rootTree).length === 0) {
    container.innerHTML = '<div style="padding:4px 8px;font-size:12px;" class="theme-transition">Loading...</div>';
    return;
  }
  const sortedKeys = Object.keys(rootTree).sort((a, b) => {
    if (rootPath === "__livecode_workspace__") {
      const aIndex = livecodeWorkspaceRootOrder.indexOf(a);
      const bIndex = livecodeWorkspaceRootOrder.indexOf(b);
      if (aIndex !== -1 || bIndex !== -1) {
        if (aIndex === -1) return 1;
        if (bIndex === -1) return -1;
        return aIndex - bIndex;
      }
    }
    const aIsFolder = rootTree[a].is_dir || rootTree[a].type === "folder";
    const bIsFolder = rootTree[b].is_dir || rootTree[b].type === "folder";
    if (aIsFolder && !bIsFolder) return -1;
    if (!aIsFolder && bIsFolder) return 1;
    return a.localeCompare(b);
  });
  sortedKeys.forEach(name => {
    renderNode(rootTree[name], name, name, 0, rootPath, container);
  });
  _livecodeSyncTreeActiveFile(container);
}

function _livecodeSyncTreeActiveFile(container) {
  const tree = container || document.getElementById("ide-file-tree");
  if (!tree) return;
  tree.querySelectorAll(".file-item.is-active-file").forEach(function(row) {
    if (row.dataset.idePath !== ideActiveFile) row.classList.remove("is-active-file");
  });
  if (!ideActiveFile) return;
  tree.querySelectorAll(".file-item").forEach(function(row) {
    if (row.dataset.idePath === ideActiveFile) row.classList.add("is-active-file");
  });
}

const LIVECODE_PLAN_TAB_PREFIX = "plan://";
const LIVECODE_PLAN_MERMAID_PREFIX = "livecode-plan-mermaid-";

function _livecodePlanTabKey(planFile) {
  return LIVECODE_PLAN_TAB_PREFIX + planFile;
}

function _livecodeIsPlanTabKey(key) {
  return String(key || "").indexOf(LIVECODE_PLAN_TAB_PREFIX) === 0;
}

function _livecodeIsMarkdownFileName(name) {
  return /\.(md|markdown)$/i.test(String(name || ""));
}

function _livecodeIsPlanFileName(name) {
  return /\.plan\.md$/i.test(String(name || ""));
}

function _livecodeActiveFileInfo() {
  return ideActiveFile ? (ideOpenFiles[ideActiveFile] || null) : null;
}

function _livecodeFileUsesPlanSurface(info) {
  if (!info) return false;
  if (info.isPlan) return true;

  return _livecodeIsMarkdownFileName(info.name);
}

function _livecodeHidePlanSurface() {
  const view = document.getElementById("ide-plan-view");
  if (view) {
    view.style.display = "none";
    view.classList.remove("is-monaco-source");
  }
  const monacoSurface = document.getElementById("ide-monaco");
  if (monacoSurface) monacoSurface.style.top = "0";
  _livecodeClosePlanMenu();
}

let _livecodePlanSourceSaveTimer = null;
let _livecodePlanMonacoListenerBound = false;

function _livecodeSyncPlanSourceFromEditor() {
  const info = _livecodeActiveFileInfo();
  if (!info) return "";
  let next = null;
  if (info.viewMode === "markdown" && window.ideEditor) {
    try {
      const model = window.ideEditor.getModel();
      if (model && (!info.model || info.model === model)) {
        next = window.ideEditor.getValue();
      }
    } catch (e) {}
  }
  if (next == null) {
    const editor = document.getElementById("ide-plan-source-editor");
    if (!editor) return info.content || "";
    next = editor.value;
  }
  info.content = next;
  const original = info.originalContent || "";
  const isModified = next !== original;
  if (info.modified !== isModified) {
    info.modified = isModified;
    updateOpenFilesList();
  }
  return next;
}

function _livecodeBindPlanMonacoAutosave() {
  if (_livecodePlanMonacoListenerBound || !window.ideEditor) return;
  _livecodePlanMonacoListenerBound = true;
  window.ideEditor.onDidChangeModelContent(function() {
    const info = _livecodeActiveFileInfo();
    if (!info || info.viewMode !== "markdown" || !_livecodeFileUsesPlanSurface(info)) return;
    _livecodeSyncPlanSourceFromEditor();
    _livecodeSchedulePlanSourceSave();
  });
}

function _livecodeFallbackPlanMarkdownTextarea(fileInfo, body, monacoSurface, view) {
  if (view) view.classList.remove("is-monaco-source");
  if (monacoSurface) {
    monacoSurface.style.display = "none";
    monacoSurface.style.top = "0";
  }
  if (!body || !fileInfo) return false;
  body.classList.add("is-source");
  body.innerHTML = "";
  const editor = document.createElement("textarea");
  editor.id = "ide-plan-source-editor";
  editor.className = "ide-plan-source-editor theme-transition";
  editor.value = fileInfo.content || "";
  editor.spellcheck = false;
  editor.setAttribute("aria-label", "Edit markdown");
  editor.addEventListener("input", function() {
    _livecodeSyncPlanSourceFromEditor();
    _livecodeSchedulePlanSourceSave();
  });
  editor.addEventListener("blur", function() {
    clearTimeout(_livecodePlanSourceSaveTimer);
    _livecodeSyncPlanSourceFromEditor();
    _livecodeSavePlanSourceContent(ideActiveFile, { silent: true });
  });
  body.appendChild(editor);
  requestAnimationFrame(function() {
    try { editor.focus(); } catch (e) {}
  });
  return false;
}

function _livecodeFinishMountPlanMarkdownInMonaco(fileKey) {
  const fileInfo = ideOpenFiles[fileKey];
  const view = document.getElementById("ide-plan-view");
  const body = document.getElementById("ide-plan-body");
  const header = document.getElementById("ide-plan-header");
  const monacoSurface = document.getElementById("ide-monaco");
  const codeEditor = document.getElementById("ide-code-editor");
  const placeholder = document.getElementById("ide-editor-placeholder");
  if (!fileInfo || !view || !monacoSurface) return false;
  if (ideActiveFile !== fileKey || fileInfo.viewMode !== "markdown") return false;
  if (placeholder) placeholder.style.display = "none";
  if (body) {
    body.classList.remove("is-source");
    body.innerHTML = "";
  }
  view.classList.add("is-monaco-source");
  if (codeEditor) codeEditor.style.display = "none";
  if (!window.ideEditor || !window.monaco || !window.monaco.editor) {
    return _livecodeFallbackPlanMarkdownTextarea(fileInfo, body, monacoSurface, view);
  }
  const top = header ? Math.max(header.offsetHeight, 38) : 38;
  monacoSurface.style.display = "block";
  monacoSurface.style.top = top + "px";
  monacoSurface.style.zIndex = "1";
  const detectedLang = typeof window.inferLangFromPath === "function"
    ? window.inferLangFromPath(fileInfo.name || fileKey)
    : "markdown";
  const monacoLang = typeof window.mapToMonacoLang === "function"
    ? window.mapToMonacoLang(detectedLang)
    : "markdown";
  if (!fileInfo.model) {
    try {
      const uri = (fileInfo.path || fileKey).startsWith("/")
        ? monaco.Uri.file(fileInfo.path || fileKey)
        : monaco.Uri.parse("inmemory://ide/" + encodeURIComponent(String(fileKey).replace(/^\/+/, "")));
      fileInfo.model = monaco.editor.createModel(fileInfo.content || "", monacoLang, uri);
    } catch (e) {
      try {
        fileInfo.model = monaco.editor.createModel(fileInfo.content || "", monacoLang);
      } catch (e2) {
        console.error("LiveCode: could not create Monaco model for markdown", e2);
        return _livecodeFallbackPlanMarkdownTextarea(fileInfo, body, monacoSurface, view);
      }
    }
  } else {
    try {
      if (fileInfo.model.getValue() !== (fileInfo.content || "")) {
        fileInfo.model.setValue(fileInfo.content || "");
      }
      monaco.editor.setModelLanguage(fileInfo.model, monacoLang);
    } catch (e) {}
  }
  try {
    window.ideEditor.setModel(fileInfo.model);
  } catch (e) {
    console.error("LiveCode: setModel failed for plan markdown", e);
    return _livecodeFallbackPlanMarkdownTextarea(fileInfo, body, monacoSurface, view);
  }
  _livecodeBindPlanMonacoAutosave();
  requestAnimationFrame(function() {
    try {
      window.ideEditor.layout();
      window.ideEditor.focus();
    } catch (e) {}
  });
  return true;
}

function _livecodeMountPlanMarkdownInMonaco(fileKey) {
  const fileInfo = ideOpenFiles[fileKey];
  const view = document.getElementById("ide-plan-view");
  const body = document.getElementById("ide-plan-body");
  const monacoSurface = document.getElementById("ide-monaco");
  if (!fileInfo || !view || !monacoSurface) return false;
  if (body) {
    body.classList.remove("is-source");
    body.innerHTML = "";
  }
  view.classList.add("is-monaco-source");
  if (!window.ideEditor) {
    try { initializeIDEEditor(); } catch (e) {
      console.error("LiveCode: Monaco init failed for plan markdown", e);
    }
  }
  if (window.ideEditor) {
    return _livecodeFinishMountPlanMarkdownInMonaco(fileKey);
  }

  let tries = 0;
  const wait = setInterval(function() {
    tries += 1;
    if (window.ideEditor) {
      clearInterval(wait);
      _livecodeFinishMountPlanMarkdownInMonaco(fileKey);
      return;
    }
    if (tries >= 50) {
      clearInterval(wait);
      _livecodeFallbackPlanMarkdownTextarea(fileInfo, body, monacoSurface, view);
    }
  }, 100);
  return false;
}

function _livecodeSavePlanSourceContent(fileKey, options) {
  const opts = options || {};
  const key = fileKey || ideActiveFile;
  const info = key ? ideOpenFiles[key] : null;
  if (!info) return Promise.resolve(false);
  const content = opts.content != null ? String(opts.content) : (info.content || "");
  if (content === (info.originalContent || "") && !opts.force) {
    info.modified = false;
    return Promise.resolve(true);
  }
  if (info.isPlan && info.planFile) {
    return fetch("/livecode/plan/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file: info.planFile,
        content: content,
        title: info.title || info.name || "",
      }),
    })
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        if (!data || !data.ok) throw new Error((data && data.error) || "Save failed");
        info.content = data.content != null ? data.content : content;
        info.originalContent = info.content;
        info.modified = false;
        if (data.title) info.title = data.title;
        updateOpenFilesList();
        if (!opts.silent) _livecodeSetPlanStatus("Plan saved");
        return true;
      })
      .catch(function(err) {
        console.error("LiveCode: plan save failed", err);
        if (!opts.silent) _livecodeSetPlanStatus("Save failed: " + (err.message || err));
        return false;
      });
  }
  if (!info.path || _livecodeIsPlanTabKey(info.path)) {
    info.content = content;
    info.originalContent = content;
    info.modified = false;
    updateOpenFilesList();
    return Promise.resolve(true);
  }
  return _livecodeIdeSocketRequest(
    "ide_write_file", { path: info.path, content: content },
    "ide_file_saved",
    function(data) { return data && data.path === info.path; }
  ).then(function(result) {
    const data = result.data;
    if (data && data.error) {
      console.error("LiveCode: markdown save failed", data.error);
      if (!opts.silent) _livecodeSetPlanStatus("Save failed: " + data.error);
      return false;
    }
    info.content = content;
    info.originalContent = content;
    info.modified = false;
    updateOpenFilesList();
    if (!opts.silent) _livecodeSetPlanStatus("Saved");
    return true;
  });
}

function _livecodeSchedulePlanSourceSave() {
  clearTimeout(_livecodePlanSourceSaveTimer);
  _livecodePlanSourceSaveTimer = setTimeout(function() {
    _livecodeSyncPlanSourceFromEditor();
    _livecodeSavePlanSourceContent(ideActiveFile, { silent: true });
  }, 800);
}

function _livecodeRenderPlanMarkdownLikeChat(body, markdown) {
  body.innerHTML = "";
  const doc = document.createElement("div");
  doc.className = "livecode-plan-md";
  body.appendChild(doc);
  const text = String(markdown || "");
  if (!text.trim()) {
    doc.innerHTML = '<p style="opacity:0.4;font-style:italic;">No content.</p>';
    return;
  }
  const diagrams = [];
  const source = text.replace(/```\s*mermaid\s*\n([\s\S]*?)```/gi, function(_m, code) {
    diagrams.push(String(code).trim());
    return "\n\n%%LIVECODE_PLAN_MERMAID_" + (diagrams.length - 1) + "%%\n\n";
  });
  window.mountLivecodeChatMarkdown(doc, source, { livecode: true, persistRaw: false, keepTableTitle: true });
  doc.querySelectorAll('a[href]').forEach(function(a) {
    const href = String(a.getAttribute("href") || "").trim();
    const target = href.replace(/#L?(\d+).*$/i, "");
    const lineMatch = href.match(/#L?(\d+)/i) || target.match(/:(\d+)$/);
    const path = target.replace(/:\d+$/, "");
    if (path && !/^[a-z][\w+.-]*:/i.test(path) && _livecodeLooksLikeFilePath(path)) {
      const link = document.createElement("span");
      link.className = "livecode-md-file-link livecode-plan-file-link";
      link.setAttribute("data-file-path", _livecodeResolveProjectFilePath(path));
      if (lineMatch) link.setAttribute("data-line-meta", "L" + lineMatch[1]);
      link.setAttribute("role", "link");
      link.setAttribute("tabindex", "0");
      link.setAttribute("title", path);
      link.textContent = a.textContent;
      a.replaceWith(link);
    } else {
      a.replaceWith(document.createTextNode(a.textContent));
    }
  });
  _livecodeDecorateFileCodeSpans(doc);
  if (!diagrams.length) return;
  doc.querySelectorAll("p").forEach(function(p) {
    const hit = /^%%LIVECODE_PLAN_MERMAID_(\d+)%%$/.exec(p.textContent.trim());
    if (!hit) return;
    const wrap = document.createElement("div");
    wrap.className = "mermaid-container";
    const node = document.createElement("div");
    node.className = "mermaid";
    node.id = LIVECODE_PLAN_MERMAID_PREFIX + "src-" + hit[1];
    node.textContent = diagrams[Number(hit[1])];
    wrap.appendChild(node);
    p.replaceWith(wrap);
  });
  if (typeof _mdpdfRenderMermaidDiagrams === "function") {
    body._mdpdfMermaidPromise = _mdpdfRenderMermaidDiagrams(doc, LIVECODE_PLAN_MERMAID_PREFIX, LIVECODE_PLAN_MERMAID_PREFIX, false, _livecodePlanMermaidThemeVariables());
  }
}

function _livecodePlanMermaidThemeVariables() {
  const view = document.getElementById("ide-plan-view");
  if (!view) return {};
  const styles = getComputedStyle(view);
  const fg = (styles.getPropertyValue("--lc-fg") || "").trim().split(/\s+/).join(", ");
  if (!fg) return {};
  const bg = (styles.getPropertyValue("--lc-bg") || "").trim() || "transparent";
  const surface = (styles.getPropertyValue("--lc-bubble-bg") || "").trim() || bg;
  const text = "rgb(" + fg + ")";
  const muted = "rgba(" + fg + ", 0.7)";
  const border = "rgba(" + fg + ", 0.28)";
  const faint = "rgba(" + fg + ", 0.08)";
  return {
    background: "transparent",
    primaryColor: surface,
    primaryTextColor: text,
    primaryBorderColor: border,
    lineColor: muted,
    secondaryColor: faint,
    tertiaryColor: surface,
    clusterBkg: faint,
    clusterBorder: border,
    edgeLabelBackground: bg,
    textColor: text,
    actorBkg: surface,
    actorBorder: border,
    actorTextColor: text,
    actorLineColor: border,
    signalColor: muted,
    signalTextColor: text,
    labelBoxBkgColor: surface,
    labelBoxBorderColor: border,
    labelTextColor: text,
    loopTextColor: text,
    noteBkgColor: surface,
    noteBorderColor: border,
    noteTextColor: text
  };
}

let _livecodePlanThemeObserver = null;
function _livecodeWatchPlanTheme() {
  if (_livecodePlanThemeObserver || typeof MutationObserver === "undefined") return;
  let lastTheme = document.body.className;
  _livecodePlanThemeObserver = new MutationObserver(function() {
    const theme = ["dark-theme", "white-theme", "pink-theme", "black-theme"].filter(function(c) {
      return document.body.classList.contains(c);
    }).join(" ");
    if (theme === lastTheme) return;
    lastTheme = theme;
    const info = ideActiveFile ? ideOpenFiles[ideActiveFile] : null;
    if (info && info.isPlan) _livecodeRenderPlanSurface(ideActiveFile);
  });
  _livecodePlanThemeObserver.observe(document.body, { attributes: true, attributeFilter: ["class"] });
}

function _livecodeRenderPlanSurface(fileKey) {
  _livecodeWatchPlanTheme();
  const info = ideOpenFiles[fileKey];
  const view = document.getElementById("ide-plan-view");
  const body = document.getElementById("ide-plan-body");
  if (!info || !view || !body) return false;
  const placeholder = document.getElementById("ide-editor-placeholder");
  const monacoSurface = document.getElementById("ide-monaco");
  const codeEditor = document.getElementById("ide-code-editor");
  if (placeholder) placeholder.style.display = "none";
  if (codeEditor) codeEditor.style.display = "none";
  view.style.display = "flex";

  const rootEl = document.getElementById("ide-plan-breadcrumb-root");
  const nameEl = document.getElementById("ide-plan-name");
  if (rootEl) rootEl.textContent = info.isPlan ? "Plans" : (livecodeProjectName || "Workspace");
  if (nameEl) nameEl.textContent = info.name || "";
  _livecodeSetPlanStatus("");

  const markdown = info.content || "";
  if (info.viewMode === "markdown") {
    _livecodeMountPlanMarkdownInMonaco(fileKey);
  } else {
    view.classList.remove("is-monaco-source");
    if (monacoSurface) {
      monacoSurface.style.display = "none";
      monacoSurface.style.top = "0";
    }
    body.classList.remove("is-source");
    if (typeof window.mountLivecodeChatMarkdown === "function") {
      _livecodeRenderPlanMarkdownLikeChat(body, info.isPlan ? _livecodePlanMarkdownWithoutChecklist(markdown) : markdown);
      if (info.isPlan) _livecodeMountPlanTodos(body, fileKey);
    } else if (typeof window.renderMarkdownLikeMdpdfPreview === "function") {
      window.renderMarkdownLikeMdpdfPreview(body, markdown, LIVECODE_PLAN_MERMAID_PREFIX);
    } else if (typeof window.renderMarkdownInElement === "function") {
      window.renderMarkdownInElement(body, markdown);
    } else {
      body.textContent = markdown;
    }
    body.scrollTop = 0;
  }
  updateIdePlanChrome();
  return true;
}

function _livecodeSetPlanStatus(message) {
  const el = document.getElementById("ide-plan-status");
  if (!el) return;
  if (!message) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.textContent = message;
  el.style.display = "inline-flex";
  clearTimeout(el._hideTimer);
  el._hideTimer = setTimeout(function() {
    el.style.display = "none";
    el.textContent = "";
  }, 4000);
}

function updateIdePlanChrome() {
  const info = _livecodeActiveFileInfo();
  const isPlan = !!(info && (info.isPlan || _livecodeIsPlanFileName(info.name)));
  const buildBtn = document.getElementById("ide-plan-build");
  if (buildBtn) buildBtn.style.display = isPlan ? "inline-flex" : "none";
  if (isPlan) _livecodeSyncPlanHeaderBuildButton();
  const previewBtn = document.getElementById("ide-md-preview-button");

  if (previewBtn) previewBtn.style.display = "none";
  const menu = document.getElementById("ide-plan-menu");
  if (menu && menu.style.display !== "none") _livecodeRenderPlanMenu();
}

window.openLiveCodePlanTab = function(planFile, title, options) {
  const file = String(planFile || "").trim();
  if (!file) return Promise.resolve(false);
  const opts = options || {};
  const key = _livecodePlanTabKey(file);
  return fetch("/livecode/plan-content?file=" + encodeURIComponent(file))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (!data || !data.ok) throw new Error((data && data.error) || "Plan not found");
      const existing = ideOpenFiles[key];
      const content = data.content || "";
      ideOpenFiles[key] = {
        isPlan: true,
        planFile: file,
        path: key,
        name: file,
        title: data.title || title || file,
        content: content,
        originalContent: content,
        modified: false,
        viewMode: (existing && existing.viewMode) || "preview",
      };
      if (opts.activate === false && ideActiveFile !== key) {
        updateOpenFilesList();
      } else {
        switchToFile(key);
      }
      updatePlayButtonVisibility();
      if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
      return true;
    })
    .catch(function(err) {
      console.error("LiveCode: could not open plan", file, err);
      return false;
    });
};

window.setLiveCodePlanViewMode = function(mode) {
  const info = _livecodeActiveFileInfo();
  if (!info) return;
  clearTimeout(_livecodePlanSourceSaveTimer);
  _livecodeSyncPlanSourceFromEditor();
  const next = mode === "markdown" ? "markdown" : "preview";
  const finish = function() {
    info.viewMode = next;
    _livecodeClosePlanMenu();
    const planBody = document.getElementById("ide-plan-body");
    const planView = document.getElementById("ide-plan-view");
    if (planBody) planBody.style.transition = "none";
    if (planView) planView.style.transition = "none";
    switchToFile(ideActiveFile);
    requestAnimationFrame(function() {
      if (planBody) planBody.style.transition = "";
      if (planView) planView.style.transition = "";
    });
  };
  if (info.modified) {
    _livecodeSavePlanSourceContent(ideActiveFile, { silent: true }).then(finish);
  } else {
    finish();
  }
};

window.toggleLiveCodeMarkdownPreview = function() {
  const info = _livecodeActiveFileInfo();
  if (!info || !_livecodeIsMarkdownFileName(info.name)) return;
  window.setLiveCodePlanViewMode(info.viewMode === "preview" ? "markdown" : "preview");
};

function _livecodeRenderPlanMenu() {
  const menu = document.getElementById("ide-plan-menu");
  const info = _livecodeActiveFileInfo();
  if (!menu || !info) return;
  const mode = info.viewMode === "markdown" ? "markdown" : "preview";
  const check = '<svg class="ide-plan-menu-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>';
  const modeItem = function(value, label) {
    return '<button type="button" class="ide-plan-menu-item theme-transition" role="menuitemradio" aria-checked="'
      + (mode === value) + '" onclick="setLiveCodePlanViewMode(\'' + value + '\'); return false;">'
      + '<span>' + label + '</span>' + (mode === value ? check : "") + "</button>";
  };
  let html = '<div class="ide-plan-menu-label">Editor Mode</div>';
  html += modeItem("preview", "Preview");
  html += modeItem("markdown", "Markdown");
  html += '<div class="ide-plan-menu-sep"></div>';
  if (info.isPlan) {
    html += '<button type="button" class="ide-plan-menu-item theme-transition" role="menuitem" onclick="saveLiveCodePlanToWorkspace(); return false;"><span>Save to Workspace</span></button>';
  }
  html += '<button type="button" class="ide-plan-menu-item theme-transition" role="menuitem" onclick="downloadLiveCodeMarkdownPdf(); return false;"><span>Download PDF</span></button>';
  menu.innerHTML = html;
}

function _livecodeClosePlanMenu() {
  const menu = document.getElementById("ide-plan-menu");
  const btn = document.getElementById("ide-plan-menu-btn");
  if (menu) menu.style.display = "none";
  if (btn) btn.setAttribute("aria-expanded", "false");
}

window.toggleLiveCodePlanMenu = function(event) {
  if (event) event.stopPropagation();
  const menu = document.getElementById("ide-plan-menu");
  const btn = document.getElementById("ide-plan-menu-btn");
  if (!menu || !btn) return;
  if (menu.style.display === "block") {
    _livecodeClosePlanMenu();
    return;
  }
  _livecodeRenderPlanMenu();
  menu.style.display = "block";
  btn.setAttribute("aria-expanded", "true");
  if (!window._livecodePlanMenuGlobalBound) {
    window._livecodePlanMenuGlobalBound = true;
    document.addEventListener("click", function(e) {
      const open = document.getElementById("ide-plan-menu");
      if (!open || open.style.display !== "block") return;
      if (e.target.closest("#ide-plan-menu") || e.target.closest("#ide-plan-menu-btn")) return;
      _livecodeClosePlanMenu();
    });
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") _livecodeClosePlanMenu();
    });
  }
};

window.saveLiveCodePlanToWorkspace = function() {
  const info = _livecodeActiveFileInfo();
  if (!info || !info.isPlan || !info.planFile) return;
  if (!livecodeProjectPath) {
    _livecodeSetPlanStatus("Open a project folder first");
    return;
  }
  _livecodeClosePlanMenu();
  clearTimeout(_livecodePlanSourceSaveTimer);
  _livecodeSyncPlanSourceFromEditor();
  const doSave = function() {
    fetch("/livecode/plan/save-to-workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: info.planFile, project_path: livecodeProjectPath }),
    })
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        if (!data || !data.ok) throw new Error((data && data.error) || "Save failed");
        _livecodeSetPlanStatus("Saved to " + data.relative_path);
        if (livecodeProjectPath) loadIDEFileTree(livecodeProjectPath);
      })
      .catch(function(err) {
        _livecodeSetPlanStatus("Save failed: " + (err.message || err));
      });
  };
  _livecodeSavePlanSourceContent(ideActiveFile, { silent: true }).then(doSave);
};

window.downloadLiveCodeMarkdownPdf = function() {
  const info = _livecodeActiveFileInfo();
  if (!info) return;
  _livecodeClosePlanMenu();
  if (typeof window.downloadMarkdownAsPdf !== "function") return;

  if (info.viewMode === "markdown") {
    info.viewMode = "preview";
    _livecodeRenderPlanSurface(ideActiveFile);
  }
  const body = document.getElementById("ide-plan-body");
  if (!body) return;
  const fileName = String(info.name || "document").replace(/\.(plan\.md|md|markdown)$/i, "") + ".pdf";
  window.downloadMarkdownAsPdf(body, fileName);
};

window.buildLiveCodePlan = function() {
  const info = _livecodeActiveFileInfo();
  if (!info || !info.isPlan || !info.planFile) return;
  if (!livecodeProjectPath) {
    _livecodeSetPlanStatus("Open a project folder first");
    return;
  }
  const tab = _livecodeGetActiveChatTab();
  if (tab && tab.agentRunning) {
    _livecodeSetPlanStatus("Agent is still running");
    return;
  }
  clearTimeout(_livecodePlanSourceSaveTimer);
  _livecodeSyncPlanSourceFromEditor();
  const startBuild = function() {
    _livecodeStartPlanBuild(info.planFile, info.title || info.name);
  };
  _livecodeSavePlanSourceContent(ideActiveFile, { silent: true, force: !!info.modified }).then(function(ok) {
    if (info.modified && !ok) {
      _livecodeSetPlanStatus("Save plan edits before building");
      return;
    }
    startBuild();
  });
};

document.addEventListener("keydown", function(e) {
  if (!(e.metaKey || e.ctrlKey) || e.key !== "Enter") return;
  const view = document.getElementById("ide-plan-view");
  if (!view || view.style.display === "none") return;
  const info = _livecodeActiveFileInfo();
  if (!info || !info.isPlan) return;
  if (e.target && e.target.closest && e.target.closest(".chatbot-composer")) return;
  e.preventDefault();
  window.buildLiveCodePlan();
});

let _livecodeIdeToastEl = null;
let _livecodeIdeToastTimer = null;

function _livecodeShowIdeToast(message) {
  if (!_livecodeIdeToastEl) {
    _livecodeIdeToastEl = document.createElement("div");
    _livecodeIdeToastEl.className = "chat-history-menu theme-transition";
    _livecodeIdeToastEl.style.position = "fixed";
    _livecodeIdeToastEl.style.top = "auto";
    _livecodeIdeToastEl.style.right = "auto";
    _livecodeIdeToastEl.style.bottom = "20px";
    _livecodeIdeToastEl.style.left = "50%";
    _livecodeIdeToastEl.style.transform = "translateX(-50%)";
    _livecodeIdeToastEl.style.zIndex = "10060";
    _livecodeIdeToastEl.style.maxWidth = "60vw";
    _livecodeIdeToastEl.style.fontSize = "12px";
    _livecodeIdeToastEl.style.fontWeight = "600";
    _livecodeIdeToastEl.style.display = "none";
    document.body.appendChild(_livecodeIdeToastEl);
  }
  _livecodeIdeToastEl.textContent = message;
  _livecodeIdeToastEl.style.display = "flex";
  if (_livecodeIdeToastTimer) clearTimeout(_livecodeIdeToastTimer);
  _livecodeIdeToastTimer = setTimeout(function() {
    if (_livecodeIdeToastEl) _livecodeIdeToastEl.style.display = "none";
  }, 4000);
}

function _livecodeNormalizeLineNumber(value) {
  const n = parseInt(value, 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function _livecodeRevealEditorLine(lineNumber) {
  const line = _livecodeNormalizeLineNumber(lineNumber);
  if (!line) return;
  if (window.ideEditor) {
    try {
      window.ideEditor.setPosition({ lineNumber: line, column: 1 });
      window.ideEditor.revealLineInCenter(line);
      window.ideEditor.focus();
      return;
    } catch (_) {}
  }
  const codeEditor = document.getElementById("ide-code-editor");
  if (codeEditor && codeEditor.style.display !== "none") {
    const lines = String(codeEditor.value || "").split("\n");
    let offset = 0;
    for (let i = 0; i < Math.min(line - 1, lines.length); i++) {
      offset += lines[i].length + 1;
    }
    codeEditor.focus();
    try { codeEditor.setSelectionRange(offset, offset); } catch (_) {}
  }
}

function openFileInEditorFromPath(filePath, options) {
  const opts = options || {};
  if (ideOpenFiles[filePath]) {
    switchToFile(filePath, opts);
    return;
  }
  _livecodeIdeSocketRequest(
    "ide_read_file", { path: filePath },
    "ide_file_content",
    function(data) { return data && data.path === filePath; }
  ).then(function(result) {
    const data = result.data;
    if (data.error) {
      console.error("Error reading file:", data.error);
      _livecodeShowIdeToast(
        result.error === "timeout"
          ? "Timed out opening " + filePath.split("/").pop()
          : "Couldn't open " + filePath.split("/").pop() + ": " + data.error
      );
      return;
    }
    const content = data.content || "";
    const fileName = filePath.split("/").pop();
    ideOpenFiles[filePath] = {
      content: content,
      path: filePath,
      name: fileName,
      modified: false,
      originalContent: content,
      readOnlyLarge: !!data.large_file,
    };
    if (data.large_file) {
      _livecodeShowIdeToast("Large file opened read-only: " + fileName);
    }
    switchToFile(filePath, opts);
    updatePlayButtonVisibility();
    if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
    if (opts && opts.lineNumber) {
      _livecodeRevealEditorLine(opts.lineNumber);
    }
  });
}

function _livecodeUpdateOpenFileTabBadge(filePath) {
  const filesList = document.getElementById("ide-open-files-list");
  if (!filesList) return;
  const file = ideOpenFiles[filePath];
  if (!file) return;
  const item = Array.from(filesList.querySelectorAll(".ide-open-file-item")).find(function(el) {
    return el.dataset.path === filePath;
  });
  if (!item) {
    updateOpenFilesList(true);
    return;
  }
  item.classList.toggle("active", ideActiveFile === filePath);
  item.classList.toggle("is-dirty", !!file.modified);
}

function updateOpenFilesList(forceRebuild) {
  _livecodeSyncTreeActiveFile();
  const filesList = document.getElementById("ide-open-files-list");
  if (!filesList) return;
  const openPaths = Object.keys(ideOpenFiles);
  if (openPaths.length === 0) {
    filesList.innerHTML = "";
    filesList.style.display = "none";
    return;
  }
  filesList.style.display = "flex";
  if (!forceRebuild) {
    const existingItems = filesList.querySelectorAll(".ide-open-file-item");
    if (existingItems.length === openPaths.length) {
      const existingPaths = new Set(Array.from(existingItems).map(function(el) { return el.dataset.path; }));
      if (openPaths.every(function(p) { return existingPaths.has(p); })) {
        openPaths.forEach(function(filePath) {
          _livecodeUpdateOpenFileTabBadge(filePath);
        });
        return;
      }
    }
  }
  filesList.innerHTML = "";
  openPaths.forEach(filePath => {
    const file = ideOpenFiles[filePath];
    const isActive = ideActiveFile === filePath;
    const fileItem = document.createElement("div");
    fileItem.className = `ide-open-file-item theme-transition${isActive ? " active" : ""}`;
    fileItem.style.cssText = "cursor:pointer;display:flex;align-items:center;white-space:nowrap;user-select:none;";
    fileItem.dataset.path = filePath;
    const iconPath = file.isSettings ? _LIVECODE_SETTINGS_TAB_ICON : file.isBrowser ? _LIVECODE_BROWSER_TAB_ICON : _livecodeFileIcon(file.name);
    const iconImg = document.createElement("img");
    iconImg.src = iconPath;
    iconImg.alt = "File";
    iconImg.style.cssText = "width:16px;height:16px;flex-shrink:0;display:block;";
    iconImg.onerror = function() {
      this.src = _livecodeFileIcon("");
      this.onerror = null;
    };
    const fileNameSpan = document.createElement("span");
    fileNameSpan.textContent = file.name;
    fileNameSpan.className = "ide-open-file-label theme-transition";
    if (file.modified) fileItem.classList.add("is-dirty");
    const closeBtn = document.createElement("span");
    closeBtn.innerHTML = _livecodeIcon("x", { size: "lg" });
    closeBtn.className = "ide-file-close theme-transition";
    closeBtn.title = "Close";
    closeBtn.onclick = e => {
      e.stopPropagation();
      closeFile(filePath);
    };
    fileItem.appendChild(iconImg);
    fileItem.appendChild(fileNameSpan);
    fileItem.appendChild(closeBtn);
    fileItem.onclick = () => switchToFile(filePath);
    filesList.appendChild(fileItem);
  });
}

function switchToFile(filePath, options) {
  if (!ideOpenFiles[filePath]) return;
  const opts = options || {};
  if (ideOpenFiles[filePath].isSettings) {
    ideActiveFile = filePath;
    _livecodeHideBrowserSurface();
    _livecodeShowSettingsSurface();
    updateOpenFilesList();
    updatePlayButtonVisibility();
    if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
    return;
  }
  if (ideOpenFiles[filePath].isBrowser) {
    ideActiveFile = filePath;
    _livecodeShowBrowserSurface();
    updateOpenFilesList();
    updatePlayButtonVisibility();
    if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
    return;
  }
  _livecodeHideSettingsSurface();
  _livecodeHideBrowserSurface();
  if (_livecodeFileUsesPlanSurface(ideOpenFiles[filePath])) {
    ideActiveFile = filePath;
    _livecodeRenderPlanSurface(filePath);
    updateOpenFilesList();
    if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
    return;
  }
  _livecodeHidePlanSurface();
  const placeholder = document.getElementById("ide-editor-placeholder");
  const codeEditor = document.getElementById("ide-code-editor");
  if (placeholder) placeholder.style.display = "none";
  const monacoSurface = document.getElementById("ide-monaco");
  if (monacoSurface && window.ideEditor && window.monaco && window.monaco.editor) {
    if (codeEditor) codeEditor.style.display = "none";
    monacoSurface.style.display = "block";
    const fileInfo = ideOpenFiles[filePath];
    const detectedLang = typeof window.inferLangFromPath === "function" ? window.inferLangFromPath(fileInfo.name || filePath) : "text";
    let monacoLang = typeof window.mapToMonacoLang === "function" ? window.mapToMonacoLang(detectedLang) : "plaintext";
    if (fileInfo.readOnlyLarge) {
      monacoLang = "plaintext";
    }
    if (!fileInfo.model) {
      try {
        const uri = filePath.startsWith("/")
          ? monaco.Uri.file(filePath)
          : monaco.Uri.parse("inmemory://ide/" + encodeURIComponent(filePath.replace(/^\/+/, "")));
        const existing = monaco.editor.getModel(uri);
        if (existing) {
          fileInfo.model = existing;
          if (existing.getValue() !== (fileInfo.content || "")) {
            existing.setValue(fileInfo.content || "");
          }
          if (existing.getLanguageId() !== monacoLang) {
            try { monaco.editor.setModelLanguage(existing, monacoLang); } catch (_) {}
          }
        } else {
          fileInfo.model = monaco.editor.createModel(fileInfo.content || "", monacoLang, uri);
        }
      } catch (e) {
        try {
          fileInfo.model = monaco.editor.createModel(fileInfo.content || "", monacoLang);
        } catch (e2) {
          if (codeEditor) {
            codeEditor.style.display = "block";
            codeEditor.value = fileInfo.content || "";
            codeEditor.readOnly = !!fileInfo.readOnlyLarge;
            return;
          }
        }
      }
    } else {
      const modelContent = fileInfo.model.getValue();
      const desiredContent = fileInfo.content || "";
      if (modelContent !== desiredContent) {
        fileInfo.model.setValue(desiredContent);
      }
      if (fileInfo.model.getLanguageId() !== monacoLang) {
        try {
          monaco.editor.setModelLanguage(fileInfo.model, monacoLang);
        } catch (e) {}
      }
    }
    try {
      window.ideEditor.setModel(fileInfo.model);
      window.ideEditor.updateOptions({ readOnly: !!fileInfo.readOnlyLarge });
      try { if (window.WBLsp && !fileInfo.readOnlyLarge) window.WBLsp.onFileOpened(filePath, fileInfo.model); } catch (_) {}
      requestAnimationFrame(function() {
        try { window.ideEditor.layout(); } catch (_) {}
      });
    } catch (e) {
      if (codeEditor) {
        codeEditor.style.display = "block";
        codeEditor.value = fileInfo.content || "";
        codeEditor.readOnly = !!fileInfo.readOnlyLarge;
        return;
      }
    }
  } else if (monacoSurface) {
    if (codeEditor) {
      codeEditor.style.display = "block";
      const fileInfo = ideOpenFiles[filePath];
      codeEditor.value = fileInfo.content || "";
      codeEditor.readOnly = !!fileInfo.readOnlyLarge;
    }
    _livecodeUpgradeEditorToMonacoWhenReady(filePath);
  } else if (codeEditor) {
    codeEditor.style.display = "block";
    const fileInfo = ideOpenFiles[filePath];
    codeEditor.value = fileInfo.content || "";
    codeEditor.readOnly = !!fileInfo.readOnlyLarge;
  }
  ideActiveFile = filePath;
  updateOpenFilesList();
  updatePlayButtonVisibility();
  if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
  if (opts && opts.lineNumber) {
    _livecodeRevealEditorLine(opts.lineNumber);
  }
}

let _livecodeMonacoUpgradeTimer = null;

function _livecodeUpgradeEditorToMonacoWhenReady(filePath) {
  function _mountNow() {
    const target = (ideActiveFile && ideOpenFiles[ideActiveFile]) ? ideActiveFile : filePath;
    if (ideOpenFiles[target] && !_livecodeFileUsesPlanSurface(ideOpenFiles[target])) {
      switchToFile(target);
    }
  }
  if (!window.ideEditor) {
    try { initializeIDEEditor(); } catch (e) { console.error("LiveCode: Monaco init failed", e); }
  }
  if (window.ideEditor && window.monaco && window.monaco.editor) {
    _mountNow();
    return;
  }
  if (_livecodeMonacoUpgradeTimer) return;
  let tries = 0;
  _livecodeMonacoUpgradeTimer = setInterval(function() {
    tries += 1;
    if (window.ideEditor && window.monaco && window.monaco.editor) {
      clearInterval(_livecodeMonacoUpgradeTimer);
      _livecodeMonacoUpgradeTimer = null;
      _mountNow();
    } else if (tries >= 100) {
      clearInterval(_livecodeMonacoUpgradeTimer);
      _livecodeMonacoUpgradeTimer = null;
    }
  }, 100);
}

function autoSaveIDEFile(filePath) {
  if (!filePath || !ideOpenFiles[filePath] || !window.ideEditor) {
    return;
  }
  const file = ideOpenFiles[filePath];
  if (file.readOnlyLarge || file.isSettings || file.isBrowser) return;

  if (_livecodeFileUsesPlanSurface(file) && file.viewMode === "markdown") {
    _livecodeSyncPlanSourceFromEditor();
    _livecodeSavePlanSourceContent(filePath, { silent: true });
    return;
  }
  const content = window.ideEditor.getValue();
  if (content === file.originalContent) {
    return;
  }
  _livecodeIdeSocketRequest(
    "ide_write_file", { path: file.path, content: content },
    "ide_file_saved",
    function(data) { return data && data.path === file.path; }
  ).then(function(result) {
    const data = result.data;
    if (data.error) {
      console.error("Auto-save error:", data.error);
      if (result.error) _livecodeShowIdeToast("Auto-save failed for " + (file.name || file.path.split("/").pop()));
    } else {
      file.originalContent = content;
      file.modified = false;
      _livecodeUpdateOpenFileTabBadge(filePath);
    }
  });
}

function showIDEPanel(panelName) {
  const recentBtn = document.getElementById("ide-activity-recent");
  if (recentBtn) {
    const recentActive = panelName === "recent" || panelName === "settings";
    recentBtn.classList.toggle("active", recentActive);
    recentBtn.setAttribute("aria-pressed", recentActive ? "true" : "false");
  }
  const explorerPanel = document.getElementById("ide-explorer-panel");
  const recentPanel = document.getElementById("ide-recent-panel");
  const panelContent = document.getElementById("ide-panel-content");
  if (explorerPanel) explorerPanel.style.display = "none";
  if (recentPanel) recentPanel.style.display = "none";
  if (panelName === "explorer") {
    if (explorerPanel) explorerPanel.style.display = "block";
    if (recentPanel) recentPanel.style.display = "none";
    if (panelContent) {
      panelContent.style.overflowX = "auto";
      panelContent.style.overflowY = "auto";
    }
  } else if (panelName === "recent" || panelName === "settings") {
    if (explorerPanel) explorerPanel.style.display = "none";
    if (recentPanel) {
      recentPanel.style.display = "block";
      renderLiveCodeRecentProjects();
    }
    if (panelContent) {
      panelContent.style.overflowX = "hidden";
      panelContent.style.overflowY = "auto";
    }
  } else {
    if (explorerPanel) explorerPanel.style.display = "block";
    if (recentPanel) recentPanel.style.display = "none";
    if (panelContent) {
      panelContent.style.overflowX = "auto";
      panelContent.style.overflowY = "auto";
    }
  }
}

function closeFile(filePath) {
  if (!ideOpenFiles[filePath]) return;
  try { if (window.WBLsp) window.WBLsp.onFileClosed(filePath); } catch (e) {}
  delete ideOpenFiles[filePath];
  updateOpenFilesList();
  const openPaths = Object.keys(ideOpenFiles);
  if (openPaths.length > 0) {
    switchToFile(openPaths[openPaths.length - 1]);
  } else {
    showLiveCodeEditorIdle();
  }
  updatePlayButtonVisibility();
  if (livecodeProjectPath) _livecodePersistEditorTabsDebounced(livecodeProjectPath);
}

let ideContextMenuTarget = null;

function _livecodeToRepoPath(absPath) {
  const project = livecodeProjectPath || "";
  if (!project || !absPath) return "";
  const norm = function(p) { return String(p || "").replace(/\\/g, "/"); };
  let rel = norm(absPath);
  const root = norm(project).replace(/\/$/, "");
  if (rel === root) return "";
  if (rel.startsWith(root + "/")) rel = rel.slice(root.length + 1);
  return rel;
}

function _livecodeWorkspaceRepoPath(path) {
  const full = _livecodeNormalizePath(path);
  const folders = livecodeWorkspaceFolders.length
    ? livecodeWorkspaceFolders
    : (
        livecodeProjectPath
          ? [{
              name: _livecodeFolderNameFromPath(livecodeProjectPath),
              path: livecodeProjectPath
            }]
          : []
      );

  const matches = folders.map(function(folder) {
    const root = _livecodeNormalizePath((folder || {}).path);
    return { folder: folder || {}, root: root };
  }).filter(function(item) {
    return item.root && (full === item.root || full.startsWith(item.root + "/"));
  }).sort(function(a, b) {
    return b.root.length - a.root.length;
  });

  if (!matches.length) return _livecodeToRepoPath(path);

  const match = matches[0];
  const rel = full === match.root ? "" : full.slice(match.root.length + 1);
  if (folders.length <= 1) return rel;

  const name = String(
    match.folder.name || _livecodeFolderNameFromPath(match.root)
  ).trim();
  return rel ? name + "/" + rel : name;
}

let _livecodeFsClipboard = null;

function _livecodeSyncCutMarks() {
  const cutPath = _livecodeFsClipboard && _livecodeFsClipboard.mode === "move" ? _livecodeFsClipboard.path : null;
  document.querySelectorAll("[data-ide-path]").forEach(function(el) {
    el.classList.toggle("ide-cut-pending", cutPath !== null && el.dataset.idePath === cutPath);
  });
}

const _IDE_CTX_ICONS = {
  open: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>',
  newFile: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline>',
  newFolder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>',
  cut: '<circle cx="6" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><line x1="20" y1="4" x2="8.12" y2="15.88"></line><line x1="14.47" y1="14.48" x2="20" y2="20"></line><line x1="8.12" y1="8.12" x2="12" y2="12"></line>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>',
  paste: '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>',
  duplicate: '<rect x="8" y="8" width="12" height="12" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path>',
  path: '<polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line>',
  tag: '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path><line x1="7" y1="7" x2="7.01" y2="7"></line>',
  reveal: '<path d="M4 9V6.47214C4 6.16165 4.07229 5.85542 4.21115 5.57771L5 4H10L11 6H21C21.5523 6 22 6.44772 22 7V9V18C22 19.1046 21.1046 20 20 20H18"></path><path d="M17.2362 9H2.30925C1.64988 9 1.17099 9.62698 1.34449 10.2631L3.59806 18.5262C3.83537 19.3964 4.62569 20 5.52759 20H19.6908C20.3501 20 20.829 19.373 20.6555 18.7369L18.201 9.73688C18.0823 9.30182 17.6872 9 17.2362 9Z"></path>',
  rename: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z"></path>',
  trash: '<polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>',
  workspace: '<path d="M3 6h18"></path><path d="M3 12h18"></path><path d="M3 18h18"></path><path d="M7 3v18"></path>',
};

function _ideCtxIcon(name) {
  const body = _IDE_CTX_ICONS[name] || _IDE_CTX_ICONS.file;
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
}

function _ideCtxItemHtml(icon, label, action, opts) {
  const o = opts || {};
  if (o.disabled) {
    return '<div class="ide-context-menu-item is-disabled" aria-disabled="true">' + _ideCtxIcon(icon) + '<span>' + label + '</span></div>';
  }
  const cls = o.danger ? "ide-context-menu-item is-danger" : "ide-context-menu-item";
  return '<div class="' + cls + '" onclick="ideContextMenuAction(\'' + action + '\');">' + _ideCtxIcon(icon) + '<span>' + label + '</span></div>';
}

function _ideCtxSeparator() {
  return '<div class="ide-context-menu-separator" role="separator"></div>';
}

function showIDEContextMenu(e, itemPath, itemName, isDir) {
  const contextMenu = document.getElementById("ide-context-menu");
  if (!contextMenu) return;
  e.preventDefault();
  e.stopPropagation();
  const repoPath = _livecodeWorkspaceRepoPath(itemPath);
  const workspaceRootIndex = isDir ? _livecodeWorkspaceFolderIndexByPath(itemPath) : -1;
  ideContextMenuTarget = {
    path: itemPath,
    repoPath: repoPath,
    name: itemName,
    isDir: !!isDir,
    workspaceRootIndex: workspaceRootIndex,
  };
  const clip = _livecodeFsClipboard;
  const pasteDisabled = !clip;
  const pasteLabel = "Paste";
  const rows = [];
  if (isDir) {
    if (workspaceRootIndex > 0) {
      rows.push(_ideCtxItemHtml("workspace", "Make primary", "workspace-make-primary"));
    }
    if (workspaceRootIndex >= 0 && livecodeWorkspaceFolders.length > 1) {
      rows.push(_ideCtxItemHtml("close", "Remove from workspace", "workspace-remove-folder"));
    }
    if (workspaceRootIndex >= 0) {
      rows.push(_ideCtxSeparator());
    }
    rows.push(_ideCtxItemHtml("folder", "Add folder to chat", "add-folder-to-chat"));
    rows.push(_ideCtxItemHtml("chat", "Add to new Chat", "add-to-new-chat"));
    rows.push(_ideCtxSeparator());
    rows.push(_ideCtxItemHtml("newFile", "New File…", "new-file"));
    rows.push(_ideCtxItemHtml("newFolder", "New Folder…", "new-folder"));
    rows.push(_ideCtxSeparator());
  } else {
    rows.push(_ideCtxItemHtml("open", "Open", "open-file"));
    rows.push(_ideCtxItemHtml("file", "Add file to chat", "add-file-to-chat"));
    rows.push(_ideCtxItemHtml("chat", "Add to new Chat", "add-to-new-chat"));
    rows.push(_ideCtxSeparator());
  }
  rows.push(_ideCtxItemHtml("cut", "Cut", "fs-cut"));
  rows.push(_ideCtxItemHtml("copy", "Copy", "fs-copy"));
  rows.push(_ideCtxItemHtml("paste", pasteLabel, "fs-paste", { disabled: pasteDisabled }));
  rows.push(_ideCtxItemHtml("duplicate", "Duplicate", "fs-duplicate"));
  rows.push(_ideCtxSeparator());
  rows.push(_ideCtxItemHtml("reveal", _livecodeRevealLabel(), "reveal-in-os"));
  rows.push(_ideCtxItemHtml("path", "Copy Path", "copy-path"));
  rows.push(_ideCtxItemHtml("path", "Copy Relative Path", "copy-relative-path"));
  rows.push(_ideCtxSeparator());
  rows.push(_ideCtxItemHtml("rename", "Rename…", "rename"));
  rows.push(_ideCtxItemHtml("trash", "Delete", "delete"));

  contextMenu.innerHTML = rows.join("");
  contextMenu.style.display = "block";
  contextMenu.style.left = e.clientX + "px";
  contextMenu.style.top = e.clientY + "px";
  const rect = contextMenu.getBoundingClientRect();
  let left = e.clientX;
  if (left + rect.width > window.innerWidth - 8) left = window.innerWidth - 8 - rect.width;
  left = Math.max(8, left);
  let top = e.clientY;
  if (top + rect.height > window.innerHeight - 8) top = window.innerHeight - 8 - rect.height;
  top = Math.max(8, top);
  contextMenu.style.left = left + "px";
  contextMenu.style.top = top + "px";
  const closeMenu = function(event) {
    if (!contextMenu.contains(event.target)) {
      contextMenu.style.display = "none";
      document.removeEventListener("click", closeMenu);
    }
  };
  setTimeout(function() {
    document.addEventListener("click", closeMenu);
  }, 100);
}

function showFolderContextMenu(e, folderPath, folderName) {
  showIDEContextMenu(e, folderPath, folderName, true);
}

function _livecodeAddRepoContextToActiveChat(target) {
  if (!target || typeof window.addLivecodeRepoContextToChat !== "function") return false;
  return !!window.addLivecodeRepoContextToChat({
    repoPath: target.repoPath,
    kind: target.isDir ? "folder" : "file",
    name: target.name,
  });
}

function _livecodeRefreshIdeTree() {
  if (!livecodeProjectPath) return;
  ideFileTreeData = {};
  if (livecodeWorkspaceFolders.length) {
    _livecodeLoadWorkspaceFolderTrees();
    return;
  }
  loadIDEFileTree(livecodeProjectPath);
}

const LIVECODE_TREE_MUTATING_TOOLS = new Set(["write_file", "edit_file", "multi_edit", "run_command", "checkout_branch"]);
let _livecodeTreeRefreshTimer = null;
let _livecodeTreeRefreshRunning = false;
let _livecodeTreeRefreshQueued = false;

function _livecodeCollectLoadedTreeDirs(nodes, out) {
  Object.keys(nodes || {}).forEach(function(name) {
    const node = nodes[name];
    if (!node || !(node.is_dir || node.type === "folder") || !node.loaded) return;
    out.push(node);
    _livecodeCollectLoadedTreeDirs(node.children, out);
  });
}

function _livecodeDropExpandedUnder(node) {
  ideExpandedFolders.delete(node.path);
  Object.keys(node.children || {}).forEach(function(name) {
    _livecodeDropExpandedUnder(node.children[name]);
  });
}

function _livecodeMergeDirListing(node) {
  const listPath = node.path;
  return _livecodeIdeSocketRequest(
    "ide_list_files", { path: listPath },
    "ide_files_list",
    function(data) {
      return data && (data.requested_path !== undefined ? data.requested_path === listPath : data.path === listPath);
    }
  ).then(function(result) {
    const data = result.data;
    if (!data || data.error || !Array.isArray(data.files)) return;
    const previous = node.children || {};
    const next = {};
    data.files.forEach(function(item) {
      const existing = previous[item.name];
      if (existing && !!existing.is_dir === !!item.is_dir) {
        existing.path = item.path;
        next[item.name] = existing;
      } else {
        next[item.name] = _livecodeTreeNodeFromListItem(item);
      }
    });
    Object.keys(previous).forEach(function(name) {
      if (next[name] !== previous[name]) _livecodeDropExpandedUnder(previous[name]);
    });
    node.children = next;
  });
}

function _livecodeRunSilentTreeRefresh() {
  if (_livecodeTreeRefreshRunning) {
    _livecodeTreeRefreshQueued = true;
    return;
  }
  const dirs = [];
  Object.keys(ideFileTreeData || {}).forEach(function(key) {
    _livecodeCollectLoadedTreeDirs(ideFileTreeData[key], dirs);
  });
  if (!dirs.length) return;
  const snapshot = ideFileTreeData;
  _livecodeTreeRefreshRunning = true;
  Promise.all(dirs.map(_livecodeMergeDirListing)).catch(function() {}).then(function() {
    _livecodeTreeRefreshRunning = false;
    if (snapshot === ideFileTreeData) _livecodeRenderWorkspaceTree();
    if (_livecodeTreeRefreshQueued) {
      _livecodeTreeRefreshQueued = false;
      _livecodeScheduleSilentTreeRefresh();
    }
  });
}

function _livecodeScheduleSilentTreeRefresh() {
  if (_livecodeTreeRefreshTimer) clearTimeout(_livecodeTreeRefreshTimer);
  _livecodeTreeRefreshTimer = setTimeout(function() {
    _livecodeTreeRefreshTimer = null;
    _livecodeRunSilentTreeRefresh();
  }, 350);
}

function _livecodeCopyToClipboard(text, label) {
  const value = String(text == null ? "" : text);
  const message = label || "Copied: " + value;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(function() {
      _livecodeShowIdeToast(message);
    }).catch(function() {
      _livecodeShowIdeToast("Couldn't copy to clipboard");
    });
    return;
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    _livecodeShowIdeToast(message);
  } catch (err) {
    _livecodeShowIdeToast("Couldn't copy to clipboard");
  }
}

function _livecodeParentDir(path) {
  const norm = String(path || "").replace(/\/+$/, "");
  const idx = norm.lastIndexOf("/");
  return idx > 0 ? norm.slice(0, idx) : norm;
}

function _livecodeFsRequest(emitEvent, payload, opName) {
  return _livecodeIdeSocketRequest(
    emitEvent, payload, "ide_fs_result",
    function(data) { return data && data.op === opName; }
  ).then(function(result) {
    const data = result.data || {};
    if (result.error || data.error) {
      _livecodeModalAlert({ title: "Couldn't complete that action", message: data.error || (opName + " failed") });
      return null;
    }
    return data;
  });
}

function _livecodeCtxNewEntry(target, kind) {
  const label = kind === "folder" ? "Folder" : "File";
  _livecodeModalPrompt({
    title: "New " + label,
    message: "Create inside " + target.name,
    placeholder: label.toLowerCase() + " name",
    confirmText: "Create",
  }).then(function(name) {
    if (!name || !name.trim()) return;
    _livecodeFsRequest("ide_create_entry", { parent: target.path, name: name.trim(), kind: kind }, "create").then(function(data) {
      if (!data) return;
      ideExpandedFolders.add(target.path);
      _livecodeRefreshIdeTree();
      if (kind === "file" && data.path) {
        setTimeout(function() { openFileInEditorFromPath(data.path); }, 150);
      }
    });
  });
}

function _livecodeCtxRename(target) {
  _livecodeModalPrompt({
    title: "Rename",
    message: "Rename “" + target.name + "”",
    value: target.name,
    confirmText: "Rename",
  }).then(function(next) {
    if (!next || !next.trim() || next.trim() === target.name) return;
    _livecodeFsRequest("ide_rename_entry", { path: target.path, new_name: next.trim() }, "rename").then(function(data) {
      if (!data) return;
      if (ideOpenFiles[target.path]) closeFile(target.path);
      _livecodeRefreshIdeTree();
      _livecodeShowIdeToast("Renamed to " + next.trim());
    });
  });
}

function _livecodeCtxDelete(target) {
  const kindWord = target.isDir ? "folder" : "file";
  _livecodeModalConfirm({
    title: "Move to Trash",
    message: "Move " + kindWord + " “" + target.name + "” to the Trash?",
    confirmText: "Move to Trash",
  }).then(function(ok) {
    if (!ok) return;
    _livecodeFsRequest("ide_delete_entry", { path: target.path }, "delete").then(function(data) {
      if (!data) return;
      if (ideOpenFiles[target.path]) closeFile(target.path);
      _livecodeRefreshIdeTree();
      _livecodeShowIdeToast("Moved to Trash: " + target.name);
    });
  });
}

function _livecodeCtxReveal(target) {
  _livecodeIdeSocketRequest(
    "ide_reveal_path", { path: target.path }, "ide_reveal_result",
    function(data) { return data && data.path === target.path; }
  ).then(function(result) {
    const data = result.data || {};
    if (result.error || data.error) {
      _livecodeModalAlert({ title: "Couldn't open the file manager", message: data.error || "Reveal failed" });
    }
  });
}

function _livecodeCtxDuplicate(target) {
  _livecodeFsRequest("ide_duplicate_entry", { path: target.path }, "duplicate").then(function(data) {
    if (!data) return;
    _livecodeRefreshIdeTree();
    _livecodeShowIdeToast("Duplicated " + target.name);
  });
}

function _livecodeCtxPaste(target) {
  const clip = _livecodeFsClipboard;
  if (!clip) return;
  const destDir = target.isDir ? target.path : _livecodeParentDir(target.path);
  _livecodeFsRequest(
    "ide_transfer_entry",
    { source: clip.path, dest_dir: destDir, mode: clip.mode },
    "transfer"
  ).then(function(data) {
    if (!data) return;
    if (clip.mode === "move" && ideOpenFiles[clip.path]) closeFile(clip.path);
    _livecodeFsClipboard = null;
    _livecodeSyncCutMarks();
    ideExpandedFolders.add(destDir);
    _livecodeRefreshIdeTree();
    _livecodeShowIdeToast((clip.mode === "move" ? "Moved " : "Copied ") + clip.name);
  });
}

function ideContextMenuAction(action) {
  const contextMenu = document.getElementById("ide-context-menu");
  if (contextMenu) {
    contextMenu.style.display = "none";
  }
  const target = ideContextMenuTarget;
  ideContextMenuTarget = null;
  if (!target) {
    if (action === "recent" || action === "settings") showIDEPanel("recent");
    return;
  }
  if (action === "workspace-make-primary") {
    _livecodeMakeWorkspaceFolderPrimary(target.path);
    return;
  }
  if (action === "workspace-remove-folder") {
    _livecodeRemoveWorkspaceFolder(target.path);
    return;
  }
  if (action === "add-file-to-chat" || action === "add-folder-to-chat") {
    _livecodeAddRepoContextToActiveChat(target);
    toggleLiveCodeAgentPane(true);
    return;
  }
  if (action === "add-to-new-chat") {
    if (typeof window.createLiveCodeChatTab === "function") {
      window.createLiveCodeChatTab();
    }
    if (typeof window.clearLivecodeComposer === "function") {
      window.clearLivecodeComposer();
    }
    _livecodeAddRepoContextToActiveChat(target);
    toggleLiveCodeAgentPane(true);
    return;
  }
  if (action === "open-file") {
    openFileInEditorFromPath(target.path);
    return;
  }
  if (action === "new-file") { _livecodeCtxNewEntry(target, "file"); return; }
  if (action === "new-folder") { _livecodeCtxNewEntry(target, "folder"); return; }
  if (action === "reveal-in-os") { _livecodeCtxReveal(target); return; }
  if (action === "fs-cut") {
    _livecodeFsClipboard = { path: target.path, name: target.name, isDir: target.isDir, mode: "move" };
    _livecodeSyncCutMarks();
    _livecodeShowIdeToast("Cut " + target.name);
    return;
  }
  if (action === "fs-copy") {
    _livecodeFsClipboard = { path: target.path, name: target.name, isDir: target.isDir, mode: "copy" };
    _livecodeSyncCutMarks();
    _livecodeShowIdeToast("Copied " + target.name);
    return;
  }
  if (action === "fs-paste") { _livecodeCtxPaste(target); return; }
  if (action === "fs-duplicate") { _livecodeCtxDuplicate(target); return; }
  if (action === "copy-path") { _livecodeCopyToClipboard(target.path); return; }
  if (action === "copy-relative-path") { _livecodeCopyToClipboard(target.repoPath || target.name); return; }
  if (action === "rename") { _livecodeCtxRename(target); return; }
  if (action === "delete") { _livecodeCtxDelete(target); return; }
  if (action === "recent" || action === "settings") {
    showIDEPanel("recent");
  }
}

function _livecodeRevealLabel() {
  const p = String(navigator.platform || navigator.userAgent || "").toLowerCase();
  if (p.indexOf("mac") !== -1) return "Reveal in Finder";
  if (p.indexOf("win") !== -1) return "Show in Explorer";
  return "Show in File Manager";
}

function _livecodeEscapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let _livecodeModalResolve = null;

function _livecodeEnsureModal() {
  let overlay = document.getElementById("ide-modal-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "ide-modal-overlay";
  overlay.className = "ide-modal-overlay theme-transition";
  overlay.hidden = true;
  overlay.innerHTML =
    '<div class="ide-modal theme-transition" role="dialog" aria-modal="true">' +
      '<button type="button" class="ide-modal-close" id="ide-modal-close" aria-label="Close"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>' +
      '<div class="ide-modal-title" id="ide-modal-title"></div>' +
      '<div class="ide-modal-message" id="ide-modal-message"></div>' +
      '<input type="text" class="ide-modal-input theme-transition" id="ide-modal-input" autocomplete="off" spellcheck="false" hidden />' +
      '<div class="ide-modal-actions">' +
        '<button type="button" class="ide-modal-btn theme-transition" id="ide-modal-cancel"></button>' +
        '<button type="button" class="ide-modal-btn theme-transition ide-modal-btn-primary" id="ide-modal-confirm"></button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  overlay.addEventListener("mousedown", function(ev) {
    if (ev.target === overlay) _livecodeCloseModal(null);
  });
  overlay.querySelector("#ide-modal-close").addEventListener("click", function() { _livecodeCloseModal(null); });
  overlay.querySelector("#ide-modal-cancel").addEventListener("click", function() { _livecodeCloseModal(null); });
  overlay.querySelector("#ide-modal-confirm").addEventListener("click", function() {
    const input = overlay.querySelector("#ide-modal-input");
    _livecodeCloseModal(input.hidden ? true : input.value);
  });
  overlay.querySelector("#ide-modal-input").addEventListener("keydown", function(ev) {
    if (ev.key === "Enter") { ev.preventDefault(); _livecodeCloseModal(ev.target.value); }
  });
  document.addEventListener("keydown", function(ev) {
    if (!overlay.hidden && ev.key === "Escape") { ev.preventDefault(); _livecodeCloseModal(null); }
  });
  return overlay;
}

function _livecodeCloseModal(value) {
  const overlay = document.getElementById("ide-modal-overlay");
  if (overlay) {
    overlay.hidden = true;
    const input = overlay.querySelector("#ide-modal-input");
    if (input) { input.value = ""; input.type = "text"; }
  }
  const resolve = _livecodeModalResolve;
  _livecodeModalResolve = null;
  if (resolve) resolve(value);
}

function _livecodeOpenModal(opts) {
  const o = opts || {};
  const overlay = _livecodeEnsureModal();
  if (_livecodeModalResolve) _livecodeModalResolve(null);
  overlay.querySelector("#ide-modal-title").textContent = o.title || "";
  const msgEl = overlay.querySelector("#ide-modal-message");
  msgEl.textContent = o.message || "";
  msgEl.hidden = !o.message;
  const input = overlay.querySelector("#ide-modal-input");
  const cancelBtn = overlay.querySelector("#ide-modal-cancel");
  const confirmBtn = overlay.querySelector("#ide-modal-confirm");
  if (o.mode === "prompt") {
    input.hidden = false;
    input.type = o.inputType || "text";
    input.value = o.value || "";
    input.placeholder = o.placeholder || "";
  } else {
    input.hidden = true;
  }
  cancelBtn.hidden = o.mode === "alert";
  cancelBtn.textContent = o.cancelText || "Cancel";
  confirmBtn.textContent = o.confirmText || (o.mode === "alert" ? "OK" : "Confirm");
  confirmBtn.classList.toggle("ide-modal-btn-danger", !!o.danger);
  overlay.classList.toggle("is-wide", !!o.wide);
  overlay.hidden = false;
  return new Promise(function(resolve) {
    _livecodeModalResolve = resolve;
    setTimeout(function() {
      if (o.mode === "prompt") { input.focus(); input.select(); }
      else confirmBtn.focus();
    }, 30);
  });
}

function _livecodeModalPrompt(opts) {
  return _livecodeOpenModal(Object.assign({ mode: "prompt" }, opts || {})).then(function(v) {
    return v == null ? null : String(v);
  });
}

function _livecodeModalConfirm(opts) {
  return _livecodeOpenModal(Object.assign({ mode: "confirm" }, opts || {})).then(function(v) {
    return v === true;
  });
}

function _livecodeModalAlert(opts) {
  return _livecodeOpenModal(Object.assign({ mode: "alert" }, opts || {})).then(function() {});
}

function getLiveCodeRecentProjects() {
  try {
    const raw = localStorage.getItem(LIVECODE_RECENT_PROJECTS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter(p => typeof p === "string" && p.length > 0) : [];
  } catch (e) {
    return [];
  }
}

function saveLiveCodeRecentProject(path) {
  if (!path) return;
  let list = getLiveCodeRecentProjects().filter(p => p !== path);
  list.unshift(path);
  list = list.slice(0, 10);
  try {
    localStorage.setItem(LIVECODE_RECENT_PROJECTS_KEY, JSON.stringify(list));
    localStorage.setItem(LIVECODE_LAST_PROJECT_KEY, path);
  } catch (e) {}
}

function _livecodeClearActiveProject() {
  const previousPath = livecodeProjectPath;
  livecodeChatTabs.forEach(function(t) {
    if (t.agentRunning) _livecodeAbortTabTurn(t);
  });
  if (previousPath) {
    try { _livecodeSaveTabsForProject(previousPath); } catch (e) {}
    try { _livecodePersistEditorTabs(previousPath); } catch (e) {}
  }
  _livecodeInvalidateSessionFetches();
  if (typeof closeLiveCodeSessionMenu === "function") closeLiveCodeSessionMenu();
  _livecodeResetTerminalsForProject();
  _livecodeClearEditorFiles();
  showLiveCodeEditorIdle();

  livecodeProjectPath = null;
  livecodeProjectName = null;
  livecodeWorkspacePath = null;
  livecodeWorkspaceFolders = [];
  livecodeWorkspaceSettings = {};
  livecodeWorkspaceMcpServers = {};
  _livecodePersistBrowserWorkspace();
  ideHomePath = "~";
  ideFileTreeData = {};
  ideExpandedFolders.clear();
  livecodeIndexReady = false;
  livecodeIndexFileCount = 0;
  livecodeIndexSymbolCount = 0;
  livecodeIndexTruncated = false;

  livecodeChatTabs = [];
  livecodeActiveChatTabId = null;
  livecodeChatTabCounter = 0;
  livecodeAgentSessionId = _livecodeNewChatSessionId();
  _livecodeInitChatTabs();
  _livecodeUpdateChatWelcome();
  _livecodeRenderChatTabs();

  renderLiveCodeExplorerEmpty();
  updateLiveCodeExplorerHeader();
  updateOpenFilesList();
  renderLiveCodeRecentSessions();
  updateLiveCodeEditorPlaceholder();
  _livecodeRenderProjectTabs();
}

function _livecodeRecentEntryIsActive(path) {
  if (_livecodeIsWorkspaceFilePath(path)) {
    return !!livecodeWorkspacePath && _livecodeNormalizeProjectKey(path) === _livecodeNormalizeProjectKey(livecodeWorkspacePath);
  }
  return _livecodeNormalizeProjectKey(path) === _livecodeNormalizeProjectKey(livecodeProjectPath);
}

function removeLiveCodeRecentProject(path) {
  const list = getLiveCodeRecentProjects().filter(p => p !== path);
  const wasActive = _livecodeRecentEntryIsActive(path);
  try {
    localStorage.setItem(LIVECODE_RECENT_PROJECTS_KEY, JSON.stringify(list));
    const last = localStorage.getItem(LIVECODE_LAST_PROJECT_KEY);
    if (last === path || !list.length) {
      localStorage.removeItem(LIVECODE_LAST_PROJECT_KEY);
    }
  } catch (e) {}
  if (!_livecodeIsWorkspaceFilePath(path)) {
    fetch("/livecode/project-storage", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_path: path })
    }).catch(() => {});
  }
  if (wasActive) {
    _livecodeClearActiveProject();
  }
  _livecodeForgetOpenProject(_livecodeNormalizeProjectKey(path));
  renderLiveCodeRecentProjects();
  if (_livecodeProjectMenuOpen) renderLiveCodeProjectDropdown();
}

function renderLiveCodeRecentProjects() {
  _livecodeRenderWelcomeRecents();
  const el = document.getElementById("ide-recent-projects-list");
  if (!el) return;
  const list = getLiveCodeRecentProjects();
  if (!list.length) {
    el.innerHTML = '<div style="padding:8px 4px;opacity:0.6;font-size:12px;">No recent projects</div>';
    return;
  }
  el.innerHTML = list.map(p => {
    const name = p.split("/").filter(Boolean).pop() || p;
    const esc = p.replace(/'/g, "\\'").replace(/"/g, "&quot;");
    const isActive = _livecodeRecentEntryIsActive(p);
    return `<div class="theme-transition livecode-recent-item${isActive ?" is-active" : ""}" style="padding:8px 10px;cursor:pointer;border-radius:4px;display:flex;align-items:center;gap:6px;margin-bottom:2px;" onclick="_livecodeOpenProjectOrWorkspace('${esc}'); showIDEPanel('explorer'); return false;">
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:3px;">
        <span style="font-size:13px;font-weight:${isActive ? "600" : "500"};">${name.replace(/</g, "&lt;")}</span>
        <span class="livecode-recent-item-path" style="font-size:10px;opacity:0.55;line-height:1.35;white-space:nowrap;overflow-x:auto;">${p.replace(/</g, "&lt;")}</span>
      </div>
      <button type="button" class="livecode-recent-item-remove" title="Remove from recents" aria-label="Remove from recents" onclick="event.stopPropagation(); removeLiveCodeRecentProject('${esc}'); return false;">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"></path></svg>
      </button>
    </div>`;
  }).join("");
}

function updateLiveCodeExplorerHeader() {

}
window.updateLiveCodeExplorerHeader = updateLiveCodeExplorerHeader;

function renderLiveCodeExplorerEmpty() {
  const fileTreeEl = document.getElementById("ide-file-tree");
  if (!fileTreeEl) return;
  fileTreeEl.innerHTML = `<div id="ide-explorer-empty" class="theme-transition">
     <div class="ide-explorer-empty-title">No folder open</div>
     <div class="ide-explorer-empty-sub">Browse files and let the agent work in a folder.</div>
     <button type="button" class="ide-explorer-empty-btn" onclick="openLiveCodeProjectBrowser(); return false;">Open folder</button>
     <button type="button" class="ide-explorer-empty-link" onclick="openLiveCodeCloneDialog(); return false;">Clone a repository</button>
  </div>`;
  updateLiveCodeExplorerHeader();
}

function _livecodeWarmWorkspaceIndex(path) {
  const scheduleIndex = typeof requestIdleCallback === "function"
    ? requestIdleCallback
    : function(cb) { setTimeout(cb, 1); };
  scheduleIndex(function() {
    fetch("/livecode/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_path: path, workspace: _livecodeCurrentWorkspacePayload() })
    }).then(r => r.json()).then(data => {
      if (data.success) {
        _livecodeApplyIndexResult(data);
        livecodeWorkspaceMissingFolders = Array.isArray(data.missing) ? data.missing.map(function(f) { return f.path; }) : [];
        _livecodeRefreshWorkspaceMissingBadges();
      }
    }).catch(() => {});
  });
}

window.setLiveCodeProject = function(path) {
  if (!path) return;
  if (_livecodeIsWorkspaceFilePath(path)) {
    _livecodeOpenProjectOrWorkspace(path);
    return;
  }
  const previousPath = livecodeProjectPath;
  const normPrev = _livecodeNormalizeProjectKey(previousPath);
  const normNext = _livecodeNormalizeProjectKey(path);
  if (normPrev !== normNext) {
    if (normPrev) {
      _livecodeStashRunningProjectChats(previousPath);
      _livecodeSaveTabsForProject(previousPath);
      _livecodePersistEditorTabs(previousPath);
    }
    _livecodeClearEditorFiles();
    if (!previousPath) {
      livecodeAgentSessionId = null;
    }
    _livecodeInvalidateSessionFetches();
    if (typeof closeLiveCodeSessionMenu === "function") closeLiveCodeSessionMenu();
  }
  livecodeProjectPath = path;
  livecodeProjectName = _livecodeFolderNameFromPath(path);
  if (!livecodeApplyingWorkspace && (!livecodeWorkspaceFolders.length || normPrev !== normNext)) {
    livecodeWorkspacePath = null;
    livecodeWorkspaceFolders = [{ name: _livecodeFolderNameFromPath(path), path: path }];
    livecodeWorkspaceSettings = {};
    livecodeWorkspaceMcpServers = {};
    _livecodePersistBrowserWorkspace();
  }
  ideHomePath = path;
  try {
    if (window.WBTsIntel) window.WBTsIntel.onProjectOpen(path, previousPath || null);
    if (window.WBLsp && window.monaco) window.WBLsp.onProjectOpen(path, previousPath || null);
  } catch (e) {}
  ideFileTreeData = {};
  ideExpandedFolders.clear();
  saveLiveCodeRecentProject(path);
  livecodeIndexReady = false;
  livecodeIndexFileCount = 0;
  livecodeIndexSymbolCount = 0;
  livecodeIndexTruncated = false;
  if (normPrev !== normNext || !livecodeChatTabs.length) _livecodeLoadTabsForProject(path);
  const activeTab = _livecodeGetActiveChatTab();
  if (activeTab && !activeTab.chatStarted) {
    livecodeAgentSessionId = activeTab.sessionId || _livecodeNewChatSessionId();
    activeTab.sessionId = livecodeAgentSessionId;
  } else if (!livecodeAgentSessionId) {
    livecodeAgentSessionId = _livecodeNewChatSessionId();
  }
  if (normPrev !== normNext) {
    _livecodeSwapTerminalsForProject(previousPath, path);
  }
  _livecodeLoadWorkspaceFolderTrees();
  _livecodeRestoreEditorTabs(path);
  toggleLiveCodeAgentPane(true);
  renderLiveCodeRecentProjects();
  if (_livecodeProjectMenuOpen) renderLiveCodeProjectDropdown();
  renderLiveCodeRecentSessions();
  _livecodeUpdateChatWelcome();
  updateLiveCodeExplorerHeader();
  _livecodeMcpStatusRequestSeq += 1;
  _livecodeMcpStatusLoadedWithoutProbe = false;
  _livecodeLoadMcpSelection();
  refreshLiveCodeMcpStatus({ probeServers: [], hydrateSelected: true });
  _livecodeWarmWorkspaceIndex(path);
  _livecodeSyncActiveProjectTab();
};


const LIVECODE_OPEN_PROJECTS_KEY = "livecode_open_projects_v1";
let _livecodeOpenProjects = null;
let _livecodeProjectTabsHtml = null;
let _livecodeProjectSwitching = false;

function _livecodeOpenProjectEntry(raw) {
  if (!raw || !raw.path) return null;
  return {
    key: _livecodeNormalizeProjectKey(raw.path),
    path: raw.path,
    primary: raw.primary || raw.path,
    workspace: raw.workspace && Array.isArray(raw.workspace.folders) ? raw.workspace : null,
  };
}

function _livecodeLoadOpenProjects() {
  if (_livecodeOpenProjects) return _livecodeOpenProjects;
  let raw = [];
  try { raw = JSON.parse(localStorage.getItem(LIVECODE_OPEN_PROJECTS_KEY) || "[]"); } catch (e) {}
  const seen = new Set();
  _livecodeOpenProjects = (Array.isArray(raw) ? raw : []).map(_livecodeOpenProjectEntry).filter(function(entry) {
    if (!entry || !entry.key || seen.has(entry.key)) return false;
    seen.add(entry.key);
    return true;
  });
  return _livecodeOpenProjects;
}

function _livecodePersistOpenProjects() {
  try {
    localStorage.setItem(LIVECODE_OPEN_PROJECTS_KEY, JSON.stringify(_livecodeLoadOpenProjects().map(function(entry) {
      return { path: entry.path, primary: entry.primary, workspace: entry.workspace || undefined };
    })));
  } catch (e) {}
}

function _livecodeActiveProjectKey() {
  return _livecodeNormalizeProjectKey(livecodeWorkspacePath || livecodeProjectPath);
}

function _livecodeSyncActiveProjectTab() {
  const key = _livecodeActiveProjectKey();
  const list = _livecodeLoadOpenProjects();
  if (key && livecodeProjectPath) {
    const payload = _livecodeCurrentWorkspacePayload();
    const entry = _livecodeOpenProjectEntry({
      path: livecodeWorkspacePath || livecodeProjectPath,
      primary: livecodeProjectPath,
      workspace: !livecodeWorkspacePath && payload.folders.length > 1 ? payload : null,
    });
    let index = list.findIndex(function(e) { return e.key === key; });
    if (index === -1 && livecodeWorkspacePath) {
      const primaryKey = _livecodeNormalizeProjectKey(livecodeProjectPath);
      index = list.findIndex(function(e) { return e.key === primaryKey && e.workspace; });
    }
    if (index === -1) list.push(entry);
    else list[index] = entry;
    _livecodePersistOpenProjects();
  }
  _livecodeRenderProjectTabs();
}

function _livecodeProjectTabLabel(entry) {
  if (_livecodeIsWorkspaceFilePath(entry.path)) {
    return _livecodeFolderNameFromPath(entry.path).replace(/\.code-workspace$/i, "") || "Workspace";
  }
  const name = _livecodeFolderNameFromPath(entry.path) || entry.path;
  const extra = entry.workspace ? entry.workspace.folders.length - 1 : 0;
  return extra > 0 ? name + " +" + extra : name;
}

function _livecodeProjectTabStatus(entry, isActive) {
  const background = _livecodeBackgroundProjectChats[_livecodeNormalizeProjectKey(entry.primary)];
  const tabs = isActive ? livecodeChatTabs : (background ? background.tabs : []);
  if (!isActive && tabs.some(function(t) { return t.pendingPermissionRequestId || t.pendingQuestion; })) return "attention";
  if (tabs.some(function(t) { return t.agentRunning; })) return "running";
  if (!isActive && tabs.some(function(t) { return t.hasUnread; })) return "unread";
  return "";
}

const _LIVECODE_PROJECT_TAB_CUBE_ICON = '<svg class="ui-icon" width="14" height="14" viewBox="0 0 512 512" fill="currentColor" aria-hidden="true"><g transform="translate(64 34.346667)"><path d="M192,0 L384,110.851252 L384,332.553755 L192,443.405007 L0,332.553755 L0,110.851252 L192,0 Z M42.666,157.654 L42.6666667,307.920144 L170.666,381.82 L170.666,231.555 L42.666,157.654 Z M341.333,157.655 L213.333,231.555 L213.333,381.82 L341.333333,307.920144 L341.333,157.655 Z M192,49.267223 L66.1333333,121.936377 L192,194.605531 L317.866667,121.936377 L192,49.267223 Z"></path></g></svg>';

const _LIVECODE_PROJECT_TAB_ICONS = {
  folder: _LIVECODE_PROJECT_TAB_CUBE_ICON,
  workspace: _LIVECODE_PROJECT_TAB_CUBE_ICON,
  close: _livecodeIcon("x", { size: "base" }),
  add: _livecodeIcon("plus", { size: "lg" }),
};

function _livecodeRenderProjectTabs() {
  const strip = document.getElementById("ide-project-tabs");
  if (!strip) return;
  const list = _livecodeLoadOpenProjects();
  const activeKey = _livecodeActiveProjectKey();
  const esc = function(text) { return _livecodeEscapeHtml(String(text || "")).replace(/"/g, "&quot;"); };
  const tabsHtml = list.map(function(entry) {
    const isActive = entry.key === activeKey && !!livecodeProjectPath;
    const status = _livecodeProjectTabStatus(entry, isActive);
    const label = _livecodeProjectTabLabel(entry);
    const kind = _livecodeIsWorkspaceFilePath(entry.path) || entry.workspace ? "workspace" : "folder";
    let icon = _LIVECODE_PROJECT_TAB_ICONS[kind];
    let statusLabel = "";
    if (status === "running") {
      icon = _livecodeAgentIconHtml("running");
      statusLabel = "Agent working";
    } else if (status === "attention") {
      icon = '<span class="ide-project-tab-dot is-attention"></span>';
      statusLabel = "Agent waiting for you";
    } else if (status === "unread") {
      icon = '<span class="ide-project-tab-dot"></span>';
      statusLabel = "Agent finished";
    }
    const title = entry.path + (statusLabel ? " (" + statusLabel + ")" : "");
    return '<div class="ide-project-tab' + (isActive ? " is-active" : "") + (status ? " is-" + status : "") + '" role="tab"' +
      ' aria-selected="' + (isActive ? "true" : "false") + '" tabindex="' + (isActive ? "0" : "-1") + '"' +
      ' data-project-key="' + esc(entry.key) + '" title="' + esc(title) + '">' +
      (status === "attention" || status === "unread" || status === "running" ?
        '<span class="ide-project-tab-icon" aria-hidden="true">' + icon + "</span>" : "") +
      '<span class="ide-project-tab-name">' + _livecodeEscapeHtml(label) + "</span>" +
      (statusLabel ? '<span class="ide-project-tab-sr">' + _livecodeEscapeHtml(statusLabel) + "</span>" : "") +
      '<button type="button" class="ide-project-tab-close" data-project-close tabindex="-1" title="Close project" aria-label="Close ' + esc(label) + '">' +
      _LIVECODE_PROJECT_TAB_ICONS.close + "</button></div>";
  }).join("");
  const html = '<div class="ide-project-tabs-list" role="tablist" aria-label="Open projects">' + tabsHtml + "</div>" +
    '<button type="button" class="ide-project-tab-add" data-project-open title="Open folder" aria-label="Open folder">' +
    _LIVECODE_PROJECT_TAB_ICONS.add + "</button>" +
    '<button type="button" id="ide-activity-recent" class="livecode-chat-tab-action theme-transition" title="Recent Projects" aria-label="Recent Projects" aria-haspopup="true" aria-expanded="false" aria-pressed="false">' +
    '<svg class="ui-icon" width="16" height="16" viewBox="0 0 512 512" fill="currentColor" aria-hidden="true"><g transform="translate(64 34.346667)"><path d="M192,-7.10542736e-15 L384,110.851252 L384,242.986 L341.333,242.986 L341.333,157.655 L213.333,231.555 L213.333,431.088 L192,443.405007 L0,332.553755 L0,110.851252 L192,-7.10542736e-15 Z M341.333333,264.32 L341.333,328.32 L405.333333,328.32 L405.333333,370.986667 L341.333,370.986 L341.333333,434.986667 L298.666667,434.986667 L298.666,370.986 L234.666667,370.986667 L234.666667,328.32 L298.666,328.32 L298.666667,264.32 L341.333333,264.32 Z M42.666,157.654 L42.6666667,307.920144 L170.666,381.82 L170.666,231.555 L42.666,157.654 Z M192,49.267223 L66.1333333,121.936377 L192,194.605531 L317.866667,121.936377 L192,49.267223 Z"></path></g></svg>' + "</button>" +
    '<button type="button" id="ide-play-button" class="livecode-chat-tab-action theme-transition" onclick="runCurrentPythonFile(); return false;" title="Run Python File" aria-label="Run Python File" style="display:none;">' +
    '<svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>' + "</button>" +
    '<button type="button" id="livecode-theme-toggle" class="livecode-chat-tab-action theme-transition" onclick="toggleLiveCodeTheme(); return false;" title="Theme" aria-label="Theme">' +
    _LIVECODE_THEME_ICONS + "</button>" +
    '<button type="button" id="livecode-browser-open" class="livecode-chat-tab-action theme-transition" onclick="openLiveCodeBrowser(); return false;" title="Browser" aria-label="Browser">' +
    _livecodeIcon("globe", { size: "lg" }) + "</button>" +
    '<button type="button" id="ide-activity-terminal" class="livecode-chat-tab-action theme-transition" onclick="toggleIDETerminal(); return false;" title="Toggle Terminal" aria-label="Toggle Terminal" aria-pressed="false">' +
    '<svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>' + "</button>" +
    '<button type="button" id="livecode-settings-open" class="livecode-chat-tab-action theme-transition" onclick="openLiveCodeSettings(); return false;" title="Settings" aria-label="Settings">' +
    _livecodeIcon("settings-gear", { size: "lg" }) + "</button>" +
    '<button type="button" id="ide-fullscreen-toggle" class="livecode-chat-tab-action theme-transition" onclick="toggleIDEFullscreen(); return false;" title="Toggle Fullscreen" aria-label="Toggle Fullscreen">' +
    '<svg id="ide-fullscreen-enter-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path></svg>' +
    '<svg id="ide-fullscreen-exit-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:none;"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"></path></svg>' + "</button>";
  strip.hidden = !list.length;
  if (html === _livecodeProjectTabsHtml) return;
  const focusedKey = document.activeElement && strip.contains(document.activeElement) && document.activeElement.getAttribute("data-project-key");
  strip.innerHTML = html;
  _livecodeSyncThemeButton();
  _livecodeProjectTabsHtml = html;
  if (focusedKey) {
    const again = Array.from(strip.querySelectorAll(".ide-project-tab")).find(function(el) { return el.getAttribute("data-project-key") === focusedKey; });
    if (again) again.focus();
  }
  const active = strip.querySelector(".ide-project-tab.is-active");
  const tabsList = strip.querySelector(".ide-project-tabs-list");
  if (active && tabsList) {
    if (active.offsetLeft < tabsList.scrollLeft) {
      tabsList.scrollLeft = active.offsetLeft;
    } else if (active.offsetLeft + active.offsetWidth > tabsList.scrollLeft + tabsList.clientWidth) {
      tabsList.scrollLeft = active.offsetLeft + active.offsetWidth - tabsList.clientWidth;
    }
  }
}

async function _livecodeActivateProjectTab(key) {
  const entry = _livecodeLoadOpenProjects().find(function(e) { return e.key === key; });
  if (!entry || _livecodeProjectSwitching) return false;
  if (entry.key === _livecodeActiveProjectKey() && livecodeProjectPath) return true;
  _livecodeProjectSwitching = true;
  let ok = false;
  try {
    if (_livecodeIsWorkspaceFilePath(entry.path)) {
      await _livecodeLoadWorkspaceFile(entry.path, { silent: true, allowPartial: true });
      ok = true;
    } else if (entry.workspace && entry.workspace.folders.length > 1) {
      ok = await _livecodeRestoreBrowserWorkspace(entry.workspace);
    } else {
      const result = await _livecodeValidateWorkspaceFolders([{ name: _livecodeFolderNameFromPath(entry.path), path: entry.path }]);
      const present = (result.folders || [])[0];
      ok = !!present && _livecodeSetWorkspaceFromPayload({ path: "", folders: [present], settings: {}, mcpServers: {} });
    }
  } catch (e) {
    ok = false;
  } finally {
    _livecodeProjectSwitching = false;
  }
  if (!ok) {
    _livecodeShowIdeToast("Couldn't open " + _livecodeProjectTabLabel(entry) + ": it no longer exists.");
    _livecodeForgetOpenProject(entry.key);
  }
  _livecodeRenderProjectTabs();
  return ok;
}

function _livecodeForgetOpenProject(key) {
  const list = _livecodeLoadOpenProjects();
  const index = list.findIndex(function(e) { return e.key === key; });
  if (index === -1) return;
  const entry = list[index];
  list.splice(index, 1);
  _livecodePersistOpenProjects();
  const primaryKey = _livecodeNormalizeProjectKey(entry.primary);
  if (primaryKey !== _livecodeNormalizeProjectKey(livecodeProjectPath)) {
    _livecodeStopBackgroundProjectChats(primaryKey);
    _livecodeCloseProjectTerminals(primaryKey);
  }
  _livecodeRenderProjectTabs();
}

window.closeLiveCodeProjectTab = async function(key) {
  const list = _livecodeLoadOpenProjects();
  const index = list.findIndex(function(e) { return e.key === key; });
  if (index === -1 || _livecodeProjectSwitching) return;
  const entry = list[index];
  const isActive = entry.key === _livecodeActiveProjectKey() && !!livecodeProjectPath;
  const label = _livecodeProjectTabLabel(entry);
  const status = _livecodeProjectTabStatus(entry, isActive);
  if (status === "running" || status === "attention") {
    const ok = await _livecodeModalConfirm({
      title: "Stop the agent?",
      message: "An agent is still working in " + label + ". Closing the project stops it.",
      confirmText: "Stop and close",
      danger: true,
    });
    if (!ok) return;
  }
  if (!isActive) {
    _livecodeForgetOpenProject(entry.key);
    return;
  }
  livecodeChatTabs.forEach(function(t) {
    if (t.agentRunning) _livecodeAbortTabTurn(t);
  });
  _livecodeResetTerminalsForProject();
  list.splice(index, 1);
  _livecodePersistOpenProjects();
  let switched = false;
  for (let tries = list.length; tries > 0 && !switched; tries--) {
    const remaining = _livecodeLoadOpenProjects();
    const next = remaining[Math.min(index, remaining.length - 1)];
    if (!next) break;
    switched = await _livecodeActivateProjectTab(next.key);
  }
  if (!switched) {
    _livecodeClearActiveProject();
    try { localStorage.removeItem(LIVECODE_LAST_PROJECT_KEY); } catch (e) {}
    renderLiveCodeRecentProjects();
  }
  _livecodeRenderProjectTabs();
};

function _livecodeBindProjectTabsOnce() {
  const strip = document.getElementById("ide-project-tabs");
  if (!strip || strip._livecodeBound) return;
  strip._livecodeBound = true;
  strip.addEventListener("click", function(e) {
    const target = e.target && e.target.closest ? e.target : null;
    if (!target) return;
    if (target.closest("[data-project-open]")) {
      openLiveCodeProjectBrowser();
      return;
    }
    const tab = target.closest(".ide-project-tab");
    if (!tab) return;
    const key = tab.getAttribute("data-project-key");
    if (target.closest("[data-project-close]")) {
      e.stopPropagation();
      closeLiveCodeProjectTab(key);
      return;
    }
    _livecodeActivateProjectTab(key);
  });
  strip.addEventListener("auxclick", function(e) {
    if (e.button !== 1) return;
    const tab = e.target && e.target.closest ? e.target.closest(".ide-project-tab") : null;
    if (!tab) return;
    e.preventDefault();
    closeLiveCodeProjectTab(tab.getAttribute("data-project-key"));
  });
  strip.addEventListener("keydown", function(e) {
    const tab = e.target && e.target.closest ? e.target.closest(".ide-project-tab") : null;
    if (!tab) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      _livecodeActivateProjectTab(tab.getAttribute("data-project-key"));
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const tabs = Array.from(strip.querySelectorAll(".ide-project-tab"));
      const next = tabs[(tabs.indexOf(tab) + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      if (next) next.focus();
    } else if (e.key === "Delete") {
      e.preventDefault();
      closeLiveCodeProjectTab(tab.getAttribute("data-project-key"));
    }
  });
}

function updateLiveCodeEditorPlaceholder() {
  const noProject = document.getElementById("ide-editor-placeholder-no-project");
  const noFile = document.getElementById("ide-editor-placeholder-no-file");
  const hasProject = !!livecodeProjectPath;
  if (noProject) noProject.style.display = hasProject ? "none" : "";
  if (noFile) noFile.style.display = hasProject ? "" : "none";
  if (!hasProject) _livecodeRenderWelcomeRecents();
}

function showLiveCodeEditorIdle() {
  ideActiveFile = null;
  const placeholder = document.getElementById("ide-editor-placeholder");
  const monacoSurface = document.getElementById("ide-monaco");
  const codeEditor = document.getElementById("ide-code-editor");
  if (monacoSurface) monacoSurface.style.display = "none";
  if (codeEditor) codeEditor.style.display = "none";
  _livecodeHidePlanSurface();
  _livecodeHideSettingsSurface();
  _livecodeHideBrowserSurface();
  updateLiveCodeEditorPlaceholder();
  if (placeholder) placeholder.style.display = "flex";
  if (window.ideEditor) try { window.ideEditor.setModel(null); } catch (e) {}
  updateOpenFilesList();
  updateIdePlanChrome();
}

async function _livecodeValidateWorkspaceFolders(folders) {
  const resp = await fetch("/livecode/workspace/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folders: folders })
  });
  const payload = await resp.json();
  if (!resp.ok) throw new Error(payload.error || "Unable to validate workspace");
  return payload;
}

async function _livecodeRestoreBrowserWorkspace(workspace) {
  const folders = _livecodeNormalizeWorkspaceFolders(workspace.folders || []);
  if (!folders.length) return false;
  let result;
  try {
    result = await _livecodeValidateWorkspaceFolders(folders);
  } catch (e) {
    return false;
  }
  const present = result.folders || [];
  if (!present.length) {
    try { localStorage.removeItem(LIVECODE_BROWSER_WORKSPACE_KEY); } catch (e) {}
    return false;
  }
  const missing = result.missing || [];
  const applied = _livecodeSetWorkspaceFromPayload(Object.assign({}, workspace, { folders: present }));
  if (!applied) return false;
  if (missing.length) {
    const names = missing.map(function(f) { return f.name || f.path; }).join(", ");
    const primaryGone = missing.some(function(f) { return f.path === folders[0].path; });
    _livecodeShowIdeToast(
      (primaryGone ? "Primary workspace folder was missing; promoted the next folder. " : "") +
      `Removed ${missing.length} missing workspace folder(s): ${names}.`
    );
  }
  return true;
}

async function _livecodeRestoreLastProject(last) {
  if (_livecodeIsWorkspaceFilePath(last)) {
    try {
      await _livecodeLoadWorkspaceFile(last, { silent: true, allowPartial: true });
      return true;
    } catch (e) {
      return false;
    }
  }
  let result;
  try {
    result = await _livecodeValidateWorkspaceFolders([{ name: _livecodeFolderNameFromPath(last), path: last }]);
  } catch (e) {
    return false;
  }
  const present = result.folders || [];
  if (!present.length) return false;
  setLiveCodeProject(present[0].path);
  return true;
}

async function initLiveCodeProjectState() {
  _livecodeBindProjectStateListenersOnce();
  _livecodeBindProjectTabsOnce();
  _livecodeLoadOpenProjects();
  const workspace = _livecodeLoadBrowserWorkspace();
  let restored = workspace ? await _livecodeRestoreBrowserWorkspace(workspace) : false;
  if (!restored) {
    let last = null;
    try { last = localStorage.getItem(LIVECODE_LAST_PROJECT_KEY); } catch (e) {}
    if (last) {
      restored = await _livecodeRestoreLastProject(last);
      if (!restored) {
        _livecodeShowIdeToast("Couldn't reopen the last project — it no longer exists.");
        removeLiveCodeRecentProject(last);
      }
    }
    for (const entry of _livecodeLoadOpenProjects().slice()) {
      if (restored) break;
      restored = await _livecodeActivateProjectTab(entry.key);
    }
    if (!restored) {
      renderLiveCodeExplorerEmpty();
      updateLiveCodeExplorerHeader();
      updateOpenFilesList();
    }
  }
  _livecodeSyncActiveProjectTab();
  initLiveCodeAgentSocket();
  if (!livecodeChatTabs.length) {
    if (!livecodeAgentSessionId) {
      livecodeAgentSessionId = _livecodeNewChatSessionId();
    }
    _livecodeInitChatTabs();
  }
  _livecodeUpdateChatWelcome();
  toggleLiveCodeAgentPane(true);
}

var _livecodeBrowserPath = "~";
var _livecodeBrowserHomePath = null;
var _livecodeBrowserSelected = null;
var _livecodeBrowserMode = "folder";
var _livecodeBrowserOnSelect = null;
var _livecodeBrowserFileExtensions = null;

function _livecodeBrowserExtensionAllowed(path) {
  if (!_livecodeBrowserFileExtensions || !_livecodeBrowserFileExtensions.length) return true;
  const lower = String(path || "").toLowerCase();
  return _livecodeBrowserFileExtensions.some(function(ext) {
    return lower.endsWith(ext.toLowerCase());
  });
}
var _livecodeBrowserSock = null;
var _livecodeBrowserItems = [];
var _livecodeBrowserHistory = [];
var _livecodeBrowserHistoryIndex = -1;
var _livecodeBrowserListHandler = null;

var LIVECODE_FINDER_FAV_KEY = "livecodeBrowserFavorites";

var LIVECODE_FINDER_SORT_KEY = "livecodeBrowserSort";
var _livecodeFinderSortKey = "name";
var _livecodeFinderSortDir = "asc";
try {
  var _savedFinderSort = JSON.parse(localStorage.getItem(LIVECODE_FINDER_SORT_KEY) || "null");
  if (_savedFinderSort && _savedFinderSort.key) {
    _livecodeFinderSortKey = _savedFinderSort.key;
    _livecodeFinderSortDir = _savedFinderSort.dir === "desc" ? "desc" : "asc";
  }
} catch (e) {}

function _livecodeSortFinderItems(items) {
  var key = _livecodeFinderSortKey;
  var factor = _livecodeFinderSortDir === "desc" ? -1 : 1;
  var sorted = (items || []).slice();
  sorted.sort(function(a, b) {
    var aDir = a.is_dir || a.type === "folder";
    var bDir = b.is_dir || b.type === "folder";
    if (aDir !== bDir) return aDir ? -1 : 1;
    var cmp = 0;
    if (key === "size") {
      cmp = (Number(a.size) || 0) - (Number(b.size) || 0);
    } else if (key === "mtime") {
      cmp = (Number(a.mtime) || 0) - (Number(b.mtime) || 0);
    } else if (key === "kind") {
      var aKind = String(a.kind || (aDir ? "Folder" : "Document"));
      var bKind = String(b.kind || (bDir ? "Folder" : "Document"));
      cmp = aKind.localeCompare(bKind, undefined, { sensitivity: "base" });
    } else {
      cmp = String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base", numeric: true });
    }
    if (cmp === 0) {
      return String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base", numeric: true });
    }
    return cmp * factor;
  });
  return sorted;
}

function _livecodeUpdateFinderSortHeaders() {
  document.querySelectorAll("#livecodeProjectBrowserModal .livecode-finder-sort").forEach(function(th) {
    var active = th.getAttribute("data-sort-key") === _livecodeFinderSortKey;
    th.classList.toggle("is-sorted", active);
    th.classList.toggle("is-desc", active && _livecodeFinderSortDir === "desc");
    th.setAttribute("aria-sort", active ? (_livecodeFinderSortDir === "desc" ? "descending" : "ascending") : "none");
  });
}

window.livecodeBrowserSortBy = function(key) {
  if (!/^(name|size|kind|mtime)$/.test(key)) return;
  if (_livecodeFinderSortKey === key) {
    _livecodeFinderSortDir = _livecodeFinderSortDir === "asc" ? "desc" : "asc";
  } else {
    _livecodeFinderSortKey = key;
    _livecodeFinderSortDir = (key === "size" || key === "mtime") ? "desc" : "asc";
  }
  try {
    localStorage.setItem(LIVECODE_FINDER_SORT_KEY, JSON.stringify({ key: _livecodeFinderSortKey, dir: _livecodeFinderSortDir }));
  } catch (e) {}
  var search = document.getElementById("livecode-finder-search");
  window.livecodeBrowserFilterList(search ? search.value : "");
};

var _LIVECODE_STAR_OUTLINE = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>';
var _LIVECODE_STAR_FILLED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>';

function _livecodeGetFavorites() {
  try {
    const raw = localStorage.getItem(LIVECODE_FINDER_FAV_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter(function (p) { return typeof p === "string" && p; }) : [];
  } catch (e) { return []; }
}

function _livecodeSetFavorites(list) {
  try { localStorage.setItem(LIVECODE_FINDER_FAV_KEY, JSON.stringify(list)); } catch (e) {}
}

function _livecodeIsFavorite(path) {
  return !!path && _livecodeGetFavorites().indexOf(path) !== -1;
}

function _livecodeFinderBasename(path) {
  const parts = String(path || "").replace(/\/$/, "").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}

function _livecodeRenderFinderFavorites() {
  const wrap = document.getElementById("livecode-finder-favorites");
  if (!wrap) return;
  const favs = _livecodeGetFavorites();
  if (!favs.length) { wrap.innerHTML = ""; return; }
  let html = '<div class="livecode-finder-sidebar-sublabel">Favorites</div>';
  html += favs.map(function (p) {
    const esc = p.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, "&quot;");
    const name = _livecodeFinderBasename(p).replace(/</g, "&lt;");
    return '<div class="livecode-finder-fav-row">' +
      '<button type="button" class="livecode-finder-sidebar-item livecode-finder-fav-item theme-transition" data-finder-path="' + esc + '" title="' + esc + '" onclick="livecodeBrowserNavigate(\'' + esc + '\'); return false;">' + name + '</button>' +
      '<button type="button" class="livecode-finder-fav-remove" title="Remove from Locations" onclick="livecodeBrowserToggleFavorite(\'' + esc + '\', event); return false;">' + _LIVECODE_STAR_FILLED + '</button>' +
      '</div>';
  }).join("");
  wrap.innerHTML = html;
}

window.livecodeBrowserToggleFavorite = function (path, ev) {
  if (ev) { ev.stopPropagation(); ev.preventDefault(); }
  if (!path) return;
  const list = _livecodeGetFavorites();
  const idx = list.indexOf(path);
  if (idx === -1) list.push(path);
  else list.splice(idx, 1);
  _livecodeSetFavorites(list);
  _livecodeRenderFinderFavorites();
  _livecodeUpdateFinderSidebarActive(_livecodeBrowserPath);
  const search = document.getElementById("livecode-finder-search");
  window.livecodeBrowserFilterList(search ? search.value : "");
};

function _livecodeEnsureBrowserSocket() {
  if (!_livecodeBrowserSock) {
    _livecodeBrowserSock = _livecodeGetIdeSocket();
  }
  return _livecodeBrowserSock;
}

function _livecodeGetParentPath(path) {
  if (!path || path === "~") return null;
  const normalized = String(path).replace(/\/$/, "");
  if (!normalized || normalized === "/") return null;
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 1) return "/";
  return "/" + parts.slice(0, -1).join("/");
}

function _livecodeRenderFinderBreadcrumb(actualPath) {
  const el = document.getElementById("livecode-finder-breadcrumb");
  if (!el) return;
  const chevron = '<span class="livecode-finder-breadcrumb-sep">›</span>';
  let html = '<span class="livecode-finder-breadcrumb-item" onclick="livecodeBrowserNavigate(\'~\');return false;">Home</span>';
  if (!actualPath || actualPath === "~") {
    el.innerHTML = html;
    return;
  }
  const parts = actualPath.replace(/\/$/, "").split("/").filter(Boolean);
  let currentPath = "";
  parts.forEach(function(part) {
    currentPath += "/" + part;
    const esc = currentPath.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    html += chevron + '<span class="livecode-finder-breadcrumb-item" title="' + currentPath.replace(/"/g, "&quot;") + '" onclick="livecodeBrowserNavigate(\'' + esc + '\');return false;">' + part.replace(/</g, "&lt;") + "</span>";
  });
  el.innerHTML = html;
}

function _livecodeUsernameFromHomePath(homePath) {
  if (!homePath) return null;
  const parts = homePath.replace(/\\/g, "/").replace(/\/$/, "").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
}

function _livecodeUpdateFinderSidebarHomeLabel() {
  const btn = document.querySelector('.livecode-finder-sidebar-item[data-finder-path="~"]');
  if (!btn) return;
  const username = _livecodeUsernameFromHomePath(_livecodeBrowserHomePath);
  btn.textContent = username || "Home";
}

function _livecodeMaybeSetHomePath(actualPath) {
  if (!actualPath || actualPath === "~") return;
  const match = actualPath.match(/^(\/Users\/[^/]+)/) || actualPath.match(/^(\/home\/[^/]+)/i);
  if (match) {
    _livecodeBrowserHomePath = match[1];
    _livecodeUpdateFinderSidebarHomeLabel();
  }
}

function _livecodeUpdateFinderSidebarActive(actualPath) {
  const home = _livecodeBrowserHomePath;
  const desktopPath = home ? home + "/Desktop" : null;
  const downloadsPath = home ? home + "/Downloads" : null;
  document.querySelectorAll(".livecode-finder-sidebar-item").forEach(function(btn) {
    const shortcut = btn.getAttribute("data-finder-path") || "";
    let active = false;
    if (shortcut === "~/Desktop" && desktopPath && actualPath) {
      active = actualPath === desktopPath || actualPath.indexOf(desktopPath + "/") === 0;
    } else if (shortcut === "~/Downloads" && downloadsPath && actualPath) {
      active = actualPath === downloadsPath || actualPath.indexOf(downloadsPath + "/") === 0;
    } else if (shortcut === "~") {
      if (!actualPath || actualPath === "~") {
        active = true;
      } else if (home) {
        active = actualPath === home;
      }
    } else if (shortcut && actualPath) {
      active = actualPath === shortcut;
    }
    btn.classList.toggle("is-active", active);
  });
}

function _livecodeFormatFileSize(bytes) {
  if (bytes == null || bytes === "") return "—";
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return n + " bytes";
  if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0).replace(/\.0$/, "") + " KB";
  if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(n < 10485760 ? 1 : 0).replace(/\.0$/, "") + " MB";
  return (n / (1024 * 1024 * 1024)).toFixed(1).replace(/\.0$/, "") + " GB";
}

function _livecodeFormatFinderDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (sameDay) return "Today at " + time;
  if (isYesterday) return "Yesterday at " + time;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function _livecodeFinderIcon(item) {
  const name = item.name || "";
  if (item.is_dir || item.type === "folder") {
    return _livecodeFolderIcon(false, name);
  }
  return _livecodeFileIcon(name);
}

function _livecodeUpdateFinderNavButtons() {
  const back = document.getElementById("livecode-finder-back");
  const fwd = document.getElementById("livecode-finder-forward");
  if (back) back.disabled = _livecodeBrowserHistoryIndex <= 0;
  if (fwd) fwd.disabled = _livecodeBrowserHistoryIndex < 0 || _livecodeBrowserHistoryIndex >= _livecodeBrowserHistory.length - 1;
}

function _livecodePushBrowserHistory(path) {
  if (_livecodeBrowserHistoryIndex >= 0 && _livecodeBrowserHistory[_livecodeBrowserHistoryIndex] === path) return;
  _livecodeBrowserHistory = _livecodeBrowserHistory.slice(0, _livecodeBrowserHistoryIndex + 1);
  _livecodeBrowserHistory.push(path);
  _livecodeBrowserHistoryIndex = _livecodeBrowserHistory.length - 1;
  _livecodeUpdateFinderNavButtons();
}

window.livecodeBrowserGoBack = function() {
  if (_livecodeBrowserHistoryIndex <= 0) return;
  _livecodeBrowserHistoryIndex -= 1;
  livecodeBrowserNavigate(_livecodeBrowserHistory[_livecodeBrowserHistoryIndex], true);
};

window.livecodeBrowserGoForward = function() {
  if (_livecodeBrowserHistoryIndex >= _livecodeBrowserHistory.length - 1) return;
  _livecodeBrowserHistoryIndex += 1;
  livecodeBrowserNavigate(_livecodeBrowserHistory[_livecodeBrowserHistoryIndex], true);
};

window.livecodeBrowserRefresh = function() {
  if (_livecodeBrowserPath) livecodeBrowserNavigate(_livecodeBrowserPath, true);
};

function _livecodeRenderFinderRows(items, selectedPath, currentPath) {
  const tbody = document.getElementById("livecode-browser-list");
  if (!tbody) return;
  let rowsHtml = "";
  const parentPath = _livecodeGetParentPath(currentPath);
  if (parentPath) {
    const parentEsc = parentPath.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, "&quot;");
    rowsHtml += `<tr class="livecode-finder-row theme-transition is-folder is-parent" data-path="${parentEsc}" onclick="livecodeBrowserNavigate('${parentEsc}'); return false;">
      <td><div class="livecode-finder-name-cell"><img class="livecode-finder-icon" src="${_livecodeFolderIcon(false)}" alt=""/><span>..</span></div></td>
      <td>—</td>
      <td>Folder</td>
      <td>—</td>
    </tr>`;
  }
  if (!items || !items.length) {
    tbody.innerHTML = rowsHtml || '<tr><td colspan="4" style="padding:24px;text-align:center;opacity:0.6;">Empty folder</td></tr>';
    return;
  }
  rowsHtml += items.map(function(it) {
    const isDir = it.is_dir || it.type === "folder";
    const esc = (it.path || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, "&quot;");
    const name = (it.name || "").replace(/</g, "&lt;");
    const selected = selectedPath && it.path === selectedPath;
    const rowClass = "livecode-finder-row theme-transition" + (isDir ? " is-folder" : " is-file") + (selected ? " is-selected" : "");
    const icon = _livecodeFinderIcon(it);
    const size = isDir ? "—" : _livecodeFormatFileSize(it.size);
    const kind = (it.kind || (isDir ? "Folder" : "Document")).replace(/</g, "&lt;");
    const date = _livecodeFormatFinderDate(it.mtime);
    const click = isDir
      ? `onclick="livecodeBrowserNavigate('${esc}'); return false;"`
      : `onclick="livecodeBrowserSelectFolder('${esc}', false); return false;" ondblclick="livecodeBrowserActivateSelection('${esc}', false); return false;"`;
    const isFav = isDir && _livecodeIsFavorite(it.path);
    const favStar = isDir
      ? `<button type="button" class="livecode-finder-fav-star${isFav ? " is-fav" : ""}" title="${isFav ? "Remove from Locations" : "Add to Locations"}" onclick="livecodeBrowserToggleFavorite('${esc}', event); return false;">${isFav ? _LIVECODE_STAR_FILLED : _LIVECODE_STAR_OUTLINE}</button>`
      : "";
    return `<tr class="${rowClass}" data-path="${esc}" ${click}>
      <td><div class="livecode-finder-name-cell"><img class="livecode-finder-icon" src="${icon}" alt=""/><span>${name}</span>${favStar}</div></td>
      <td>${size}</td>
      <td>${kind}</td>
      <td>${date}</td>
    </tr>`;
  }).join("");
  tbody.innerHTML = rowsHtml;
}

// The footer checkbox and Settings > General "Show hidden files" are the same setting.
function _livecodeSyncFinderHiddenToggle() {
  const chk = document.getElementById("livecode-finder-show-hidden");
  if (chk) chk.checked = !!_livecodeSettingsGet("showHiddenFiles");
}

window.livecodeBrowserToggleHidden = function(checked) {
  _livecodeSettingsSet("showHiddenFiles", !!checked);
  const search = document.getElementById("livecode-finder-search");
  window.livecodeBrowserFilterList(search ? search.value : "");
};

window.livecodeBrowserFilterList = function(query) {
  const q = String(query || "").trim().toLowerCase();
  let base = _livecodeBrowserItems;
  if (!_livecodeSettingsGet("showHiddenFiles")) {
    base = base.filter(function(it) { return !String(it.name || "").startsWith("."); });
  }
  const filtered = !q ? base : base.filter(function(it) {
    return (it.name || "").toLowerCase().includes(q);
  });
  const sorted = _livecodeSortFinderItems(filtered);
  _livecodeUpdateFinderSortHeaders();
  _livecodeRenderFinderRows(sorted, _livecodeBrowserSelected, _livecodeBrowserPath);
};

window.openLiveCodeProjectBrowser = function() {
  const modal = document.getElementById("livecodeProjectBrowserModal");
  if (!modal) return;
  _livecodeBrowserMode = "folder";
  _livecodeBrowserOnSelect = null;
  modal.classList.add("open");
  _livecodeBrowserSelected = null;
  _livecodeBrowserHistory = [];
  _livecodeBrowserHistoryIndex = -1;
  const btn = document.getElementById("livecodeBrowserSelectBtn");
  if (btn) btn.disabled = true;
  const search = document.getElementById("livecode-finder-search");
  if (search) search.value = "";
  const sel = document.getElementById("livecode-browser-selected");
  if (sel) { sel.textContent = ""; sel.style.display = "none"; }
  let start = "~";
  try { start = localStorage.getItem("livecodeBrowserLastPath") || livecodeProjectPath || "~"; } catch (e) {}
  _livecodeSyncFinderHiddenToggle();
  _livecodeRenderFinderFavorites();
  _livecodeMaybeSetHomePath(start);
  _livecodeBrowserPath = start;
  livecodeBrowserNavigate(start);
};

window.openLiveCodeFileBrowser = function(onSelect, startPath, extensions) {
  const modal = document.getElementById("livecodeProjectBrowserModal");
  if (!modal) return;
  _livecodeBrowserMode = "file";
  _livecodeBrowserOnSelect = typeof onSelect === "function" ? onSelect : null;
  _livecodeBrowserFileExtensions = Array.isArray(extensions) && extensions.length ? extensions : null;
  modal.classList.add("open");
  _livecodeBrowserSelected = null;
  _livecodeBrowserHistory = [];
  _livecodeBrowserHistoryIndex = -1;
  const btn = document.getElementById("livecodeBrowserSelectBtn");
  if (btn) btn.disabled = true;
  const search = document.getElementById("livecode-finder-search");
  if (search) search.value = "";
  const sel = document.getElementById("livecode-browser-selected");
  if (sel) { sel.textContent = ""; sel.style.display = "none"; }
  let start = startPath || "~";
  try { if (!startPath) start = localStorage.getItem("livecodeBrowserLastPath") || "~"; } catch (e) {}
  _livecodeSyncFinderHiddenToggle();
  _livecodeRenderFinderFavorites();
  _livecodeMaybeSetHomePath(start);
  _livecodeBrowserPath = start;
  livecodeBrowserNavigate(start);
};

window.openLiveCodeFolderBrowser = function(onSelect, startPath) {
  const modal = document.getElementById("livecodeProjectBrowserModal");
  if (!modal) return;
  _livecodeBrowserMode = "folder";
  _livecodeBrowserOnSelect = typeof onSelect === "function" ? onSelect : null;
  _livecodeBrowserFileExtensions = null;
  modal.classList.add("open");
  _livecodeBrowserSelected = null;
  _livecodeBrowserHistory = [];
  _livecodeBrowserHistoryIndex = -1;
  const btn = document.getElementById("livecodeBrowserSelectBtn");
  if (btn) btn.disabled = true;
  const search = document.getElementById("livecode-finder-search");
  if (search) search.value = "";
  const sel = document.getElementById("livecode-browser-selected");
  if (sel) { sel.textContent = ""; sel.style.display = "none"; }
  let start = startPath || "~";
  try { if (!startPath) start = localStorage.getItem("livecodeBrowserLastPath") || "~"; } catch (e) {}
  _livecodeSyncFinderHiddenToggle();
  _livecodeRenderFinderFavorites();
  _livecodeMaybeSetHomePath(start);
  _livecodeBrowserPath = start;
  livecodeBrowserNavigate(start);
};

window.closeLiveCodeProjectBrowser = function() {
  const modal = document.getElementById("livecodeProjectBrowserModal");
  if (modal) modal.classList.remove("open");
  if (_livecodeBrowserSock && _livecodeBrowserListHandler) {
    _livecodeBrowserSock.off("ide_files_list", _livecodeBrowserListHandler);
    _livecodeBrowserListHandler = null;
  }
  _livecodeBrowserMode = "folder";
  _livecodeBrowserOnSelect = null;
  _livecodeBrowserFileExtensions = null;
};

window.livecodeBrowserNavigate = function(path, skipHistory) {
  _livecodeBrowserPath = path;
  if (!skipHistory) _livecodePushBrowserHistory(path);
  else _livecodeUpdateFinderNavButtons();
  _livecodeBrowserSelected = null;
  const btn = document.getElementById("livecodeBrowserSelectBtn");
  if (btn) btn.disabled = true;
  const sel = document.getElementById("livecode-browser-selected");
  if (sel) { sel.textContent = ""; sel.style.display = "none"; }
  const tbody = document.getElementById("livecode-browser-list");
  if (tbody) tbody.innerHTML = '<tr><td colspan="4" style="padding:24px;text-align:center;opacity:0.6;">Loading…</td></tr>';
  _livecodeRenderFinderBreadcrumb(path);
  _livecodeUpdateFinderSidebarActive(path);
  if (!livecodeProjectPath) updateLiveCodeExplorerHeader();
  const sock = _livecodeEnsureBrowserSocket();
  if (_livecodeBrowserListHandler) sock.off("ide_files_list", _livecodeBrowserListHandler);
  _livecodeBrowserListHandler = function(data) {
    if (!data) return;
    const matches = data.requested_path !== undefined ? data.requested_path === path : data.path === path;
    if (!matches) return;
    if (data.error) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="4" style="padding:24px;color:#ef4444;">${data.error}</td></tr>`;
      return;
    }
    const actualPath = data.path || path;
    if (path === "~") {
      _livecodeBrowserHomePath = actualPath;
      _livecodeUpdateFinderSidebarHomeLabel();
    } else {
      _livecodeMaybeSetHomePath(actualPath);
    }
    _livecodeBrowserPath = actualPath;
    _livecodeRenderFinderBreadcrumb(actualPath);
    _livecodeUpdateFinderSidebarActive(actualPath);
    try { localStorage.setItem("livecodeBrowserLastPath", actualPath); } catch (e) {}
    if (!livecodeProjectPath) updateLiveCodeExplorerHeader();
    _livecodeBrowserItems = data.files || [];
    const search = document.getElementById("livecode-finder-search");
    window.livecodeBrowserFilterList(search ? search.value : "");
    livecodeBrowserSelectFolder(actualPath, true);
  };
  sock.on("ide_files_list", _livecodeBrowserListHandler);
  sock.emit("ide_list_files", { path: path });
};

window.livecodeBrowserSelectFolder = function(path, isFolder) {
  if (!path) return;
  _livecodeBrowserSelected = path;
  document.querySelectorAll("#livecode-browser-list .livecode-finder-row").forEach(function(row) {
    row.classList.toggle("is-selected", row.dataset.path === path);
  });
  const sel = document.getElementById("livecode-browser-selected");
  const btn = document.getElementById("livecodeBrowserSelectBtn");
  if (_livecodeBrowserMode === "file") {
    if (isFolder) {
      if (sel) { sel.textContent = ""; sel.style.display = "none"; }
      if (btn) btn.disabled = true;
    } else if (!_livecodeBrowserExtensionAllowed(path)) {
      if (sel) { sel.textContent = "Unsupported file type"; sel.style.display = "block"; }
      if (btn) btn.disabled = true;
    } else {
      if (sel) { sel.textContent = path; sel.style.display = "none"; }
      if (btn) btn.disabled = false;
    }
    return;
  }
  if (isFolder) {
    if (sel) { sel.textContent = path; sel.style.display = "none"; }
    if (btn) btn.disabled = false;
  } else {
    if (sel) { sel.textContent = "Select a folder to open as project"; sel.style.display = "block"; }
    if (btn) btn.disabled = true;
  }
};

window.livecodeBrowserNewFolder = function() {
  const name = prompt("New folder name:");
  if (!name || !name.trim()) return;
  const sock = _livecodeEnsureBrowserSocket();
  sock.off("ide_mkdir_result");
  sock.on("ide_mkdir_result", function(data) {
    sock.off("ide_mkdir_result");
    if (data.error) {
      alert(data.error);
      return;
    }
    livecodeBrowserNavigate(data.parent || _livecodeBrowserPath);
  });
  sock.emit("ide_mkdir", { path: _livecodeBrowserPath, name: name.trim() });
};

window.livecodeBrowserActivateSelection = function(path, isFolder) {
  livecodeBrowserSelectFolder(path, isFolder);
  const btn = document.getElementById("livecodeBrowserSelectBtn");
  if (btn && !btn.disabled) selectLiveCodeProjectFromBrowser();
};

window.selectLiveCodeProjectFromBrowser = function() {
  if (!_livecodeBrowserSelected) return;
  const path = _livecodeBrowserSelected;
  const onSelect = _livecodeBrowserOnSelect;
  closeLiveCodeProjectBrowser();
  if (onSelect) {
    onSelect(path);
    return;
  }
  setLiveCodeProject(path);
};

window.toggleLiveCodeAgentPane = function(forceOpen) {

  const panel = document.getElementById("livecode-agent-panel");
  const divider = document.getElementById("ide-agent-divider");
  if (!panel) return;
  void forceOpen;
  panel.style.display = "flex";
  if (!window._livecodeAgentDefaultWidthApplied) {
    panel.style.width = "420px";
    window._livecodeAgentDefaultWidthApplied = true;
  }
  if (divider) divider.style.display = "block";
  _livecodeInitChatTabs();
  _livecodeBindChatScrollWheel();
  const out = getLiveCodeChatOutput();
  if (out) requestAnimationFrame(function() { out.scrollTop = out.scrollHeight; });
  if (window.ideEditor) {
    setTimeout(() => { try { window.ideEditor.layout(); } catch (e) {} }, 100);
  }
};

let _livecodeChatAbortController = null;
let _livecodeStatusRow = null;
let _livecodeStatusMsg = null;
let _livecodeCurrentUserRow = null;
let _livecodeAssistantStreamEl = null;
let _livecodeChatStarted = false;

function getLiveCodeChatOutput() {
  return document.getElementById("livecode-chat-messages");
}

function _livecodeTruncateTabTitle(text, maxLen) {
  let t = String(text || "");
  try {
    const decoder = document.createElement("textarea");
    decoder.innerHTML = t;
    t = decoder.value || t;
  } catch (e) {}
  t = t
    .replace(/<[^>]*>/g, " ")
    .replace(/[`*_#>\[\](){}]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!t) return "New chat";
  maxLen = maxLen || 28;
  return t.length <= maxLen ? t : t.slice(0, maxLen - 1).trimEnd() + "\u2026";
}

function _livecodeNormalizeProjectKey(path) {
  if (!path) return "";
  return String(path).replace(/\\/g, "/").replace(/\/+$/, "");
}

function _livecodeSnapshotTab(tab) {
  if (!tab) return null;
  return {
    id: tab.id,
    title: tab.title,
    sessionId: tab.sessionId,
    messagesHtml: tab.messagesHtml,
    chatStarted: !!tab.chatStarted,
    planFile: tab.planFile || "",
    costUsd: tab.costUsd || 0,
    contextUsed: tab.contextUsed || 0,
    contextLimit: tab.contextLimit || 0,
  };
}

function _livecodeSaveTabsForProject(path) {
  const key = _livecodeNormalizeProjectKey(path);
  if (!key) return;
  _livecodeSaveActiveChatTabState();
  livecodeTabsByProject[key] = {
    tabs: livecodeChatTabs.map(_livecodeSnapshotTab),
    activeTabId: livecodeActiveChatTabId,
    tabCounter: livecodeChatTabCounter,
  };
  _livecodePersistTabsStorage(path);
  livecodeChatTabs.forEach(function(tab) {
    if (tab.messagesHtml && tab.sessionId) {
      _livecodePersistChatSnapshot(path, tab);
    }
  });
}

const _livecodeBackgroundProjectChats = {};

function _livecodeStashRunningProjectChats(path) {
  const key = _livecodeNormalizeProjectKey(path);
  if (!key || !livecodeChatTabs.some(function(t) { return t.agentRunning; })) return false;
  const current = _livecodeGetActiveChatTab();
  if (current) {
    _livecodeSaveTurnCtxToTab(current);
    _livecodeSyncTabStreamOutput(current);
    _livecodePrepareTabStreamBackground(current);
  }
  _livecodeBackgroundProjectChats[key] = {
    path: path,
    stateKey: _livecodeWorkspaceStateKey() || key,
    tabs: livecodeChatTabs,
    activeTabId: livecodeActiveChatTabId,
    tabCounter: livecodeChatTabCounter,
  };
  return true;
}

function _livecodeBackgroundEntryForTab(tab) {
  if (!tab) return null;
  for (const key in _livecodeBackgroundProjectChats) {
    const entry = _livecodeBackgroundProjectChats[key];
    if (entry.tabs.indexOf(tab) !== -1) return entry;
  }
  return null;
}

function _livecodeFindBackgroundChatTab(sessionId) {
  if (!sessionId) return null;
  for (const key in _livecodeBackgroundProjectChats) {
    const tab = _livecodeBackgroundProjectChats[key].tabs.find(function(t) { return t.sessionId === sessionId; });
    if (tab) return tab;
  }
  return null;
}

function _livecodePersistBackgroundProjectChats(entry, tab) {
  if (!entry) return;
  _livecodePersistChatSnapshot(entry.path, tab, entry.stateKey);
  const snapshot = {
    tabs: entry.tabs.map(_livecodeSnapshotTab),
    activeTabId: entry.activeTabId,
    tabCounter: entry.tabCounter,
  };
  livecodeTabsByProject[_livecodeNormalizeProjectKey(entry.path)] = snapshot;
  try { _livecodeStorageSet(LIVECODE_TABS_STORAGE_PREFIX + entry.stateKey, JSON.stringify(snapshot)); } catch (e) {}
}

function _livecodeStopBackgroundProjectChats(key) {
  const entry = _livecodeBackgroundProjectChats[key];
  if (!entry) return;
  delete _livecodeBackgroundProjectChats[key];
  entry.tabs.forEach(function(tab) {
    if (tab.agentRunning) _livecodeAbortTabTurn(tab, { saveState: false });
  });
}

function _livecodeLoadTabsForProject(path) {
  const key = _livecodeNormalizeProjectKey(path);
  const live = _livecodeBackgroundProjectChats[key];
  if (live) {
    delete _livecodeBackgroundProjectChats[key];
    livecodeChatTabs = live.tabs;
    livecodeActiveChatTabId = live.activeTabId;
    livecodeChatTabCounter = live.tabCounter;
    const shown = _livecodeGetActiveChatTab() || livecodeChatTabs[0];
    if (shown) {
      livecodeActiveChatTabId = shown.id;
      livecodeAgentSessionId = shown.sessionId || _livecodeNewChatSessionId();
      shown.sessionId = livecodeAgentSessionId;
      _livecodeClearTabUnread(shown);
      _livecodeLoadChatTabState(shown);
      if (shown.agentRunning && shown._backgroundOutput) {
        const out = getLiveCodeChatOutput();
        if (out) out.innerHTML = shown._backgroundOutput.innerHTML;
        shown._backgroundOutput = null;
      }
      _livecodeSyncTurnPointersFromDom();
      if (shown.queueWaiting && !shown.agentRunning) setTimeout(function() { _livecodeMaybeRunQueue(shown); }, 250);
    }
    _livecodeRenderChatTabs();
    _livecodeSyncGlobalRunningFromActiveTab();
    _livecodeSyncPermissionToolbar();
    return;
  }
  let saved = livecodeTabsByProject[key];
  if (!saved || !saved.tabs || !saved.tabs.length) {
    saved = _livecodeLoadTabsStorage(path);
  }
  if (saved && saved.tabs && saved.tabs.length) {
    livecodeChatTabs = saved.tabs.map(function(t) {
      let title = t.title || "New chat";
      let messagesHtml = t.messagesHtml || "";
      if (_livecodeIsLegacyTranscriptHtml(messagesHtml)) messagesHtml = "";
      if (!messagesHtml && t.chatStarted && t.sessionId) {
        const snap = _livecodeLoadChatSnapshot(path, t.sessionId);
        if (snap && snap.messagesHtml) {
          messagesHtml = snap.messagesHtml;
          if (snap.title) title = snap.title;
        }
      }
      const started = !!(t.chatStarted && t.sessionId);
      return {
        id: t.id,
        title: title,
        sessionId: started ? t.sessionId : _livecodeNewChatSessionId(),
        messagesHtml: messagesHtml,
        chatStarted: started,
        planFile: t.planFile || "",
        abortController: null,
        agentRunning: false,
        hasUnread: false,
        costUsd: t.costUsd || 0,
        contextUsed: t.contextUsed || 0,
        contextLimit: t.contextLimit || 0,
      };
    });
    livecodeActiveChatTabId = saved.activeTabId || livecodeChatTabs[0].id;
    livecodeChatTabCounter = saved.tabCounter || livecodeChatTabs.length;
  } else {
    livecodeChatTabs = [];
    livecodeActiveChatTabId = null;
    livecodeChatTabCounter = 0;
    livecodeAgentSessionId = null;
    _livecodeInitChatTabs();
  }
  const activeTab = _livecodeGetActiveChatTab();
  if (activeTab) {
    livecodeAgentSessionId = activeTab.sessionId || _livecodeNewChatSessionId();
    activeTab.sessionId = livecodeAgentSessionId;
    if (activeTab.chatStarted && activeTab.sessionId) {
      if (activeTab.agentRunning) {
        _livecodeLoadChatTabState(activeTab);
      } else if (activeTab.messagesHtml && String(activeTab.messagesHtml).trim()) {
        _livecodeLoadChatTabState(activeTab);
      } else {
        _livecodeFetchSessionIntoTab(activeTab, activeTab.sessionId, {});
      }
    } else {
      _livecodeLoadChatTabState(activeTab);
    }
  } else {
    livecodeAgentSessionId = _livecodeNewChatSessionId();
  }
  _livecodeRenderChatTabs();
  _livecodeSyncGlobalRunningFromActiveTab();
}

function _livecodeSyncTabStreamOutput(tab) {
  if (!tab) return;
  if (_livecodeIsActiveTab(tab)) {
    const out = getLiveCodeChatOutput();
    if (out) tab.messagesHtml = out.innerHTML;
  } else if (tab._backgroundOutput) {
    tab.messagesHtml = tab._backgroundOutput.innerHTML;
  }
}

function _livecodePrepareTabStreamBackground(tab) {

  if (!tab || !tab.agentRunning) return;
  if (!_livecodeIsActiveTab(tab)) return;
  const out = getLiveCodeChatOutput();
  if (!out) return;
  if (!_livecodeChatOutputBelongsToTab(out, tab)) return;

  const cloned = out.cloneNode(true);
  const tmp = document.createElement("div");
  tmp.innerHTML = cloned.innerHTML;

  tab.messagesHtml = tmp.innerHTML;
  tab._backgroundOutput = tmp;
  _livecodeSyncTurnPointersFromOutput(tmp, tab);
}

function _livecodeEnsureBackgroundOutput(tab) {

  if (!tab) return null;
  if (_livecodeIsActiveTab(tab)) {
    return getLiveCodeChatOutput();
  }
  if (!tab._backgroundOutput) {
    tab._backgroundOutput = document.createElement("div");
    tab._backgroundOutput.innerHTML = tab.messagesHtml || "";
  }
  return tab._backgroundOutput;
}

function _livecodeGetOutputForTab(tab) {
  if (!tab) return getLiveCodeChatOutput();
  if (_livecodeIsActiveTab(tab)) {
    if (tab._backgroundOutput) {
      const live = getLiveCodeChatOutput();
      if (live) live.innerHTML = tab._backgroundOutput.innerHTML;
      tab._backgroundOutput = null;
    }
    return getLiveCodeChatOutput();
  }
  return _livecodeEnsureBackgroundOutput(tab);
}

function _livecodeGetOutputForSession(sessionId) {
  const tab = livecodeChatTabs.find(function(t) { return t.sessionId === sessionId; });
  return tab ? _livecodeGetOutputForTab(tab) : null;
}

function _livecodeGetTabStreamOutput(tab) {
  return _livecodeGetOutputForTab(tab);
}

function _livecodeAbortTabTurn(tab, options) {
  const opts = options || {};
  if (!tab) return;
  const stopSessionId = tab.sessionId || (_livecodeIsActiveTab(tab) ? livecodeAgentSessionId : "");
  if (tab.agentRunning && stopSessionId) {
    fetch("/livecode/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: stopSessionId }),
    }).catch(function() {});
  }
  if (tab.abortController) {
    try {
      tab.abortController.abort();
    } catch (e) {}
    tab.abortController = null;
  }
  if (tab.agentRunning) {
    tab.agentRunning = false;
    if (opts.saveState !== false && _livecodeIsActiveTab(tab)) {
      _livecodeSaveActiveChatTabState();
    }
  }
  if (_livecodeIsActiveTab(tab)) {
    _livecodeChatAbortController = null;
    _livecodeClearPendingToolSteps();
  }
}

function _livecodeIsTabRunning(tab) {
  return !!(tab && tab.agentRunning);
}

function _livecodeGetRunningTab() {
  for (let i = 0; i < livecodeChatTabs.length; i++) {
    if (livecodeChatTabs[i].agentRunning) return livecodeChatTabs[i];
  }
  return null;
}

function _livecodeSyncGlobalRunningFromActiveTab() {
  const tab = _livecodeGetActiveChatTab();
  livecodeAgentRunning = _livecodeIsTabRunning(tab);
  _setLiveCodeChatBusy(livecodeAgentRunning);
}

function _livecodeNewChatSessionId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return "livecode_" + window.crypto.randomUUID().replace(/-/g, "");
  }
  const randomPart = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  return "livecode_" + randomPart.replace(/[^a-z0-9]/g, "").padEnd(32, "0").slice(0, 32);
}

function _livecodeGetActiveChatTab() {
  return livecodeChatTabs.find(function(t) { return t.id === livecodeActiveChatTabId; }) || null;
}

function _livecodeIsActiveTab(tab) {
  return !!tab && tab === _livecodeGetActiveChatTab();
}

function _livecodeSaveActiveChatTabState() {
  const tab = _livecodeGetActiveChatTab();
  if (!tab) return;
  const out = getLiveCodeChatOutput();
  if (out && !tab.agentRunning) {
    _livecodeFinalizeDomForSnapshot(out);
  }
  tab.messagesHtml = out ? out.innerHTML : "";
  tab.sessionId = livecodeAgentSessionId || tab.sessionId;
  tab.chatStarted = _livecodeChatStarted;
  if (livecodeProjectPath && tab.sessionId && _livecodeTabHasConversation(tab)) {
    _livecodePersistChatSnapshot(livecodeProjectPath, tab);
  }
}

function _livecodeSyncTurnPointersFromOutput(out, tab) {
  if (!out) {
    _livecodeStatusRow = null;
    _livecodeStatusMsg = null;
    _livecodeCurrentUserRow = null;
    _livecodeAssistantStreamEl = null;
    if (tab) _livecodeSaveTurnCtxToTab(tab);
    return;
  }
  _livecodeStatusRow = out.querySelector("#livecode-agent-status-row");
  _livecodeStatusMsg = _livecodeStatusRow ? _livecodeStatusRow.querySelector(".livecode-status-msg") : null;
  const userRows = out.querySelectorAll(".livecode-user-row");
  _livecodeCurrentUserRow = userRows.length ? userRows[userRows.length - 1] : null;
  const streamRows = out.querySelectorAll(".livecode-assistant-row .livecode-stream-msg");
  _livecodeAssistantStreamEl = streamRows.length ? streamRows[streamRows.length - 1] : null;

  if (tab) {
    if (!(tab._assistantStreamRow && out.contains(tab._assistantStreamRow))) {
      tab._assistantStreamRow = null;
    }
  }
  const runningOuter = out.querySelector(".livecode-activity-wrap-outer.is-running");
  _livecodeRunningActivityEl = runningOuter || null;
  if (tab) _livecodeSaveTurnCtxToTab(tab);
}

function _livecodeSyncTurnPointersFromDom() {
  _livecodeSyncTurnPointersFromOutput(getLiveCodeChatOutput(), _livecodeGetActiveChatTab());
  _livecodeLastToolLabel = "";
  _livecodeLastTool = "";
  _livecodeLastToolArgs = {};
  _livecodeThinkingStartMs = null;
  _livecodePendingDurationS = null;
  _livecodePendingDurationMs = null;
  _livecodeCancelThoughtStreamFlush();
  _livecodePendingThoughtContent = "";
  _livecodeStreamingThoughtContentEl = null;
  _livecodeStopThinkingTicker();
}

function _livecodeSanitizeLoadedActivityHtml(out) {
  if (!out) return;

  out.querySelectorAll(".is-running").forEach(function(el) {
    el.classList.remove("is-running");
  });
  out.querySelectorAll(".ui-shimmer").forEach(function(el) {
    el.classList.remove("ui-shimmer");
  });
  out.querySelectorAll(".livecode-term-action").forEach(function(el) {
    if (el.textContent === "Running") el.textContent = "Ran";
  });
  out.querySelectorAll(".livecode-term-card-title").forEach(function(el) {
    if (el.textContent === "Running command") el.textContent = "Ran command";
  });
  out.querySelectorAll(".livecode-activity-wrap-outer.is-running").forEach(function(el) {
    el.classList.remove("is-running");
  });
  out.querySelectorAll(".livecode-thought-wrap.is-streaming").forEach(function(el) {
    el.classList.remove("is-streaming");
  });
  _livecodeRemoveGenericWorkingActivities(out);
  out.querySelectorAll(".livecode-run-spinner").forEach(function(el) {
    el.remove();
  });
}

function _livecodeUpdateUserMessageCollapseState(out) {
  if (!out) return;
  Array.from(out.querySelectorAll(".chat-row.livecode-user-row")).forEach(function(row) {
    const msg = row.querySelector(".chat-msg.user");
    if (!msg) return;
    row.classList.remove("is-collapsible");
    const isExpanded = row.classList.contains("is-expanded");
    if (isExpanded) row.classList.remove("is-expanded");

    const styles = getComputedStyle(msg);
    let lineHeight = parseFloat(styles.lineHeight || "0");
    if (!Number.isFinite(lineHeight) || lineHeight <= 0) {
      const fontSize = parseFloat(styles.fontSize || "0");
      lineHeight = Number.isFinite(fontSize) && fontSize > 0 ? fontSize * 1.55 : 18;
    }
    const collapsedHeight = lineHeight * 6.2;
    const isTooTallForCollapsedRange = msg.scrollHeight > collapsedHeight + 2;

    if (isExpanded) row.classList.add("is-expanded");
    if (isTooTallForCollapsedRange) {
      row.classList.add("is-collapsible");
    } else {
      row.classList.remove("is-expanded");
    }
  });
}

function _livecodeScheduleUserMessageCollapseState(out) {
  if (!out) return;
  requestAnimationFrame(function() {
    _livecodeUpdateUserMessageCollapseState(out);
  });
}

function _livecodeFinalizeDomForSnapshot(out) {
  if (!out) return;
  _livecodeSanitizeLoadedActivityHtml(out);
  _livecodeSettleRunningAgents(out);
  _livecodeUpdateUserMessageCollapseState(out);
  Array.from(out.querySelectorAll(".livecode-agent-steps-row")).forEach(function(row) {
    if (!row.querySelector(".livecode-activity-wrap-outer")) {
      row.remove();
    }
  });
}

function _livecodeLoadChatTabState(tab) {
  if (!tab) return;
  if (!tab.agentRunning) tab._backgroundOutput = null;
  if (!tab.messagesHtml && tab.chatStarted && tab.sessionId && livecodeProjectPath) {
    const snap = _livecodeLoadChatSnapshot(livecodeProjectPath, tab.sessionId);
    if (snap && snap.messagesHtml) {
      tab.messagesHtml = snap.messagesHtml;
      if (snap.title) tab.title = _livecodeTruncateTabTitle(snap.title);
    }
  }
  _livecodeResetTurnState();
  livecodeAgentSessionId = tab.sessionId || _livecodeNewChatSessionId();
  tab.sessionId = livecodeAgentSessionId;
  _livecodeChatStarted = !!tab.chatStarted;
  const out = getLiveCodeChatOutput();
  if (out) {
    _livecodeMarkChatOutputForTab(out, tab);
    out.innerHTML = tab.messagesHtml || "";
  }
  _livecodeStripStaleWelcome(out);
  _livecodeSanitizeLoadedActivityHtml(out);
  if (out && !tab.agentRunning && out.querySelector(".livecode-term-card:not([data-done])")) {
    _livecodeSettleRunningTerminalCards(out, { keepEmpty: true });
    tab.messagesHtml = out.innerHTML;
  }
  if (out && !tab.agentRunning && out.querySelector('.livecode-agent-row[data-state="running"]')) {
    _livecodeSettleRunningAgents(out);
    tab.messagesHtml = out.innerHTML;
  }
  _livecodePostProcessRestoredOutput(out);
  _livecodeScheduleUserMessageCollapseState(out);
  _livecodeSyncTurnPointersFromDom();
  _livecodeSaveTurnCtxToTab(tab);
  _livecodeSchedulePendingChangesRefresh();
  _livecodeSyncComposerPlaceholder();
  _livecodeRenderQuestionsBar();
  _livecodeRenderQueueBar();
}

function _livecodeRenderChatTabs() {
  const list = document.getElementById("livecode-chat-tabs-list");
  if (!list) return;
  list.innerHTML = "";
  livecodeChatTabs.forEach(function(tab) {
    const tabEl = document.createElement("div");
    const runningClass = tab.agentRunning ? " is-running" : "";
    const unreadClass = tab.hasUnread && !tab.agentRunning ? " has-unread" : "";
    tabEl.className = "livecode-chat-tab theme-transition" + (tab.id === livecodeActiveChatTabId ? " active" : "") + runningClass + unreadClass;
    tabEl.dataset.tabId = tab.id;
    tabEl.setAttribute("role", "tab");
    tabEl.setAttribute("tabindex", tab.id === livecodeActiveChatTabId ? "0" : "-1");
    tabEl.setAttribute("aria-selected", tab.id === livecodeActiveChatTabId ? "true" : "false");
    const tabTitle = _livecodeTruncateTabTitle(tab.title);
    tabEl.innerHTML =
      '<span class="livecode-chat-tab-icon">' + _livecodeGetTabIconHtml(tab) + "</span>" +
      '<span class="livecode-chat-tab-label" title="' + _livecodeEscapeHtml(tabTitle) + '">' + _livecodeEscapeHtml(tabTitle) + "</span>" +
      '<button type="button" class="livecode-chat-tab-close theme-transition" title="Close chat" aria-label="Close chat">' +
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
      "</button>";
    tabEl.addEventListener("click", function(e) {
      if (e.target.closest(".livecode-chat-tab-close")) return;
      _livecodeSwitchChatTab(tab.id);
    });
    const closeBtn = tabEl.querySelector(".livecode-chat-tab-close");
    if (closeBtn) {
      closeBtn.addEventListener("click", function(e) {
        e.preventDefault();
        e.stopPropagation();
        _livecodeCloseChatTab(tab.id);
      });
    }
    list.appendChild(tabEl);
  });
  _livecodeUpdateBackgroundStatusPill();
  _livecodeUpdateChatCostDisplay(_livecodeGetActiveChatTab());
  _livecodeUpdateContextRing(_livecodeGetActiveChatTab());
  _livecodeRenderProjectTabs();
}

let LIVECODE_USD_TO_INR = 95;
let _livecodeFxRateFetchStarted = false;

function _livecodeLoadCachedFxRate() {
  try {
    const raw = localStorage.getItem("livecode_usd_to_inr");
    const val = raw ? parseFloat(raw) : NaN;
    if (Number.isFinite(val) && val > 0) LIVECODE_USD_TO_INR = val;
  } catch (e) {}
}

function _livecodeFetchFxRateOnce() {
  if (_livecodeFxRateFetchStarted) return;
  _livecodeFxRateFetchStarted = true;
  _livecodeLoadCachedFxRate();
  fetch("/livecode/fx-rate")
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(data) {
      const rate = data && Number(data.rate);
      if (Number.isFinite(rate) && rate > 0) {
        LIVECODE_USD_TO_INR = rate;
        try { localStorage.setItem("livecode_usd_to_inr", String(rate)); } catch (e) {}
        _livecodeUpdateChatCostDisplay(_livecodeGetActiveChatTab());
      }
    })
    .catch(function() {});
}

const LIVECODE_INR_ICON_SVG =
  '<span class="livecode-chat-cost-icon" aria-hidden="true">₹</span>';

function _livecodeFormatInrCount(amount) {
  const rounded = Math.round(amount);
  if (rounded < 1000) return String(rounded);
  const thousands = rounded / 1000;
  return thousands.toFixed(1) + "K";
}

const _livecodeCostMarkerAnim = { raf: null, shownInr: 0, tabId: null };

function _livecodeRenderCostMarkerValue(el, roundedInr) {
  el.innerHTML = LIVECODE_INR_ICON_SVG +
    '<span class="livecode-chat-cost-value">' + _livecodeEscapeHtml(_livecodeFormatInrCount(roundedInr)) + "</span>";
}

function _livecodeBumpCostMarker(el) {
  el.classList.remove("is-ticking");
  void el.offsetWidth;
  el.classList.add("is-ticking");
}

function _livecodeAnimateCostMarker(el, fromVal, toVal) {
  if (_livecodeCostMarkerAnim.raf) {
    cancelAnimationFrame(_livecodeCostMarkerAnim.raf);
    _livecodeCostMarkerAnim.raf = null;
  }
  const delta = toVal - fromVal;

  if (delta <= 3) {
    _livecodeCostMarkerAnim.shownInr = toVal;
    _livecodeRenderCostMarkerValue(el, toVal);
    _livecodeBumpCostMarker(el);
    return;
  }

  const start = performance.now();
  const duration = Math.min(450, Math.max(120, delta * 4));
  function step(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 2);
    const current = Math.round(fromVal + delta * eased);
    if (current !== _livecodeCostMarkerAnim.shownInr) {
      _livecodeCostMarkerAnim.shownInr = current;
      _livecodeRenderCostMarkerValue(el, current);
    }
    if (t < 1) {
      _livecodeCostMarkerAnim.raf = requestAnimationFrame(step);
    } else {
      _livecodeCostMarkerAnim.raf = null;
      _livecodeCostMarkerAnim.shownInr = toVal;
      _livecodeRenderCostMarkerValue(el, toVal);
      _livecodeBumpCostMarker(el);
    }
  }
  _livecodeCostMarkerAnim.raf = requestAnimationFrame(step);
}

function _livecodeUpdateContextRing(tab) {
  const ring = document.getElementById("livecode-context-ring");
  if (!ring) return;
  const arc = ring.querySelector(".livecode-ctx-arc");
  if (!arc) return;
  const used = Number(tab && tab.contextUsed) || 0;
  const limit = Number(tab && tab.contextLimit) || 0;
  const frac = limit > 0 ? Math.min(1, Math.max(0, used / limit)) : 0;
  const circumference = 56.549;
  arc.style.strokeDashoffset = String(circumference * (1 - frac));
  ring.classList.toggle("is-warn", frac >= 0.75 && frac < 0.9);
  ring.classList.toggle("is-full", frac >= 0.9);
  const label = limit > 0
    ? "Context: " + Math.round(frac * 100) + "% used (" + used.toLocaleString() + " / " + limit.toLocaleString() + " tokens)"
    : "Context: 0% used (updates after the first message)";
  ring.title = label;
  ring.setAttribute("aria-label", label);
}

function _livecodeUpdateChatCostDisplay(tab, options) {
  const el = document.getElementById("livecode-chat-cost-marker");
  if (!el) return;
  const opts = options || {};
  const usd = (tab && tab.costUsd) || 0;
  const inr = usd * LIVECODE_USD_TO_INR;
  const targetRounded = Math.round(inr);
  const tabId = tab ? tab.id : null;

  if (usd <= 0 || targetRounded <= 0) {
    if (_livecodeCostMarkerAnim.raf) {
      cancelAnimationFrame(_livecodeCostMarkerAnim.raf);
      _livecodeCostMarkerAnim.raf = null;
    }
    _livecodeCostMarkerAnim.shownInr = 0;
    _livecodeCostMarkerAnim.tabId = tabId;
    el.style.display = "none";
    return;
  }

  const titleText = "Estimated spend for this chat: $" + usd.toFixed(4) +
    " (₹" + inr.toFixed(2) + " at ₹" + LIVECODE_USD_TO_INR + "/$1)";
  if (el.title !== titleText) el.title = titleText;
  if (el.style.display === "none") el.style.display = "";

  const sameTab = _livecodeCostMarkerAnim.tabId === tabId;
  _livecodeCostMarkerAnim.tabId = tabId;

  if (sameTab && !_livecodeCostMarkerAnim.raf && _livecodeCostMarkerAnim.shownInr === targetRounded) {
    return;
  }

  if (opts.animate && sameTab && _livecodeCostMarkerAnim.shownInr < targetRounded) {
    _livecodeAnimateCostMarker(el, _livecodeCostMarkerAnim.shownInr, targetRounded);
  } else {
    if (_livecodeCostMarkerAnim.raf) {
      cancelAnimationFrame(_livecodeCostMarkerAnim.raf);
      _livecodeCostMarkerAnim.raf = null;
    }
    _livecodeCostMarkerAnim.shownInr = targetRounded;
    _livecodeRenderCostMarkerValue(el, targetRounded);
  }
}

function _livecodeUpdateBackgroundStatusPill() {
  const pill = document.getElementById("livecode-chat-background-status");
  if (!pill) return;
  const badgeEl = pill.querySelector(".livecode-chat-background-badge");
  const runningCount = livecodeChatTabs.filter(function(t) {
    return t.agentRunning && t.id !== livecodeActiveChatTabId;
  }).length;
  if (runningCount > 0) {
    pill.style.display = "";
    if (badgeEl) badgeEl.textContent = String(runningCount);
    pill.title = runningCount === 1 ?
      "1 chat still running in the background" :
      runningCount + " chats still running in the background";
  } else {
    pill.style.display = "none";
  }
}

function _livecodeSwitchChatTab(tabId) {
  if (!tabId || tabId === livecodeActiveChatTabId) return;
  const currentTab = _livecodeGetActiveChatTab();
  if (currentTab) {
    _livecodeSaveTurnCtxToTab(currentTab);
    _livecodeSyncTabStreamOutput(currentTab);
    if (!currentTab.agentRunning) {
      _livecodeSaveActiveChatTabState();
    }
    _livecodePrepareTabStreamBackground(currentTab);
  }
  const tab = livecodeChatTabs.find(function(t) { return t.id === tabId; });
  if (!tab) return;
  livecodeActiveChatTabId = tabId;
  _livecodeClearTabUnread(tab);
  _livecodeLoadChatTabState(tab);
  if (tab.agentRunning && tab._backgroundOutput) {
    const out = getLiveCodeChatOutput();
    if (out) out.innerHTML = tab._backgroundOutput.innerHTML;
    tab._backgroundOutput = null;
  }
  _livecodeSyncTurnPointersFromDom();
  _livecodeSyncGlobalRunningFromActiveTab();
  _livecodeSyncPermissionToolbar();
  _livecodeRenderChatTabs();
  const out = getLiveCodeChatOutput();
  if (out) requestAnimationFrame(function() { out.scrollTop = out.scrollHeight; });
  if (livecodeProjectPath) _livecodeSaveTabsForProject(livecodeProjectPath);
  if (tab.queueWaiting && !tab.agentRunning) setTimeout(function() { _livecodeMaybeRunQueue(tab); }, 250);
}

function _livecodeCloseChatTab(tabId) {
  const tabIndex = livecodeChatTabs.findIndex(function(t) { return t.id === tabId; });
  if (tabIndex === -1) return;
  const closingTab = livecodeChatTabs[tabIndex];
  const wasActive = livecodeActiveChatTabId === tabId;
  closingTab.queuedMessages = [];
  _livecodePersistQueue(closingTab);
  if (wasActive) {
    _livecodeSaveActiveChatTabState();
  }
  _livecodeAbortTabTurn(closingTab, { saveState: wasActive });
  if (livecodeChatTabs.length === 1) {
    const tab = livecodeChatTabs[0];
    tab.title = "New chat";
    tab.messagesHtml = "";
    tab.chatStarted = false;
    tab.sessionId = _livecodeNewChatSessionId();
    tab.hasUnread = false;
    tab.pendingPermissionRequestId = "";
    tab.pendingQuestion = null;
    _livecodeLoadChatTabState(tab);
    _livecodeSyncGlobalRunningFromActiveTab();
    _livecodeSyncPermissionToolbar();
    _livecodeRenderChatTabs();
    if (livecodeProjectPath) _livecodeSaveTabsForProject(livecodeProjectPath);
    return;
  }
  livecodeChatTabs.splice(tabIndex, 1);
  if (wasActive) {
    const nextTab = livecodeChatTabs[Math.max(0, tabIndex - 1)];
    livecodeActiveChatTabId = nextTab.id;
    _livecodeLoadChatTabState(nextTab);
    _livecodeSyncGlobalRunningFromActiveTab();
  }
  _livecodeRenderChatTabs();
  if (livecodeProjectPath) _livecodeSaveTabsForProject(livecodeProjectPath);
}

function _livecodeCreateChatTab(title, options) {
  const opts = options || {};
  const currentTab = _livecodeGetActiveChatTab();
  if (currentTab && !opts.skipSave) {
    _livecodeSaveTurnCtxToTab(currentTab);
    _livecodeSyncTabStreamOutput(currentTab);
    _livecodeSaveActiveChatTabState();
    _livecodePrepareTabStreamBackground(currentTab);
  }
  const tabId = "chat-" + (++livecodeChatTabCounter);
  const tab = {
    id: tabId,
    title: title || "New chat",
    sessionId: _livecodeNewChatSessionId(),
    messagesHtml: "",
    chatStarted: false,
    abortController: null,
    agentRunning: false,
    hasUnread: false,
    pendingPermissionRequestId: "",
    costUsd: 0,
  };
  livecodeChatTabs.push(tab);
  if (opts.activate !== false) {
    livecodeActiveChatTabId = tabId;
    _livecodeClearTabUnread(tab);
    _livecodeLoadChatTabState(tab);
    _livecodeSyncGlobalRunningFromActiveTab();
  }
  _livecodeRenderChatTabs();
  if (livecodeProjectPath) _livecodeSaveTabsForProject(livecodeProjectPath);
  return tabId;
}

let _livecodeChatTabsBound = false;

function _livecodeBindChatTabControlsOnce() {
  if (_livecodeChatTabsBound) return;
  _livecodeChatTabsBound = true;
  const addBtn = document.getElementById("livecode-chat-tab-add");
  if (addBtn) {
    addBtn.addEventListener("click", function(e) {
      e.preventDefault();
      window.createLiveCodeChatTab();
    });
  }
  const historyBtn = document.getElementById("livecode-chat-session-history");
  if (historyBtn) {
    historyBtn.addEventListener("click", function(e) {
      e.preventDefault();
      e.stopPropagation();
      window.toggleLiveCodeSessionMenu();
    });
  }
  const backgroundStatusBtn = document.getElementById("livecode-chat-background-status");
  if (backgroundStatusBtn) {
    backgroundStatusBtn.addEventListener("click", function(e) {
      e.preventDefault();
      e.stopPropagation();
      window.toggleLiveCodeSessionMenu();
    });
  }
  const projectTabsStrip = document.getElementById("ide-project-tabs");
  if (projectTabsStrip) {
    projectTabsStrip.addEventListener("click", function(e) {
      if (!e.target.closest("#ide-activity-recent")) return;
      e.preventDefault();
      e.stopPropagation();
      window.toggleLiveCodeProjectMenu();
    });
  }
  const sessionMenu = document.getElementById("livecode-chat-session-menu");
  if (sessionMenu) {
    sessionMenu.addEventListener("click", function(e) { e.stopPropagation(); });
  }
  const projectMenu = document.getElementById("livecode-project-menu");
  if (projectMenu) {
    projectMenu.addEventListener("click", function(e) { e.stopPropagation(); });
  }
  if (!window._livecodeSessionMenuGlobalBound) {
    window._livecodeSessionMenuGlobalBound = true;
    document.addEventListener("click", function(e) {
      if (_livecodeSessionMenuOpen) {
        if (!e.target.closest("#livecode-chat-session-menu") &&
            !e.target.closest("#livecode-chat-session-history") &&
            !e.target.closest("#livecode-chat-background-status")) {
          closeLiveCodeSessionMenu();
        }
      }
      if (_livecodeProjectMenuOpen) {
        if (!e.target.closest("#livecode-project-menu") &&
            !e.target.closest("#ide-activity-recent") &&
            !e.target.closest(".livecode-chat-session-item-menu")) {
          closeLiveCodeProjectMenu();
        }
      }
    });
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") {
        if (_livecodeSessionMenuOpen) closeLiveCodeSessionMenu();
        if (_livecodeProjectMenuOpen) closeLiveCodeProjectMenu();
      }
    });
  }
}

function _livecodeInitChatTabs() {
  _livecodeBindChatTabControlsOnce();
  if (livecodeChatTabs.length) return;
  const sessionId = livecodeProjectPath
    ? _livecodeNewChatSessionId()
    : (livecodeAgentSessionId || _livecodeNewChatSessionId());
  livecodeChatTabCounter += 1;
  const tab = {
    id: "chat-" + livecodeChatTabCounter,
    title: "New chat",
    sessionId: sessionId,
    messagesHtml: getLiveCodeChatOutput() ? getLiveCodeChatOutput().innerHTML : "",
    chatStarted: _livecodeChatStarted,
    abortController: null,
    agentRunning: false,
    hasUnread: false,
    costUsd: 0,
  };
  livecodeChatTabs.push(tab);
  livecodeActiveChatTabId = tab.id;
  livecodeAgentSessionId = sessionId;
  _livecodeRenderChatTabs();
}

function _livecodeUpdateActiveChatTabTitle(text, force) {
  const tab = _livecodeGetActiveChatTab();
  if (!tab) return;
  const nextTitle = _livecodeTruncateTabTitle(text);
  if (force || tab.title === "New chat" || !tab.title) {
    tab.title = nextTitle;
    _livecodeRenderChatTabs();
  }
}

window.createLiveCodeChatTab = function() {
  return _livecodeCreateChatTab("New chat");
};

window.switchLiveCodeChatTab = function(tabId) {
  _livecodeSwitchChatTab(tabId);
};

const _LIVECODE_REVERT_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 3.5 3 6l2.5 2.5"></path><path d="M3 6h6.25a3.75 3.75 0 0 1 0 7.5H6.5"></path></svg>';

function _livecodeDecorateUserRows(rows) {
  rows.forEach(function(row) {
    const bubble = row.querySelector(".chat-msg.user");
    if (!bubble || bubble.querySelector(".livecode-user-revert")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "livecode-user-revert";
    btn.title = "Restore checkpoint: undo file changes from here on and edit this message";
    btn.setAttribute("aria-label", "Restore checkpoint");
    btn.innerHTML = _LIVECODE_REVERT_ICON_SVG;
    bubble.appendChild(btn);
  });
}

function _livecodeSetComposerText(text) {
  const input = document.getElementById("livecode-chat-input");
  if (!input) return;
  input.textContent = String(text || "");
  input.classList.toggle("is-empty", !input.textContent);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
}

function _livecodeEnsureConfirmDialog() {
  let dialog = document.getElementById("livecode-confirm-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("div");
  dialog.id = "livecode-confirm-dialog";
  dialog.className = "livecode-confirm-dialog theme-transition";
  dialog.style.display = "none";
  dialog.innerHTML =
    '<div class="livecode-confirm-modal theme-transition" role="alertdialog" aria-modal="true" aria-labelledby="livecode-confirm-title" aria-describedby="livecode-confirm-message">' +
    '<div id="livecode-confirm-title" class="livecode-confirm-title"></div>' +
    '<div id="livecode-confirm-message" class="livecode-confirm-message"></div>' +
    '<pre id="livecode-confirm-detail" class="livecode-confirm-detail" hidden></pre>' +
    '<div class="livecode-confirm-footer">' +
    '<button type="button" class="livecode-confirm-btn theme-transition" data-livecode-confirm="cancel">Cancel</button>' +
    '<button type="button" class="livecode-confirm-btn is-primary theme-transition" data-livecode-confirm="ok">OK</button>' +
    "</div></div>";
  document.body.appendChild(dialog);
  return dialog;
}

function _livecodeOpenConfirmDialog(opts) {
  const dialog = _livecodeEnsureConfirmDialog();
  const detail = dialog.querySelector("#livecode-confirm-detail");
  const cancelBtn = dialog.querySelector('[data-livecode-confirm="cancel"]');
  const okBtn = dialog.querySelector('[data-livecode-confirm="ok"]');
  dialog.querySelector("#livecode-confirm-title").textContent = opts.title || "";
  dialog.querySelector("#livecode-confirm-message").textContent = opts.message || "";
  detail.textContent = opts.detail || "";
  detail.hidden = !opts.detail;
  cancelBtn.textContent = opts.cancelLabel || "Cancel";
  cancelBtn.style.display = opts.cancelLabel === null ? "none" : "";
  okBtn.textContent = opts.confirmLabel || "OK";
  dialog.classList.toggle("is-error", !!opts.isError);
  dialog.style.display = "flex";
  return new Promise(function(resolve) {
    function done(result) {
      dialog.style.display = "none";
      dialog.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKey, true);
      resolve(result);
    }
    function onClick(e) {
      const btn = e.target && e.target.closest ? e.target.closest("[data-livecode-confirm]") : null;
      if (btn) {
        e.stopPropagation();
        done(btn.getAttribute("data-livecode-confirm") === "ok");
      } else if (e.target === dialog) {
        e.stopPropagation();
        done(false);
      }
    }
    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        done(false);
      } else if (e.key === "Enter" && document.activeElement !== cancelBtn) {
        e.preventDefault();
        e.stopPropagation();
        done(true);
      }
    }
    dialog.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);
    okBtn.focus();
  });
}

function _livecodeShowErrorDialog(title, message, detail) {
  return _livecodeOpenConfirmDialog({
    title: title,
    message: message,
    detail: detail,
    confirmLabel: "Close",
    cancelLabel: null,
    isError: true,
  });
}

function _livecodeSessionPost(path, extra) {
  const tab = _livecodeGetActiveChatTab();
  if (!tab || !tab.sessionId) return Promise.reject(new Error("This chat has no saved session yet."));
  if (!livecodeProjectPath) return Promise.reject(new Error("No project is open."));
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({
      project_path: livecodeProjectPath,
      session_id: tab.sessionId,
      workspace: _livecodeCurrentWorkspacePayload(),
    }, extra || {})),
  }).then(function(resp) {
    return resp.text().then(function(text) {
      let data = null;
      try { data = JSON.parse(text); } catch (e) { data = null; }
      if (data && data.success) return data;
      const err = new Error((data && data.error) || "The server returned HTTP " + resp.status + (resp.statusText ? " " + resp.statusText : "") + ".");
      const failed = data && Array.isArray(data.failed) ? data.failed : [];
      err.detail = failed.length
        ? failed.join("\n")
        : (data ? "" : String(text || "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 400));
      err.status = resp.status;
      throw err;
    });
  });
}

function _livecodeRestoreRequest(userIndex) {
  return _livecodeSessionPost("/livecode/session/checkpoints/restore", { user_index: userIndex, rewind_chat: true });
}

function _livecodeRestoreCheckpoint(row) {
  const out = getLiveCodeChatOutput();
  const tab = _livecodeGetActiveChatTab();
  if (!out || !row || !tab) return;
  if (tab.agentRunning || livecodeAgentRunning) {
    _livecodeShowErrorDialog("Can't restore right now", "Stop the agent before restoring a checkpoint.", "");
    return;
  }
  const rows = Array.from(out.querySelectorAll(":scope > .chat-row.livecode-user-row"));
  const userIndex = rows.indexOf(row);
  if (userIndex < 0) return;
  _livecodeOpenConfirmDialog({
    title: "Restore to before this message?",
    message: "File changes the agent made from this message on will be undone, and the conversation will go back to this point.",
    confirmLabel: "Restore",
    cancelLabel: "Cancel",
  }).then(function(ok) {
    if (!ok) return null;
    const bubbleText = String((row.querySelector(".livecode-user-text") || row.querySelector(".chat-msg.user") || {}).textContent || "").trim();
    return _livecodeRestoreRequest(userIndex).then(function(data) {
      _livecodeReloadChangedFiles(data);
      let node = row;
      while (node) {
        const next = node.nextElementSibling;
        node.remove();
        node = next;
      }
      _livecodeCurrentUserRow = null;
      tab.messagesHtml = out.innerHTML;
      if (livecodeProjectPath) _livecodePersistChatSnapshot(livecodeProjectPath, tab);
      _livecodeSetComposerText(data.prompt || bubbleText);
      const changed = (data.restored || []).length + (data.removed || []).length;
      _livecodeShowIdeToast(changed ? "Restored " + changed + " file" + (changed === 1 ? "" : "s") : "Restored checkpoint");
    }).catch(function(err) {
      return _livecodeShowErrorDialog("Couldn't restore the checkpoint", (err && err.message) || "Unknown error.", (err && err.detail) || "");
    }).finally(function() {
      _livecodeRefreshPendingChanges();
    });
  });
}

function _livecodeBindUserRevertOnce() {
  if (window._livecodeUserRevertBound) return;
  window._livecodeUserRevertBound = true;
  document.addEventListener("click", function(e) {
    const btn = e.target && e.target.closest ? e.target.closest(".livecode-user-revert") : null;
    if (!btn || !btn.closest("#livecode-chat-messages")) return;
    e.preventDefault();
    e.stopPropagation();
    _livecodeRestoreCheckpoint(btn.closest(".chat-row.livecode-user-row"));
  }, true);
}

function _livecodeSyncStickyUserRows(out) {
  out = out || getLiveCodeChatOutput();
  if (!out) return;
  const rows = Array.from(out.querySelectorAll(":scope > .chat-row.livecode-user-row"));
  if (!rows.length) return;
  _livecodeDecorateUserRows(rows);
  _livecodeBindUserRevertOnce();
  const top = out.scrollTop;
  const naturalTops = rows.map(function(row) {
    const prev = row.previousElementSibling;
    if (!prev) return 0;
    const gap = Math.max(parseFloat(getComputedStyle(prev).marginBottom) || 0, parseFloat(getComputedStyle(row).marginTop) || 0);
    return prev.offsetTop + prev.offsetHeight + gap;
  });
  let current = 0;
  naturalTops.forEach(function(natural, i) { if (natural <= top + 1) current = i; });
  rows.forEach(function(row, i) {
    row.classList.toggle("is-unpinned", i !== current);
    row.classList.toggle("is-stuck", i === current && naturalTops[i] < top - 0.5);
  });
}

let _livecodeStickyRaf = null;

function _livecodeScheduleStickyUserRows(out) {
  if (_livecodeStickyRaf) return;
  _livecodeStickyRaf = requestAnimationFrame(function() {
    _livecodeStickyRaf = null;
    _livecodeSyncStickyUserRows(out);
  });
}

function _livecodeBindChatScrollWheel() {
  const out = getLiveCodeChatOutput();
  if (!out || out.dataset.wheelBound === "1") return;
  out.dataset.wheelBound = "1";
  out.addEventListener("wheel", function(e) {
    if (e.deltaY < 0) _livecodeScrollPinned = false;
    if (out.scrollHeight <= out.clientHeight + 1) return;
    const delta = e.deltaY;
    const atTop = out.scrollTop <= 0;
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 1;
    if ((delta < 0 && !atTop) || (delta > 0 && !atBottom)) {
      e.stopPropagation();
    }
  }, { passive: true, capture: true });
  out.addEventListener("scroll", function() {
    const gap = out.scrollHeight - out.clientHeight - out.scrollTop;
    if (gap <= 2) _livecodeScrollPinned = true;
    else if (gap > 40) _livecodeScrollPinned = false;
    _livecodeScheduleStickyUserRows(out);
  }, { passive: true });
  if (typeof ResizeObserver !== "undefined") {
    let lastHeight = out.clientHeight;
    new ResizeObserver(function() {
      const height = out.clientHeight;
      if (height === lastHeight) return;
      lastHeight = height;
      if (_livecodeScrollPinned) out.scrollTop = out.scrollHeight;
    }).observe(out);
  }
}

function _livecodeShowChatContainer() {
  _livecodeChatStarted = true;
  _livecodeSyncComposerPlaceholder();
}

function _livecodeIsPreservedChatRow(row) {
  if (!row) return true;
  if (row.classList.contains("livecode-user-row") ||
      row.classList.contains("livecode-assistant-row") ||
      row.classList.contains("livecode-agent-steps-row") ||
      row.classList.contains("livecode-status-row") ||
      row.classList.contains("livecode-diff-row") ||
      row.classList.contains("livecode-command-output") ||
      row.classList.contains("lazie-command-output") ||
      row.classList.contains("livecode-permission-row")) {
    return true;
  }

  if (row.querySelector(".livecode-diff-block, .livecode-code-card, .lazie-code-card, .livecode-json-display-block, .livecode-table-display-block, .livecode-csv-display-block")) {
    return true;
  }
  return false;
}

function _livecodeChatOutputBelongsToTab(out, tab) {
  if (!out || !tab) return false;
  if (!tab.sessionId) return true;
  const domSessionId = out.dataset ? (out.dataset.livecodeSessionId || "") : "";

  return !!domSessionId && domSessionId === tab.sessionId;
}

function _livecodeMarkChatOutputForTab(out, tab) {
  if (!out || !out.dataset) return;
  if (tab && tab.sessionId) out.dataset.livecodeSessionId = tab.sessionId;
  else delete out.dataset.livecodeSessionId;
}

function _livecodeSyncActiveTabMessagesHtml() {
  const tab = _livecodeGetActiveChatTab();
  const out = getLiveCodeChatOutput();
  if (tab && out && _livecodeIsActiveTab(tab) && _livecodeChatOutputBelongsToTab(out, tab)) {
    tab.messagesHtml = out.innerHTML;
    if (livecodeProjectPath && tab.sessionId && tab.messagesHtml) {
      _livecodePersistChatSnapshot(livecodeProjectPath, tab);
    }
  }
}

function _livecodeClearWelcomePlaceholder(out) {
  if (!out) return;
  Array.from(out.querySelectorAll(":scope > .chat-row")).forEach(function(row) {
    if (_livecodeIsPreservedChatRow(row)) return;
    if (row.querySelector(".livecode-plain-msg")) row.remove();
  });
}

function _livecodeStripStaleWelcome(out) {
  if (!out || !out.querySelector(".livecode-user-row")) return;
  _livecodeClearWelcomePlaceholder(out);
}

let _livecodeScrollPinned = true;
let _livecodeAutoScrollRaf = null;
let _livecodeAutoScrollTarget = null;

function _livecodeAutoScroll(out) {
  out = out || getLiveCodeChatOutput();
  if (out) _livecodeScheduleStickyUserRows(out);
  if (!out || !_livecodeScrollPinned) return;
  _livecodeAutoScrollTarget = out;
  if (_livecodeAutoScrollRaf) return;
  _livecodeAutoScrollRaf = requestAnimationFrame(function() {
    _livecodeAutoScrollRaf = null;
    const target = _livecodeAutoScrollTarget;
    _livecodeAutoScrollTarget = null;
    if (!target || !_livecodeScrollPinned) return;
    const bottom = target.scrollHeight - target.clientHeight;
    if (target.scrollTop < bottom - 1) target.scrollTop = bottom;
  });
}

function _livecodeScrollChatToBottom(out) {
  if (!out) return;
  _livecodeScrollPinned = true;
  out.scrollTop = out.scrollHeight;
  requestAnimationFrame(function() {
    try { out.scrollTop = out.scrollHeight; } catch (e) {}
  });
}

function _livecodeUpdateChatWelcome() {
  const out = getLiveCodeChatOutput();
  if (!out || _livecodeChatStarted) return;
  out.innerHTML = "";
}

function _setLiveCodeChatBusy(busy) {
  document.querySelectorAll('.chatbot-composer[data-sidebar-section="livecode"]').forEach(function(el) {
    el.classList.toggle("is-llm-answering", busy);
    var stop = el.querySelector(".chatbot-composer-stop-btn");
    if (stop) stop.style.display = busy ? "inline-flex" : "";
  });
  _livecodeSyncComposerDraftState();
  const chatOut = getLiveCodeChatOutput();
  if (chatOut) chatOut.classList.toggle("is-turn-running", !!busy || !!chatOut.querySelector(":scope > .livecode-user-row"));
  if (chatOut && !busy) _livecodeScheduleRegroup(chatOut);
  _livecodeRenderQueueBar();
  _livecodeRenderChangesBar(_livecodeChangesFiles);
}

function _livecodeIsGenericStatusMessage(msg) {
  const t = String(msg || "").trim();
  return !t || /^Working/i.test(t) || /^Explored project/i.test(t);
}

function _livecodeEscapeHtml(text) {
  return String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function _livecodeTypingMarkup() {
  return typeof window.livecodeChatWaitingMarkup === "function"
    ? window.livecodeChatWaitingMarkup()
    : '<div class="wb-chat-waiting" aria-busy="true"><span class="wb-chat-wait-dot"></span><span class="wb-chat-wait-dot"></span><span class="wb-chat-wait-dot"></span></div>';
}

function _livecodeGetAgentStepsRow(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return null;
  const rows = Array.from(output.querySelectorAll(".chat-row"));
  if (!rows.length) return null;
  const startIdx = _livecodeCurrentUserRow ? rows.indexOf(_livecodeCurrentUserRow) : -1;

  let lastIdx = rows.length - 1;
  while (lastIdx >= 0 && rows[lastIdx].classList.contains("livecode-assistant-row") &&
      !rows[lastIdx].classList.contains("livecode-narration-row")) {
    lastIdx -= 1;
  }
  if (lastIdx < 0) return null;
  const lastRow = rows[lastIdx];
  if (!lastRow.classList.contains("livecode-agent-steps-row")) return null;
  return lastIdx > startIdx ? lastRow : null;
}

function _livecodeGetAssistantAnchor(output, tab) {

  if (!output) return null;
  const t = tab || _livecodeGetActiveChatTab();
  if (t && t._assistantStreamRow && output.contains(t._assistantStreamRow)) {
    return t._assistantStreamRow;
  }
  return null;
}

function _livecodeAppendChatRow(output, row, tab) {
  if (!output || !row) return;
  const anchor = _livecodeGetAssistantAnchor(output, tab);
  if (anchor && anchor.parentNode === output) {
    output.insertBefore(row, anchor);
  } else {
    const statusRow = output.querySelector(".livecode-status-row, .livecode-status-row");
    if (statusRow && statusRow.parentNode === output) {
      output.insertBefore(row, statusRow);
    } else {
      output.appendChild(row);
    }
  }
}

function _livecodeEnsureAgentStepsRow(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return null;
  let stepsRow = _livecodeGetAgentStepsRow(output);
  if (!stepsRow) {
    stepsRow = document.createElement("div");
    stepsRow.className = "chat-row livecode-agent-steps-row";
    stepsRow.innerHTML = '<div class="chat-msg assistant livecode-agent-steps"></div>';
    _livecodeAppendChatRow(output, stepsRow);
  }
  return stepsRow.querySelector(".livecode-agent-steps");
}

let _livecodeLastToolLabel = "";
let _livecodeLastTool = "";
let _livecodeLastToolArgs = {};
let _livecodeRunningActivityEl = null;
let _livecodeThinkingStartMs = null;
let _livecodePendingDurationS = null;
let _livecodePendingDurationMs = null;
let _livecodePendingThoughtContent = "";
let _livecodeThoughtToggleBound = false;
let _livecodeStreamingThoughtContentEl = null;
let _livecodeThinkingTicker = null;
let _livecodeThoughtStreamFlushTimer = null;
let _livecodeThoughtStreamRaf = null;
let _livecodeLastProgressSeqBySession = {};
let _livecodeActiveTurnIdBySession = {};
const _LIVECODE_MAX_THOUGHT_CHARS = 6000;
const _LIVECODE_THOUGHT_STREAM_FLUSH_MS = 33;

function _livecodeCreateTurnCtx() {
  return {
    currentUserRow: null,
    assistantStreamEl: null,
    statusRow: null,
    statusMsg: null,
    runningActivityEl: null,
    lastToolLabel: "",
    lastTool: "",
    lastToolArgs: {},
    thinkingStartMs: null,
    pendingDurationS: null,
    pendingDurationMs: null,
    pendingThoughtContent: "",
    streamingThoughtContentEl: null,
  };
}

function _livecodeSaveTurnCtxToTab(tab) {
  if (!tab) return;
  if (!tab._turnCtx) tab._turnCtx = _livecodeCreateTurnCtx();
  const ctx = tab._turnCtx;
  ctx.currentUserRow = _livecodeCurrentUserRow;
  ctx.assistantStreamEl = _livecodeAssistantStreamEl;
  ctx.statusRow = _livecodeStatusRow;
  ctx.statusMsg = _livecodeStatusMsg;
  ctx.runningActivityEl = _livecodeRunningActivityEl;
  ctx.lastToolLabel = _livecodeLastToolLabel;
  ctx.lastTool = _livecodeLastTool;
  ctx.lastToolArgs = _livecodeLastToolArgs;
  ctx.thinkingStartMs = _livecodeThinkingStartMs;
  ctx.pendingDurationS = _livecodePendingDurationS;
  ctx.pendingDurationMs = _livecodePendingDurationMs;
  ctx.pendingThoughtContent = _livecodePendingThoughtContent;
  ctx.streamingThoughtContentEl = _livecodeStreamingThoughtContentEl;
}

function _livecodeLoadTurnCtxFromTab(tab) {
  if (!tab || !tab._turnCtx) {
    _livecodeResetTurnState();
    return;
  }
  const ctx = tab._turnCtx;
  _livecodeCurrentUserRow = ctx.currentUserRow;
  _livecodeAssistantStreamEl = ctx.assistantStreamEl;
  _livecodeStatusRow = ctx.statusRow;
  _livecodeStatusMsg = ctx.statusMsg;
  _livecodeRunningActivityEl = ctx.runningActivityEl;
  _livecodeLastToolLabel = ctx.lastToolLabel;
  _livecodeLastTool = ctx.lastTool;
  _livecodeLastToolArgs = ctx.lastToolArgs;
  _livecodeThinkingStartMs = ctx.thinkingStartMs;
  _livecodePendingDurationS = ctx.pendingDurationS;
  _livecodePendingDurationMs = ctx.pendingDurationMs;
  _livecodePendingThoughtContent = ctx.pendingThoughtContent;
  _livecodeStreamingThoughtContentEl = ctx.streamingThoughtContentEl;
}

function _livecodeWithTabContext(tab, fn) {
  const activeTab = _livecodeGetActiveChatTab();
  const isActive = !!tab && tab === activeTab;
  if (!isActive && activeTab) {
    _livecodeSaveTurnCtxToTab(activeTab);
  }
  if (tab) {
    if (!isActive) {
      const tabOutput = _livecodeGetOutputForTab(tab);
      if (tabOutput) _livecodeSyncTurnPointersFromOutput(tabOutput, tab);
    } else {
      const liveOut = getLiveCodeChatOutput();
      if (liveOut) _livecodeResolveRunningActivityEl(liveOut);
    }
    _livecodeLoadTurnCtxFromTab(tab);
    if (isActive) {
      const liveOut = getLiveCodeChatOutput();
      if (liveOut) _livecodeResolveRunningActivityEl(liveOut);
    }
  }
  try {
    return fn();
  } finally {
    if (tab) _livecodeSaveTurnCtxToTab(tab);
    if (!isActive && activeTab) {
      _livecodeLoadTurnCtxFromTab(activeTab);
    }
  }
}

function _livecodeFindRunningActivityWrap(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return null;
  const stepsContainer = _livecodeGetAgentStepsRow(output);
  if (!stepsContainer) return null;
  const outers = stepsContainer.querySelectorAll(".livecode-activity-wrap-outer.is-running");
  return outers.length ? outers[outers.length - 1] : null;
}

function _livecodeResolveRunningActivityEl(output) {
  output = output || getLiveCodeChatOutput();
  if (_livecodeRunningActivityEl && output && output.contains(_livecodeRunningActivityEl)) {
    return _livecodeRunningActivityEl;
  }
  const found = _livecodeFindRunningActivityWrap(output);
  if (found) _livecodeRunningActivityEl = found;
  return _livecodeRunningActivityEl;
}

function _livecodeCleanupStaleThinkingLines(output, keepWrap) {
  output = output || getLiveCodeChatOutput();
  const stepsContainer = _livecodeGetAgentStepsRow(output);
  if (!stepsContainer) return;
  stepsContainer.querySelectorAll(".livecode-activity-wrap-outer.is-running").forEach(function(outer) {
    if (outer === keepWrap) return;
    const text = outer.querySelector(".livecode-activity-text");
    const label = String(text && text.textContent ? text.textContent : "").trim();
    if (_LIVECODE_THINKING_LABEL_RE.test(label)) outer.remove();
  });
}

function _livecodeFindRunningThinkingWrap(output) {
  output = output || getLiveCodeChatOutput();
  const stepsContainer = _livecodeGetAgentStepsRow(output);
  if (!stepsContainer) return null;
  const thinking = [];
  stepsContainer.querySelectorAll(".livecode-activity-wrap-outer.is-running").forEach(function(outer) {
    const text = outer.querySelector(".livecode-activity-text");
    const label = String(text && text.textContent ? text.textContent : "").trim();
    if (_LIVECODE_THINKING_LABEL_RE.test(label)) thinking.push(outer);
  });
  const keep = thinking.length ? thinking[thinking.length - 1] : null;
  thinking.forEach(function(outer) {
    if (outer !== keep) outer.remove();
  });
  return keep;
}

function _livecodeAdoptRunningThinking(output, label) {
  const existing = _livecodeFindRunningThinkingWrap(output);
  if (!existing) return false;
  _livecodeRunningActivityEl = existing;
  _livecodeLastTool = "attempt_completion";
  _livecodeLastToolArgs = {};
  _livecodeLastToolLabel = label || "Thinking";
  if (!_livecodeThinkingStartMs) _livecodeThinkingStartMs = Date.now();
  const textEl = existing.querySelector(".livecode-activity-text");
  if (textEl) textEl.textContent = _livecodeLastToolLabel;
  _livecodeCleanupStaleThinkingLines(output, existing);
  return true;
}

function _livecodeComputeThoughtDuration() {
  if (!_livecodeThinkingStartMs) return 1;
  return Math.max(1, Math.round((Date.now() - _livecodeThinkingStartMs) / 1000));
}

function _livecodeComputeThoughtDurationMs() {
  if (!_livecodeThinkingStartMs) return 0;
  return Math.max(0, Date.now() - _livecodeThinkingStartMs);
}

function _livecodeThoughtDurationLabel(seconds, durationMs) {
  const ms = Number(durationMs);
  if (Number.isFinite(ms) && ms > 0) {
    return ms < 500 ? "briefly" : Math.max(1, Math.round(ms / 1000)) + "s";
  }
  const s = Number(seconds);
  return Number.isFinite(s) && s > 0 ? Math.round(s) + "s" : "briefly";
}

var _LIVECODE_THINKING_LABEL_RE = /^(Thinking|Planning next moves)\b/i;

function _livecodeIsThinkingLine() {
  return _livecodeLastTool === "attempt_completion" && _LIVECODE_THINKING_LABEL_RE.test(String(_livecodeLastToolLabel || "").trim());
}

function _livecodeStopThinkingTicker() {
  if (_livecodeThinkingTicker) {
    clearInterval(_livecodeThinkingTicker);
    _livecodeThinkingTicker = null;
  }
}

function _livecodeUpdateThinkingLineLabel() {
  _livecodeResolveRunningActivityEl();
  if (!_livecodeRunningActivityEl || !_livecodeIsThinkingLine()) return;
  const textEl = _livecodeRunningActivityEl.querySelector(".livecode-activity-text");
  if (!textEl) return;

  textEl.textContent = _livecodeLastToolLabel;
}

function _livecodeStartThinkingTicker() {
  _livecodeStopThinkingTicker();
  _livecodeUpdateThinkingLineLabel();
  _livecodeThinkingTicker = setInterval(function() {
    if (!_livecodeRunningActivityEl || !_livecodeIsThinkingLine() || !livecodeAgentRunning) {
      _livecodeStopThinkingTicker();
      return;
    }
    _livecodeUpdateThinkingLineLabel();
  }, 1000);
}

function _livecodeThoughtParts() {
  const durationMs = _livecodePendingDurationMs || _livecodeComputeThoughtDurationMs();
  return {
    verb: "Thought",
    detail: _livecodeThoughtDurationLabel(_livecodePendingDurationS, durationMs),
    meta: "",
    thoughtContent: String(_livecodePendingThoughtContent || "").trim(),
  };
}

function _livecodeBriefThoughtLine(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";

  if (/\n\s*\n/.test(raw) || /^#{1,6}\s/m.test(raw) || raw.length > 400) {
    return "";
  }
  const lines = raw.split(/\r?\n/);
  let line = "";
  for (let i = 0; i < lines.length; i++) {
    const t = String(lines[i] || "").replace(/\s+/g, " ").trim();
    if (t) {
      line = t;
      break;
    }
  }
  if (!line) return "";
  if (line.length > 160) return line.slice(0, 159).trimEnd() + "…";
  return line;
}

function _livecodeSubagentSummary(goal) {
  let g = String(goal || "").replace(/\s+/g, " ").trim();
  if (!g) return "";
  g = g.replace(/^[\w][\w-]{0,28}:\s+/, "");
  let s = g.split(/(?<=[.!?])\s+/)[0] || g;
  if (s.length > 88) s = s.slice(0, 85).replace(/\s+\S*$/, "") + "…";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function _livecodeBasename(path) {
  if (!path) return "";
  const p = String(path).replace(/\\/g, "/");
  const parts = p.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : p;
}

function _livecodeResolveProjectFilePath(filePath) {
  let p = String(filePath || "").trim().replace(/\\/g, "/");
  if (!p) return "";
  if (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
  if (p.startsWith("/") || /^[A-Za-z]:/.test(p)) return p;
  if (!livecodeProjectPath) return p;
  let base = String(livecodeProjectPath).replace(/\\/g, "/");
  if (base.length > 1 && base.endsWith("/")) base = base.slice(0, -1);
  const rel = p.replace(/^\.?\//, "");
  return base + "/" + rel;
}

function _livecodeWorkspaceMetaForArgs(args, pathValue) {
  const a = args || {};
  let workspace = String(a.workspace || "").trim();
  const rawPath = String(pathValue || a.file_path || a.path || a.directory || "").trim().replace(/\\/g, "/");
  if (!workspace && rawPath && !rawPath.startsWith("/") && !/^[A-Za-z]:/.test(rawPath)) {
    const first = rawPath.split("/").filter(Boolean)[0] || "";
    const match = (livecodeWorkspaceFolders || []).find(function(folder) {
      return String(folder.name || "") === first;
    });
    if (match) workspace = String(match.name || "").trim();
  }
  if (!workspace && rawPath && (livecodeWorkspaceFolders || []).length > 1) {
    const primary = (livecodeWorkspaceFolders || []).find(function(folder) { return folder.primary; });
    workspace = String((primary && primary.name) || "").trim();
  }
  return workspace ? "workspace " + workspace : "";
}

function _livecodeMergeActivityMeta(meta, workspaceMeta) {
  const left = String(meta || "").trim();
  const right = String(workspaceMeta || "").trim();
  if (!left) return right;
  if (!right) return left;
  return left + " · " + right;
}

function _livecodeFileActivityParts(verb, filePath, meta, args) {
  const detail = _livecodeBasename(filePath) || String(filePath || "").trim();
  const parts = { verb: verb, detail: detail, meta: _livecodeMergeActivityMeta(meta, _livecodeWorkspaceMetaForArgs(args, filePath)) };
  const abs = _livecodeResolveProjectFilePath(filePath);
  if (abs) parts.detailFilePath = abs;
  return parts;
}

function _livecodeIsFileEditTool(tool) {
  const t = String(tool || "").toLowerCase();
  return t === "edit_file" || t === "write_file" || t === "multi_edit";
}

function _livecodeEditLabelForKind(errorKind, errMsg) {
  const kind = String(errorKind || "").trim();
  const map = {
    multiple_matches: "Multiple matches found",
    no_matches: "No matches found",
    file_not_found: "File not found",
    invalid_input: "Invalid input",
  };

  const msg = String(errMsg || "").trim();
  if (kind === "file_not_found") return "File not found";
  if (kind === "invalid_input" && /missing required argument:\s*file_path/i.test(msg)) {
    return "Missing file path";
  }
  if (kind && map[kind]) return map[kind];
  if (/found multiple times/i.test(msg)) return "Multiple matches found";
  if (/not found in the file/i.test(msg)) return "No matches found";
  if (/^File not found:/i.test(msg)) return "File not found";
  if (/old string and new string are the same/i.test(msg)) return "Invalid input";
  return "Edit failed";
}

function _livecodeEditFailedParts(args, message, errorKind) {
  const a = args || {};
  const fp = _livecodeBasename(a.file_path) || "";
  const errMsg = String(message || "").trim();
  const kindStr = String(errorKind || "").trim();
  const parts = {
    verb: _livecodeEditLabelForKind(kindStr, errMsg),
    detail: fp || errMsg,
    meta: "",
    fullErrorTitle: errMsg,
    isError: true,

    suppressActivity: [
      "no_matches",
      "multiple_matches",
      "file_not_found",
      "invalid_input",
    ].includes(kindStr),
  };
  if (a.file_path) {
    const abs = _livecodeResolveProjectFilePath(a.file_path);
    if (abs) parts.detailFilePath = abs;
  }
  return parts;
}

function _livecodeClearTransientEditFailure(filePath, stepsContainer) {
  if (!stepsContainer || !filePath) return;
  const base = _livecodeBasename(filePath);
  const abs = _livecodeResolveProjectFilePath(filePath);
  const outers = Array.from(stepsContainer.querySelectorAll(".livecode-activity-wrap-outer"));
  if (!outers.length) return;

  for (let i = outers.length - 1; i >= 0; i--) {
    const outer = outers[i];
    if (outer.classList.contains("is-running")) continue;
    const line = outer.querySelector(".livecode-activity-line.is-error");
    if (!line) continue;
    const detail = outer.querySelector(".livecode-activity-detail");
    if (!detail) continue;
    const text = (detail.textContent || "").trim();
    const linkedPath = outer.querySelector("[data-file-path]");
    const detailPath = linkedPath ? String(linkedPath.getAttribute("data-file-path") || "") : "";
    if (text === base || (abs && detailPath === abs)) {
      outer.remove();
      return;
    }
  }
}

function _livecodeRecordEditFailure(args, message, errorKind, output) {
  output = output || getLiveCodeChatOutput();
  const stepsContainer = _livecodeEnsureAgentStepsRow(output);
  if (!stepsContainer) return null;
  const parts = _livecodeEditFailedParts(args, message, errorKind);

  if (_livecodeRunningActivityEl) {
    _livecodeStopThinkingTicker();
    _livecodeRunningActivityEl.remove();
    _livecodeRunningActivityEl = null;
    _livecodePendingDurationS = null;
    _livecodePendingDurationMs = null;
    _livecodeCancelThoughtStreamFlush();
    _livecodePendingThoughtContent = "";
    _livecodeStreamingThoughtContentEl = null;
    _livecodeThinkingStartMs = null;
  }
  if (!parts.suppressActivity) {
    _livecodeAppendActivityParts(parts, false, output);
    _livecodeAutoScroll(output);
  }
  return parts;
}

function _livecodeTruncateMiddle(str, maxLen) {
  const s = String(str || "");
  const n = Math.max(0, maxLen | 0);
  if (!n || s.length <= n) return s;
  if (n <= 1) return "…";
  const head = Math.ceil((n - 1) / 2);
  const tail = Math.floor((n - 1) / 2);
  return s.slice(0, head) + "…" + s.slice(s.length - tail);
}

function _livecodeMcpToolInfo(tool, args) {
  const t = String(tool || "");
  const a = args || {};
  if (t.indexOf("mcp__") === 0) {
    const rest = t.slice(5);
    const idx = rest.indexOf("__");
    return idx === -1 ? { isMcp: true, server: "", name: rest } : { isMcp: true, server: rest.slice(0, idx), name: rest.slice(idx + 2) };
  }
  if (t === "call_mcp_tool") return { isMcp: true, server: String(a.server_name || ""), name: String(a.tool_name || "") };
  return { isMcp: false, server: "", name: "" };
}

function _livecodeParseActivityParts(tool, args, message) {
  const a = args || {};
  const msg = String(message || "").trim();
  const t = String(tool || "").toLowerCase();
  if (t === "browser") return _livecodeBrowserParts(a, null);
  const mcpInfo = _livecodeMcpToolInfo(tool, a);
  if (mcpInfo.isMcp || /^Called MCP tool\b/i.test(msg)) {
    const toolName = mcpInfo.name || msg.replace(/^Called MCP tool\s*/i, "").trim();
    return { verb: "Ran", detail: toolName, meta: mcpInfo.server ? "(" + _livecodeMcpDisplayName(mcpInfo.server) + ")" : "", mcpArgs: a, isMcp: true, kind: "mcp" };
  }

  if (t === "grep_repo" || msg.startsWith("Grepped")) {
    const gm = msg.match(/Grepped\s+`([\s\S]+)`\s+in\s+(.+)$/i);
    if (gm) return { verb: "Grepped", detail: gm[1], meta: "in " + gm[2].trim(), kind: "search" };
    let pat = a.pattern != null ? String(a.pattern) : "";
    if (!pat) {
      const m = msg.match(/Grepped\s+`([\s\S]+)`/i);
      pat = m ? m[1] : msg.replace(/^Grepped\s*/i, "").replace(/`?\s*$/, "");
    }
    const scope = String(a.directory || a.path || "").trim();
    const gf = String(a.glob_filter || "").trim();
    let meta = "";
    if (scope && gf) meta = "in " + _livecodeBasename(scope) + " (" + gf + ")";
    else if (scope) meta = "in " + _livecodeBasename(scope);
    else if (gf) meta = "in " + gf;
    return { verb: "Grepped", detail: pat, meta: _livecodeMergeActivityMeta(meta, _livecodeWorkspaceMetaForArgs(a, scope)), kind: "search" };
  }
  if (t === "read_repo_file" || /^Read\s/i.test(msg)) {
    const fp = a.file_path || msg.replace(/^Read\s+/i, "").split(/\s+L\d/)[0].trim();
    let meta = "";
    const start = a.start_line;
    const end = a.end_line;
    if (start && end) meta = "L" + start + "-" + end;
    else if (start) meta = "L" + start + "+";
    else {
      const m = msg.match(/\s(L\d[\d+-]*)\s*$/);
      if (m) meta = m[1];
    }
    return Object.assign(_livecodeFileActivityParts("Read", fp, meta, a), { kind: "read" });
  }
  if (t === "list_repo_dir" || /^(Explored|Listed)\s/i.test(msg)) {
    const dir = String(a.directory || a.path || "").trim() || msg.replace(/^(Explored|Listed)\s+/i, "") || "project";
    return { verb: "Listed", detail: dir === "project" || dir === "." ? "project" : _livecodeBasename(dir) || dir, meta: _livecodeWorkspaceMetaForArgs(a, dir), kind: "list", listPath: dir };
  }
  if (t === "find_files" || /^Find files\b/i.test(msg)) {
    const d = String(a.query || "").trim() || msg.replace(/^Find files\s*/i, "").replace(/`/g, "").trim();
    const dir = String(a.path_prefix || "").trim();
    return { verb: "Searched files", detail: d || "project", meta: _livecodeMergeActivityMeta(dir ? "in " + _livecodeBasename(dir) : "", _livecodeWorkspaceMetaForArgs(a)), kind: "search" };
  }
  if (t === "glob_files" || /^Glob\b/i.test(msg)) {
    const d = String(a.pattern || "").trim() || msg.replace(/^Glob\s*/i, "").replace(/`/g, "").trim();
    const dir = String(a.path || "").trim();
    return { verb: "Searched files", detail: d || "project", meta: _livecodeMergeActivityMeta(dir ? "in " + _livecodeBasename(dir) : "", _livecodeWorkspaceMetaForArgs(a)), kind: "search" };
  }
  if (t === "find_symbol" || /^Find symbol\b/i.test(msg)) {
    const d = String(a.name || "").trim() || msg.replace(/^Find symbol\s*/i, "").replace(/`/g, "").trim();
    return { verb: "Searched", detail: d ? "symbol " + d : "symbols", meta: _livecodeWorkspaceMetaForArgs(a), kind: "search" };
  }
  if (t === "find_references" || /^Find refs\b/i.test(msg)) {
    const d = String(a.name || "").trim() || msg.replace(/^Find refs\s*/i, "").replace(/`/g, "").trim();
    return { verb: "Searched", detail: d ? "references to " + d : "references", meta: _livecodeWorkspaceMetaForArgs(a), kind: "search" };
  }
  if (t === "list_symbols" || /^List symbols\b/i.test(msg)) {
    const d = String(a.path || "").trim() || msg.replace(/^List symbols(\s+in)?\s*/i, "").trim() || "project";
    return { verb: "Listed", detail: "symbols in " + (d === "project" ? "project" : _livecodeBasename(d) || d), meta: _livecodeWorkspaceMetaForArgs(a, d), kind: "search" };
  }
  if (t === "ast_symbols" || t === "lsp_document_symbols") {
    const fp = a.file_path || msg.replace(/^(Explored|Outlined)\s+/i, "").trim();
    return Object.assign(_livecodeFileActivityParts("Outlined", fp, "", a), { kind: "read" });
  }
  if (_livecodeIsFileEditTool(t) || msg.startsWith("Editing")) {
    const fp = a.file_path || msg.replace(/^Editing\s+/i, "").trim();
    return Object.assign(_livecodeFileActivityParts("Edited", fp, "", a), { kind: "edit" });
  }
  if (t === "ask_question") {
    const count = Array.isArray(a.questions) ? a.questions.length : 0;
    const plural = count > 1 || (count === 0 && /\bquestions\b/i.test(msg));
    const noun = plural ? (count ? count + " questions" : "questions") : "question";
    return { verb: /^Skipped\b/i.test(msg) ? "Skipped" : "Asked", detail: noun, meta: "", kind: "question" };
  }
  if (t === "create_plan" || /^(Creating|Writing) plan\b/i.test(msg)) {
    return { verb: "Wrote plan", detail: "", meta: "", kind: "plan" };
  }
  if (t === "command_status") {
    const re = String(a.wait_for || a.pattern || "").trim();
    return { verb: "Waited", detail: re ? "for " + re : "for background command", meta: "", kind: "other" };
  }
  if (t === "kill_command") {
    return { verb: "Stopped", detail: "background command", meta: "", kind: "other" };
  }
  if (t === "run_command" || msg.startsWith("Running")) {
    const rawCmd = String(a.command || msg.replace(/^Running\s*`?/i, "").replace(/`?\s*$/, ""));
    const cmd = typeof window.livecodeCommandHeaderLabel === "function" ? window.livecodeCommandHeaderLabel(rawCmd) : rawCmd.slice(0, 72);
    return { verb: "Ran", detail: cmd, meta: "", kind: "shell" };
  }
  if (t === "web_search") {
    return { verb: "Searched web", detail: String(a.query || "").trim(), meta: "", kind: "search" };
  }
  if (t === "web_fetch") {
    return { verb: "Fetched page", detail: String(a.url || "").trim(), meta: "", kind: "fetch" };
  }
  if (t === "memory_search") {
    return { verb: "Searched", detail: "memory for " + String(a.query || "").trim(), meta: "", kind: "search" };
  }
  if (t === "memory_get") {
    return { verb: "Read", detail: "memory", meta: "", kind: "read" };
  }
  if (t === "update_memory" || /^Updated memory\b/i.test(msg)) {
    return { verb: "Updated memory", detail: "", meta: "", kind: "other" };
  }
  if (/^Thinking$/i.test(msg)) {
    return { verb: "Thinking", detail: "", meta: "", kind: "thinking" };
  }
  if (t === "attempt_completion" || msg === "Thought briefly" || /^Thought\b/i.test(msg)) {
    const m = msg.match(/(?:for\s+)?(\d+)\s*s\b/i);
    if (m) return { verb: "Thought", detail: _livecodeThoughtDurationLabel(m[1]), meta: "", kind: "thinking" };
    const detail = msg.replace(/^Thought\s*/i, "").trim();
    if (detail && detail !== "briefly") return { verb: "Thought", detail: detail, meta: "", kind: "thinking" };
    const durationMs = _livecodePendingDurationMs || _livecodeComputeThoughtDurationMs();
    return { verb: "Thought", detail: _livecodeThoughtDurationLabel(_livecodePendingDurationS, durationMs), meta: "", kind: "thinking" };
  }
  if (t === "error" || /^Failed\b/i.test(msg)) {
    const rawDetail = msg.replace(/^Failed\s+/i, "");
    return { verb: "Failed", detail: _livecodeTruncateMiddle(rawDetail, 140), meta: "", isError: true, fullErrorTitle: rawDetail, kind: "error" };
  }
  if (/^Indexed\s/i.test(msg)) {
    return { verb: "Indexed", detail: msg.replace(/^Indexed\s+/i, ""), meta: "", kind: "other" };
  }
  if (/^Found\s/i.test(msg)) {
    return { verb: "Found", detail: msg.replace(/^Found\s+/i, ""), meta: "", kind: "search" };
  }
  if (/^Exit\s/i.test(msg)) {
    return { verb: "Exit", detail: msg.replace(/^Exit\s+/i, ""), meta: "", kind: "other" };
  }
  if (t === "spawn_subagent" || /^Subagent\b/i.test(msg)) {
    const goal = String(a.goal || msg.replace(/^Subagent\s*:?\s*/i, "")).trim();
    return { verb: "Completed task", detail: String(a.title || "").trim() || _livecodeSubagentSummary(goal) || "Task", meta: "", isSubagent: true, subagentGoal: goal, kind: "task" };
  }
  if (t === "git_log") {
    const meta = a.grep ? "matching " + String(a.grep).slice(0, 40) : "";
    if (a.path) return Object.assign(_livecodeFileActivityParts("Checked", a.path, meta, a), { detail: "history of " + _livecodeBasename(a.path), kind: "search" });
    return { verb: "Checked", detail: "git history", meta: meta, kind: "search" };
  }
  if (t === "todo_write" || /^Updated task list\b/i.test(msg)) {
    let n = Array.isArray(a.todos) ? a.todos.length : 0;
    if (!n) {
      const m = msg.match(/\((\d+)\s+items?\)/i);
      if (m) n = parseInt(m[1], 10);
    }
    const done = Array.isArray(a.todos) ? a.todos.filter(function(td) { return td && td.status === "completed"; }).length : 0;
    return { verb: "Updated todos", detail: n ? done + " of " + n + " done" : "", meta: "", kind: "other" };
  }
  if (t === "update_goal" || /^Goal\b/i.test(msg)) {
    let detail = "progress";
    if (a.completed || /complete/i.test(msg)) detail = "complete";
    else if (a.blocked_reason || /blocked/i.test(msg)) detail = "blocked";
    return { verb: "Updated goal", detail: detail, meta: "", kind: "other" };
  }
  if (t === "compaction") {
    return { verb: "Compacted", detail: "conversation history", meta: a.forced ? "context limit" : "", kind: "other" };
  }
  if (t === "lsp_diagnostics") {
    const fp = a.file_path ? _livecodeBasename(String(a.file_path)) : "";
    return { verb: "Read lints", detail: fp, meta: "", kind: "lint" };
  }
  if (t === "lsp_definition" || t === "lsp_references" || t === "lsp_hover") {
    const fp = a.file_path ? _livecodeBasename(String(a.file_path)) : "";
    const loc = a.line ? ":" + a.line : "";
    const detail = { lsp_definition: "definition in ", lsp_references: "references in ", lsp_hover: "" }[t] + fp + loc;
    return { verb: t === "lsp_hover" ? "Inspected" : "Found", detail: detail.trim(), meta: "", kind: "search" };
  }
  return { verb: msg.split(/\s/)[0] || "Working", detail: msg.split(/\s/).slice(1).join(" "), meta: "", kind: "other" };
}

function _livecodeMarkdownToSafeHtml(text) {
  var md = String(text || "");
  if (typeof window._linkifyBareUrlsInMarkdownGlobal === "function") {
    md = window._linkifyBareUrlsInMarkdownGlobal(md);
  }
  if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    return DOMPurify.sanitize(marked.parse(md));
  }
  return _livecodeEscapeHtml(md).replace(/\n/g, "<br>");
}

function _livecodePrepareThoughtMarkdown(text) {
  var md = String(text || "");
  if (typeof window._linkifyBareUrlsInMarkdownGlobal === "function") {
    md = window._linkifyBareUrlsInMarkdownGlobal(md);
  }
  return md;
}

function _livecodeRenderThoughtMarkdown(el, markdown) {
  if (!el) return;
  el.classList.add("livecode-markdown-content", "mdpdf-preview-body");
  const md = _livecodePrepareThoughtMarkdown(markdown);
  if (!md.trim()) {
    el.innerHTML = "";
    return;
  }
  if (typeof window.renderMarkdownLikeMdpdfPreview === "function") {
    window.renderMarkdownLikeMdpdfPreview(el, md, "livecode-thought-mermaid-");
    return;
  }
  el.innerHTML = _livecodeMarkdownToSafeHtml(md);
}

function _livecodeRenderThoughtBody(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  return '<div class="livecode-thought-content livecode-markdown-content mdpdf-preview-body"></div>';
}

function _livecodeUpgradeToStreamingThought() {
  if (!_livecodeRunningActivityEl) return null;
  const outer = _livecodeRunningActivityEl;
  let wrap = outer.querySelector(".livecode-thought-wrap");
  if (!wrap) {
    outer.innerHTML =
      '<div class="livecode-activity-wrap livecode-thought-wrap ui-collapsible is-expandable is-collapsed is-streaming" data-step-kind="thinking">' +
      '<span class="livecode-activity-line livecode-thought-toggle ui-collapsible-header is-running" data-expandable role="button" tabindex="0" aria-expanded="true" data-panel-open>' +
      _livecodeThoughtLabelHtml({ verb: "Thinking" }, true) + _livecodeActivityChevronHtml() + "</span>" +
      '<div class="livecode-thought-body" hidden><div class="livecode-thought-content livecode-markdown-content mdpdf-preview-body"></div></div></div>';
    wrap = outer.querySelector(".livecode-thought-wrap");
  }
  if (wrap) {
    wrap.classList.add("is-streaming");
    const body = wrap.querySelector(".livecode-thought-body");
    if (body) body.hidden = false;
  }
  _livecodeStreamingThoughtContentEl = outer.querySelector(".livecode-thought-content");
  if (_livecodeStreamingThoughtContentEl) {
    _livecodeStreamingThoughtContentEl.classList.add("livecode-markdown-content");
  }
  return _livecodeStreamingThoughtContentEl;
}

function _livecodeCancelThoughtStreamFlush() {
  if (_livecodeThoughtStreamFlushTimer) {
    clearTimeout(_livecodeThoughtStreamFlushTimer);
    _livecodeThoughtStreamFlushTimer = null;
  }
  if (_livecodeThoughtStreamRaf && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(_livecodeThoughtStreamRaf);
  }
  _livecodeThoughtStreamRaf = null;
}

function _livecodeFlushStreamingThoughtDom() {
  _livecodeThoughtStreamFlushTimer = null;
  _livecodeThoughtStreamRaf = null;
  const contentEl = _livecodeStreamingThoughtContentEl || _livecodeUpgradeToStreamingThought();
  if (!contentEl) return;
  contentEl.classList.add("livecode-markdown-content", "mdpdf-preview-body");
  _livecodeRenderStreamingMarkdown(contentEl, _livecodePrepareThoughtMarkdown(_livecodePendingThoughtContent));
  const body = contentEl.closest(".livecode-thought-body");
  if (body) body.classList.toggle("is-clipped", contentEl.scrollHeight > body.clientHeight + 1);
  _livecodeAutoScroll();
}

function _livecodeScheduleThoughtStreamFlush() {
  if (typeof requestAnimationFrame === "function") {
    if (_livecodeThoughtStreamRaf) return;
    _livecodeThoughtStreamRaf = requestAnimationFrame(_livecodeFlushStreamingThoughtDom);
    return;
  }
  if (_livecodeThoughtStreamFlushTimer) return;
  _livecodeThoughtStreamFlushTimer = setTimeout(
    _livecodeFlushStreamingThoughtDom,
    _LIVECODE_THOUGHT_STREAM_FLUSH_MS
  );
}

function _livecodeAppendThoughtDelta(delta) {
  const text = String(delta || "");
  if (!text) return;
  _livecodePendingThoughtContent += text;
  if (_livecodePendingThoughtContent.length > _LIVECODE_MAX_THOUGHT_CHARS * 2) {
    const cut = _livecodePendingThoughtContent.length - _LIVECODE_MAX_THOUGHT_CHARS;
    const para = _livecodePendingThoughtContent.indexOf("\n\n", cut);
    _livecodePendingThoughtContent = _livecodePendingThoughtContent.slice(para >= 0 ? para + 2 : cut);
  }

  if (!_livecodeStreamingThoughtContentEl) _livecodeUpgradeToStreamingThought();
  _livecodeScheduleThoughtStreamFlush();
}

function _livecodeFillThoughtBody(wrapOrLine, text) {
  if (!wrapOrLine) return;
  const cEl = wrapOrLine.querySelector(".livecode-thought-content");
  const md = String(text || "").trim();
  if (!cEl || !md) return;
  _livecodeRenderThoughtMarkdown(cEl, md);
}

function _livecodeFinalizeThoughtActivity(thoughtContent, output, opts) {
  opts = opts || {};
  _livecodeCancelThoughtStreamFlush();
  let thought = opts.answerPending
    ? ""
    : String(thoughtContent || _livecodePendingThoughtContent || "").trim();
  if (thought.length > _LIVECODE_MAX_THOUGHT_CHARS) {
    thought = thought.slice(-_LIVECODE_MAX_THOUGHT_CHARS).trim();
  }
  _livecodeStreamingThoughtContentEl = null;
  _livecodeStopThinkingTicker();

  _livecodePendingThoughtContent = thought;
  output = output || getLiveCodeChatOutput();
  _livecodeResolveRunningActivityEl(output);
  _livecodeCleanupStaleThinkingLines(output, _livecodeRunningActivityEl);
  const parts = _livecodeThoughtParts();
  if (_livecodeRunningActivityEl) {
    _livecodeRunningActivityEl.classList.remove("is-running");
    _livecodeRunningActivityEl.removeAttribute("data-livecode-thought-transient");
    _livecodeRunningActivityEl.innerHTML = _livecodeBuildActivityHtml(parts, false);
    _livecodeFillThoughtBody(_livecodeRunningActivityEl, parts.thoughtContent);
    _livecodeLastThoughtOuter = _livecodeRunningActivityEl;
    _livecodeRunningActivityEl = null;
  } else {
    const wrap = _livecodeAppendActivityParts(parts, false, output);
    _livecodeFillThoughtBody(wrap, parts.thoughtContent);
    _livecodeLastThoughtOuter = wrap;
  }
  _livecodeLastThoughtParts = parts;
  _livecodePendingThoughtContent = "";
  _livecodePendingDurationS = null;
  _livecodePendingDurationMs = null;
  _livecodeThinkingStartMs = null;
  _livecodeAutoScroll(output);
}

function _livecodeBindThoughtTogglesOnce() {
  if (_livecodeThoughtToggleBound) return;
  _livecodeThoughtToggleBound = true;
  function toggle(e) {
    const btn = e.target && e.target.closest ? e.target.closest(".livecode-thought-toggle") : null;
    if (!btn || !btn.closest("#livecode-chat-messages")) return;
    const wrap = btn.closest(".livecode-thought-wrap");
    if (!wrap) return;
    const body = wrap.querySelector(".livecode-thought-body");
    if (!body) return;
    e.preventDefault();
    const expanded = wrap.classList.contains("is-streaming")
      ? body.hidden
      : wrap.classList.toggle("is-expanded");
    wrap.classList.toggle("is-expanded", expanded);
    wrap.classList.toggle("is-collapsed", !expanded);
    body.hidden = !expanded;
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    btn.toggleAttribute("data-panel-open", expanded);
  }
  document.addEventListener("click", toggle);
  document.addEventListener("keydown", function(e) {
    if (e.key === "Enter" || e.key === " ") toggle(e);
  });
}

const _LIVECODE_LOADING_VERBS = {
  Grepped: "Grepping",
  Read: "Reading",
  Listed: "Listing",
  Edited: "Editing",
  Created: "Creating",
  Wrote: "Writing",
  Deleted: "Deleting",
  Ran: "Running",
  Thought: "Thinking",
  Thinking: "Thinking",
  Searched: "Searching",
  "Searched files": "Searching files",
  "Searched web": "Searching web",
  "Fetched page": "Fetching page",
  "Read lints": "Reading lints",
  Outlined: "Outlining",
  Found: "Finding",
  Indexed: "Indexing",
  Compacted: "Compacting",
  "Updated todos": "Updating todos",
  "Updated goal": "Updating goal",
  "Updated memory": "Updating memory",
  "Wrote plan": "Writing plan",
  Asked: "Asking questions",
  Waited: "Waiting",
  Stopped: "Stopping",
  Checked: "Checking",
  Inspected: "Inspecting",
  "Completed task": "Working on task",
  "Navigated to": "Navigating to",
  "Navigated back": "Navigating back",
  "Navigated forward": "Navigating forward",
  "Reloaded page": "Reloading page",
  "Took snapshot": "Taking snapshot",
  "Took screenshot": "Taking screenshot",
  Cropped: "Cropping",
  "Compared with": "Comparing with",
  Clicked: "Clicking",
  "Clicked on page": "Clicking on page",
  Typed: "Typing",
  Pressed: "Pressing",
  Scrolled: "Scrolling",
  "Waited for": "Waiting for",
  "Ran page script": "Running page script",
  "Opened new tab": "Opening new tab",
  "Switched tab": "Switching tab",
  "Closed tab": "Closing tab",
  "Browser tabs": "Listing browser tabs",
  "Resized browser to": "Resizing browser to",
  "Fit browser to tab": "Fitting browser to tab",
  "Took screenshot of tab": "Taking screenshot of tab",
  "Opened background tab": "Opening background tab",
  "Loaded Figma design": "Loading Figma design",
  "Read Figma layer": "Reading Figma layer",
  "Opened Figma link in tab": "Opening Figma link in tab",
  "Scrolled to": "Scrolling to",
};

function _livecodeRunningVerb(verb) {
  return _LIVECODE_LOADING_VERBS[verb] || verb;
}

window.refreshLivecodeShimmerStyle = function() {};

function _livecodeActivityDetailHtml(p, shimmer) {
  const detail = String(p.detail || "").trim();
  const meta = p.meta ? String(p.meta).trim() : "";
  if (!detail && !meta) return "";
  const text = [detail, meta].filter(Boolean).join(" ");
  const filePath = String(p.detailFilePath || "").trim();
  const attrs = filePath
    ? ' data-file-path="' + _livecodeEscapeHtml(filePath).replace(/"/g, "&quot;") + '"' +
      ' data-line-meta="' + _livecodeEscapeHtml(meta).replace(/"/g, "&quot;") + '"' +
      ' title="' + _livecodeEscapeHtml(filePath).replace(/"/g, "&quot;") + '"'
    : "";
  const cls = "livecode-activity-detail ui-tool-call-line-details" + (filePath ? " livecode-activity-file-link" : "") + (shimmer ? " ui-shimmer" : "");
  return ' <span class="' + cls + '"' + attrs + ">" + _livecodeEscapeHtml(text) + "</span>";
}

function _livecodeShouldShimmerActivity(p, running) {
  if (running || (p && p.shimmer)) return true;
  const verb = String((p && p.verb) || "").toLowerCase();
  const detail = String((p && p.detail) || "").toLowerCase();
  return (verb === "compacted" || verb === "compacting") && detail === "conversation history";
}

function _livecodeStepKind(p) {
  if (!p) return "other";
  if (p.kind) return p.kind;
  if (p.isError) return "error";
  const v = String(p.verb || "").toLowerCase();
  if (v === "thought" || v === "thinking" || v === "planning next moves") return "thinking";
  if (v === "read" || v === "reading" || v === "outlined" || v === "outlining") return "read";
  if (v === "listed" || v === "listing") return "list";
  if (v === "grepped" || v === "grepping" || v === "searched" || v === "searching" || v === "searched files" ||
      v === "searching files" || v === "searched web" || v === "searching web" || v === "found" || v === "finding") return "search";
  if (v === "fetched page" || v === "fetching page") return "fetch";
  if (v === "read lints" || v === "reading lints") return "lint";
  return "other";
}

function _livecodeActivityLabelHtml(p, withChevron, running) {
  const shimmer = _livecodeShouldShimmerActivity(p, running);
  const verb = _livecodeEscapeHtml(p.verb || "");
  if (shimmer && !p.detail && !p.meta) {
    return '<span class="livecode-activity-text ui-shimmer"><span class="livecode-activity-verb ui-tool-call-line-action">' + verb + "</span></span>" +
      (withChevron ? _livecodeActivityChevronHtml() : "");
  }
  const verbHtml = '<span class="livecode-activity-verb ui-tool-call-line-action">' + verb + "</span>";
  const icon = p.iconHtml ? '<span class="ui-tool-call-line-icon">' + p.iconHtml + "</span>" : "";
  return icon + '<span class="livecode-activity-text' + (shimmer ? " ui-shimmer" : "") + '">' + verbHtml + _livecodeActivityDetailHtml(p, false) + "</span>" +
    (withChevron ? _livecodeActivityChevronHtml() : "");
}

function _livecodeActivityChevronHtml() {
  return _livecodeIcon("chevron-right", { className: "ui-collapsible-chevron livecode-activity-chevron" });
}

function _livecodeBuildActivityHtml(parts, running) {
  const p = Object.assign({}, parts || { verb: "", detail: "", meta: "" });
  if (p.isBrowser) return _livecodeBuildBrowserStepHtml(p, p.isError ? " is-error" : "", running);
  const isMcpCall = !!p.isMcp;
  if (running && p.verb) {
    p.verb = _livecodeRunningVerb(p.verb);
  }
  const runClass = running ? " is-running" : "";
  const errClass = p.isError ? " is-error" : "";
  const kind = _livecodeStepKind(parts);
  if (!running && p.verb === "Thought" && p.thoughtContent) {
    return '<div class="livecode-activity-wrap livecode-thought-wrap ui-collapsible is-expandable is-collapsed" data-step-kind="thinking">' +
      '<span class="livecode-activity-line livecode-thought-toggle ui-collapsible-header' + errClass + '" data-expandable role="button" tabindex="0" aria-expanded="false">' +
      _livecodeThoughtLabelHtml(p, false) + _livecodeActivityChevronHtml() + "</span>" +
      '<div class="livecode-thought-body" hidden>' + _livecodeRenderThoughtBody(p.thoughtContent) + "</div>" +
      "</div>";
  }
  if (isMcpCall) {
    return _livecodeBuildMcpCallHtml(p, errClass, running);
  }
  let titleAttr = "";
  if (p.fullErrorTitle) {
    titleAttr = ' title="' + _livecodeEscapeHtml(p.fullErrorTitle).replace(/"/g, "&quot;") + '"';
  }
  const clickable = p.detailFilePath && !running ? " ui-tool-call-line--clickable" : "";
  const dimmed = p.dimmed ? " is-dimmed" : "";
  return '<div class="livecode-activity-wrap" data-step-kind="' + kind + '">' +
    '<span class="livecode-activity-line ui-tool-call-line' + clickable + dimmed + runClass + errClass + '"' + titleAttr + ">" +
    _livecodeActivityLabelHtml(p, false, running) + "</span></div>";
}

function _livecodeThoughtLabelHtml(p, running) {
  if (running) {
    return '<span class="livecode-activity-text ui-shimmer"><span class="livecode-activity-verb ui-collapsible-action">' + _livecodeEscapeHtml(p.verb || "Thinking") + "</span></span>";
  }
  return '<span class="livecode-activity-text">' +
    '<span class="livecode-activity-verb ui-collapsible-action">' + _livecodeEscapeHtml(p.verb || "Thought") + "</span>" +
    (p.detail ? ' <span class="livecode-activity-detail ui-collapsible-details">' + _livecodeEscapeHtml(p.detail) + "</span>" : "") +
    "</span>";
}

const _LIVECODE_MCP_ERROR_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
const _LIVECODE_MCP_CALL_CHEVRON_SVG = '<span class="livecode-activity-chevron" aria-hidden="true"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg></span>';

function _livecodeHighlightJson(text) {
  return _livecodeEscapeHtml(text).replace(/"/g, "&quot;").replace(/(&quot;(?:\\.|(?!&quot;).)*&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}\[\],:])/g, function(m, str, colon, kw, num, punct) {
    if (str) return '<span class="' + (colon ? "livecode-json-key" : "livecode-json-str") + '">' + str + "</span>" + (colon ? '<span class="livecode-json-punct">' + colon + "</span>" : "");
    if (kw) return '<span class="livecode-json-kw">' + kw + "</span>";
    if (num) return '<span class="livecode-json-num">' + num + "</span>";
    return '<span class="livecode-json-punct">' + punct + "</span>";
  });
}

function _livecodeBuildMcpCallHtml(p, errClass, running) {
  const toolName = String(p.detail || "").replace(/^`|`$/g, "");
  const badge = toolName ? '<span class="livecode-mcp-call-badge">' + _livecodeEscapeHtml(toolName) + "</span>" : "";
  const status = !running && p.isError
    ? '<span class="livecode-mcp-call-status is-error" aria-hidden="true">' + _LIVECODE_MCP_ERROR_SVG + "</span>"
    : "";
  const hasArgs = p.mcpArgs && typeof p.mcpArgs === "object" && Object.keys(p.mcpArgs).length > 0;
  const wrapClass = "livecode-activity-wrap livecode-thought-wrap livecode-mcp-call-wrap" + (hasArgs ? " is-expandable is-collapsed" : "");
  const chevron = hasArgs ? _LIVECODE_MCP_CALL_CHEVRON_SVG : "";
  const role = hasArgs ? ' role="button" tabindex="0"' : "";
  const toggleClass = "livecode-activity-line livecode-thought-toggle livecode-mcp-call-toggle" + (running ? " is-running" : "") + errClass;
  const verb = running ? "Calling MCP tool" : "Called MCP tool";
  const label = chevron + '<span class="livecode-activity-text"><span class="livecode-activity-verb">' + verb + "</span>" + badge + "</span>" + status;
  const body = hasArgs
    ? '<div class="livecode-mcp-call-body livecode-thought-body" hidden><pre class="livecode-mcp-call-args">' + _livecodeHighlightJson(JSON.stringify(p.mcpArgs, null, 2)) + "</pre></div>"
    : "";
  return '<div class="' + wrapClass + '" data-step-kind="mcp"><span class="' + toggleClass + '"' + role + ' aria-expanded="false">' + label + "</span>" + body + "</div>";
}

function _livecodeRemoveTransientThought(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  output.querySelectorAll("[data-livecode-thought-transient]").forEach(function(el) { el.remove(); });
}

function _livecodeIsGenericWorkingActivityText(text) {
  const label = String(text || "").trim();
  return /^Working\s*$/i.test(label);
}

function _livecodeRemoveGenericWorkingActivities(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  output.querySelectorAll(".livecode-activity-wrap-outer").forEach(function(el) {
    const text = el.querySelector(".livecode-activity-text");
    if (_livecodeIsGenericWorkingActivityText(text && text.textContent)) el.remove();
  });
}

function _livecodeAppendActivityParts(parts, running, output) {
  output = output || getLiveCodeChatOutput();
  if (!output || !parts || !parts.verb) return null;
  const activityLabel = [parts.verb, parts.detail, parts.meta].filter(function(s) { return s; }).join(" ");
  if (_livecodeIsGenericWorkingActivityText(activityLabel)) return null;
  const stepsContainer = _livecodeEnsureAgentStepsRow(output);
  if (!stepsContainer) return null;
  _livecodeRemoveTransientThought(output);
  _livecodeRemoveGenericWorkingActivities(output);
  const wrap = document.createElement("div");
  wrap.className = "livecode-activity-wrap-outer" + (running ? " is-running" : "");
  wrap.setAttribute("data-step-id", _livecodeNextStepId());
  wrap.innerHTML = _livecodeBuildActivityHtml(parts, running);
  stepsContainer.appendChild(wrap);
  if (running) _livecodeRunningActivityEl = wrap;
  _livecodeScheduleRegroup(output);
  _livecodeAutoScroll(output);
  return wrap;
}

function _livecodeFinalizeRunningActivity(options, output) {
  const opts = options || {};
  output = output || getLiveCodeChatOutput();
  _livecodeResolveRunningActivityEl(output);
  if (!_livecodeRunningActivityEl) return;
  if (opts.remove) {
    _livecodeStopThinkingTicker();
    _livecodeCancelThoughtStreamFlush();
    _livecodeRunningActivityEl.remove();
    _livecodeRunningActivityEl = null;
    _livecodePendingDurationS = null;
    _livecodePendingDurationMs = null;
    _livecodePendingThoughtContent = "";
    _livecodeStreamingThoughtContentEl = null;
    _livecodeThinkingStartMs = null;
    return;
  }
  const parts = opts.editFailed
    ? _livecodeEditFailedParts(opts.args || _livecodeLastToolArgs, opts.message || "", opts.errorKind)
    : _livecodeIsThinkingLine()
      ? _livecodeThoughtParts()
      : _livecodeParseActivityParts(_livecodeLastTool, _livecodeLastToolArgs, _livecodeLastToolLabel);
  if (opts.pastTense && !opts.editFailed && parts && _livecodeIsFileEditTool(_livecodeLastTool) && parts.verb === "Editing") {
    parts.verb = String(_livecodeLastTool || "").toLowerCase() === "write_file" ? "Wrote" : "Edited";
  }
  if (opts.editFailed) {
    _livecodeStopThinkingTicker();
    _livecodeCancelThoughtStreamFlush();
    _livecodeRunningActivityEl.remove();
    _livecodeRunningActivityEl = null;
    _livecodePendingDurationS = null;
    _livecodePendingDurationMs = null;
    _livecodePendingThoughtContent = "";
    _livecodeStreamingThoughtContentEl = null;
    _livecodeThinkingStartMs = null;
    _livecodeRecordEditFailure(opts.args || _livecodeLastToolArgs, opts.message || "", opts.errorKind, output);
    return;
  }
  if (_livecodeIsThinkingLine()) {
    _livecodeStopThinkingTicker();
  }
  _livecodeCancelThoughtStreamFlush();
  _livecodeRunningActivityEl.classList.remove("is-running");
  _livecodeRunningActivityEl.innerHTML = _livecodeBuildActivityHtml(parts, false);
  if (parts && parts.verb === "Thought") {
    _livecodeRunningActivityEl.setAttribute("data-livecode-thought-transient", "1");
  }
  _livecodeRunningActivityEl = null;
  _livecodePendingDurationS = null;
  _livecodePendingDurationMs = null;
  _livecodePendingThoughtContent = "";
  _livecodeStreamingThoughtContentEl = null;
  if (_livecodeIsThinkingLine() || (parts && parts.verb === "Thought")) {
    _livecodeThinkingStartMs = null;
  }
}

function _livecodeAppendCompletedActivity(tool, args, message) {
  const parts = _livecodeParseActivityParts(tool, args, message);
  _livecodeAppendActivityParts(parts, false);
}


let _livecodeStepSeq = 0;

function _livecodeNextStepId() {
  _livecodeStepSeq += 1;
  return "s" + Date.now().toString(36) + _livecodeStepSeq.toString(36);
}

const _LIVECODE_TOOL_STEP_KINDS = new Set(["read", "list", "search", "fetch", "lint", "edit", "shell", "mcp", "browser"]);
const _LIVECODE_STANDALONE_STEP_KINDS = new Set(["task", "plan", "question", "barrier"]);

const LIVECODE_CONVERSATION_DENSITIES = [
  { value: "balanced", label: "Balanced" },
  { value: "detailed", label: "Detailed" },
];

const LIVECODE_STEP_GROUPINGS = [
  { value: "grouped", label: "Grouped" },
  { value: "ungrouped", label: "Ungrouped" },
];

function _livecodeStepGrouping() {
  let value = "";
  try { value = _livecodeSettingsGet("stepGrouping"); } catch (e) {}
  return value === "ungrouped" ? "ungrouped" : "grouped";
}

function _livecodeConversationDensity() {
  let value = "";
  try { value = _livecodeSettingsGet("conversationDensity"); } catch (e) {}
  return value === "balanced" ? value : "detailed";
}

function _livecodeApplyConversationDensity() {
  const out = getLiveCodeChatOutput();
  if (out) _livecodeRegroupTranscript(out);
  livecodeChatTabs.forEach(function(tab) {
    if (tab._backgroundOutput) _livecodeRegroupTranscript(tab._backgroundOutput);
  });
}
const _livecodeRegroupPending = new Set();

function _livecodeScheduleRegroup(output) {
  output = output || getLiveCodeChatOutput();
  if (!output || _livecodeRegroupPending.has(output)) return;
  _livecodeRegroupPending.add(output);
  const run = function() {
    _livecodeRegroupPending.delete(output);
    _livecodeRegroupTranscript(output, { lastTurnOnly: true });
  };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
  else setTimeout(run, 16);
}

function _livecodeStepIdFor(el) {
  let id = el.getAttribute("data-step-id");
  if (!id) {
    id = _livecodeNextStepId();
    el.setAttribute("data-step-id", id);
  }
  return id;
}

function _livecodeLineStep(outer) {
  const inner = outer.querySelector("[data-step-kind]");
  let kind = inner ? inner.getAttribute("data-step-kind") : "";
  const textEl = outer.querySelector(".livecode-activity-text");
  const label = String(textEl ? textEl.textContent : outer.textContent || "").trim();
  if (!kind) kind = _livecodeStepKind({ verb: label.split(/\s+/)[0] });
  if (/^(Thinking|Planning next moves|Retrying model)/i.test(label)) kind = "thinking";
  const detailEl = outer.querySelector(".livecode-activity-detail");
  const detail = String(detailEl ? detailEl.textContent : "").trim();
  return {
    el: outer,
    row: outer.closest(".chat-row"),
    kind: kind,
    running: outer.classList.contains("is-running") || !!outer.querySelector(".is-running"),
    detail: detail,
    file: kind === "read" ? (detail.split(/\s+L\d/)[0] || detail) : "",
    browserAction: inner ? inner.getAttribute("data-browser-action") || "" : "",
    shot: !!(inner && inner.hasAttribute("data-browser-shot-step")),
  };
}

function _livecodeTurnSteps(rows) {
  const steps = [];
  rows.forEach(function(row) {
    const cl = row.classList;
    if (cl.contains("lc-group-row") || cl.contains("livecode-status-row")) return;
    if (cl.contains("livecode-agent-steps-row")) {
      const lines = row.querySelectorAll(".livecode-activity-wrap-outer");
      if (!lines.length) return;
      lines.forEach(function(outer) {
        _livecodeStepIdFor(outer);
        steps.push(_livecodeLineStep(outer));
      });
      return;
    }
    if (cl.contains("livecode-diff-row")) {
      const block = row.querySelector(".ui-edit-tool-call, .livecode-diff-block");
      steps.push({
        el: row,
        row: row,
        kind: "edit",
        running: false,
        file: block ? String(block.getAttribute("data-file-name") || "") : "",
        additions: block ? Number(block.getAttribute("data-additions")) || 0 : 0,
        deletions: block ? Number(block.getAttribute("data-deletions")) || 0 : 0,
      });
      _livecodeStepIdFor(row);
      return;
    }
    if (cl.contains("livecode-term-row")) {
      const card = row.querySelector(".livecode-term-card");
      const desc = card ? String(card.getAttribute("data-description") || "") : "";
      steps.push({ el: row, row: row, kind: "shell", running: !!(card && !card.hasAttribute("data-done")), description: desc });
      _livecodeStepIdFor(row);
      return;
    }
    steps.push({ el: row, row: row, kind: "barrier" });
  });
  return steps;
}

function _livecodeStepGroups(steps, density) {
  const groups = [];
  const laneStandalone = density === "balanced" || density === "detailed";
  let run = [];
  const flush = function() {
    let start = 0;
    let end = run.length;
    while (start < end && run[start].kind === "thinking") start++;
    while (end > start && run[end - 1].kind === "thinking") end--;
    const trimmed = run.slice(start, end);
    const acts = trimmed.filter(function(s) { return s.kind !== "thinking"; });
    if (acts.length > 1) groups.push(trimmed);
    run = [];
  };
  steps.forEach(function(step) {
    if (_LIVECODE_STANDALONE_STEP_KINDS.has(step.kind) || (laneStandalone && (step.kind === "edit" || step.kind === "shell" || (step.kind === "browser" && step.shot)))) flush();
    else run.push(step);
  });
  flush();
  return groups;
}

function _livecodeCountText(n, word, plural) {
  return n + " " + (n === 1 ? word : (plural || word + "s"));
}

function _livecodeGroupSummary(group) {
  const tools = group.filter(function(s) { return _LIVECODE_TOOL_STEP_KINDS.has(s.kind); });
  const loading = group.some(function(s) { return s.running; });
  const count = function(kind) { return tools.filter(function(s) { return s.kind === kind; }).length; };
  const edits = tools.filter(function(s) { return s.kind === "edit"; });
  const commands = count("shell");
  let additions = 0;
  let deletions = 0;
  edits.forEach(function(s) { additions += s.additions || 0; deletions += s.deletions || 0; });

  if (!tools.length) {
    const n = group.filter(function(s) { return s.kind !== "thinking"; }).length;
    return { action: loading ? "Running" : "Ran", details: _livecodeCountText(n, "tool"), stats: null, loading: loading };
  }
  if (commands && commands === tools.length) {
    const only = tools.length === 1 ? String(tools[0].description || "").trim().replace(/^run(?=\s|$)\s*/i, "") : "";
    return {
      action: loading ? "Running" : "Ran",
      details: only ? only.charAt(0).toUpperCase() + only.slice(1) : _livecodeCountText(commands, "command"),
      stats: null,
      loading: loading,
    };
  }

  const parts = [];
  const editedFiles = [];
  edits.forEach(function(s) {
    const name = _livecodeBasename(s.file || "");
    if (name && editedFiles.indexOf(name) < 0) editedFiles.push(name);
  });
  const fileCount = editedFiles.length || edits.length;
  if (fileCount) parts.push(fileCount === 1 && editedFiles[0] ? editedFiles[0] : _livecodeCountText(fileCount, "file"));
  const explored = [];
  const allowName = !fileCount;
  const dirs = tools.filter(function(s) { return s.kind === "list"; });
  const files = tools.filter(function(s) { return s.kind === "read"; });
  if (dirs.length) explored.push(allowName && dirs.length === 1 && dirs[0].detail ? dirs[0].detail : _livecodeCountText(dirs.length, "directory", "directories"));
  if (files.length) explored.push(allowName && !explored.length && files.length === 1 && files[0].file ? files[0].file : _livecodeCountText(files.length, "file"));
  const searches = count("search");
  if (searches) explored.push(_livecodeCountText(searches, "search", "searches"));
  const fetches = count("fetch");
  if (fetches) explored.push(_livecodeCountText(fetches, "fetch", "fetches"));
  if (count("lint")) explored.push("lints");
  const mcp = count("mcp");
  if (mcp) explored.push(_livecodeCountText(mcp, "tool"));
  const browsing = _livecodeBrowserGroupWords(tools);
  if (browsing && !explored.length && !fileCount && !commands) {
    return { action: loading ? browsing.loadingAction : browsing.action, details: browsing.details, stats: null, loading: loading };
  }
  if (browsing) explored.push(browsing.phrase);
  if (fileCount && explored.length) explored[0] = "explored " + explored[0];
  parts.push.apply(parts, explored);
  if (commands) parts.push("ran " + _livecodeCountText(commands, "command"));
  return {
    action: fileCount ? (loading ? "Editing" : "Edited") : (loading ? "Exploring" : "Explored"),
    details: parts.join(", "),
    stats: additions || deletions ? { additions: additions, deletions: deletions } : null,
    loading: loading,
  };
}

function _livecodeBrowserGroupWords(tools) {
  const steps = tools.filter(function(s) { return s.kind === "browser"; });
  if (!steps.length) return null;
  const pages = steps.filter(function(s) { return _LIVECODE_BROWSER_PAGE_ACTIONS.indexOf(s.browserAction) >= 0; }).length;
  const shots = steps.filter(function(s) { return s.browserAction === "screenshot" || s.browserAction === "crop"; }).length;
  const compares = steps.filter(function(s) { return s.browserAction === "compare"; }).length;
  const inspections = steps.filter(function(s) { return s.browserAction === "inspect"; }).length;
  const designs = steps.filter(function(s) { return s.browserAction === "figma"; }).length;
  const other = steps.length - pages - shots - compares - inspections - designs;
  const phrases = [];
  if (designs) phrases.push({ action: "Read", loadingAction: "Reading", details: _livecodeCountText(designs, "Figma design") });
  if (pages) phrases.push({ action: "Browsed", loadingAction: "Browsing", details: _livecodeCountText(pages, "page") });
  if (shots) phrases.push({ action: "Took", loadingAction: "Taking", details: _livecodeCountText(shots, "screenshot") });
  if (compares) phrases.push({ action: "Compared", loadingAction: "Comparing", details: _livecodeCountText(compares, "design") });
  if (inspections) phrases.push({ action: "Inspected", loadingAction: "Inspecting", details: _livecodeCountText(inspections, "element") });
  if (other) phrases.push({ action: "Ran", loadingAction: "Running", details: _livecodeCountText(other, "browser action") });
  const rest = phrases.slice(1).map(function(ph) { return ph.action.toLowerCase() + " " + ph.details; });
  return {
    action: phrases[0].action,
    loadingAction: phrases[0].loadingAction,
    details: [phrases[0].details].concat(rest).join(", "),
    phrase: phrases.map(function(ph) { return ph.action.toLowerCase() + " " + ph.details; }).join(", "),
  };
}

function _livecodeGroupHeaderHtml(summary, open) {
  const stats = summary.stats
    ? '<span class="lc-group-stats">' +
      (summary.stats.additions ? '<span class="lc-stat-added">+' + summary.stats.additions + "</span>" : "") +
      (summary.stats.deletions ? '<span class="lc-stat-removed">-' + summary.stats.deletions + "</span>" : "") +
      "</span>"
    : "";
  return '<div class="ui-collapsible ui-step-group-collapsible" data-tone="muted">' +
    '<div class="ui-collapsible-header lc-group-toggle" data-expandable role="button" tabindex="0" aria-expanded="' + (open ? "true" : "false") + '"' + (open ? " data-panel-open" : "") + ">" +
    '<span class="ui-collapsible-action' + (summary.loading ? " ui-shimmer" : "") + '">' + _livecodeEscapeHtml(summary.action) + "</span>" +
    (summary.details || stats ? '<span class="ui-collapsible-details">' + _livecodeEscapeHtml(summary.details) + stats + "</span>" : "") +
    _livecodeIcon("chevron-right", { className: "ui-collapsible-chevron" }) +
    "</div></div>";
}

function _livecodeSplitStepsRowAt(line) {
  const steps = line.parentNode;
  const row = steps && steps.closest(".chat-row");
  if (!row || steps.firstElementChild === line) return row;
  const next = document.createElement("div");
  next.className = "chat-row livecode-agent-steps-row";
  next.innerHTML = '<div class="chat-msg assistant livecode-agent-steps"></div>';
  const target = next.firstElementChild;
  let node = line;
  while (node) {
    const after = node.nextElementSibling;
    target.appendChild(node);
    node = after;
  }
  row.after(next);
  return next;
}

function _livecodeRegroupTranscript(output, opts) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  opts = opts || {};
  const density = _livecodeConversationDensity();
  const grouped = _livecodeStepGrouping() === "grouped";
  if (output.getAttribute("data-density") !== density) output.setAttribute("data-density", density);
  let rows = Array.from(output.children).filter(function(el) { return el.classList && el.classList.contains("chat-row"); });
  if (opts.lastTurnOnly) {
    let start = 0;
    for (let i = rows.length - 1; i >= 0; i--) {
      if (rows[i].classList.contains("livecode-user-row")) { start = i + 1; break; }
    }
    rows = rows.slice(start);
  }
  const headers = {};
  rows.forEach(function(row) {
    if (row.classList.contains("lc-group-row")) headers[row.getAttribute("data-group-key")] = row;
  });

  const turns = [];
  let turn = [];
  rows.forEach(function(row) {
    if (row.classList.contains("livecode-user-row")) {
      if (turn.length) turns.push(turn);
      turn = [];
    } else {
      turn.push(row);
    }
  });
  if (turn.length) turns.push(turn);

  const members = new Map();
  const keep = new Set();
  turns.forEach(function(turnRows) {
    const groups = grouped ? _livecodeStepGroups(_livecodeTurnSteps(turnRows), density) : [];
    groups.forEach(function(group) {
      const first = group[0];
      if (first.el.classList.contains("livecode-activity-wrap-outer")) {
        first.row = _livecodeSplitStepsRowAt(first.el);
      }
      const last = group[group.length - 1];
      if (last.el.classList.contains("livecode-activity-wrap-outer") && last.el.nextElementSibling) {
        _livecodeSplitStepsRowAt(last.el.nextElementSibling);
      }
      const groupRows = [];
      group.forEach(function(step) {
        const row = step.el.closest(".chat-row");
        if (row && groupRows.indexOf(row) < 0) groupRows.push(row);
      });
      if (!groupRows.length) return;
      const key = _livecodeStepIdFor(first.el);
      let header = headers[key];
      const open = !!(header && header.getAttribute("data-open") === "1");
      if (!header) {
        header = document.createElement("div");
        header.className = "chat-row lc-group-row";
        header.setAttribute("data-group-key", key);
      }
      const html = _livecodeGroupHeaderHtml(_livecodeGroupSummary(group), open);
      if (header._curHtml !== html) {
        header.innerHTML = html;
        header._curHtml = html;
      }
      if (header.nextElementSibling !== groupRows[0]) groupRows[0].before(header);
      keep.add(header);
      groupRows.forEach(function(row, i) {
        members.set(row, { key: key, first: i === 0, open: open });
      });
    });
  });

  Object.keys(headers).forEach(function(key) {
    if (!keep.has(headers[key])) headers[key].remove();
  });
  Array.from(output.children).forEach(function(row) {
    if (!row.classList || !row.classList.contains("chat-row") || row.classList.contains("lc-group-row")) return;
    const m = members.get(row);
    if (!m && opts.lastTurnOnly && rows.indexOf(row) < 0) return;
    row.classList.toggle("lc-group-member", !!m);
    row.classList.toggle("lc-group-first", !!(m && m.first));
    row.classList.toggle("is-group-collapsed", !!(m && !m.open));
    if (m) row.setAttribute("data-group-key", m.key);
    else row.removeAttribute("data-group-key");
    row.classList.toggle("lc-empty-row", row.classList.contains("livecode-agent-steps-row") && !row.querySelector(".livecode-activity-wrap-outer"));
  });
}

function _livecodeToggleStepGroup(header) {
  const row = header.closest(".lc-group-row");
  const output = row && row.parentNode;
  if (!row || !output) return;
  const open = row.getAttribute("data-open") !== "1";
  row.setAttribute("data-open", open ? "1" : "0");
  const key = row.getAttribute("data-group-key");
  header.setAttribute("aria-expanded", open ? "true" : "false");
  header.toggleAttribute("data-panel-open", open);
  row._curHtml = row.innerHTML;
  output.querySelectorAll('.chat-row.lc-group-member[data-group-key="' + key + '"]').forEach(function(member) {
    member.classList.toggle("is-group-collapsed", !open);
  });
}

function _livecodeBindStepGroupsOnce() {
  if (window._livecodeStepGroupsBound) return;
  window._livecodeStepGroupsBound = true;
  document.addEventListener("click", function(e) {
    const header = e.target && e.target.closest ? e.target.closest("#livecode-chat-messages .lc-group-toggle") : null;
    if (!header) return;
    e.preventDefault();
    _livecodeToggleStepGroup(header);
  });
  document.addEventListener("keydown", function(e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    const header = e.target && e.target.closest ? e.target.closest("#livecode-chat-messages .lc-group-toggle") : null;
    if (!header) return;
    e.preventDefault();
    _livecodeToggleStepGroup(header);
  });
}
_livecodeBindStepGroupsOnce();

function _livecodeToolCallArgs(tc) {
  try {
    const fn = (tc && tc.function) || {};
    const raw = fn.arguments || "{}";
    return typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {});
  } catch (_) {
    return {};
  }
}

function _livecodeHumanToolLabel(tool, args) {
  const a = args || {};
  if (tool === "grep_repo") {
    const pat = String(a.pattern || "").slice(0, 80);
    return a.glob_filter ? "Grepped `" + pat + "` in " + a.glob_filter : "Grepped `" + pat + "`";
  }
  if (tool === "find_files") {
    const q = String(a.query || "").trim();
    const ext = String(a.ext || "").trim();
    const prefix = String(a.path_prefix || "").trim();
    const details = [q ? "`" + q.slice(0, 80) + "`" : "project", ext ? ("ext " + ext) : "", prefix ? ("in " + prefix) : ""].filter(Boolean).join(" ");
    return "Find files " + details;
  }
  if (tool === "glob_files") {
    const pat = String(a.pattern || "").trim();
    const dir = String(a.path || "").trim();
    const details = [pat ? "`" + pat.slice(0, 80) + "`" : "project", dir ? ("in " + dir) : ""].filter(Boolean).join(" ");
    return "Glob " + details;
  }
  if (tool === "read_repo_file") {
    const fp = _livecodeBasename(a.file_path || "");
    if (a.start_line && a.end_line) return "Read " + fp + " L" + a.start_line + "-" + a.end_line;
    if (a.start_line) return "Read " + fp + " L" + a.start_line + "+";
    return "Read " + fp;
  }
  if (tool === "list_repo_dir") return "Explored " + (String(a.directory || "").trim() || "project");
  if (tool === "ast_symbols") return "Explored " + _livecodeBasename(a.file_path || "");
  if (_livecodeIsFileEditTool(tool)) return "Editing " + _livecodeBasename(a.file_path || "");
  if (tool === "run_command") return "Running `" + String(a.command || "").slice(0, 72) + "`";
  if (tool === "find_symbol") return "Find symbol `" + String(a.name || "").slice(0, 40) + "`";
  if (tool === "find_references") return "Find refs `" + String(a.name || "").slice(0, 40) + "`";
  if (tool === "list_symbols") return "List symbols in " + String(a.path || "project").slice(0, 40);
  if (tool === "update_memory") return "Updated memory";
  if (tool === "todo_write") {
    const n = Array.isArray(a.todos) ? a.todos.length : 0;
    return "Updated task list" + (n ? " (" + n + " item" + (n === 1 ? "" : "s") + ")" : "");
  }
  if (tool === "update_goal") {
    if (a.completed) return "Goal complete";
    if (a.blocked_reason) return "Goal blocked";
    return "Goal progress";
  }
  if (tool === "lsp_definition" || tool === "lsp_references" || tool === "lsp_hover" || tool === "lsp_diagnostics") {
    const verb = { lsp_definition: "Definition", lsp_references: "References", lsp_hover: "Hover", lsp_diagnostics: "Diagnostics" }[tool];
    const fp = _livecodeBasename(a.file_path || "");
    return verb + (fp ? " in " + fp + (a.line ? ":" + a.line : "") : "");
  }
  if (tool === "spawn_subagent") return "Subagent " + String(a.goal || "");
  if (tool === "attempt_completion") return "Thought briefly";
  if (tool === "git_log") {
    if (a.path && a.grep) return "Checked history of `" + a.path + "` matching `" + String(a.grep).slice(0, 40) + "`";
    if (a.path) return "Checked history of `" + a.path + "`";
    if (a.grep) return "Checked git history matching `" + String(a.grep).slice(0, 40) + "`";
    return "Checked git history";
  }
  const mcpInfo = _livecodeMcpToolInfo(tool, a);
  if (mcpInfo.isMcp) return "Called MCP tool " + (mcpInfo.name || tool);
  return tool || "Working";
}

function _livecodeAppendLoadedToolActivity(msg, output, next) {
  const calls = (msg && msg.tool_calls) || [];
  if (!calls.length) return;
  const nextRole = next ? next.role : "";
  calls.forEach(function(tc) {
    const fn = (tc && tc.function) || {};
    const tool = fn.name || "tool";
    if (tool === "attempt_completion") return;
    if (tool === "todo_write") return;
    if (tool === "spawn_subagent") {
      const agentArgs = _livecodeToolCallArgs(tc);
      const saved = msg.subagent || {};
      _livecodeAgentsAddRow(output, {
        id: tc.id || "",
        title: saved.title || agentArgs.title || _livecodeAgentTitleFromGoal(agentArgs.goal),
        goal: agentArgs.goal || saved.goal || "",
        model: saved.model || "",
        state: "done",
      });
      _livecodeAgentsFinishRow(output, tc.id || "", msg.subagent ? saved : { state: "done", outcome: "Done" }, agentArgs);
      return;
    }
    if (tool === "ask_question" && nextRole === "tool_artifact") return;
    const args = _livecodeToolCallArgs(tc);
    if (tool === "run_command") {
      if (nextRole === "tool_artifact" && next.tool_name === "run_command") return;
      _livecodeAppendLoadedCommandBlock(args.command || "", "", undefined, output, { description: args.description });
      return;
    }
    if (_livecodeIsFileEditTool(tool) && nextRole === "diff") return;
    if (tool === "browser" && nextRole === "tool_artifact" && next.tool_name === "browser") return;
    const parts = _livecodeParseActivityParts(tool, args, _livecodeHumanToolLabel(tool, args));
    if (parts && _livecodeIsFileEditTool(tool) && parts.verb === "Editing") {
      parts.verb = String(tool || "").toLowerCase() === "write_file" ? "Wrote" : "Edited";
    }
    _livecodeAppendActivityParts(parts, false, output);
  });
}


const _LIVECODE_AGENT_LOG_MAX = 40;
const _LIVECODE_AGENT_DEFAULT_STATUS = "Planning next moves";

function _livecodeAgentIconHtml(state) {
  if (state === "running") {
    return '<span class="composer-subagent-status-indicator composer-subagent-status-indicator--running">' + _livecodeDotGridHtml("xs") + "</span>";
  }
  const variant = state === "failed" ? "error" : "done";
  return '<span class="composer-subagent-status-indicator"><span class="composer-subagent-status-indicator__dot composer-subagent-status-indicator__dot--' + variant + '" aria-hidden="true"></span></span>';
}

function _livecodeAgentCount(n, word, plural) {
  return n + " " + (n === 1 ? word : (plural || word + "s"));
}

function _livecodeAgentTitleFromGoal(goal) {
  const first = String(goal || "").replace(/\s+/g, " ").trim().split(/(?<=[.!?;:])\s/)[0] || "";
  const words = first.split(" ").slice(0, 6).join(" ").replace(/[\s.;:-]+$/, "");
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

function _livecodeAgentDuration(seconds) {
  const s = Math.max(1, Math.round(Number(seconds) || 0));
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function _livecodeAgentsCard(output, create) {
  output = output || getLiveCodeChatOutput();
  if (!output) return null;
  let last = output.lastElementChild;
  while (last && (
    !last.classList.contains("chat-row") ||
    last.classList.contains("livecode-status-row") ||
    last.classList.contains("lc-group-row") ||
    (last.classList.contains("livecode-assistant-row") && !last.classList.contains("livecode-narration-row"))
  )) {
    last = last.previousElementSibling;
  }
  if (last && last.classList.contains("livecode-agents-row")) return last.querySelector(".livecode-agents-card");
  if (!create) return null;
  _livecodeBindAgentsCardOnce();
  const row = document.createElement("div");
  row.className = "chat-row livecode-agents-row";
  row.innerHTML = '<div class="livecode-agents-card" data-state="running"><div class="livecode-agents-list" role="list"></div></div>';
  _livecodeAppendChatRow(output, row);
  return row.querySelector(".livecode-agents-card");
}

function _livecodeAgentRow(output, agentId) {
  if (!output || !agentId) return null;
  const rows = output.querySelectorAll(".livecode-agent-row");
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].getAttribute("data-agent-id") === agentId) return rows[i];
  }
  return null;
}

function _livecodeSetAgentState(row, state) {
  if (row.getAttribute("data-state") === state) return;
  row.setAttribute("data-state", state);
  const icon = row.querySelector(".livecode-agent-icon");
  icon.innerHTML = _livecodeAgentIconHtml(state);
  icon.setAttribute("role", "img");
  icon.setAttribute("aria-label", { running: "Running", done: "Completed", failed: "Failed", stopped: "Stopped" }[state] || state);
}

function _livecodeSetAgentAction(row, text, live) {
  const el = row.querySelector(".livecode-agent-action");
  if (el.textContent !== text) el.textContent = text;
  el.classList.toggle("ui-shimmer", !!live);
  el.title = text;
}

function _livecodeSetAgentCounts(row, data) {
  ["files_read", "searches", "files_changed"].forEach(function(key) {
    const value = data[key];
    if (value == null) return;
    const n = Array.isArray(value) ? value.length : (Number(value) || 0);
    row.setAttribute("data-" + key.replace(/_/g, "-"), String(n));
  });
}

function _livecodeSetAgentModel(row, model) {
  const el = row.querySelector(".lc-subagent__model");
  const text = String(model || "").trim();
  if (el && text && el.textContent !== text) el.textContent = text;
}

function _livecodeSetAgentStats(row) {
  let added = 0;
  let removed = 0;
  row.querySelectorAll(".livecode-agent-file").forEach(function(file) {
    added += Number(file.getAttribute("data-additions")) || 0;
    removed += Number(file.getAttribute("data-deletions")) || 0;
  });
  const el = row.querySelector(".lc-subagent__stats");
  if (!el) return;
  el.innerHTML = (added ? '<span class="lc-stat-added">+' + added + "</span>" : "") + (removed ? '<span class="lc-stat-removed">-' + removed + "</span>" : "");
}

function _livecodeUpdateAgentsSummary(card) {
  if (!card) return;
  const running = card.querySelector('.livecode-agent-row[data-state="running"]');
  card.setAttribute("data-state", running ? "running" : "done");
}

function _livecodeAgentsAddRow(output, agent) {
  const id = String(agent.id || "");
  let row = id ? _livecodeAgentRow(output, id) : null;
  if (!row) {
    const card = _livecodeAgentsCard(output, true);
    if (!card) return null;
    row = document.createElement("div");
    row.className = "livecode-agent-row";
    row.setAttribute("role", "listitem");
    row.setAttribute("data-agent-id", id);
    row.setAttribute("data-subagent-task-card", "");
    row.innerHTML =
      '<div class="livecode-agent-head" data-subagent-task-card-header role="button" tabindex="0" aria-expanded="false">' +
      '<div class="lc-subagent__main">' +
      '<span class="livecode-agent-icon lc-subagent__indicator"></span>' +
      '<div class="lc-subagent__body">' +
      '<div class="lc-subagent__title-row"><span class="livecode-agent-title lc-subagent__title"></span><span class="lc-subagent__model"></span></div>' +
      '<span class="lc-subagent__status"><span class="livecode-agent-action lc-subagent__status-text"></span><span class="lc-subagent__stats"></span></span>' +
      "</div>" +
      '<span class="lc-subagent__chevron">' + _livecodeIcon("chevron-right", { size: "sm" }) + "</span>" +
      "</div></div>" +
      '<div class="livecode-agent-detail lc-subagent__detail" hidden>' +
      '<div class="livecode-agent-goal lc-subagent__goal"></div>' +
      '<ol class="livecode-agent-log lc-subagent__steps"></ol>' +
      '<div class="livecode-agent-files lc-subagent__files"></div>' +
      '<div class="livecode-agent-findings lc-subagent__findings"></div>' +
      '<div class="livecode-agent-findings-src" hidden></div></div>';
    card.querySelector(".livecode-agents-list").appendChild(row);
  }
  const title = String(agent.title || "").trim() || "Subagent";
  const goal = String(agent.goal || "").trim();
  row.querySelector(".livecode-agent-title").textContent = title;
  if (goal) row.querySelector(".livecode-agent-goal").textContent = goal;
  row.querySelector(".livecode-agent-head").title = goal || title;
  _livecodeSetAgentModel(row, agent.model);
  const state = agent.state || "running";
  _livecodeSetAgentState(row, state);
  if (state === "running") _livecodeSetAgentAction(row, _LIVECODE_AGENT_DEFAULT_STATUS, true);
  _livecodeUpdateAgentsSummary(row.closest(".livecode-agents-card"));
  return row;
}

function _livecodeAgentStepHtml(text) {
  const raw = String(text || "").trim();
  const m = raw.match(/^((?:Searched the web for|Searched memory for|Found references to|Finding references to|Listed symbols in|Read history of|Checked command|Found files|[A-Z][a-z]+))\s+(.*)$/);
  const verb = m ? m[1] : raw;
  const detail = m ? m[2].replace(/^["`]|["`]$/g, "") : "";
  return '<li class="ui-tool-call-line"><span class="ui-tool-call-line-action">' + _livecodeEscapeHtml(verb) + "</span>" +
    (detail ? '<span class="ui-tool-call-line-details">' + _livecodeEscapeHtml(detail) + "</span>" : "") + "</li>";
}

function _livecodeAppendAgentLog(row, text) {
  const log = row.querySelector(".livecode-agent-log");
  const tmp = document.createElement("ol");
  tmp.innerHTML = _livecodeAgentStepHtml(text);
  log.appendChild(tmp.firstElementChild);
  while (log.children.length > _LIVECODE_AGENT_LOG_MAX) log.removeChild(log.firstChild);
}

function _livecodeAgentFileHtml(file) {
  const path = String(file.path || "");
  const name = _livecodeBasename(path) || path;
  const open = _livecodeEscapeHtml(String(file.absolute_path || path)).replace(/"/g, "&quot;");
  const added = Number(file.additions) || 0;
  const deleted = Number(file.deletions) || 0;
  return '<div class="livecode-agent-file ui-tool-call-line ui-tool-call-line--clickable" data-path="' + _livecodeEscapeHtml(path).replace(/"/g, "&quot;") + '"' +
    ' data-additions="' + added + '" data-deletions="' + deleted + '">' +
    '<span class="ui-tool-call-line-icon"><img src="' + _livecodeEscapeHtml(_livecodeFileIcon(path)).replace(/"/g, "&quot;") + '" alt="" aria-hidden="true" draggable="false"></span>' +
    '<span class="ui-tool-call-line-details livecode-activity-file-link" data-file-path="' + open + '" title="' + _livecodeEscapeHtml(path).replace(/"/g, "&quot;") + '">' + _livecodeEscapeHtml(name) + "</span>" +
    (added || deleted ? '<span class="lc-group-stats">' +
      (added ? '<span class="lc-stat-added">+' + added + "</span>" : "") +
      (deleted ? '<span class="lc-stat-removed">-' + deleted + "</span>" : "") + "</span>" : "") +
    "</div>";
}

function _livecodeRenderAgentFiles(row, files) {
  row.querySelector(".livecode-agent-files").innerHTML = (files || [])
    .filter(function(f) { return f && f.path; })
    .map(_livecodeAgentFileHtml)
    .join("");
  _livecodeSetAgentStats(row);
}

function _livecodeAgentFileChanged(row, file) {
  const list = row.querySelector(".livecode-agent-files");
  const path = String(file.path || "");
  if (!path) return;
  const existing = Array.from(list.children).find(function(el) { return el.getAttribute("data-path") === path; });
  const total = {
    path: path,
    absolute_path: file.absolute_path || "",
    additions: (Number(file.additions) || 0) + (existing ? Number(existing.getAttribute("data-additions")) || 0 : 0),
    deletions: (Number(file.deletions) || 0) + (existing ? Number(existing.getAttribute("data-deletions")) || 0 : 0),
  };
  const tmp = document.createElement("div");
  tmp.innerHTML = _livecodeAgentFileHtml(total);
  if (existing) existing.replaceWith(tmp.firstElementChild);
  else list.appendChild(tmp.firstElementChild);
  _livecodeSetAgentStats(row);
}

function _livecodeAgentsApplyUpdate(output, data) {
  const id = String(data.agent_id || "");
  let row = _livecodeAgentRow(output, id);
  if (!row) row = _livecodeAgentsAddRow(output, { id: id, title: data.title || data.message || "", model: data.model, state: "running" });
  if (!row) return;
  const state = data.state || "running";
  _livecodeSetAgentState(row, state);
  _livecodeSetAgentCounts(row, data);
  _livecodeSetAgentModel(row, data.model);
  if (state === "running") {
    _livecodeSetAgentAction(row, String(data.action || "") || _LIVECODE_AGENT_DEFAULT_STATUS, true);
    if (data.action && !data.action_running) _livecodeAppendAgentLog(row, data.action);
  } else {
    _livecodeSetAgentAction(row, _livecodeAgentFinalStatus(state, data), false);
  }
  if (data.changed_file && data.changed_file.path) {
    _livecodeAgentFileChanged(row, data.changed_file);
    if (data.changed_file.absolute_path) refreshLiveCodeFileFromDisk(data.changed_file.absolute_path);
    _livecodeSchedulePendingChangesRefresh();
    _livecodeScheduleSilentTreeRefresh();
  }
  _livecodeUpdateAgentsSummary(row.closest(".livecode-agents-card"));
}

function _livecodeAgentFinalStatus(state, result) {
  if (state === "failed") return "Stopped with error";
  if (state === "stopped") return "Stopped";
  return "Completed";
}

function _livecodeAgentsFinishRow(output, agentId, result, args) {
  result = result || {};
  args = args || {};
  let row = _livecodeAgentRow(output, agentId);
  if (!row) {
    row = _livecodeAgentsAddRow(output, {
      id: agentId,
      title: result.title || args.title || "",
      goal: args.goal || result.goal || "",
      model: result.model,
      state: "done",
    });
  }
  if (!row) return;
  const state = result.state || (result.error ? "failed" : "done");
  _livecodeSetAgentState(row, state);
  _livecodeSetAgentModel(row, result.model);
  const changedFiles = Array.isArray(result.files_changed) ? result.files_changed : null;
  _livecodeSetAgentCounts(row, {
    files_read: result.files_read,
    searches: result.searches,
    files_changed: changedFiles,
  });
  _livecodeSetAgentAction(row, _livecodeAgentFinalStatus(state, result), false);
  const action = row.querySelector(".livecode-agent-action");
  if (state === "failed" && result.error) action.title = String(result.error);
  else if (result.duration_s != null) action.title = String(result.outcome || "Completed") + " · " + _livecodeAgentDuration(result.duration_s);
  if (Array.isArray(result.actions) && result.actions.length) {
    row.querySelector(".livecode-agent-log").innerHTML = "";
    result.actions.slice(-_LIVECODE_AGENT_LOG_MAX).forEach(function(step) { _livecodeAppendAgentLog(row, String(step)); });
  }
  if (changedFiles) _livecodeRenderAgentFiles(row, changedFiles);
  const findings = String(result.result || "").trim();
  const src = row.querySelector(".livecode-agent-findings-src");
  if (findings && src.textContent !== findings) {
    src.textContent = findings;
    row.querySelector(".livecode-agent-findings").innerHTML = "";
    if (!row.querySelector(".livecode-agent-detail").hidden) _livecodeRenderAgentFindings(row);
  }
  _livecodeUpdateAgentsSummary(row.closest(".livecode-agents-card"));
}

function _livecodeRenderAgentFindings(row) {
  const box = row.querySelector(".livecode-agent-findings");
  const src = row.querySelector(".livecode-agent-findings-src");
  if (!box || !src || box.childNodes.length || !src.textContent.trim()) return;
  _livecodeRenderAssistantMarkdown(box, src.textContent);
}

function _livecodeSettleRunningAgents(out) {
  if (!out) return;
  out.querySelectorAll('.livecode-agent-row[data-state="running"]').forEach(function(row) {
    _livecodeSetAgentState(row, "stopped");
    _livecodeSetAgentAction(row, "Stopped", false);
  });
  out.querySelectorAll(".livecode-agents-card").forEach(_livecodeUpdateAgentsSummary);
}

function _livecodeToggleAgentRow(head) {
  const row = head.closest(".livecode-agent-row");
  const detail = row && row.querySelector(".livecode-agent-detail");
  if (!detail) return;
  const open = detail.hidden;
  detail.hidden = !open;
  row.classList.toggle("is-open", open);
  head.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) _livecodeRenderAgentFindings(row);
}

function _livecodeBindAgentsCardOnce() {
  if (window._livecodeAgentsCardBound) return;
  window._livecodeAgentsCardBound = true;
  document.addEventListener("click", function(e) {
    const target = e.target && e.target.closest ? e.target : null;
    if (!target || !target.closest("#livecode-chat-messages")) return;
    if (target.closest(".livecode-agent-detail")) return;
    const head = target.closest(".livecode-agent-head");
    if (head) _livecodeToggleAgentRow(head);
  });
  document.addEventListener("keydown", function(e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    const head = e.target && e.target.closest ? e.target.closest("#livecode-chat-messages .livecode-agent-head") : null;
    if (!head) return;
    e.preventDefault();
    _livecodeToggleAgentRow(head);
  });
}

function _livecodeAppendLoadedActivitySummary(text, output) {
  const raw = String(text || "").trim();
  if (!raw) return;
  raw.split(/\n+/).map(function(line) { return line.trim(); }).filter(Boolean).forEach(function(line) {
    _livecodeAppendCompletedActivity("", {}, line);
  });
}

function _livecodeAppendLoadedToolArtifact(msg, output) {
  const tool = msg && msg.tool_name;
  const result = (msg && msg.result) || {};
  const args = (msg && msg.tool_args) || {};
  if (_livecodeIsFileEditTool(tool) && result.diff_html) {
    appendLiveCodeDiffBlock({
      file_name: result.file_path || args.file_path || "file",
      diff_html: result.diff_html,
      additions: result.additions || 0,
      deletions: result.deletions || 0,
      absolute_path: result.absolute_path || "",
      created: !!result.is_new_file,
    }, output);
    return;
  }
  if (tool === "run_command") {
    _livecodeAppendLoadedCommandBlock(result.command || args.command || "", result.output || result.error || "", result.exit_code, output, {
      description: args.description,
    });
    return;
  }
  if (tool === "browser") {
    _livecodeAppendActivityParts(_livecodeBrowserParts(args, result), false, output);
    return;
  }
  if (tool === "ask_question" && !result.error) {
    const count = Number(result.question_count) || (Array.isArray(args.questions) ? args.questions.length : 0);
    _livecodeAppendActivityParts({ verb: result.skipped ? "Skipped" : "Asked", detail: count === 1 ? "question" : "questions", meta: "" }, false, output);
    return;
  }
  if (tool === "create_plan" && result.success && result.plan_file) {
    _livecodeAppendPlanCard({ file: result.plan_file, title: result.title || args.title || "Plan", overview: result.overview || args.overview || "" }, output);
    return;
  }
  if (result.error) {
    if (_livecodeIsFileEditTool(tool)) {
      _livecodeRecordEditFailure(args, String(result.error), result.error_kind, output);
      return;
    }
    const full = String(result.error);
    _livecodeAppendActivityParts({
      verb: "Failed",
      detail: _livecodeTruncateMiddle(full, 140),
      meta: "",
      isError: true,
      fullErrorTitle: full,
    }, false, output);
  }
}

function _livecodeAppendLoadedCommandBlock(command, outputText, exitCode, container, opts) {
  container = container || getLiveCodeChatOutput();
  if (!container) return null;
  const row = _livecodeCreateTerminalRow({
    command: command,
    description: opts && opts.description,
    output: outputText,
    exitCode: exitCode,
  });
  _livecodeRemoveTransientThought(container);
  _livecodeAppendChatRow(container, row);
  _livecodeTerminalSyncScroll(row.querySelector(".livecode-term-card"));
  _livecodeAutoScroll(container);
  _livecodeSyncActiveTabMessagesHtml();
  return row;
}

const _LIVECODE_TERM_SHELL_SVG = "";
const _LIVECODE_TERM_CHEVRON_SVG = "";
const _LIVECODE_TERM_MORE_SVG = "";

function _livecodeShellTokens(command) {
  const s = String(command || "");
  const tokens = [];
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (/\s/.test(ch)) {
      let j = i + 1;
      while (j < s.length && /\s/.test(s[j])) j++;
      tokens.push({ type: "space", text: s.slice(i, j) });
      i = j;
      continue;
    }
    if (ch === "'" || ch === '"') {
      let j = i + 1;
      while (j < s.length && s[j] !== ch) j += (ch === '"' && s[j] === "\\") ? 2 : 1;
      j = Math.min(j + 1, s.length);
      tokens.push({ type: "str", text: s.slice(i, j) });
      i = j;
      continue;
    }
    const op = s.slice(i, i + 2).match(/^(&&|\|\||;|\||&|\(|\))/);
    if (op && !(op[0] === "&" && /[<>]/.test(s[i - 1] || ""))) {
      tokens.push({ type: "op", text: op[0] });
      i += op[0].length;
      continue;
    }
    let j = i;
    while (j < s.length && !/[\s'"()]/.test(s[j])) {
      if (/[;|]/.test(s[j]) || (s[j] === "&" && !/[<>]/.test(s[j - 1] || ""))) break;
      j++;
    }
    tokens.push({ type: "word", text: s.slice(i, j) });
    i = j;
  }
  return tokens;
}

function _livecodeIsShellAssignment(word) {
  return /^[A-Za-z_][A-Za-z0-9_]*=/.test(word);
}

function _livecodeHighlightShell(command) {
  let expectCmd = true;
  const tok = function(type, text) { return '<span class="ui-shell-tool-call__token--' + type + '">' + text + "</span>"; };
  return _livecodeShellTokens(command).map(function(t) {
    const text = _livecodeEscapeHtml(t.text);
    if (t.type === "space") return text;
    if (t.type === "op") {
      expectCmd = true;
      return tok("operator", text);
    }
    if (t.type === "str") {
      expectCmd = false;
      return tok("string", text);
    }
    if (/^\$/.test(t.text)) {
      expectCmd = false;
      return tok("variable", text);
    }
    if (expectCmd) {
      if (_livecodeIsShellAssignment(t.text)) return text;
      expectCmd = false;
      return tok("command", text);
    }
    if (/^-(?!-?$)/.test(t.text)) return tok("flag", text);
    return tok("text", text);
  }).join("");
}

function _livecodeCommandNames(command) {
  const names = [];
  let expectCmd = true;
  _livecodeShellTokens(command).forEach(function(tok) {
    if (tok.type === "space") return;
    if (tok.type === "op") {
      expectCmd = tok.text !== ")";
      return;
    }
    if (!expectCmd) return;
    if (tok.type === "word" && _livecodeIsShellAssignment(tok.text)) return;
    expectCmd = false;
    const name = tok.text.replace(/^['"]|['"]$/g, "").split("/").pop();
    if (name && names.indexOf(name) < 0) names.push(name);
  });
  return names;
}

function _livecodeCleanTerminalText(text) {
  return String(text || "")
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map(function(line) {
      const parts = line.split("\r");
      return parts[parts.length - 1];
    })
    .join("\n");
}

function _livecodeTerminalCardHtml(opts) {
  const o = opts || {};
  const command = String(o.command || "");
  const givenDescription = String(o.description || "").replace(/\s+/g, " ").trim();
  const rawDescription = givenDescription.replace(/^run(?=\s|$)\s*/i, "");
  const description = rawDescription ? rawDescription.charAt(0).toUpperCase() + rawDescription.slice(1) : "";
  const cardDescription = givenDescription ? givenDescription.charAt(0).toUpperCase() + givenDescription.slice(1) : "";
  const allNames = _livecodeCommandNames(command);
  const title = description || _livecodeTruncateMiddle(command.replace(/\s+/g, " ").trim(), 96) || "command";
  const allNamesText = allNames.slice(0, 5).join(", ") + (allNames.length > 5 ? " +" + (allNames.length - 5) : "");
  const names = description ? allNamesText : "";
  const output = _livecodeCleanTerminalText(o.output).replace(/\s+$/, "");
  const exitCode = o.exitCode === undefined || o.exitCode === null ? "" : String(o.exitCode);
  const failed = exitCode !== "" && exitCode !== "0";
  const cls = "livecode-term-card ui-shell-tool-call" + (o.running ? " is-running" : "") + (failed ? " is-error" : "");
  const exitAttr = exitCode !== "" ? ' data-exit-code="' + _livecodeEscapeHtml(exitCode) + '"' : "";
  const doneAttr = o.running ? "" : ' data-done="1"';
  const descAttr = ' data-description="' + _livecodeEscapeHtml(description || title).replace(/"/g, "&quot;") + '"';
  return '<div class="' + cls + '"' + exitAttr + doneAttr + descAttr + ">" +
    '<div class="livecode-term-header ui-collapsible-header" data-expandable role="button" tabindex="0" aria-expanded="false">' +
    '<span class="ui-shell-tool-call__icon-swap" aria-hidden="true">' +
    _livecodeIcon("terminal", { size: "sm", className: "ui-shell-tool-call__icon-default" }) +
    _livecodeIcon("chevron-right", { size: "sm", className: "ui-shell-tool-call__icon-hover" }) +
    "</span>" +
    '<span class="livecode-term-action ui-collapsible-action' + (o.running ? " ui-shimmer" : "") + '">' + (o.running ? "Running" : "Ran") + "</span>" +
    '<span class="ui-collapsible-details ui-shell-tool-call__line-details">' +
    '<span class="livecode-term-title ui-shell-tool-call__line-description">' + _livecodeEscapeHtml(title) + "</span>" +
    (names ? '<span class="livecode-term-cmds ui-shell-tool-call__line-summary">' + _livecodeEscapeHtml(names) + "</span>" : "") +
    "</span>" +
    '<span class="ui-shell-tool-call__description-row">' +
    '<span class="livecode-term-card-title ui-shell-tool-call__description' + (o.running ? " ui-shimmer" : "") + '" data-description="' + (cardDescription ? "1" : "") + '">' +
    _livecodeEscapeHtml(cardDescription || (o.running ? "Running command" : "Ran command")) + "</span>" +
    (allNamesText ? '<span class="ui-shell-tool-call__summary">' + _livecodeEscapeHtml(allNamesText) + "</span>" : "") +
    "</span>" +
    '<span class="livecode-term-status ui-shell-tool-call__status">' + (failed ? "exit " + _livecodeEscapeHtml(exitCode) : "") + "</span>" +
    _livecodeIcon("chevron-right", { className: "ui-collapsible-chevron" }) +
    "</div>" +
    '<div class="livecode-term-body ui-shell-tool-call__body">' +
    '<button type="button" class="livecode-term-more ui-shell-tool-call__menu" title="Shell command options" aria-label="Shell command options" aria-haspopup="menu">' + _livecodeIcon("ellipsis", { size: "sm" }) + "</button>" +
    '<div class="livecode-term-scroll ui-shell-tool-call__scroll">' +
    '<div class="livecode-term-cmdline ui-shell-tool-call__command"><span class="ui-shell-tool-call__prompt">$ </span>' + _livecodeHighlightShell(command) + "</div>" +
    '<div class="livecode-term-output ui-shell-tool-call__output"' + (output ? "" : " hidden") + ">" + _livecodeEscapeHtml(output) + "</div>" +
    "</div></div>" +
    "</div>";
}

const LIVECODE_AUTO_EXPAND_SHELL_LINES = 10;
const LIVECODE_AUTO_EXPAND_DIFF_LINES = 12;
const _LIVECODE_CARD_WRAP_CHARS = 48;

function _livecodeVisualLineCount(text) {
  const value = String(text || "");
  if (!value) return 0;
  return value.split("\n").reduce(function(total, line) {
    return total + Math.max(1, Math.ceil(line.length / _LIVECODE_CARD_WRAP_CHARS));
  }, 0);
}

function _livecodeSetTerminalExpanded(card, expanded) {
  card.classList.toggle("is-expanded", expanded);
  const header = card.querySelector(".livecode-term-header");
  if (header) {
    header.setAttribute("aria-expanded", expanded ? "true" : "false");
    header.toggleAttribute("data-panel-open", expanded);
  }
}

function _livecodeAutoExpandTerminalCard(card) {
  if (!card || card.dataset.userToggled || card.classList.contains("is-pending")) return;
  const cmd = card.querySelector(".livecode-term-cmdline");
  const out = card.querySelector(".livecode-term-output");
  const lines = _livecodeVisualLineCount(cmd ? cmd.textContent : "") +
    _livecodeVisualLineCount(out && !out.hidden ? out.textContent : "");
  _livecodeSetTerminalExpanded(card, lines <= LIVECODE_AUTO_EXPAND_SHELL_LINES);
}

function _livecodeCreateTerminalRow(opts) {
  const row = document.createElement("div");
  row.className = "chat-row livecode-command-output livecode-term-row";
  row.setAttribute("data-step-id", _livecodeNextStepId());
  row.innerHTML = '<div class="chat-msg assistant livecode-plain-msg">' + _livecodeTerminalCardHtml(opts) + "</div>";
  _livecodeAutoExpandTerminalCard(row.querySelector(".livecode-term-card"));
  return row;
}

function _livecodeSetTerminalRunning(card, running) {
  if (!card) return;
  card.classList.toggle("is-running", !!running);
  const action = card.querySelector(".livecode-term-action");
  if (action) {
    action.textContent = running ? "Running" : "Ran";
    action.classList.toggle("ui-shimmer", !!running);
  }
  const cardTitle = card.querySelector(".livecode-term-card-title");
  if (cardTitle) {
    if (!cardTitle.getAttribute("data-description")) cardTitle.textContent = running ? "Running command" : "Ran command";
    cardTitle.classList.toggle("ui-shimmer", !!running);
  }
}

function _livecodeTerminalSyncScroll(card) {
  if (!card) return;
  const scroll = card.querySelector(".livecode-term-scroll");
  if (scroll && card.classList.contains("is-running")) scroll.scrollTop = scroll.scrollHeight;
}

function _livecodeTerminalSetOutput(card, text) {
  const out = card && card.querySelector(".livecode-term-output");
  if (!out) return;
  const clean = _livecodeCleanTerminalText(text).replace(/\s+$/, "");
  out.textContent = clean;
  out.hidden = !clean;
  _livecodeTerminalSyncScroll(card);
}

function _livecodeTerminalAppendOutput(card, chunk) {
  const out = card && card.querySelector(".livecode-term-output");
  if (!out || !chunk) return;
  card._livecodeRawOutput = (card._livecodeRawOutput != null ? card._livecodeRawOutput : out.textContent) + String(chunk);
  _livecodeTerminalSetOutput(card, card._livecodeRawOutput);
}

function _livecodeRunningTerminalCard(output) {
  if (!output) return null;
  const cards = output.querySelectorAll(".livecode-term-card:not([data-done])");
  return cards.length ? cards[cards.length - 1] : null;
}

function _livecodeStartTerminalCard(args, output, tab) {
  output = output || getLiveCodeChatOutput();
  if (!output) return null;
  const a = args || {};
  const row = _livecodeCreateTerminalRow({ command: a.command || "", description: a.description, running: true });
  _livecodeRemoveTransientThought(output);
  _livecodeAppendChatRow(output, row, tab);
  _livecodeAutoScroll(output);
  return row.querySelector(".livecode-term-card");
}

function _livecodeFinishTerminalCard(card, result) {
  if (!card) return;
  const r = result || {};
  const denied = card.classList.contains("is-denied");
  const text = r.output != null && String(r.output) !== "" ? r.output : (r.error || "");
  if (text && !denied) _livecodeTerminalSetOutput(card, text);
  delete card._livecodeRawOutput;
  card.dataset.done = "1";
  _livecodeSetTerminalRunning(card, false);
  card.classList.remove("is-pending");
  const footer = card.querySelector(".livecode-term-footer");
  if (footer) footer.remove();
  if (r.exit_code !== undefined && r.exit_code !== null) card.dataset.exitCode = String(r.exit_code);
  const failed = !denied && (!!r.error || (r.exit_code !== undefined && r.exit_code !== null && Number(r.exit_code) !== 0));
  card.classList.toggle("is-error", failed);
  const statusEl = card.querySelector(".livecode-term-status");
  if (r.background && r.running) {
    card.classList.add("is-background");
    if (statusEl) statusEl.textContent = "in background";
  } else if (r.timed_out && statusEl) {
    statusEl.textContent = "timed out";
  } else if (r.cancelled && statusEl) {
    statusEl.textContent = "stopped";
  } else if (denied && statusEl) {
    statusEl.textContent = "skipped";
  } else if (statusEl) {
    statusEl.textContent = failed && r.exit_code != null ? "exit " + r.exit_code : "";
  }
  _livecodeAutoExpandTerminalCard(card);
  _livecodeTerminalSyncScroll(card);
  _livecodeScheduleRegroup(card.closest("#livecode-chat-messages") || undefined);
}

function _livecodeToggleTerminalCard(card) {
  if (!card || card.classList.contains("is-pending")) return;
  card.dataset.userToggled = "1";
  _livecodeSetTerminalExpanded(card, !card.classList.contains("is-expanded"));
  _livecodeTerminalSyncScroll(card);
}

function _livecodeCloseTerminalMenu() {
  const menu = document.getElementById("livecode-term-menu");
  if (menu) menu.remove();
}

function _livecodeOpenTerminalMenu(btn) {
  const card = btn && btn.closest(".livecode-term-card");
  if (!card) return;
  _livecodeCloseTerminalMenu();
  const menu = document.createElement("div");
  menu.id = "livecode-term-menu";
  menu.className = "livecode-term-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML =
    '<button type="button" role="menuitem" data-term-action="copy-command">Copy command</button>' +
    '<button type="button" role="menuitem" data-term-action="copy-output">Copy output</button>';
  menu._livecodeCard = card;
  document.body.appendChild(menu);
  const rect = btn.getBoundingClientRect();
  menu.style.top = Math.round(rect.bottom + 4) + "px";
  menu.style.left = Math.round(Math.max(8, rect.right - menu.offsetWidth)) + "px";
}

function _livecodeTerminalCardText(card, part) {
  if (!card) return "";
  if (part === "output") {
    const out = card.querySelector(".livecode-term-output");
    return out ? out.textContent : "";
  }
  const line = card.querySelector(".livecode-term-cmdline");
  return line ? line.textContent.replace(/^\$\s/, "") : "";
}

let _livecodeTerminalCardsBound = false;

function _livecodeBindTerminalCardsOnce() {
  if (_livecodeTerminalCardsBound) return;
  _livecodeTerminalCardsBound = true;
  document.addEventListener("click", function(e) {
    const t = e.target;
    if (!t || !t.closest) return;
    const openMenu = document.getElementById("livecode-term-menu");
    const menuItem = t.closest("#livecode-term-menu [data-term-action]");
    if (menuItem) {
      const card = openMenu && openMenu._livecodeCard;
      const action = menuItem.getAttribute("data-term-action");
      if (action === "copy-output") _livecodeCopyToClipboard(_livecodeTerminalCardText(card, "output"), "Output copied");
      else _livecodeCopyToClipboard(_livecodeTerminalCardText(card, "command"), "Command copied");
      _livecodeCloseTerminalMenu();
      return;
    }
    const more = t.closest("#livecode-chat-messages .livecode-term-more");
    if (more) {
      e.preventDefault();
      e.stopPropagation();
      const sameCard = openMenu && openMenu._livecodeCard === more.closest(".livecode-term-card");
      if (sameCard) _livecodeCloseTerminalMenu();
      else _livecodeOpenTerminalMenu(more);
      return;
    }
    if (openMenu && !t.closest("#livecode-term-menu")) _livecodeCloseTerminalMenu();
    if (!t.closest("#livecode-chat-messages")) return;
    const permBtn = t.closest(".livecode-permission-approve, .livecode-permission-deny");
    if (permBtn) {
      e.preventDefault();
      const row = permBtn.closest(".livecode-permission-row");
      _livecodeSubmitPermissionDecision(row && row.dataset.requestId, permBtn.classList.contains("livecode-permission-approve"));
      return;
    }
    const header = t.closest(".livecode-term-header");
    if (header && !header.closest(".livecode-approval-card")) {
      _livecodeToggleTerminalCard(header.closest(".livecode-term-card"));
      return;
    }
    const body = t.closest(".livecode-term-body");
    const bodyCard = body && body.closest(".livecode-term-card");
    if (bodyCard && !bodyCard.classList.contains("livecode-approval-card") && !t.closest("button, a")) {
      const sel = window.getSelection ? String(window.getSelection()) : "";
      if (!sel) _livecodeToggleTerminalCard(bodyCard);
    }
  });
  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") _livecodeCloseTerminalMenu();
    if (e.key !== "Enter" && e.key !== " ") return;
    const header = e.target && e.target.closest ? e.target.closest("#livecode-chat-messages .livecode-term-header") : null;
    if (!header || e.target.closest(".livecode-term-more") || header.closest(".livecode-approval-card")) return;
    e.preventDefault();
    _livecodeToggleTerminalCard(header.closest(".livecode-term-card"));
  });
  document.addEventListener("scroll", function(e) {
    const el = e.target;
    if (!el || !el.classList) return;
    const isBody = el.classList.contains("livecode-term-body");
    if (isBody) {
      const card = el.closest(".livecode-term-card");
      if (card) card.classList.toggle("is-clipped", el.scrollTop > 1);
    }
    if (isBody || el.id === "livecode-chat-messages") _livecodeCloseTerminalMenu();
  }, true);
}

function _livecodeSettleRunningTerminalCards(output, opts) {
  if (!output) return;
  const keepEmpty = !!(opts && opts.keepEmpty);
  output.querySelectorAll(".livecode-term-card:not([data-done])").forEach(function(card) {
    if (card.classList.contains("is-pending") && !keepEmpty) return;
    const out = card.querySelector(".livecode-term-output");
    if (keepEmpty || (out && out.textContent)) {
      _livecodeFinishTerminalCard(card, {});
      return;
    }
    const row = card.closest(".chat-row");
    if (row) row.remove();
  });
}

function _livecodeHandleCommandStream(data) {
  if (!data || data.source !== "livecode" || !data.session_id) return;
  const tab = livecodeChatTabs.find(function(t) {
    return t.sessionId === data.session_id && t.agentRunning;
  });
  if (!tab || tab._answerStreaming || tab._turnStreamComplete) return;
  const output = _livecodeGetOutputForTab(tab);
  if (!output) return;
  const isActiveTab = _livecodeIsActiveTab(tab);
  if (isActiveTab && !_livecodeChatOutputBelongsToTab(getLiveCodeChatOutput(), tab)) return;
  _livecodeWithTabContext(tab, function() {
    let card = _livecodeRunningTerminalCard(output);
    if (!card) card = _livecodeStartTerminalCard({ command: data.command || "" }, output, tab);
    if (!card) return;
    _livecodeSetTerminalRunning(card, true);
    if (data.status === "stream") {
      _livecodeTerminalAppendOutput(card, data.output || "");
    } else if (data.status === "output") {
      delete card._livecodeRawOutput;
      _livecodeTerminalSetOutput(card, data.output || "");
    }
    _livecodeAutoScroll(output);
  });
  _livecodeScheduleProgressSnapshot(tab, output, isActiveTab, data.status === "output");
}

const _LIVECODE_FILE_LINK_EXTS = new Set((
  "py pyi js jsx mjs cjs ts tsx json jsonc jsonl md mdx txt html htm css scss sass less xml yml yaml toml ini cfg conf " +
  "env lock sh bash zsh fish ps1 bat go rs java kt kts scala swift m mm c cc cpp cxx h hh hpp cs rb php pl lua r sql " +
  "graphql gql proto vue svelte astro dart ex exs erl hs ml clj tf hcl gradle properties csv tsv log ipynb pdf png jpg " +
  "jpeg gif svg webp ico wasm"
).split(" "));

function _livecodeLooksLikeFilePath(text) {
  const raw = String(text || "").trim();
  if (!raw || raw.length > 240 || /\s/.test(raw)) return false;
  const path = raw.replace(/:\d+(?::\d+)?$/, "");
  if (!/^[\w.~@\/-]+$/.test(path) || /\/$/.test(path)) return false;
  const base = path.split("/").pop();
  if (/^\.[\w-]+$/.test(base)) return true;
  const m = base.match(/^[\w@-][\w.@-]*\.([A-Za-z0-9]{1,10})$/);
  return !!(m && _LIVECODE_FILE_LINK_EXTS.has(m[1].toLowerCase()));
}

function _livecodeDecorateFileCodeSpans(el) {
  if (!el) return;
  el.querySelectorAll("code").forEach(function(code) {
    if (code.classList.contains("livecode-md-file-link") || code.closest("pre, a")) return;
    const text = String(code.textContent || "").trim();
    if (!_livecodeLooksLikeFilePath(text)) return;
    const lineMatch = text.match(/:(\d+)(?::\d+)?$/);
    code.classList.add("livecode-md-file-link");
    code.setAttribute("data-file-path", _livecodeResolveProjectFilePath(text.replace(/:\d+(?::\d+)?$/, "")));
    if (lineMatch) code.setAttribute("data-line-meta", "L" + lineMatch[1]);
    code.setAttribute("role", "link");
    code.setAttribute("tabindex", "0");
    code.setAttribute("title", text);
  });
}

function _livecodeSplitMarkdownBlocks(md) {
  const text = String(md || "");
  if (!text.trim()) return [];
  if (typeof marked !== "undefined" && marked && typeof marked.lexer === "function") {
    try {
      const blocks = [];
      marked.lexer(text).forEach(function(tok) {
        if (tok && tok.type !== "space" && tok.raw) blocks.push(String(tok.raw));
      });
      if (blocks.length) return blocks;
    } catch (e) {}
  }
  const out = [];
  let cur = [];
  let fence = "";
  text.split("\n").forEach(function(line) {
    const m = line.match(/^\s*(`{3,}|~{3,})/);
    if (m) {
      if (!fence) fence = m[1].charAt(0);
      else if (m[1].charAt(0) === fence) fence = "";
    }
    if (!fence && !line.trim()) {
      if (cur.length) out.push(cur.join("\n"));
      cur = [];
      return;
    }
    cur.push(line);
  });
  if (cur.length) out.push(cur.join("\n"));
  return out;
}

const _LIVECODE_OPEN_FENCE_RE = /^\s*(`{3,}|~{3,})([^\n]*)\n?([\s\S]*)$/;

function _livecodeOpenFence(raw) {
  const m = String(raw || "").match(_LIVECODE_OPEN_FENCE_RE);
  if (!m) return null;
  const marker = m[1];
  const body = m[3];
  const lines = body.split("\n");
  const closed = lines.some(function(line) {
    const t = line.trim();
    return t.length >= marker.length && t.charAt(0) === marker.charAt(0) && /^(`{3,}|~{3,})$/.test(t);
  });
  if (closed) return null;
  return { lang: String(m[2] || "").trim().split(/\s+/)[0] || "", code: body };
}

function _livecodeStreamBlockNodes(raw, openFence) {
  if (openFence) {
    const pre = document.createElement("pre");
    pre.className = "livecode-stream-code";
    const code = document.createElement("code");
    if (openFence.lang) code.className = "language-" + openFence.lang.replace(/[^\w-]/g, "");
    code.textContent = openFence.code;
    pre.appendChild(code);
    return [pre];
  }
  const tmp = document.createElement("div");
  tmp.innerHTML = _livecodeMarkdownToSafeHtml(raw);
  return Array.from(tmp.childNodes);
}

function _livecodeStreamStateValid(el, state) {
  return state.blocks.every(function(block) {
    return block.nodes.every(function(node) { return node.parentNode === el; });
  });
}

function _livecodeRenderStreamingMarkdown(el, text) {
  if (!el) return;
  const md = String(text || "");
  let state = el._livecodeStream;
  if (!state || !_livecodeStreamStateValid(el, state)) {
    el.innerHTML = "";
    state = { blocks: [], text: "" };
    el._livecodeStream = state;
  }
  el._livecodeFinalMd = null;
  const blocks = _livecodeSplitMarkdownBlocks(md);
  let same = 0;
  while (same < blocks.length && same < state.blocks.length && state.blocks[same].raw === blocks[same]) same++;
  if (same === blocks.length - 1 && same === state.blocks.length - 1) {
    const prev = state.blocks[same];
    const fence = _livecodeOpenFence(blocks[same]);
    if (prev.fence && fence && fence.lang === prev.fence.lang && prev.nodes.length === 1) {
      const code = prev.nodes[0].firstChild;
      if (code) code.textContent = fence.code;
      prev.raw = blocks[same];
      prev.fence = fence;
      state.text = md;
      return;
    }
  }
  for (let k = same; k < state.blocks.length; k++) {
    state.blocks[k].nodes.forEach(function(node) { if (node.parentNode === el) el.removeChild(node); });
  }
  state.blocks.length = same;
  for (let k = same; k < blocks.length; k++) {
    const fence = k === blocks.length - 1 ? _livecodeOpenFence(blocks[k]) : null;
    const nodes = _livecodeStreamBlockNodes(blocks[k], fence);
    nodes.forEach(function(node) { el.appendChild(node); });
    state.blocks.push({ raw: blocks[k], nodes: nodes, fence: fence });
  }
  state.text = md;
}

function _livecodeRenderAssistantMarkdown(el, text) {
  if (!el) return;
  const md = String(text || "");
  const key = md.trim();
  if (el._livecodeFinalMd === key && el.childNodes.length) return;
  const streamed = el._livecodeStream && _livecodeStreamStateValid(el, el._livecodeStream) ? el._livecodeStream : null;
  el._livecodeStream = null;
  const hostMount = typeof window.mountLivecodeChatMarkdown === "function";
  if (streamed && !hostMount && streamed.text.trim() === key && !(streamed.blocks.length && streamed.blocks[streamed.blocks.length - 1].fence)) {
    _livecodeDecorateRenderedMarkdown(el);
    el._livecodeFinalMd = key;
    return;
  }
  const pinned = streamed && el.isConnected ? el.offsetHeight : 0;
  if (pinned) el.style.minHeight = pinned + "px";
  if (hostMount) {
    window.mountLivecodeChatMarkdown(el, md, {
      livecode: true,
      persistRaw: true,
    });
    _livecodeDecorateFileCodeSpans(el);
  } else if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    el.innerHTML = _livecodeMarkdownToSafeHtml(md);
    _livecodeDecorateRenderedMarkdown(el);
  } else {
    el.textContent = md;
  }
  el._livecodeFinalMd = key;
  if (pinned) {
    requestAnimationFrame(function() {
      requestAnimationFrame(function() { el.style.minHeight = ""; });
    });
  }
}

function _livecodeDecorateRenderedMarkdown(el) {
  if (typeof window._decorateChatLinksGlobal === "function") {
    window._decorateChatLinksGlobal(el);
  }
  if (typeof window._decorateCodeBlocksGlobal === "function") {
    window._decorateCodeBlocksGlobal(el, { livecode: true });
  }
  _livecodeDecorateFileCodeSpans(el);
}

function _livecodeAppendNarration(text, output, tab) {
  const md = String(text || "").trim();
  output = output || getLiveCodeChatOutput();
  if (!md || !output) return null;
  const row = document.createElement("div");
  row.className = "chat-row livecode-assistant-row livecode-narration-row";
  const msgEl = document.createElement("div");
  msgEl.className = "chat-msg assistant livecode-plain-msg";
  row.appendChild(msgEl);
  _livecodeAppendChatRow(output, row, tab);
  _livecodeRenderAssistantMarkdown(msgEl, md);
  _livecodeAutoScroll(output);
  return row;
}

let _livecodeAnswerLiveEl = null;
let _livecodeAnswerLiveBuf = "";
let _livecodeAnswerLiveRaf = null;

function _livecodeResetAnswerLiveStream() {
  if (_livecodeAnswerLiveRaf) {
    cancelAnimationFrame(_livecodeAnswerLiveRaf);
    _livecodeAnswerLiveRaf = null;
  }
  _livecodeAnswerLiveEl = null;
  _livecodeAnswerLiveBuf = "";
}

function _livecodeDropLiveAnswerPreamble() {
  if (!_livecodeAnswerLiveEl && !_livecodeAnswerLiveBuf) return;
  if (_livecodeAnswerLiveRaf) {
    cancelAnimationFrame(_livecodeAnswerLiveRaf);
    _livecodeAnswerLiveRaf = null;
  }
  if (_livecodeAnswerLiveEl) {
    const row = _livecodeAnswerLiveEl.closest(".livecode-assistant-row");
    if (row && row.parentNode) row.parentNode.removeChild(row);
    if (_livecodeAssistantStreamEl === _livecodeAnswerLiveEl) _livecodeAssistantStreamEl = null;
  }
  _livecodeAnswerLiveEl = null;
  _livecodeAnswerLiveBuf = "";
}

function _livecodeEnsureAnswerLiveEl(output, tab) {
  output = output || getLiveCodeChatOutput();
  if (!output) return null;
  if (_livecodeAnswerLiveEl && output.contains(_livecodeAnswerLiveEl)) {
    return _livecodeAnswerLiveEl;
  }
  _livecodeFinalizeRunningActivity(undefined, output);
  const row = document.createElement("div");
  row.className = "chat-row livecode-assistant-row";
  const msgEl = document.createElement("div");
  msgEl.className = "chat-msg assistant livecode-stream-msg livecode-plain-msg";
  row.appendChild(msgEl);
  output.appendChild(row);
  _livecodeAnswerLiveEl = msgEl;
  if (tab) tab._assistantStreamRow = row;
  _livecodeAssistantStreamEl = msgEl;
  return msgEl;
}

function _livecodeAppendAnswerDelta(delta, output, tab) {
  const text = String(delta || "");
  if (!text) return;
  _livecodeAnswerLiveBuf += text;
  _livecodeEnsureAnswerLiveEl(output, tab);
  if (_livecodeAnswerLiveRaf) return;
  _livecodeAnswerLiveRaf = requestAnimationFrame(function() {
    _livecodeAnswerLiveRaf = null;
    const el = _livecodeAnswerLiveEl;
    if (!el || (el._livecodeFinalMd != null && el._livecodeFinalMd === _livecodeAnswerLiveBuf.trim())) return;
    _livecodeRenderStreamingMarkdown(el, _livecodeAnswerLiveBuf);
    _livecodeAutoScroll(output || getLiveCodeChatOutput());
  });
}

function _livecodeCancelAnswerLiveFrame() {
  if (!_livecodeAnswerLiveRaf) return;
  cancelAnimationFrame(_livecodeAnswerLiveRaf);
  _livecodeAnswerLiveRaf = null;
}

function _livecodeFinalizeLiveAnswer(finalText, output, tab) {
  _livecodeCancelAnswerLiveFrame();
  const txt = finalText || _livecodeAnswerLiveBuf;
  if (txt) {
    const el = _livecodeEnsureAnswerLiveEl(output, tab);
    if (el) _livecodeRenderAssistantMarkdown(el, txt);
    _livecodeAutoScroll(output || getLiveCodeChatOutput());
  }
  if (tab) tab._answerStreamedText = txt;
  const o = output || getLiveCodeChatOutput();
  if (tab && o) tab.messagesHtml = o.innerHTML;
  _livecodeAnswerLiveEl = null;
  _livecodeAnswerLiveBuf = "";
}

let _livecodeStepNarrationEl = null;
let _livecodeLastThoughtOuter = null;
let _livecodeLastThoughtParts = null;
const _LIVECODE_SILENT_TOOLS = new Set(["todo_write", "update_goal", "attempt_completion", "structured_output"]);

function _livecodeCommitLiveNarration(output, tab, finalText) {
  output = output || getLiveCodeChatOutput();
  if (!_livecodeAnswerLiveEl || !output || !output.contains(_livecodeAnswerLiveEl)) return null;
  _livecodeCancelAnswerLiveFrame();
  const el = _livecodeAnswerLiveEl;
  const row = el.closest(".livecode-assistant-row");
  const text = String(finalText != null ? finalText : _livecodeAnswerLiveBuf || "").trim();
  if (text) {
    _livecodeRenderAssistantMarkdown(el, text);
    el.classList.remove("livecode-stream-msg");
    if (row) row.classList.add("livecode-narration-row");
  } else if (row && row.parentNode) {
    row.parentNode.removeChild(row);
  }
  if (tab && tab._assistantStreamRow === row) tab._assistantStreamRow = null;
  if (_livecodeAssistantStreamEl === el) _livecodeAssistantStreamEl = null;
  _livecodeAnswerLiveEl = null;
  _livecodeAnswerLiveBuf = "";
  _livecodeStepNarrationEl = text ? el : null;
  return _livecodeStepNarrationEl;
}

function _livecodeAttachThoughtBody(text) {
  const body = String(text || "").trim();
  const outer = _livecodeLastThoughtOuter;
  if (!body || !outer || !outer.isConnected || outer.querySelector(".livecode-thought-body")) return;
  const parts = Object.assign({}, _livecodeLastThoughtParts || { verb: "Thought", detail: "briefly", meta: "" }, { thoughtContent: body });
  outer.innerHTML = _livecodeBuildActivityHtml(parts, false);
  _livecodeFillThoughtBody(outer, body);
}

function _livecodeSettleLiveContent(data, output, tab, isActiveTab) {
  const role = data.role === "answer" ? "answer" : "narration";
  const text = String(data.text || "").trim();
  if (data.thought_content) _livecodeAttachThoughtBody(data.thought_content);
  if (role === "answer") {
    if (isActiveTab && _livecodeAnswerLiveEl && text) {
      _livecodeCancelAnswerLiveFrame();
      _livecodeAnswerLiveBuf = text;
      _livecodeRenderAssistantMarkdown(_livecodeAnswerLiveEl, text);
    }
    return;
  }
  if (isActiveTab && _livecodeCommitLiveNarration(output, tab, text)) {
    _livecodeAutoScroll(output);
    return;
  }
  if (_livecodeStepNarrationEl && _livecodeStepNarrationEl.isConnected) {
    if (text) _livecodeRenderAssistantMarkdown(_livecodeStepNarrationEl, text);
    return;
  }
  if (text) {
    const row = _livecodeAppendNarration(text, output, tab);
    _livecodeStepNarrationEl = row ? row.querySelector(".chat-msg") : null;
  }
}

function _livecodeRemovePendingToolLines(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  output.querySelectorAll(".livecode-pending-tool").forEach(function(el) {
    if (_livecodeRunningActivityEl === el) _livecodeRunningActivityEl = null;
    el.remove();
  });
}

function _livecodePendingToolReady(tool, args) {
  if (_livecodeIsFileEditTool(tool)) return !!String(args.file_path || "").trim();
  if (tool === "spawn_subagent") return !!String(args.goal || "").trim();
  return tool === "create_plan" || tool === "ask_question";
}

function _livecodeShowPendingToolCall(data, output, tab) {
  const tool = String(data.tool || "");
  output = output || getLiveCodeChatOutput();
  if (!tool || !output || _LIVECODE_SILENT_TOOLS.has(tool)) return;
  _livecodeCommitLiveNarration(output, tab);
  const args = data.args || {};
  if (!_livecodePendingToolReady(tool, args)) return;
  const index = String(data.index != null ? data.index : 0);
  if (output.querySelector('.livecode-pending-tool[data-pending-index="' + index + '"]')) return;
  const parts = _livecodeParseActivityParts(tool, args, _livecodeHumanToolLabel(tool, args));
  if (!parts || !parts.verb) return;
  _livecodeFinalizeRunningActivity(undefined, output);
  const el = _livecodeAppendActivityParts(parts, true, output);
  if (!el) return;
  el.classList.add("livecode-pending-tool");
  el.setAttribute("data-pending-index", index);
  _livecodeAutoScroll(output);
}

function _livecodeActivityFeedActive() {
  return !!livecodeAgentRunning;
}

function _livecodeShowImmediateThinking(output, options) {
  output = output || getLiveCodeChatOutput();
  const label = (options && options.bootstrap) ? "Planning next moves" : "Thinking";
  if (_livecodeRunningActivityEl && _livecodeIsThinkingLine()) {
    _livecodeLastToolLabel = label;
    _livecodeUpdateThinkingLineLabel();
    _livecodeCleanupStaleThinkingLines(output, _livecodeRunningActivityEl);
    _livecodeStartThinkingTicker();
    return;
  }
  if (_livecodeAdoptRunningThinking(output, label)) {
    _livecodeStartThinkingTicker();
    return;
  }
  _livecodeThinkingStartMs = Date.now();
  _livecodeLastTool = "attempt_completion";
  _livecodeLastToolArgs = {};
  _livecodeLastToolLabel = label;
  _livecodeAppendActivityParts({ verb: label, detail: "", meta: "" }, true, output);
  _livecodeStartThinkingTicker();
}

function _livecodeEnsureStatusRow(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  if (!_livecodeStatusRow) {
    _livecodeStatusRow = document.createElement("div");
    _livecodeStatusRow.className = "chat-row livecode-status-row";
    _livecodeStatusRow.id = "livecode-agent-status-row";
    _livecodeStatusMsg = document.createElement("div");
    _livecodeStatusMsg.className = "chat-msg assistant livecode-status-msg livecode-typing-indicator";
    _livecodeStatusRow.appendChild(_livecodeStatusMsg);
  }
  if (!_livecodeStatusRow.parentNode) _livecodeAppendChatRow(output, _livecodeStatusRow);
  else if (_livecodeStatusRow !== output.lastElementChild) {
    const anchor = _livecodeGetAssistantAnchor(output);
    if (anchor && anchor.parentNode === output) {
      output.insertBefore(_livecodeStatusRow, anchor);
    } else {
      output.appendChild(_livecodeStatusRow);
    }
  }
}

function _livecodeShowTyping(hint, output) {
  if (_livecodeActivityFeedActive()) return;
  output = output || getLiveCodeChatOutput();
  _livecodeEnsureStatusRow(output);
  if (!_livecodeStatusMsg) return;
  _livecodeStatusMsg.classList.remove("livecode-running-status");
  _livecodeStatusMsg.classList.add("livecode-typing-indicator");
  _livecodeStatusMsg.innerHTML = '<span class="ui-shimmer">' + _livecodeEscapeHtml(hint || "Planning next moves") + "</span>";
  _livecodeAutoScroll(output);
}

function _livecodeShowShimmer(message, output) {
  if (_livecodeActivityFeedActive()) return;
  output = output || getLiveCodeChatOutput();
  _livecodeEnsureStatusRow(output);
  if (!_livecodeStatusMsg) return;
  _livecodeStatusMsg.classList.remove("livecode-typing-indicator");
  _livecodeStatusMsg.classList.add("livecode-running-status");
  _livecodeStatusMsg.innerHTML = '<span class="ui-shimmer">' + _livecodeEscapeHtml(message) + "</span>";
  _livecodeAutoScroll(output);
}

function _livecodeSetPermissionToolbarVisible(visible) {
  const wrap = document.getElementById("livecode-permission-toolbar-actions");
  if (!wrap) return;
  wrap.style.display = visible ? "flex" : "none";
}

function _livecodeGetActivePendingPermissionRequestId() {
  const tab = _livecodeGetActiveChatTab();
  return tab ? String(tab.pendingPermissionRequestId || "") : "";
}

function _livecodeSyncPermissionToolbar() {
  _livecodeSetPermissionToolbarVisible(!!_livecodeGetActivePendingPermissionRequestId());
  _livecodeRenderQuestionsBar();
  _livecodeSyncPlanCardButtons();
}

function _livecodeClearPermissionRequest(requestId) {
  livecodeChatTabs.forEach(function(tab) {
    if (tab.pendingPermissionRequestId === requestId) {
      tab.pendingPermissionRequestId = "";
    }
  });
  _livecodeSyncPermissionToolbar();
}

function _livecodeResolvePermissionRow(requestId, approved, output, label) {
  output = output || getLiveCodeChatOutput();
  if (!output || !requestId) return;
  const row = Array.from(output.querySelectorAll(".livecode-permission-row"))
    .find(function(r) { return r.dataset && r.dataset.requestId === requestId; });
  if (!row) return;
  row.dataset.resolved = approved ? "approved" : "denied";
  const card = row.querySelector(".livecode-term-card");
  if (card) {
    const footer = card.querySelector(".livecode-term-footer");
    if (footer) footer.remove();
    card.classList.remove("is-pending");
    if (approved && !row.classList.contains("livecode-term-row")) {
      row.remove();
    } else if (!approved) {
      card.classList.remove("is-running");
      card.classList.add("is-denied");
      const status = card.querySelector(".livecode-term-status");
      if (status) status.textContent = label || (row.classList.contains("livecode-term-row") ? "Skipped" : "Denied");
    }
  } else if (approved) {
    row.remove();
  } else {
    const msg = row.querySelector(".livecode-permission-msg");
    if (msg) {
      msg.classList.add("is-denied");
      msg.querySelectorAll("button").forEach(function(btn) { btn.disabled = true; btn.remove(); });
      const kicker = msg.querySelector(".livecode-permission-kicker");
      if (kicker) kicker.textContent = label || "Permission denied";
    }
  }
  if (typeof _livecodeSyncActiveTabMessagesHtml === "function") {
    _livecodeSyncActiveTabMessagesHtml();
  }
}

function _livecodeSubmitPermissionDecision(requestId, approved) {
  if (!requestId) return;
  const out = getLiveCodeChatOutput();
  const row = out && Array.from(out.querySelectorAll(".livecode-permission-row"))
    .find(function(r) { return r.dataset && r.dataset.requestId === requestId; });
  if (row && row.dataset.resolved) return;
  _livecodeClearPermissionRequest(requestId);
  _livecodeResolvePermissionRow(requestId, approved, out);
  fetch("/livecode/permission", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: requestId, approved: approved }),
  }).catch(function() {});
}

function _livecodePermissionFooterHtml(approveLabel) {
  return '<div class="livecode-term-footer ui-shell-tool-call__approval">' +
    '<button type="button" class="livecode-term-btn livecode-permission-deny ui-shell-tool-call__skip-btn">Skip</button>' +
    '<button type="button" class="livecode-term-btn is-primary livecode-permission-approve ui-shell-tool-call__run-btn">' + _livecodeEscapeHtml(approveLabel) +
    '<span class="ui-shell-tool-call__kbd">\u23ce</span></button>' +
    "</div>";
}

function _livecodeShowPermissionRequest(data, output, targetTab) {
  const tab = targetTab || _livecodeGetActiveChatTab();
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  const requestId = data.request_id || "";

  if (requestId) {
    if (tab) tab.pendingPermissionRequestId = requestId;
    _livecodeSyncPermissionToolbar();
    const existing = Array.from(output.querySelectorAll(".livecode-permission-row"))
      .find(function(row) { return row.dataset && row.dataset.requestId === requestId; });
    if (existing) {
      _livecodeAutoScroll(output);
      return;
    }
  }
  const tool = data.tool || "";
  const args = data.args || {};

  if (tool === "run_command") {
    const card = _livecodeRunningTerminalCard(output) || _livecodeStartTerminalCard(args, output, tab);
    const cmdRow = card && card.closest(".chat-row");
    if (!cmdRow) return;
    cmdRow.classList.add("livecode-permission-row");
    cmdRow.dataset.requestId = requestId;
    card.classList.add("is-pending");
    const cardBody = card.querySelector(".livecode-term-body") || card;
    if (!card.querySelector(".livecode-term-footer")) {
      cardBody.insertAdjacentHTML("beforeend", _livecodePermissionFooterHtml("Run"));
    }
    _livecodeAutoScroll(output);
    return;
  }

  function previewValue(value) {
    if (value == null || value === "") return "";
    let text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    text = String(text || "").trim();
    return text.length > 600 ? text.slice(0, 597) + "…" : text;
  }

  const filePath = String(args.file_path || args.path || args.directory || "");
  let preview = previewValue(args.content || args.new_string || args.old_string) || filePath || tool || "unknown";
  let detail = filePath ? _livecodeBasename(filePath) : tool;
  let verbLabel = _livecodeIsFileEditTool(tool) ? "Edit" : "Run";
  if (tool === "browser") {
    const ask = _livecodeBrowserApproval(args);
    verbLabel = ask.verb;
    detail = ask.detail;
    preview = previewValue(ask.preview) || detail;
  }

  const row = document.createElement("div");
  row.className = "chat-row livecode-permission-row livecode-command-output";
  row.dataset.requestId = requestId;
  row.innerHTML =
    '<div class="chat-msg assistant livecode-permission-msg livecode-plain-msg">' +
    '<div class="livecode-term-card ui-shell-tool-call livecode-approval-card is-pending">' +
    '<div class="livecode-term-header ui-collapsible-header">' +
    '<span class="ui-collapsible-action">' + _livecodeEscapeHtml(verbLabel) + "</span>" +
    '<span class="ui-collapsible-details ui-shell-tool-call__line-details"><span class="livecode-term-title ui-shell-tool-call__line-description">' + _livecodeEscapeHtml(detail) + "</span></span>" +
    '<span class="ui-shell-tool-call__description-row"><span class="ui-shell-tool-call__description">' + _livecodeEscapeHtml(verbLabel + " " + detail) + "</span></span>" +
    '<span class="livecode-term-status ui-shell-tool-call__status"></span>' +
    "</div>" +
    '<div class="livecode-term-body ui-shell-tool-call__body"><div class="livecode-term-scroll ui-shell-tool-call__scroll">' +
    '<div class="livecode-term-output ui-shell-tool-call__output livecode-approval-preview">' + _livecodeEscapeHtml(preview) + "</div>" +
    "</div>" + _livecodePermissionFooterHtml("Allow") + "</div>" +
    "</div></div>";
  _livecodeAppendChatRow(output, row);
  _livecodeTerminalSyncScroll(row.querySelector(".livecode-term-card"));
  _livecodeAutoScroll(output);
}

const LIVECODE_FREEFORM_OPTION_ID = "__freeform_other__";
const LIVECODE_QUESTIONS_PLACEHOLDER = "Add more optional details";
const _LIVECODE_QUESTION_ICON_SVG =
  '<svg class="livecode-questions-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M2.5 3.5a1.5 1.5 0 0 1 1.5-1.5h8a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H7l-3 2.5V11A1.5 1.5 0 0 1 2.5 9.5z"></path>' +
  '<path d="M6.6 5.1a1.45 1.45 0 0 1 2.8.5c0 .95-1.4 1.2-1.4 2"></path><circle cx="8" cy="9.1" r=".35" fill="currentColor" stroke="none"></circle></svg>';
const _LIVECODE_CHEVRON_UP_SVG =
  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4.5 10 8 6.5 11.5 10"></polyline></svg>';
const _LIVECODE_CHEVRON_DOWN_SVG =
  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4.5 6.5 8 10 11.5 6.5"></polyline></svg>';

function _livecodeActiveQuestionState() {
  const tab = _livecodeGetActiveChatTab();
  return tab && tab.pendingQuestion ? tab.pendingQuestion : null;
}

function _livecodeQuestionAnswered(state, question) {
  return (state.selections[question.id] || []).length > 0;
}

function _livecodeQuestionsAllAnswered(state) {
  return state.questions.every(function(q) { return _livecodeQuestionAnswered(state, q); });
}

function _livecodeNextUnansweredQuestion(state, fromIndex) {
  const qs = state.questions;
  for (let i = fromIndex + 1; i < qs.length; i++) {
    if (!_livecodeQuestionAnswered(state, qs[i])) return i;
  }
  for (let i = 0; i < fromIndex; i++) {
    if (!_livecodeQuestionAnswered(state, qs[i])) return i;
  }
  return -1;
}

function _livecodeShowQuestionRequest(data, targetTab) {
  const tab = targetTab || _livecodeGetActiveChatTab();
  const questions = Array.isArray(data.questions) ? data.questions : [];
  if (!tab || !data.request_id || !questions.length) return;
  if (tab.pendingQuestion && tab.pendingQuestion.requestId === data.request_id) return;
  tab.pendingQuestion = {
    requestId: data.request_id,
    questions: questions,
    selections: {},
    other: {},
    active: 0,
    focused: -1,
    collapsed: false,
    submitting: false,
    focusOnRender: true,
  };
  if (_livecodeIsActiveTab(tab)) _livecodeRenderQuestionsBar();
}

function _livecodeClearQuestionRequest(tab, requestId) {
  if (!tab || !tab.pendingQuestion) return;
  if (requestId && tab.pendingQuestion.requestId !== requestId) return;
  tab.pendingQuestion = null;
  if (_livecodeIsActiveTab(tab)) _livecodeRenderQuestionsBar();
}

function _livecodeQuestionRowHtml(state, question, qIndex, oIndex, optionId, labelHtml) {
  const selected = (state.selections[question.id] || []).indexOf(optionId) >= 0;
  const hasSelection = _livecodeQuestionAnswered(state, question);
  const focused = state.active === qIndex && state.focused === oIndex;
  const freeform = optionId === LIVECODE_FREEFORM_OPTION_ID;
  return '<div class="livecode-questions-option' +
    (freeform ? " is-freeform" : "") +
    (focused ? " is-focused" : "") +
    (!selected && hasSelection ? " is-unselected" : "") +
    (selected ? " is-selected" : "") +
    '" role="button" data-q="' + qIndex + '" data-o="' + oIndex + '">' +
    '<button type="button" class="livecode-questions-letter" tabindex="-1">' + String.fromCharCode(65 + oIndex) + "</button>" +
    labelHtml + "</div>";
}

function _livecodeRenderQuestionsBar() {
  const bar = document.getElementById("livecode-questions-bar");
  const composer = document.getElementById("livecode-chat-composer");
  const input = document.getElementById("livecode-chat-input");
  const state = _livecodeActiveQuestionState();
  if (!bar) return;
  _livecodeBindQuestionsBarOnce(bar);
  if (!state) {
    if (!bar.hidden) {
      bar.hidden = true;
      bar.innerHTML = "";
      if (composer) composer.classList.remove("has-pending-questionnaire");
      _livecodeApplyChatModeToUI();
    }
    return;
  }
  const oldScroll = bar.querySelector(".livecode-questions-scroll");
  const scrollTop = oldScroll ? oldScroll.scrollTop : 0;
  const activeEl = document.activeElement;
  const refocusFreeform = activeEl && activeEl.classList && activeEl.classList.contains("livecode-questions-freeform") && bar.contains(activeEl)
    ? Number(activeEl.getAttribute("data-q"))
    : -1;

  const total = state.questions.length;
  const answered = state.questions.filter(function(q) { return _livecodeQuestionAnswered(state, q); }).length;
  const canContinue = _livecodeQuestionsAllAnswered(state) && !state.submitting;
  const headerRight = state.collapsed
    ? '<span class="livecode-questions-answered">' + answered + " of " + total + " answered</span>"
    : '<span class="livecode-questions-stepper">' +
        '<button type="button" class="livecode-questions-icon-btn" data-questions-action="prev" title="Previous question" aria-label="Previous question">' + _LIVECODE_CHEVRON_UP_SVG + "</button>" +
        '<span class="livecode-questions-stepper-label">' + (state.active + 1) + " of " + total + "</span>" +
        '<button type="button" class="livecode-questions-icon-btn" data-questions-action="next" title="Next question" aria-label="Next question">' + _LIVECODE_CHEVRON_DOWN_SVG + "</button>" +
      "</span>";
  const questionsHtml = state.questions.map(function(q, qIndex) {
    const rows = q.options.map(function(opt, oIndex) {
      return _livecodeQuestionRowHtml(state, q, qIndex, oIndex, opt.id,
        '<span class="livecode-questions-option-label">' + _livecodeEscapeHtml(opt.label) + "</span>");
    }).join("") + _livecodeQuestionRowHtml(state, q, qIndex, q.options.length, LIVECODE_FREEFORM_OPTION_ID,
      '<textarea class="livecode-questions-freeform" rows="1" placeholder="Other..." data-q="' + qIndex + '"></textarea>');
    return '<div class="livecode-questions-question' + (qIndex === state.active ? " is-active" : "") + '" data-q="' + qIndex + '">' +
      '<div class="livecode-questions-prompt"><span class="livecode-questions-number">' + (qIndex + 1) + ".</span>" +
      '<span class="livecode-questions-prompt-text">' + _livecodeEscapeHtml(q.prompt) + "</span></div>" +
      '<div class="livecode-questions-options">' + rows + "</div></div>";
  }).join("");

  bar.hidden = false;
  bar.classList.toggle("is-collapsed", !!state.collapsed);
  bar.innerHTML =
    '<div class="livecode-questions-header" data-questions-action="' + (state.collapsed ? "expand" : "") + '">' +
      _LIVECODE_QUESTION_ICON_SVG +
      '<span class="livecode-questions-title">Questions</span>' +
      headerRight +
      '<button type="button" class="livecode-questions-icon-btn livecode-questions-collapse" data-questions-action="collapse" title="' +
        (state.collapsed ? "Expand questions" : "Minimize questions") + '" aria-label="' + (state.collapsed ? "Expand questions" : "Minimize questions") + '">' +
        (state.collapsed ? _LIVECODE_CHEVRON_UP_SVG : _LIVECODE_CHEVRON_DOWN_SVG) +
      "</button>" +
    "</div>" +
    (state.collapsed ? "" :
      '<div class="livecode-questions-scroll"><div class="livecode-questions-list">' + questionsHtml + "</div></div>" +
      '<div class="livecode-questions-actions">' +
        '<button type="button" class="livecode-btn is-text" data-questions-action="skip"' + (state.submitting ? " disabled" : "") + '>Skip<span class="livecode-kbd">Esc</span></button>' +
        '<button type="button" class="livecode-btn is-yellow" data-questions-action="continue"' + (canContinue ? "" : " disabled") + '>Continue<span class="livecode-kbd">⏎</span></button>' +
      "</div>");

  bar.querySelectorAll(".livecode-questions-freeform").forEach(function(ta) {
    const q = state.questions[Number(ta.getAttribute("data-q"))];
    ta.value = (q && state.other[q.id]) || "";
    _livecodeAutosizeQuestionFreeform(ta);
  });
  const scroll = bar.querySelector(".livecode-questions-scroll");
  if (scroll) {
    scroll.scrollTop = scrollTop;
    scroll.addEventListener("scroll", function() { _livecodeSyncQuestionsScrollMask(scroll); });
    _livecodeSyncQuestionsScrollMask(scroll);
  }
  if (composer) composer.classList.add("has-pending-questionnaire");
  if (input) input.setAttribute("data-placeholder", LIVECODE_QUESTIONS_PLACEHOLDER);
  if (refocusFreeform >= 0) {
    const ta = bar.querySelector('.livecode-questions-freeform[data-q="' + refocusFreeform + '"]');
    if (ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
  } else if (state.focusOnRender) {
    state.focusOnRender = false;
    requestAnimationFrame(function() {
      bar.focus({ preventScroll: true });
      const firstScroll = bar.querySelector(".livecode-questions-scroll");
      if (firstScroll) firstScroll.scrollTop = 0;
    });
  }
}

function _livecodeAutosizeQuestionFreeform(ta) {
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + "px";
}

function _livecodeSyncQuestionsScrollMask(scroll) {
  const atBottom = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 2;
  scroll.classList.toggle("is-at-bottom", atBottom);
}

function _livecodeScrollToActiveQuestion() {
  const state = _livecodeActiveQuestionState();
  const bar = document.getElementById("livecode-questions-bar");
  const scroll = bar && bar.querySelector(".livecode-questions-scroll");
  const el = scroll && scroll.querySelector('.livecode-questions-question[data-q="' + (state ? state.active : 0) + '"]');
  if (!el) return;
  const maxTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
  scroll.scrollTo({ top: Math.min(el.offsetTop, maxTop), behavior: "smooth" });
}

function _livecodeSetActiveQuestion(state, index) {
  if (index < 0 || index >= state.questions.length) return;
  state.active = index;
  state.focused = -1;
  _livecodeRenderQuestionsBar();
  _livecodeScrollToActiveQuestion();
}

function _livecodeToggleQuestionOption(state, qIndex, optionId) {
  const question = state.questions[qIndex];
  if (!question || state.submitting) return;
  const current = state.selections[question.id] || [];
  const has = current.indexOf(optionId) >= 0;
  let next;
  if (question.allow_multiple) {
    next = has ? current.filter(function(id) { return id !== optionId; }) : current.concat([optionId]);
  } else {
    next = has ? [] : [optionId];
  }
  state.selections[question.id] = next;
  state.active = qIndex;
  if (!question.allow_multiple && next.length && optionId !== LIVECODE_FREEFORM_OPTION_ID && !_livecodeQuestionsAllAnswered(state)) {
    const nextIndex = _livecodeNextUnansweredQuestion(state, qIndex);
    if (nextIndex >= 0) {
      _livecodeSetActiveQuestion(state, nextIndex);
      return;
    }
  }
  _livecodeRenderQuestionsBar();
}

function _livecodeFocusQuestionFreeform(qIndex) {
  setTimeout(function() {
    const bar = document.getElementById("livecode-questions-bar");
    const ta = bar && bar.querySelector('.livecode-questions-freeform[data-q="' + qIndex + '"]');
    if (ta) ta.focus();
  }, 0);
}

function _livecodeSubmitQuestions(skipped) {
  const tab = _livecodeGetActiveChatTab();
  const state = tab && tab.pendingQuestion;
  if (!state || state.submitting) return;
  if (!skipped && !_livecodeQuestionsAllAnswered(state)) {
    const nextIndex = _livecodeNextUnansweredQuestion(state, state.active);
    _livecodeSetActiveQuestion(state, nextIndex >= 0 ? nextIndex : state.active);
    return;
  }
  const composerState = typeof window.getLivecodeComposerState === "function" ? window.getLivecodeComposerState() : { text: "" };
  const details = String(composerState.text || "").trim();
  const answers = skipped ? [] : state.questions.map(function(q) {
    return { id: q.id, selected: state.selections[q.id] || [], other: state.other[q.id] || "" };
  });
  state.submitting = true;
  _livecodeRenderQuestionsBar();
  fetch("/livecode/question", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: state.requestId, skipped: !!skipped, answers: answers, details: details }),
  }).then(function(resp) {
    if (!resp.ok && resp.status !== 404) throw new Error("HTTP " + resp.status);
    if (details && typeof window.clearLivecodeComposer === "function") window.clearLivecodeComposer();
    _livecodeClearQuestionRequest(tab, state.requestId);
  }).catch(function() {
    state.submitting = false;
    _livecodeRenderQuestionsBar();
  });
}

function _livecodeHandleQuestionsKeydown(e) {
  const state = _livecodeActiveQuestionState();
  const bar = document.getElementById("livecode-questions-bar");
  if (!state || !bar || bar.hidden || e.metaKey || e.ctrlKey || e.altKey) return;
  const target = e.target;
  if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
  const active = document.activeElement;
  const messages = getLiveCodeChatOutput();
  if (!(active === document.body || bar.contains(active) || (messages && messages.contains(active)))) return;
  if (state.collapsed) {
    if ((e.key === "Enter" || e.key === " ") && bar.contains(active)) {
      e.preventDefault();
      state.collapsed = false;
      _livecodeRenderQuestionsBar();
    }
    return;
  }
  const question = state.questions[state.active];
  if (!question) return;
  const optionCount = question.options.length + 1;
  const stop = function() { e.preventDefault(); e.stopPropagation(); };
  const pick = function(oIndex) {
    if (oIndex < question.options.length) {
      _livecodeToggleQuestionOption(state, state.active, question.options[oIndex].id);
      return;
    }
    const qIndex = state.active;
    if ((state.selections[question.id] || []).indexOf(LIVECODE_FREEFORM_OPTION_ID) < 0) {
      _livecodeToggleQuestionOption(state, qIndex, LIVECODE_FREEFORM_OPTION_ID);
    }
    _livecodeFocusQuestionFreeform(qIndex);
  };
  switch (e.key) {
    case "Tab":
      stop();
      _livecodeSetActiveQuestion(state, (state.active + (e.shiftKey ? -1 : 1) + state.questions.length) % state.questions.length);
      return;
    case "ArrowDown":
    case "ArrowRight":
    case "ArrowUp":
    case "ArrowLeft": {
      stop();
      const step = (e.key === "ArrowDown" || e.key === "ArrowRight") ? 1 : -1;
      state.focused = state.focused < 0 ? 0 : (state.focused + step + optionCount) % optionCount;
      _livecodeRenderQuestionsBar();
      return;
    }
    case " ":
      stop();
      if (state.focused < 0) state.focused = 0;
      pick(state.focused);
      return;
    case "Enter":
      stop();
      _livecodeSubmitQuestions(false);
      return;
    case "Escape":
      stop();
      _livecodeSubmitQuestions(true);
      return;
    default:
      if (e.key.length === 1) {
        const oIndex = e.key.toUpperCase().charCodeAt(0) - 65;
        if (oIndex >= 0 && oIndex < optionCount) {
          stop();
          state.focused = -1;
          pick(oIndex);
        }
      }
  }
}

function _livecodeBindQuestionsBarOnce(bar) {
  if (bar.dataset.bound === "1") return;
  bar.dataset.bound = "1";
  window.addEventListener("keydown", _livecodeHandleQuestionsKeydown, { capture: true });
  bar.addEventListener("click", function(e) {
    const state = _livecodeActiveQuestionState();
    if (!state) return;
    const actionEl = e.target.closest("[data-questions-action]");
    const action = actionEl ? actionEl.getAttribute("data-questions-action") : "";
    if (action) {
      e.preventDefault();
      e.stopPropagation();
      if (action === "prev" || action === "next") {
        const n = state.questions.length;
        _livecodeSetActiveQuestion(state, (state.active + (action === "next" ? 1 : -1) + n) % n);
      } else if (action === "collapse" || action === "expand") {
        state.collapsed = action === "collapse" ? !state.collapsed : false;
        _livecodeRenderQuestionsBar();
        bar.focus({ preventScroll: true });
      } else if (action === "skip") {
        _livecodeSubmitQuestions(true);
      } else if (action === "continue") {
        _livecodeSubmitQuestions(false);
      }
      return;
    }
    const row = e.target.closest(".livecode-questions-option");
    if (!row) return;
    const qIndex = Number(row.getAttribute("data-q"));
    const oIndex = Number(row.getAttribute("data-o"));
    const question = state.questions[qIndex];
    if (!question) return;
    state.focused = -1;
    if (row.classList.contains("is-freeform")) {
      const selected = (state.selections[question.id] || []).indexOf(LIVECODE_FREEFORM_OPTION_ID) >= 0;
      if (!selected || e.target.closest(".livecode-questions-letter")) {
        _livecodeToggleQuestionOption(state, qIndex, LIVECODE_FREEFORM_OPTION_ID);
      }
      if (!selected) _livecodeFocusQuestionFreeform(qIndex);
      return;
    }
    _livecodeToggleQuestionOption(state, qIndex, question.options[oIndex].id);
    if (document.activeElement !== bar) bar.focus({ preventScroll: true });
  });
  bar.addEventListener("input", function(e) {
    const ta = e.target.closest(".livecode-questions-freeform");
    const state = _livecodeActiveQuestionState();
    if (!ta || !state) return;
    const question = state.questions[Number(ta.getAttribute("data-q"))];
    if (!question) return;
    state.other[question.id] = ta.value;
    _livecodeAutosizeQuestionFreeform(ta);
  });
  bar.addEventListener("focusin", function(e) {
    const ta = e.target.closest && e.target.closest(".livecode-questions-freeform");
    const state = _livecodeActiveQuestionState();
    if (!ta || !state) return;
    const qIndex = Number(ta.getAttribute("data-q"));
    const question = state.questions[qIndex];
    if (question && (state.selections[question.id] || []).indexOf(LIVECODE_FREEFORM_OPTION_ID) < 0) {
      _livecodeToggleQuestionOption(state, qIndex, LIVECODE_FREEFORM_OPTION_ID);
    }
  });
  bar.addEventListener("keydown", function(e) {
    const ta = e.target.closest && e.target.closest(".livecode-questions-freeform");
    if (!ta || !_livecodeActiveQuestionState()) return;
    e.stopPropagation();
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ta.blur();
      bar.focus({ preventScroll: true });
      _livecodeSubmitQuestions(false);
    } else if (e.key === "Escape") {
      e.preventDefault();
      ta.blur();
      bar.focus({ preventScroll: true });
    }
  });
}

const _LIVECODE_TODOS_ICON_SVG =
  '<svg class="livecode-todos-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" aria-hidden="true">' +
  '<circle cx="3.6" cy="4.6" r="1.35"></circle><circle cx="3.6" cy="11.4" r="1.35"></circle>' +
  '<line x1="7" y1="4.6" x2="13.5" y2="4.6"></line><line x1="7" y1="11.4" x2="13.5" y2="11.4"></line></svg>';
const _LIVECODE_PLUS_SVG =
  '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><line x1="8" y1="3" x2="8" y2="13"></line><line x1="3" y1="8" x2="13" y2="8"></line></svg>';
const _LIVECODE_X_SVG =
  '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" aria-hidden="true"><line x1="4" y1="4" x2="12" y2="12"></line><line x1="12" y1="4" x2="4" y2="12"></line></svg>';
const _LIVECODE_TODO_SECTION_RE = /^##\s+task checklist\s*$/i;
const _LIVECODE_TODO_LINE_RE = /^(\s*[-*]\s+\[)([ xX~-])(\]\s+)(.*)$/;
const _LIVECODE_TODO_MARK_STATUS = { x: "completed", X: "completed", "~": "in_progress", "-": "cancelled", " ": "pending" };
const _LIVECODE_TODO_STATUS_MARK = { completed: "x", in_progress: "~", cancelled: "-", pending: " " };

function _livecodeTodoStatusIconHtml(status) {
  const cls = "livecode-todo-status is-" + (status || "pending");
  if (status === "completed") {
    return '<span class="' + cls + '" aria-label="Completed"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
      '<circle cx="8" cy="8" r="6.5" fill="currentColor"></circle><polyline points="5.2 8.2 7.2 10.1 10.9 6.1" stroke="var(--lc-bg)" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"></polyline></svg></span>';
  }
  if (status === "in_progress") {
    return '<span class="' + cls + '" aria-label="In progress"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" aria-hidden="true">' +
      '<circle cx="8" cy="8" r="6" stroke-dasharray="2.4 2.2"></circle></svg></span>';
  }
  return '<span class="' + cls + '" aria-label="' + (status === "cancelled" ? "Cancelled" : "Pending") + '"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="6"></circle></svg></span>';
}

function _livecodePlanChecklistBounds(lines) {
  for (let i = 0; i < lines.length; i++) {
    if (_LIVECODE_TODO_SECTION_RE.test(lines[i].trim())) {
      let end = lines.length;
      for (let j = i + 1; j < lines.length; j++) {
        if (/^#/.test(lines[j])) { end = j; break; }
      }
      return { heading: i, start: i + 1, end: end };
    }
  }
  return null;
}

function _livecodePlanTodosFromMarkdown(markdown) {
  const lines = String(markdown || "").split("\n");
  const bounds = _livecodePlanChecklistBounds(lines);
  if (!bounds) return [];
  const todos = [];
  for (let i = bounds.start; i < bounds.end; i++) {
    const m = _LIVECODE_TODO_LINE_RE.exec(lines[i]);
    if (!m || !m[4].trim()) continue;
    todos.push({ id: "plan-" + (todos.length + 1), content: m[4].trim(), status: _LIVECODE_TODO_MARK_STATUS[m[2]] || "pending" });
  }
  return todos;
}

function _livecodePlanMarkdownWithTodos(markdown, todos) {
  const lines = String(markdown || "").replace(/\s+$/, "").split("\n");
  const items = (todos || []).filter(function(t) { return String(t.content || "").trim(); }).map(function(t) {
    return "- [" + (_LIVECODE_TODO_STATUS_MARK[t.status] || " ") + "] " + String(t.content).replace(/\s+/g, " ").trim();
  });
  const bounds = _livecodePlanChecklistBounds(lines);
  if (!bounds) {
    if (!items.length) return lines.join("\n");
    return lines.join("\n") + "\n\n## Task checklist\n\n" + items.join("\n") + "\n";
  }
  const section = items.length ? ["", ...items, ""] : [""];
  const head = lines.slice(0, items.length ? bounds.start : bounds.heading);
  const tail = lines.slice(bounds.end);
  return (items.length ? head.concat(section) : head).concat(tail).join("\n").replace(/\n{3,}/g, "\n\n").replace(/\s+$/, "") + "\n";
}

function _livecodePlanMarkdownWithoutChecklist(markdown) {
  const lines = String(markdown || "").split("\n");
  const bounds = _livecodePlanChecklistBounds(lines);
  if (!bounds) return String(markdown || "");
  return lines.slice(0, bounds.heading).concat(lines.slice(bounds.end)).join("\n");
}

function _livecodePlanCardHtml(plan) {
  const file = String(plan.file || "");
  const overview = String(plan.overview || "").trim();
  return '<div class="livecode-plan-card" data-plan-file="' + _livecodeEscapeHtml(file).replace(/"/g, "&quot;") + '">' +
    '<div class="livecode-plan-card-head">' +
      '<span class="livecode-plan-card-kicker">Created Plan</span>' +
      '<span class="livecode-plan-card-title">' + _livecodeEscapeHtml(plan.title || "Plan") + "</span>" +
    "</div>" +
    '<div class="livecode-plan-card-body">' +
      (overview ? '<div class="livecode-plan-card-overview">' + _livecodeMarkdownToSafeHtml(overview) + "</div>" : "") +
      '<div class="livecode-plan-card-actions">' +
        '<button type="button" class="livecode-btn is-text" data-plan-action="view">View Plan</button>' +
        '<button type="button" class="livecode-btn is-yellow livecode-plan-card-build" data-plan-action="build">Build<span class="livecode-kbd">⌘⏎</span></button>' +
      "</div>" +
    "</div></div>";
}

function _livecodeAppendPlanCard(plan, output, tab) {
  output = output || getLiveCodeChatOutput();
  if (!output || !plan || !plan.file) return null;
  output.querySelectorAll('.livecode-plan-card-row').forEach(function(row) {
    if (row.getAttribute("data-plan-file") === plan.file) row.remove();
  });
  const row = document.createElement("div");
  row.className = "chat-row livecode-plan-card-row";
  row.setAttribute("data-plan-file", plan.file);
  row.innerHTML = _livecodePlanCardHtml(plan);
  _livecodeAppendChatRow(output, row, tab);
  _livecodeSyncPlanCardButtons(output);
  _livecodeAutoScroll(output);
  return row;
}

function _livecodeSyncPlanCardButtons(output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  const tab = _livecodeGetActiveChatTab();
  const building = tab && tab.agentRunning ? String(tab.buildingPlanFile || "") : "";
  output.querySelectorAll(".livecode-plan-card").forEach(function(card) {
    const btn = card.querySelector(".livecode-plan-card-build");
    if (!btn) return;
    const isBuilding = !!building && card.getAttribute("data-plan-file") === building;
    btn.disabled = !!(tab && tab.agentRunning);
    btn.classList.toggle("is-building", isBuilding);
    btn.innerHTML = "Build" + '<span class="livecode-kbd">⌘⏎</span>';
  });
  _livecodeSyncPlanHeaderBuildButton();
  const planInfo = _livecodeActiveFileInfo();
  if (planInfo && planInfo.isPlan && planInfo.planFile) _livecodeRefreshPlanTodosView(planInfo.planFile);
}

function _livecodeSyncPlanHeaderBuildButton() {
  const btn = document.getElementById("ide-plan-build");
  if (!btn) return;
  const info = _livecodeActiveFileInfo();
  const tab = _livecodeGetActiveChatTab();
  const running = !!(tab && tab.agentRunning);
  const isBuilding = running && !!info && !!info.planFile && tab.buildingPlanFile === info.planFile;
  btn.disabled = running;
  btn.classList.toggle("is-building", isBuilding);
  btn.innerHTML = "Build" + '<span class="livecode-kbd">⌘⏎</span>';
}

function _livecodeBuildTodoSummary(todos) {
  const list = (todos || []).filter(function(t) { return t && t.status !== "cancelled"; });
  if (!list.length) return null;
  let index = list.findIndex(function(t) { return t.status === "in_progress"; });
  if (index < 0) index = list.findIndex(function(t) { return t.status !== "completed"; });
  if (index < 0) index = list.length - 1;
  return { item: list[index], position: index + 1, total: list.length };
}

function _livecodeRenderBuildTodos(row, todos) {
  if (!row) return;
  const wrap = row.querySelector(".livecode-plan-exec-todos");
  if (!wrap) return;
  const known = row._livecodeTodoTexts || (row._livecodeTodoTexts = {});
  (row._livecodeTodos || []).forEach(function(t) {
    if (t && t.id && t.content && t.content !== t.id) known[t.id] = t.content;
  });
  todos = (todos || []).map(function(t) {
    if (!t || !t.id || (t.content && t.content !== t.id)) return t;
    return known[t.id] ? Object.assign({}, t, { content: known[t.id] }) : t;
  });
  const planFile = row.getAttribute("data-plan-file");
  if (planFile && !row._livecodeTextsFetched && todos.some(function(t) { return t && t.id && (!t.content || t.content === t.id); })) {
    row._livecodeTextsFetched = true;
    fetch("/livecode/plan-content?file=" + encodeURIComponent(planFile))
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        if (!data || !data.ok) return;
        (data.todos || []).forEach(function(t) { if (t && t.id && t.content) known[t.id] = t.content; });
        _livecodeRenderBuildTodos(row, row._livecodeTodos || todos);
      })
      .catch(function() {});
  }
  const summary = _livecodeBuildTodoSummary(todos);
  if (!summary) {
    wrap.hidden = true;
    wrap.innerHTML = "";
    return;
  }
  row._livecodeTodos = todos;
  const expanded = row.classList.contains("is-todos-expanded");
  wrap.hidden = false;
  wrap.innerHTML =
    '<button type="button" class="livecode-plan-exec-todo-summary" data-plan-action="toggle-todos" aria-expanded="' + expanded + '">' +
      _livecodeTodoStatusIconHtml(summary.item.status) +
      '<span class="livecode-plan-exec-todo-text">' + _livecodeEscapeHtml(summary.item.content) + "</span>" +
      '<span class="livecode-plan-exec-todo-count">' + summary.position + "/" + summary.total + "</span>" +
    "</button>" +
    (expanded ? '<div class="livecode-plan-exec-todo-list">' + todos.map(function(t) {
      return '<div class="livecode-plan-exec-todo-item is-' + (t.status || "pending") + '">' + _livecodeTodoStatusIconHtml(t.status) +
        '<span class="livecode-plan-exec-todo-text">' + _livecodeEscapeHtml(t.content || "") + "</span></div>";
    }).join("") + "</div>" : "");
}

function _livecodeRenderPlanBuildRow(output, displayPayload) {
  const build = displayPayload && displayPayload.plan_build;
  if (!output || !build || !build.file) return null;
  const row = document.createElement("div");
  row.className = "chat-row livecode-user-row livecode-plan-exec-row";
  row.setAttribute("data-plan-file", build.file);
  row.innerHTML =
    '<div class="livecode-plan-exec">' +
      '<button type="button" class="livecode-plan-exec-head" data-plan-action="view" title="Open plan">' +
        '<span class="livecode-plan-exec-label">Build</span>' + _LIVECODE_TODOS_ICON_SVG +
        '<span class="livecode-plan-exec-title">' + _livecodeEscapeHtml(build.title || "Plan") + "</span>" +
      "</button>" +
      '<div class="livecode-plan-exec-todos" hidden></div>' +
    "</div>";
  output.appendChild(row);
  fetch("/livecode/plan-content?file=" + encodeURIComponent(build.file))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data && data.ok && !row._livecodeTodos) _livecodeRenderBuildTodos(row, data.todos || []);
    })
    .catch(function() {});
  return row;
}

function _livecodeUpdateBuildTodos(output, planFile, todos) {
  output = output || getLiveCodeChatOutput();
  if (!output || !planFile) return;
  const rows = Array.from(output.querySelectorAll(".livecode-plan-exec-row")).filter(function(r) {
    return r.getAttribute("data-plan-file") === planFile;
  });
  if (rows.length) _livecodeRenderBuildTodos(rows[rows.length - 1], todos);
}

function _livecodeStartPlanBuild(planFile, title) {
  if (!planFile) return;
  if (!livecodeProjectPath) {
    _livecodeSetPlanStatus("Open a project folder first");
    return;
  }
  const tab = _livecodeGetActiveChatTab();
  if (tab && tab.agentRunning) {
    _livecodeSetPlanStatus("Agent is still running");
    return;
  }
  window.livecodeChatMode = "agent";
  try { localStorage.setItem(LIVECODE_CHAT_MODE_STORAGE_KEY, "agent"); } catch (e) {}
  _livecodeApplyChatModeToUI();
  toggleLiveCodeAgentPane(true);
  window.sendLiveCodeAgentMessage(
    "Implement the approved plan: " + (title || planFile),
    { mode: "agent", planFile: planFile, planTitle: title || planFile }
  );
}

function _livecodeBindPlanCardsOnce() {
  if (window._livecodePlanCardsBound) return;
  window._livecodePlanCardsBound = true;
  document.addEventListener("click", function(e) {
    const actionEl = e.target.closest && e.target.closest("[data-plan-action]");
    if (!actionEl || !actionEl.closest("#livecode-chat-messages")) return;
    const holder = actionEl.closest("[data-plan-file]");
    const planFile = holder ? holder.getAttribute("data-plan-file") : "";
    const action = actionEl.getAttribute("data-plan-action");
    e.preventDefault();
    if (action === "toggle-todos") {
      const row = actionEl.closest(".livecode-plan-exec-row");
      if (!row) return;
      row.classList.toggle("is-todos-expanded");
      if (row._livecodeTodos) {
        _livecodeRenderBuildTodos(row, row._livecodeTodos);
      } else if (planFile) {
        fetch("/livecode/plan-content?file=" + encodeURIComponent(planFile))
          .then(function(resp) { return resp.json(); })
          .then(function(data) { if (data && data.ok) _livecodeRenderBuildTodos(row, data.todos || []); })
          .catch(function() {});
      }
      return;
    }
    if (!planFile) return;
    if (action === "view") {
      window.openLiveCodePlanTab(planFile, "");
    } else if (action === "build" && !actionEl.disabled) {
      const title = (holder.querySelector(".livecode-plan-card-title") || {}).textContent || "";
      _livecodeStartPlanBuild(planFile, title);
    }
  });
}

const _LIVECODE_CHAT_BUBBLE_SVG =
  '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M8 2.5c3.3 0 5.5 2.1 5.5 4.8S11.3 12 8 12c-.6 0-1.2-.1-1.8-.2L3 13.3l.9-2.6C3 9.8 2.5 8.6 2.5 7.3 2.5 4.6 4.7 2.5 8 2.5z"></path></svg>';
const _LIVECODE_SPINNER_SVG =
  '<svg class="livecode-plan-todo-spinner" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true">' +
  '<path d="M8 2a6 6 0 1 1-6 6"></path></svg>';

function _livecodePlanBuildTab(planFile) {
  if (!planFile) return null;
  const marker = 'data-plan-file="' + planFile.replace(/"/g, "&quot;") + '"';
  let found = null;
  livecodeChatTabs.forEach(function(tab) {
    if (found && found.agentRunning) return;
    const live = _livecodeIsActiveTab(tab) ? getLiveCodeChatOutput() : null;
    const html = live ? live.innerHTML : String(tab.messagesHtml || (tab._backgroundOutput && tab._backgroundOutput.innerHTML) || "");
    if (html.indexOf("livecode-plan-exec-row") >= 0 && html.indexOf(marker) >= 0) found = tab;
  });
  return found;
}

function _livecodePlanTodosSectionHtml(todos, planFile) {
  const buildTab = _livecodePlanBuildTab(planFile);
  const building = !!(buildTab && buildTab.agentRunning && buildTab.buildingPlanFile === planFile);
  return '<div class="livecode-plan-todos">' +
    '<div class="livecode-plan-todos-header">' +
      '<span class="livecode-plan-todos-label">' + todos.length + " To-do" + (todos.length === 1 ? "" : "s") + "</span>" +
      '<span class="livecode-plan-todos-actions">' +
        '<button type="button" class="livecode-btn is-secondary" data-plan-todo-action="add">' + _LIVECODE_PLUS_SVG + "New</button>" +
      "</span>" +
    "</div>" +
    '<div class="livecode-plan-todos-list">' +
      (todos.length ? todos.map(function(t, i) {
        const assignment = buildTab
          ? '<span class="livecode-plan-todo-trailing"><button type="button" class="livecode-plan-todo-assignment" data-plan-todo-action="open-build" tabindex="-1" aria-label="Open the Build chat" title="Open the Build chat">' +
              (building && t.status === "in_progress" ? _LIVECODE_SPINNER_SVG : _LIVECODE_CHAT_BUBBLE_SVG) + "</button></span>"
          : "";
        return '<div class="livecode-plan-todo is-' + (t.status || "pending") + '" data-index="' + i + '">' +
          '<button type="button" class="livecode-plan-todo-status" data-plan-todo-action="toggle" tabindex="-1" aria-label="Mark ' +
            (t.status === "completed" ? "not done" : "done") + '">' + _livecodeTodoStatusIconHtml(t.status) + "</button>" +
          '<textarea class="livecode-plan-todo-text" rows="1" spellcheck="false" placeholder="Todo description..." aria-label="Todo content" data-index="' + i + '"></textarea>' +
          assignment +
        "</div>";
      }).join("") : '<button type="button" class="livecode-plan-todos-empty" data-plan-todo-action="add">Add a to-do to get started</button>') +
    "</div></div>";
}

function _livecodeRefreshPlanTodosView(planFile, todos) {
  const key = _livecodePlanTabKey(planFile);
  const info = ideOpenFiles[key];
  if (!info || !info.isPlan) return;
  if (Array.isArray(todos) && todos.length) {
    const statuses = {};
    todos.forEach(function(t) { if (t && t.id) statuses[t.id] = t.status; });
    const merged = _livecodePlanTodosFromMarkdown(info.content || "").map(function(t) {
      return statuses[t.id] ? Object.assign({}, t, { status: statuses[t.id] }) : t;
    });
    const wasClean = info.content === (info.originalContent || "");
    info.content = _livecodePlanMarkdownWithTodos(info.content || "", merged);
    if (wasClean) info.originalContent = info.content;
  }
  const body = document.getElementById("ide-plan-body");
  if (ideActiveFile !== key || !body || info.viewMode === "markdown") return;
  const active = document.activeElement;
  if (active && active.classList && active.classList.contains("livecode-plan-todo-text") && body.contains(active)) return;
  body.querySelectorAll(".livecode-plan-todos").forEach(function(el) { el.remove(); });
  _livecodeMountPlanTodos(body, key);
}

function _livecodeAutosizePlanTodo(ta) {
  if (!ta || (window.CSS && CSS.supports && CSS.supports("field-sizing", "content"))) return;
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + "px";
}

function _livecodeMountPlanTodos(body, fileKey, focus, list) {
  const info = ideOpenFiles[fileKey];
  if (!body || !info || !info.isPlan) return;
  const todos = (list || _livecodePlanTodosFromMarkdown(info.content || "")).map(function(t) {
    return { content: String(t.content || ""), status: t.status || "pending" };
  });
  const holder = document.createElement("div");
  holder.innerHTML = _livecodePlanTodosSectionHtml(todos, info.planFile);
  const section = holder.firstChild;
  section._todos = todos;
  const doc = body.querySelector(".livecode-plan-md");
  (doc || body).appendChild(section);
  section.querySelectorAll(".livecode-plan-todo-text").forEach(function(ta) {
    const item = todos[Number(ta.getAttribute("data-index"))];
    ta.value = item ? item.content : "";
    _livecodeAutosizePlanTodo(ta);
  });
  if (focus && focus.index != null) {
    const ta = section.querySelector('.livecode-plan-todo-text[data-index="' + focus.index + '"]');
    if (ta) {
      ta.focus();
      const pos = focus.caret === "start" ? 0 : ta.value.length;
      ta.setSelectionRange(pos, pos);
    }
  }

  const current = function() {
    section.querySelectorAll(".livecode-plan-todo-text").forEach(function(ta) {
      const item = section._todos[Number(ta.getAttribute("data-index"))];
      if (item) item.content = ta.value.replace(/\s+/g, " ").trim();
    });
    return section._todos.map(function(t) { return Object.assign({}, t); });
  };
  const save = function(next) {
    const markdown = _livecodePlanMarkdownWithTodos(info.content || "", next.filter(function(t) { return t.content; }));
    if (markdown === info.content) return;
    info.content = markdown;
    info.modified = info.content !== (info.originalContent || "");
    _livecodeSavePlanSourceContent(fileKey, { silent: true });
  };
  const rerender = function(next, focusAfter) {
    const scrollTop = body.scrollTop;
    section.remove();
    _livecodeMountPlanTodos(body, fileKey, focusAfter, next);
    body.scrollTop = scrollTop;
  };

  section.addEventListener("click", function(e) {
    const btn = e.target.closest("[data-plan-todo-action]");
    if (!btn) return;
    e.preventDefault();
    const action = btn.getAttribute("data-plan-todo-action");
    const next = current();
    if (action === "add") {
      const last = next.length - 1;
      if (last >= 0 && !next[last].content) {
        rerender(next, { index: last });
        return;
      }
      next.push({ content: "", status: "pending" });
      rerender(next, { index: next.length - 1 });
      return;
    }
    if (action === "open-build") {
      const tab = _livecodePlanBuildTab(info.planFile);
      if (!tab) return;
      if (!_livecodeIsActiveTab(tab)) _livecodeSwitchChatTab(tab.id);
      toggleLiveCodeAgentPane(true);
      const out = getLiveCodeChatOutput();
      const rows = out ? Array.from(out.querySelectorAll(".livecode-plan-exec-row")).filter(function(r) {
        return r.getAttribute("data-plan-file") === info.planFile;
      }) : [];
      if (rows.length) rows[rows.length - 1].scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    const rowEl = btn.closest(".livecode-plan-todo");
    const index = rowEl ? Number(rowEl.getAttribute("data-index")) : -1;
    if (action === "toggle" && next[index] && next[index].content) {
      next[index].status = next[index].status === "completed" ? "pending" : "completed";
      save(next);
      rerender(next.filter(function(t) { return t.content; }));
    }
  });
  section.addEventListener("input", function(e) {
    const ta = e.target.closest && e.target.closest(".livecode-plan-todo-text");
    if (ta) _livecodeAutosizePlanTodo(ta);
  });
  section.addEventListener("keydown", function(e) {
    const ta = e.target.closest && e.target.closest(".livecode-plan-todo-text");
    if (!ta) return;
    const index = Number(ta.getAttribute("data-index"));
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const next = current();
      if (!next[index] || !next[index].content) return;
      next.splice(index + 1, 0, { content: "", status: "pending" });
      save(next);
      rerender(next, { index: index + 1 });
    } else if (e.key === "Backspace" && !ta.value && ta.selectionStart === 0) {
      e.preventDefault();
      const next = current();
      next.splice(index, 1);
      save(next);
      rerender(next, index > 0 ? { index: index - 1 } : null);
    } else if (e.key === "ArrowUp" && ta.selectionStart === 0 && index > 0) {
      e.preventDefault();
      const prev = section.querySelector('.livecode-plan-todo-text[data-index="' + (index - 1) + '"]');
      if (prev) { prev.focus(); prev.setSelectionRange(prev.value.length, prev.value.length); }
    } else if (e.key === "ArrowDown" && ta.selectionEnd === ta.value.length) {
      const below = section.querySelector('.livecode-plan-todo-text[data-index="' + (index + 1) + '"]');
      if (below) { e.preventDefault(); below.focus(); below.setSelectionRange(0, 0); }
    } else if (e.key === "Escape") {
      e.preventDefault();
      ta.blur();
    }
  });
  section.addEventListener("focusout", function(e) {
    const ta = e.target.closest && e.target.closest(".livecode-plan-todo-text");
    if (!ta || !section.isConnected) return;
    const next = current();
    save(next);
    const stillInList = e.relatedTarget && section.contains(e.relatedTarget);
    if (!stillInList && next.some(function(t) { return !t.content; })) {
      rerender(next.filter(function(t) { return t.content; }));
    }
  });
}

function _livecodeHideStatusRow() {
  if (_livecodeStatusRow && _livecodeStatusRow.parentNode) _livecodeStatusRow.remove();
}

function _livecodeClearPendingToolSteps() {
  _livecodeFinalizeRunningActivity();
  _livecodeSettleRunningTerminalCards(getLiveCodeChatOutput());
  _livecodeHideStatusRow();
}

let _livecodeProgressSnapshotTimer = null;
const _livecodeProgressSnapshotTimersByTab = {};

function _livecodeFlushProgressSnapshot(targetTab, output, isActiveTab) {
  if (!targetTab || !output) return;
  targetTab.messagesHtml = output.innerHTML;
  if (!isActiveTab) {
    targetTab.hasUnread = true;
    _livecodeRenderChatTabs();
  } else if (targetTab.messagesHtml && targetTab.sessionId) {
    _livecodeSyncActiveTabMessagesHtml();
  }
}

function _livecodeScheduleProgressSnapshot(targetTab, output, isActiveTab, immediate) {
  if (immediate) _livecodeRegroupTranscript(output, { lastTurnOnly: true });
  else _livecodeScheduleRegroup(output);
  const tabId = targetTab && (targetTab.sessionId || targetTab.id);
  if (immediate) {
    if (tabId && _livecodeProgressSnapshotTimersByTab[tabId]) {
      clearTimeout(_livecodeProgressSnapshotTimersByTab[tabId]);
      delete _livecodeProgressSnapshotTimersByTab[tabId];
    }
    _livecodeFlushProgressSnapshot(targetTab, output, isActiveTab);
    return;
  }
  if (!tabId) return;
  if (_livecodeProgressSnapshotTimersByTab[tabId]) {
    clearTimeout(_livecodeProgressSnapshotTimersByTab[tabId]);
  }
  _livecodeProgressSnapshotTimersByTab[tabId] = setTimeout(function() {
    delete _livecodeProgressSnapshotTimersByTab[tabId];
    _livecodeFlushProgressSnapshot(targetTab, output, isActiveTab);
  }, 1500);
}

function handleLiveCodeProgress(data) {
  if (!data) return;
  const sessionId = data.session_id || "";
  const turnId = data.turn_id || "";
  const seq = Number(data.seq || 0);
  const targetTab = livecodeChatTabs.find(function(t) { return t.sessionId === data.session_id; })
    || _livecodeFindBackgroundChatTab(data.session_id);
  const isCostEvent = data.status === "complete" || data.type === "usage";
  if (!targetTab || (!targetTab.agentRunning && !isCostEvent)) return;
  if (sessionId && turnId) {
    const activeTurnId = _livecodeActiveTurnIdBySession[sessionId];
    if (activeTurnId && activeTurnId !== turnId) return;
    _livecodeActiveTurnIdBySession[sessionId] = turnId;
  }
  if (sessionId && seq) {
    const seqKey = turnId ? (sessionId + ":" + turnId) : sessionId;
    const lastSeq = _livecodeLastProgressSeqBySession[seqKey] || 0;
    if (seq <= lastSeq) return;
    _livecodeLastProgressSeqBySession[seqKey] = seq;
  }

  const isActiveTab = _livecodeIsActiveTab(targetTab);
  if (typeof window._livecodeNotifyProgress === "function") {
    try { window._livecodeNotifyProgress(data); } catch (e) {}
  }
  if (data.status === "complete") {
    const completeCost = Number(data.cost_usd || 0);
    if (completeCost > 0) {
      const turnUsageKey = sessionId && turnId ? (sessionId + ":" + turnId) : "";
      const priorTurnCost = turnUsageKey && targetTab._usageCostByTurn ? Number(targetTab._usageCostByTurn[turnUsageKey] || 0) : 0;
      const missingCost = completeCost - priorTurnCost;
      if (missingCost > 0.0000001) {
        targetTab.costUsd = (targetTab.costUsd || 0) + missingCost;
        if (isActiveTab) _livecodeUpdateChatCostDisplay(targetTab, { animate: true });
      }
    }
    targetTab._turnStreamComplete = true;
  } else if (data.status === "progress" && data.type === "usage") {
    const promptTokens = Number(data.prompt_tokens || 0);
    const contextLimit = Number(data.context_tokens || 0);
    if (promptTokens > 0 && contextLimit > 0) {
      targetTab.contextUsed = promptTokens;
      targetTab.contextLimit = contextLimit;
      if (isActiveTab) _livecodeUpdateContextRing(targetTab);
    }
    const usageCost = Number(data.cost_usd || 0);
    if (usageCost > 0) {
      targetTab.costUsd = (targetTab.costUsd || 0) + usageCost;
      if (sessionId && turnId) {
        const usageKey = sessionId + ":" + turnId;
        targetTab._usageCostByTurn = targetTab._usageCostByTurn || {};
        targetTab._usageCostByTurn[usageKey] = Number(targetTab._usageCostByTurn[usageKey] || 0) + usageCost;
      }
      if (isActiveTab) _livecodeUpdateChatCostDisplay(targetTab, { animate: true });
    }
    return;
  }

  const output = _livecodeGetOutputForTab(targetTab);
  if (!output) return;

  _livecodeWithTabContext(targetTab, function() {
    if (isActiveTab) {
      if (data.session_id !== livecodeAgentSessionId) return;
      if (!_livecodeChatOutputBelongsToTab(getLiveCodeChatOutput(), targetTab)) return;
    }

    if (data.status === "complete") {
      _livecodeScheduleProgressSnapshot(targetTab, output, isActiveTab, true);
      return;
    }
    if (data.status !== "progress") return;

    const progressType = data.type || "shimmer";

    if (
      (targetTab._answerStreaming || targetTab._turnStreamComplete) &&
      progressType !== "permission_request"
    ) {
      return;
    }

    const message = data.message || "";
    const tool = data.tool || "";
    const args = data.args || {};

    if (progressType === "permanent") {
      return;
    } else if (progressType === "diff_block") {
      _livecodeCommitLiveNarration(output, targetTab);
      _livecodeFinalizeRunningActivity(_livecodeIsFileEditTool(_livecodeLastTool) ? { remove: true } : { pastTense: true }, output);
      appendLiveCodeDiffBlock({
        file_name: data.file_name,
        diff_html: message,
        additions: data.additions || 0,
        deletions: data.deletions || 0,
        absolute_path: data.absolute_path || "",
        created: !!data.created,
      }, output);
    } else if (progressType === "plan_created") {
      const planFile = data.plan_file || "";
      const planTitle = data.plan_title || message || "Plan";
      targetTab.planFile = planFile;
      if (planFile) {
        _livecodeFinalizeRunningActivity({ pastTense: true }, output);
        _livecodeAppendPlanCard({ file: planFile, title: planTitle, overview: data.overview || "" }, output, targetTab);
        if (isActiveTab && _livecodeSettingsGet("planAutoOpen")) {
          try { window.openLiveCodePlanTab(planFile, planTitle); } catch (e) {}
        }
      }
    } else if (progressType === "compaction") {
      _livecodeResolveRunningActivityEl(output);
      if (_livecodeRunningActivityEl) {
        const text = _livecodeRunningActivityEl.querySelector(".livecode-activity-text");
        const label = String(text && text.textContent ? text.textContent : "").trim();
        if (/^Compacting conversation history\b/i.test(label) || /^Compacted conversation history\b/i.test(label)) return;
      }
      _livecodeFinalizeRunningActivity(undefined, output);
      _livecodeHideStatusRow();
      _livecodeLastTool = "compaction";
      _livecodeLastToolArgs = { forced: !!data.forced };
      _livecodeLastToolLabel = "Compacting conversation history";
      _livecodeAppendActivityParts({
        verb: "Compacted",
        detail: "conversation history",
        meta: data.forced ? "context limit" : "",
        shimmer: true,
      }, true, output);
    } else if (progressType === "model_resolved") {
      return;
    } else if (progressType === "agent_thinking") {
      _livecodeHideStatusRow();
      _livecodeStepNarrationEl = null;
      _livecodeRemovePendingToolLines(output);
      _livecodeSettleRunningTerminalCards(output);
      _livecodeCancelThoughtStreamFlush();
      _livecodePendingThoughtContent = "";
      _livecodeStreamingThoughtContentEl = null;
      const thinkingLabel = message && !/^Thinking$/i.test(message.trim()) ? message : "Thinking";
      if (_livecodeRunningActivityEl && _livecodeIsThinkingLine()) {
        _livecodeThinkingStartMs = Date.now();
        _livecodeLastToolLabel = thinkingLabel;
        _livecodeUpdateThinkingLineLabel();
        _livecodeCleanupStaleThinkingLines(output, _livecodeRunningActivityEl);
        _livecodeStartThinkingTicker();
        return;
      }
      if (_livecodeAdoptRunningThinking(output, thinkingLabel)) {
        _livecodeThinkingStartMs = Date.now();
        _livecodeStartThinkingTicker();
        return;
      }
      _livecodeThinkingStartMs = Date.now();
      _livecodeLastTool = "attempt_completion";
      _livecodeLastToolArgs = {};
      _livecodeFinalizeRunningActivity(undefined, output);
      _livecodeLastToolLabel = thinkingLabel;
      if (message && !/^Thinking$/i.test(message.trim())) {
        _livecodeAppendActivityParts(_livecodeParseActivityParts("attempt_completion", {}, message), true, output);
      } else {
        _livecodeAppendActivityParts({ verb: "Thinking", detail: "", meta: "" }, true, output);
      }
      _livecodeStartThinkingTicker();
    } else if (progressType === "agent_thinking_delta") {
      if (/^Retrying\b/i.test(String(_livecodeLastToolLabel || "").trim())) {
        _livecodeLastToolLabel = "Thinking";
        _livecodeThinkingStartMs = Date.now();
        _livecodeStartThinkingTicker();
      }
      if (!_livecodeIsThinkingLine()) {
        _livecodeLastTool = "attempt_completion";
        _livecodeLastToolLabel = "Thinking";
        if (!_livecodeThinkingStartMs) _livecodeThinkingStartMs = Date.now();
        _livecodeStartThinkingTicker();
      } else if (_livecodeLastToolLabel !== "Thinking") {
        _livecodeLastToolLabel = "Thinking";
        _livecodeStartThinkingTicker();
      }
      _livecodeAppendThoughtDelta(data.delta || "");
    } else if (progressType === "agent_thinking_done") {
      _livecodePendingDurationS = data.duration_s || null;
      _livecodePendingDurationMs = data.duration_ms || null;
      _livecodeFinalizeThoughtActivity(data.thought_content || "", output, {
        answerPending: !!data.answer_pending,
      });
      if (data.narration) _livecodeAppendNarration(data.narration, output, targetTab);
    } else if (progressType === "content_delta") {
      if (isActiveTab) _livecodeAppendAnswerDelta(data.delta || "", output, targetTab);
    } else if (progressType === "content_done") {
      _livecodeSettleLiveContent(data, output, targetTab, isActiveTab);
    } else if (progressType === "content_retract") {
      if (isActiveTab) _livecodeDropLiveAnswerPreamble();
      if (_livecodeStepNarrationEl) {
        const row = _livecodeStepNarrationEl.closest(".livecode-narration-row");
        if (row && row.parentNode) row.parentNode.removeChild(row);
        _livecodeStepNarrationEl = null;
      }
      _livecodeRemovePendingToolLines(output);
    } else if (progressType === "tool_call_pending") {
      _livecodeShowPendingToolCall(data, output, targetTab);
    } else if (progressType === "answer_delta") {
      if (isActiveTab) _livecodeAppendAnswerDelta(data.delta || "", output, targetTab);
    } else if (progressType === "answer_done") {
      if (isActiveTab) {
        _livecodeFinalizeLiveAnswer(data.answer || "", output, targetTab);
      } else {
        targetTab._answerStreamedText = data.answer || "";
        _livecodeResetAnswerLiveStream();
      }
    } else if (progressType === "tool_call") {
      if (tool === "todo_write") {
        _livecodeLastTool = tool;
        _livecodeLastToolArgs = args;
        return;
      }
      _livecodeCommitLiveNarration(output, targetTab);
      _livecodeRemovePendingToolLines(output);
      _livecodeFinalizeRunningActivity(undefined, output);
      _livecodeSettleRunningTerminalCards(output);
      _livecodeLastTool = tool;
      _livecodeLastToolArgs = args;
      _livecodeLastToolLabel = message;
      _livecodeHideStatusRow();
      if (tool === "run_command") {
        _livecodeStartTerminalCard(args, output, targetTab);
        return;
      }
      if (tool === "spawn_subagent") {
        _livecodeAgentsAddRow(output, {
          id: data.tool_call_id || "",
          title: args.title || String(message || "").replace(/^Subagent:\s*/, ""),
          goal: args.goal || "",
          state: "running",
        });
        _livecodeAutoScroll(output);
        return;
      }
      if (tool === "edit_file" && args && args.file_path) {
        const stepsContainer = _livecodeEnsureAgentStepsRow(output);
        _livecodeClearTransientEditFailure(args.file_path, stepsContainer);
      }
      if (tool === "browser") _livecodeBrowserAgentActivity(false);
      const parts = _livecodeParseActivityParts(tool, args, message);
      _livecodeAppendActivityParts(parts, true, output);
    } else if (progressType === "tool_result") {
      if (LIVECODE_TREE_MUTATING_TOOLS.has(tool)) _livecodeScheduleSilentTreeRefresh();
      if (tool === "todo_write") {
        _livecodeHideStatusRow();
        if (!targetTab._turnStreamComplete) _livecodeShowImmediateThinking(output);
        return;
      }
      if (tool === "run_command") {
        const result = data.result || {};
        const card = _livecodeRunningTerminalCard(output);
        if (card) {
          _livecodeFinishTerminalCard(card, result);
        } else if (result.command || (data.args && data.args.command)) {
          _livecodeAppendLoadedCommandBlock(result.command || data.args.command, result.output || result.error || "", result.exit_code, output, {
            description: data.args && data.args.description,
          });
        }
        _livecodeHideStatusRow();
        if (!targetTab._turnStreamComplete) _livecodeShowImmediateThinking(output);
        return;
      }
      if (tool === "spawn_subagent") {
        _livecodeAgentsFinishRow(output, data.tool_call_id || "", data.result || {}, data.args || {});
        _livecodeHideStatusRow();
        if (!targetTab._turnStreamComplete) _livecodeShowImmediateThinking(output);
        return;
      }
      if (tool === "browser") {
        _livecodeFinishBrowserStep(data.args || _livecodeLastToolArgs, Object.assign({}, data.result || {}, data.error && !(data.result || {}).error ? { error: data.error_full || message } : {}), output);
        _livecodeBrowserAgentActivity(true);
        _livecodeHideStatusRow();
        if (!targetTab._turnStreamComplete) _livecodeShowImmediateThinking(output);
        return;
      }
      if (tool === "ask_question") {
        _livecodeClearQuestionRequest(targetTab);
        if (message) _livecodeLastToolLabel = message;
      }
      const errorMessage = data.error_full || message;
      if (data.error && errorMessage && _livecodeRunningActivityEl && _livecodeIsFileEditTool(tool)) {
        _livecodeRecordEditFailure(
          data.args || _livecodeLastToolArgs,
          errorMessage,
          data.error_kind,
          output
        );
      } else {
        _livecodeFinalizeRunningActivity({ pastTense: !data.error }, output);
        if (data.error && errorMessage) {
          if (_livecodeIsFileEditTool(tool)) {
            _livecodeRecordEditFailure(
              data.args || _livecodeLastToolArgs,
              errorMessage,
              data.error_kind,
              output
            );
          } else {
            const full = String(errorMessage);
            const isResolutionMiss = /(not found|no such|does(?:n'?t| not) exist|no matches?\b|path outside|path traversal)/i.test(full);
            if (!isResolutionMiss) {
              const parts = {
                verb: "Failed",
                detail: _livecodeTruncateMiddle(full, 140),
                meta: "",
                isError: true,
                fullErrorTitle: full,
              };
              _livecodeAppendActivityParts(parts, false, output);
            }
          }
        }
      }
      _livecodeHideStatusRow();
      if (!targetTab._turnStreamComplete && tool !== "attempt_completion") {
        _livecodeShowImmediateThinking(output);
      }
    } else if (progressType === "subagent_update") {
      _livecodeAgentsApplyUpdate(output, data);
      return;
    } else if (progressType === "todo_update") {
      if (data.plan_file) {
        _livecodeUpdateBuildTodos(output, data.plan_file, data.todos || []);
        _livecodeRefreshPlanTodosView(data.plan_file, data.todos || []);
      }
      return;
    } else if (
      progressType === "goal_blocked" ||
      progressType === "goal_completed"
    ) {
      return;
    } else if (progressType === "permission_request") {
      _livecodeFinalizeRunningActivity(tool === "browser" ? { remove: true } : undefined, output);
      _livecodeHideStatusRow();
      _livecodeShowPermissionRequest(data, output, targetTab);
    } else if (progressType === "question_request") {
      _livecodeShowQuestionRequest(data, targetTab);
    } else if (progressType === "question_closed") {
      _livecodeClearQuestionRequest(targetTab, data.request_id || "");
    } else if (progressType === "permission_expired") {
      const requestId = data.request_id || "";
      if (targetTab && targetTab.pendingPermissionRequestId === requestId) targetTab.pendingPermissionRequestId = "";
      _livecodeClearPermissionRequest(requestId);
      _livecodeResolvePermissionRow(requestId, false, output, "Permission expired");
    } else if (progressType === "agent_status") {
      const statusMsg = String(message || "").trim();
      if (/retrying model/i.test(statusMsg)) {
        _livecodeThinkingStartMs = Date.now();
        _livecodePendingDurationS = null;
        _livecodePendingDurationMs = null;
        _livecodeLastToolLabel = "Retrying model…";
        _livecodeStopThinkingTicker();
        _livecodeResolveRunningActivityEl();
        if (_livecodeRunningActivityEl) {
          const textEl = _livecodeRunningActivityEl.querySelector(".livecode-activity-text");
          if (textEl) textEl.textContent = "Retrying model…";
        }
      }
      return;
    } else if (message && !_livecodeIsGenericStatusMessage(message)) {
      _livecodeShowShimmer(message, output);
    } else {
      _livecodeShowTyping(undefined, output);
    }
  });

  _livecodeScheduleProgressSnapshot(targetTab, output, isActiveTab, false);
}

function initLiveCodeAgentSocket() {
  _livecodeBindPlanCardsOnce();
  if (window._livecodeAgentSocket) return;
  _livecodeBindThoughtTogglesOnce();
  _livecodeBindDiffFileNameClicksOnce();
  _livecodeBindTerminalCardsOnce();
  _livecodeBindAgentsCardOnce();
  window._livecodeAgentSocket = (typeof socket !== "undefined" && socket) ? socket : io.connect(location.protocol + "//" + location.host);
  window._livecodeAgentSocket.on("livecode_progress", handleLiveCodeProgress);
  if (!window._livecodeCommandBound) {
    window._livecodeCommandBound = true;
    window._livecodeAgentSocket.on("lazie_command_stream", _livecodeHandleCommandStream);
  }
}

function _livecodeParseDiffLineHint(diffHtml) {
  const html = String(diffHtml || "");
  const startM = html.match(/data-start-line="(\d+)"/);
  if (!startM) return "";
  const wrappers = html.match(/diff-block-wrapper/g) || [];
  const endM = html.match(/data-end-line="(\d+)"/);
  const start = startM[1];
  if (wrappers.length === 1 && endM && endM[1] !== start) return "L" + start + "-" + endM[1];
  return "L" + start;
}

function _livecodeDiffRowsHtml(diffHtml) {
  const tpl = document.createElement("div");
  tpl.innerHTML = String(diffHtml || "");
  const blocks = Array.from(tpl.querySelectorAll(".diff-block-wrapper"));
  const groups = blocks.length ? blocks : [tpl];
  const rows = [];
  const types = [];
  let widest = 1;
  groups.forEach(function(block, blockIndex) {
    const lines = Array.from(block.querySelectorAll(".diff-line-row, .diff-line-wrapper"))
      .filter(function(el) { return !el.querySelector(".diff-line-row"); });
    if (!lines.length) return;
    if (blockIndex > 0 && rows.length) {
      rows.push('<div class="ui-default-diff__separator"><span>\u22ef</span></div>');
      types.push("separator");
    }
    lines.forEach(function(line) {
      const wrap = line.closest(".diff-line-wrapper") || line;
      const has = function(sel) { return line.matches(sel) || wrap.matches(sel) || !!line.querySelector(sel); };
      const type = has(".diff-line-added-row, .diff-line-added-wrapper, .diff-line-sign-added, .diff-line-added") ? "added"
        : has(".diff-line-deleted-row, .diff-line-deleted-wrapper, .diff-line-sign-deleted, .diff-line-deleted") ? "removed"
          : "unchanged";
      const numEl = line.querySelector(".diff-line-number-inline");
      const num = numEl ? numEl.textContent.trim() : "";
      widest = Math.max(widest, num.length);
      const codeEl = line.querySelector(".diff-line-content");
      const code = codeEl ? codeEl.innerHTML : _livecodeEscapeHtml(line.textContent || "");
      const sign = type === "added" ? "+" : (type === "removed" ? "-" : "\u00a0");
      rows.push('<div class="ui-default-diff__line" data-type="' + type + '">' +
        '<div class="ui-default-diff__gutter"><span class="ui-default-diff__line-number">' + _livecodeEscapeHtml(num) + "</span>" +
        '<span class="ui-default-diff__line-indicator">' + sign + "</span></div>" +
        '<div class="ui-default-diff__line-content">' + (code || " ") + "</div></div>");
      types.push(type);
    });
  });
  if (!rows.length) return "";
  const firstChange = types.findIndex(function(t) { return t === "added" || t === "removed"; });
  const previewStart = Math.max(0, firstChange - 1);
  for (let i = 0; i < previewStart; i++) {
    rows[i] = rows[i].replace(/^<div class="ui-default-diff__(line|separator)"/, '<div data-preview="before" class="ui-default-diff__$1"');
  }
  return '<div class="ui-default-diff" style="--ui-default-diff-line-number-width:' + widest + 'ch">' + rows.join("") + "</div>";
}

function _livecodeBuildDiffBlockHtml({ fileName, absolutePath, fileIcon, additions, deletions, diffContent, diffBlockId, lineHint, isNewFile }) {
  const safeFullPath = _livecodeEscapeHtml(fileName);
  const baseName = String(fileName || "").split("/").pop() || fileName;
  const openPath = absolutePath || fileName;
  const safeFilePathAttr = _livecodeEscapeHtml(openPath).replace(/"/g, "&quot;");
  const lineMetaAttr = lineHint ? ' data-line-meta="' + _livecodeEscapeHtml(lineHint).replace(/"/g, "&quot;") + '"' : "";
  const rows = _livecodeDiffRowsHtml(diffContent) || '<div class="ui-default-diff">' + String(diffContent || "") + "</div>";
  const stats = (additions ? '<span class="ui-edit-tool-call__additions">+' + additions + "</span>" : "") +
    (deletions ? '<span class="ui-edit-tool-call__deletions">-' + deletions + "</span>" : "");
  const expanded = (rows.match(/data-type="/g) || []).length <= LIVECODE_AUTO_EXPAND_DIFF_LINES;
  const expandedAttr = expanded ? "true" : "false";
  return '<div class="livecode-diff-block ui-edit-tool-call' + (expanded ? " is-expanded" : "") + '" data-file-name="' + _livecodeEscapeHtml(fileName).replace(/"/g, "&quot;") + '" data-additions="' + (additions || 0) + '" data-deletions="' + (deletions || 0) + '">' +
    '<div class="livecode-diff-header-row ui-edit-tool-call__header" data-file-path="' + safeFilePathAttr + '"' + lineMetaAttr + ' title="' + safeFullPath + '">' +
    (fileIcon ? '<img class="ui-edit-tool-call__icon" src="' + _livecodeEscapeHtml(fileIcon).replace(/"/g, "&quot;") + '" alt="" aria-hidden="true" draggable="false">' : "") +
    '<span class="ui-edit-tool-call__action">' + (isNewFile ? "Created" : "Edited") + "</span>" +
    '<span class="livecode-diff-file-name-link ui-edit-tool-call__filename" data-file-path="' + safeFilePathAttr + '"' + lineMetaAttr + ">" + _livecodeEscapeHtml(baseName) + "</span>" +
    (stats ? '<span class="ui-edit-tool-call__stats">' + stats + "</span>" : "") +
    '<button type="button" class="ui-edit-tool-call__expand" aria-expanded="' + expandedAttr + '" aria-label="Show diff" data-edit-expand>' + _livecodeIcon("chevron-right", { size: "sm" }) + "</button>" +
    "</div>" +
    '<div id="' + diffBlockId + '-content" class="livecode-diff-content ui-edit-tool-call__content"><div class="ui-edit-tool-call__scroll">' + rows + "</div></div>" +
    '<button type="button" class="ui-tool-call-card__expand-button" aria-expanded="' + expandedAttr + '" aria-label="Show full diff" data-edit-expand>' +
    '<span class="ui-tool-call-card__expand-icon">' + _livecodeIcon("chevron-down", { size: "sm" }) + "</span></button>" +
    "</div>";
}

function _livecodeToggleEditCard(block) {
  const open = !block.classList.contains("is-expanded");
  block.classList.toggle("is-expanded", open);
  block.querySelectorAll("[data-edit-expand]").forEach(function(btn) {
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

function _livecodeBindDiffFileNameClicksOnce() {
  if (window._livecodeDiffFileNameClicksBound) return;
  window._livecodeDiffFileNameClicksBound = true;
  document.addEventListener("click", function(e) {
    const expand = e.target.closest ? e.target.closest("#livecode-chat-messages [data-edit-expand]") : null;
    if (expand) {
      e.preventDefault();
      e.stopPropagation();
      _livecodeToggleEditCard(expand.closest(".ui-edit-tool-call"));
      return;
    }
    const editCard = e.target.closest ? e.target.closest("#livecode-chat-messages .ui-edit-tool-call") : null;
    if (editCard && !e.target.closest(".livecode-diff-file-name-link")) {
      const selected = window.getSelection ? String(window.getSelection()) : "";
      if (!selected) {
        e.preventDefault();
        e.stopPropagation();
        _livecodeToggleEditCard(editCard);
      }
      return;
    }
    const label = e.target.closest
      ? e.target.closest(".livecode-diff-file-name-link, .livecode-activity-file-link, .livecode-md-file-link, .ui-edit-tool-call__header, .ui-tool-call-line--clickable")
      : null;
    if (!label || !label.closest("#livecode-chat-messages, #ide-plan-body")) return;
    const source = label.hasAttribute("data-file-path") ? label : label.querySelector("[data-file-path]");
    const filePath = source && source.getAttribute("data-file-path");
    if (!filePath) return;
    e.preventDefault();
    e.stopPropagation();
    showIDEPanel("editor");
    const meta = String(source.getAttribute("data-line-meta") || "").trim();
    const m = meta.match(/^L(\d+)/i);
    const lineNumber = m ? _livecodeNormalizeLineNumber(m[1]) : null;
    openFileInEditorFromPath(filePath, lineNumber ? { lineNumber: lineNumber } : {});
  });
}
_livecodeBindDiffFileNameClicksOnce();

function appendLiveCodeDiffBlock(data, container) {
  const out = container || getLiveCodeChatOutput();
  if (!out) return;
  _livecodeBindDiffFileNameClicksOnce();
  const fileName = data.file_name || "file";
  const diffRow = document.createElement("div");
  diffRow.className = "chat-row livecode-stream-msg livecode-diff-row";
  diffRow.setAttribute("data-step-id", _livecodeNextStepId());
  const diffBlockId = "livecode-diff-" + Date.now() + "-" + Math.random().toString(36).slice(2, 9);
  const diffHtml = data.diff_html || "";
  const lineHint = _livecodeParseDiffLineHint(diffHtml);
  diffRow.innerHTML = '<div class="chat-msg assistant livecode-plain-msg">' + _livecodeBuildDiffBlockHtml({
    fileName: fileName,
    absolutePath: data.absolute_path || "",
    fileIcon: _livecodeFileIcon(fileName),
    additions: Number(data.additions) || 0,
    deletions: Number(data.deletions) || 0,
    diffContent: diffHtml,
    diffBlockId: diffBlockId,
    lineHint: lineHint,
    isNewFile: !!data.created,
  }) + "</div>";
  _livecodeRemoveTransientThought(out);
  _livecodeAppendChatRow(out, diffRow);
  _livecodeScheduleRegroup(out);
  _livecodeAutoScroll(out);
  _livecodeSyncActiveTabMessagesHtml();
  if (data.absolute_path) refreshLiveCodeFileFromDisk(data.absolute_path);
  _livecodeSchedulePendingChangesRefresh();
}

let _livecodeChangesFiles = [];
let _livecodeChangesFetchSeq = 0;
let _livecodeChangesRefreshTimer = null;
let _livecodeChangesBusy = false;
let _livecodeChangesBarBound = false;

function _livecodeChangesRequest(path, extra) {
  const tab = _livecodeGetActiveChatTab();
  const sessionId = tab && tab.sessionId;
  if (!sessionId || !livecodeProjectPath) return Promise.resolve(null);
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({
      project_path: livecodeProjectPath,
      session_id: sessionId,
      workspace: _livecodeCurrentWorkspacePayload(),
    }, extra || {})),
  }).then(function(resp) { return resp.json(); });
}

function _livecodeRefreshPendingChanges() {
  const seq = ++_livecodeChangesFetchSeq;
  _livecodeChangesRequest("/livecode/session/changes").then(function(data) {
    if (seq !== _livecodeChangesFetchSeq) return;
    _livecodeRenderChangesBar(data && data.success ? (data.files || []) : []);
  }).catch(function() {});
}

function _livecodeSchedulePendingChangesRefresh() {
  if (_livecodeChangesRefreshTimer) clearTimeout(_livecodeChangesRefreshTimer);
  _livecodeChangesRefreshTimer = setTimeout(function() {
    _livecodeChangesRefreshTimer = null;
    _livecodeRefreshPendingChanges();
  }, 250);
}

function _livecodeChangesFileRowHtml(file) {
  const abs = String(file.path || "");
  const rel = _livecodeWorkspaceRepoPath(abs) || abs;
  const name = _livecodeBasename(rel);
  const dir = rel.slice(0, Math.max(0, rel.length - name.length)).replace(/\/$/, "");
  const icon = _livecodeFileIcon(name);
  const stats = (file.additions ? `<span class="livecode-changes-added">+${file.additions}</span>` : "") +
    (file.deletions ? `<span class="livecode-changes-deleted">-${file.deletions}</span>` : "");
  const status = file.created ? "Created" : (file.deleted ? "Deleted" : "");
  const pathAttr = _livecodeEscapeHtml(abs).replace(/"/g, "&quot;");
  const canUndo = file.can_undo !== false;
  return `<div class="livecode-changes-file-row">` +
    `<button type="button" class="livecode-changes-file${file.deleted ? " is-deleted" : ""}" data-changes-action="open" data-changes-path="${pathAttr}" title="${_livecodeEscapeHtml(rel).replace(/"/g, "&quot;")}">` +
    `<img class="livecode-changes-file-icon" src="${_livecodeEscapeHtml(icon).replace(/"/g, "&quot;")}" alt="">` +
    `<span class="livecode-changes-file-name">${_livecodeEscapeHtml(name)}</span>` +
    (dir ? `<span class="livecode-changes-file-dir">${_livecodeEscapeHtml(dir)}</span>` : "") +
    (status ? `<span class="livecode-changes-file-status">${status}</span>` : "") +
    `<span class="livecode-changes-stats">${stats}</span></button>` +
    `<span class="livecode-changes-file-actions">` +
    (canUndo ? `<button type="button" class="livecode-changes-file-btn" data-changes-action="undo-file" data-changes-path="${pathAttr}" title="Undo changes to ${_livecodeEscapeHtml(name).replace(/"/g, "&quot;")}" aria-label="Undo changes to ${_livecodeEscapeHtml(name).replace(/"/g, "&quot;")}">${_LIVECODE_REVERT_ICON_SVG}</button>` : "") +
    `<button type="button" class="livecode-changes-file-btn" data-changes-action="keep-file" data-changes-path="${pathAttr}" title="Keep changes to ${_livecodeEscapeHtml(name).replace(/"/g, "&quot;")}" aria-label="Keep changes to ${_livecodeEscapeHtml(name).replace(/"/g, "&quot;")}"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"></path></svg></button>` +
    `</span></div>`;
}

function _livecodeSetChangesExpanded(bar, expanded) {
  const list = document.getElementById("livecode-changes-list");
  const toggle = bar && bar.querySelector(".livecode-changes-toggle");
  if (!bar || !list) return;
  bar.classList.toggle("is-expanded", !!expanded);
  list.hidden = !expanded;
  if (toggle) toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function _livecodeRenderChangesBar(files) {
  _livecodeChangesFiles = Array.isArray(files) ? files : [];
  const bar = document.getElementById("livecode-changes-bar");
  if (!bar) return;
  const count = _livecodeChangesFiles.length;
  const tab = _livecodeGetActiveChatTab();
  const running = !!(tab && tab.agentRunning);
  bar.classList.toggle("is-agent-running", running);
  bar.classList.toggle("has-files", count > 0);
  const label = bar.querySelector(".livecode-changes-count");
  const labelMode = running ? "running" : "files:" + count;
  if (label && label.getAttribute("data-mode") !== labelMode) {
    label.setAttribute("data-mode", labelMode);
    label.innerHTML = running
      ? 'Generating<span class="livecode-gen-dots" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span>'
      : count + (count === 1 ? " File" : " Files");
  }
  const toggle = bar.querySelector(".livecode-changes-toggle");
  if (toggle) toggle.disabled = !count;
  const list = document.getElementById("livecode-changes-list");
  if (list) list.innerHTML = _livecodeChangesFiles.map(_livecodeChangesFileRowHtml).join("");
  bar.querySelectorAll("[data-changes-action]").forEach(function(btn) {
    const action = btn.getAttribute("data-changes-action");
    if (action === "stop") btn.hidden = !running;
    else if (action === "undo" || action === "keep") btn.hidden = running || !count;
    else if (action === "review") btn.hidden = !count;
    if (action === "undo" || action === "keep" || action === "undo-file" || action === "keep-file") {
      btn.disabled = running || (action === "undo" && !_livecodeChangesFiles.some(function(f) { return f.can_undo; }));
    }
  });
  if (!count) _livecodeSetChangesExpanded(bar, false);
  _livecodeBindChangesBarOnce();
  _livecodeSyncComposerStack();
}

function _livecodeSyncComposerStack() {
  const stack = document.getElementById("livecode-composer-stack");
  if (!stack) return;
  const queue = document.getElementById("livecode-queue-bar");
  const status = document.getElementById("livecode-changes-bar");
  const tab = _livecodeGetActiveChatTab();
  const hasQueue = !!(queue && !queue.hidden);
  if (status) status.hidden = !_livecodeChangesFiles.length && !(tab && tab.agentRunning && hasQueue);
  const hasStatus = !!(status && !status.hidden);
  stack.hidden = !hasQueue && !hasStatus;
  stack.classList.toggle("has-queue", hasQueue);
  stack.classList.toggle("has-status", hasStatus);
  if (!hasQueue) _livecodeCloseQueueMenu();
}

function _livecodeReloadChangedFiles(result) {
  const r = result || {};
  (r.restored || []).forEach(function(path) { refreshLiveCodeFileFromDisk(path); });
  (r.removed || []).forEach(function(path) {
    const key = _livecodeResolveOpenFileKey(path);
    if (key) closeFile(key);
  });
  if ((r.restored || []).length || (r.removed || []).length) _livecodeRefreshIdeTree();
}

function _livecodeRunChangesAction(action, paths) {
  if (_livecodeChangesBusy) return;
  const activeTab = _livecodeGetActiveChatTab();
  if (activeTab && activeTab.agentRunning) {
    _livecodeShowIdeToast("Wait for the agent to finish, or stop it first.");
    return;
  }
  _livecodeChangesBusy = true;
  const bar = document.getElementById("livecode-changes-bar");
  if (bar) bar.classList.add("is-busy");
  const extra = paths && paths.length ? { paths: paths } : null;
  _livecodeSessionPost("/livecode/session/changes/" + action, extra).then(function(data) {
    if (action !== "undo") return;
    _livecodeReloadChangedFiles(data);
    const failed = data.failed || [];
    const done = (data.restored || []).length + (data.removed || []).length;
    if (failed.length) {
      return _livecodeShowErrorDialog(
        "Couldn't undo " + failed.length + " file" + (failed.length === 1 ? "" : "s"),
        done ? "The other " + done + " file" + (done === 1 ? " was" : "s were") + " put back." : "No files were changed.",
        failed.join("\n")
      );
    }
  }).catch(function(err) {
    return _livecodeShowErrorDialog(
      action === "undo" ? "Couldn't undo the changes" : "Couldn't keep the changes",
      (err && err.message) || "Unknown error.",
      (err && err.detail) || ""
    );
  }).finally(function() {
    _livecodeChangesBusy = false;
    if (bar) bar.classList.remove("is-busy");
    _livecodeRefreshPendingChanges();
  });
}

function _livecodeBindChangesBarOnce() {
  if (_livecodeChangesBarBound) return;
  const bar = document.getElementById("livecode-changes-bar");
  if (!bar) return;
  _livecodeChangesBarBound = true;
  bar.addEventListener("click", function(e) {
    const btn = e.target && e.target.closest ? e.target.closest("[data-changes-action]") : null;
    if (!btn || btn.disabled) return;
    e.preventDefault();
    const action = btn.getAttribute("data-changes-action");
    if (action === "toggle") {
      _livecodeSetChangesExpanded(bar, !bar.classList.contains("is-expanded"));
    } else if (action === "stop") {
      window.stopLiveCodeAgent();
    } else if (action === "open") {
      const path = btn.getAttribute("data-changes-path");
      if (path && !btn.classList.contains("is-deleted")) {
        showIDEPanel("editor");
        openFileInEditorFromPath(path);
      }
    } else if (action === "review") {
      _livecodeSetChangesExpanded(bar, true);
      const openable = _livecodeChangesFiles.filter(function(f) { return !f.deleted; }).slice(0, 12);
      if (openable.length) showIDEPanel("editor");
      openable.forEach(function(f) { openFileInEditorFromPath(f.path); });
    } else if (action === "undo" || action === "keep") {
      _livecodeRunChangesAction(action);
    } else if (action === "undo-file" || action === "keep-file") {
      const path = btn.getAttribute("data-changes-path");
      if (path) _livecodeRunChangesAction(action === "undo-file" ? "undo" : "keep", [path]);
    }
  });
}

function _livecodeNormalizePath(filePath) {
  if (!filePath) return "";
  let p = String(filePath).trim().replace(/\\/g, "/");
  p = p.replace(/\/+/g, "/");
  if (p.length > 1 && p.endsWith("/")) {
    p = p.slice(0, -1);
  }
  return p;
}

function _livecodeResolveOpenFileKey(absPath) {
  if (!absPath) return null;
  const normalized = _livecodeNormalizePath(absPath);
  if (ideOpenFiles[normalized]) return normalized;
  if (ideOpenFiles[absPath]) return absPath;
  const keys = Object.keys(ideOpenFiles);
  for (let i = 0; i < keys.length; i++) {
    const key = keys[i];
    if (_livecodeNormalizePath(key) === normalized) return key;
  }
  const lower = normalized.toLowerCase();
  for (let i = 0; i < keys.length; i++) {
    if (_livecodeNormalizePath(keys[i]).toLowerCase() === lower) return keys[i];
  }
  return null;
}

function _livecodeApplyFileContentToOpenTab(filePath, content) {
  if (!filePath || !ideOpenFiles[filePath]) return false;
  const fileInfo = ideOpenFiles[filePath];
  const nextContent = content != null ? content : "";
  fileInfo.content = nextContent;
  fileInfo.originalContent = nextContent;
  fileInfo.modified = false;
  if (fileInfo.model) {
    fileInfo.model.setValue(nextContent);
  }
  const codeEditor = document.getElementById("ide-code-editor");
  if (ideActiveFile === filePath && codeEditor && !fileInfo.model) {
    codeEditor.value = nextContent;
  }
  if (ideActiveFile === filePath && window.ideEditor && fileInfo.model) {
    try {
      window.ideEditor.setModel(fileInfo.model);
    } catch (e) {

    }
  }
  updateOpenFilesList();
  return true;
}

function refreshLiveCodeFileFromDisk(absPath) {
  if (!absPath) return;
  const openKey = _livecodeResolveOpenFileKey(absPath);
  if (!openKey) return;
  _livecodeIdeSocketRequest(
    "ide_read_file", { path: absPath },
    "ide_file_content",
    function(data) { return data && data.path === absPath; }
  ).then(function(result) {
    const data = result.data;
    if (!data.error && data.content !== undefined) {
      const resolvedPath = _livecodeResolveOpenFileKey(data.path || absPath) || openKey;
      _livecodeApplyFileContentToOpenTab(resolvedPath, data.content);
    }
  });
}

function _livecodeResetTurnState() {
  _livecodeStopThinkingTicker();
  _livecodeLastToolLabel = "";
  _livecodeLastTool = "";
  _livecodeLastToolArgs = {};
  _livecodeRunningActivityEl = null;
  _livecodeThinkingStartMs = null;
  _livecodePendingDurationS = null;
  _livecodePendingDurationMs = null;
  _livecodeCancelThoughtStreamFlush();
  _livecodePendingThoughtContent = "";
  _livecodeStreamingThoughtContentEl = null;
  _livecodeStatusRow = null;
  _livecodeStatusMsg = null;
  _livecodeCurrentUserRow = null;
  _livecodeAssistantStreamEl = null;
}

const _LIVECODE_MCP_ICON_SVG = _livecodeIcon("plug", { size: "lg" });

function _livecodeUpdateMcpToggle() {
  const toggle = document.getElementById("livecode-mcp-toggle");
  const panel = document.getElementById("livecode-mcp-panel");
  if (!toggle) return;
  const count = livecodeMcpSelectedServers.size;
  toggle.innerHTML = _LIVECODE_MCP_ICON_SVG + (count ? `<span class="livecode-mcp-toggle-badge">${count}</span>` : "");
  toggle.setAttribute("aria-label", count ? "MCP servers (" + count + " selected)" : "MCP servers");
  toggle.classList.toggle("is-active", count > 0);
  toggle.setAttribute("aria-expanded", panel && panel.style.display !== "none" ? "true" : "false");
}

function _livecodeMcpDiagnosticsText(server) {
  if (!server) return "No MCP server selected.";
  return JSON.stringify({
    name: server.name || "",
    connected: !!server.connected,
    transport: server.transport || "",
    source: server.source || "",
    config_path: server.config_path || "",
    error: server.error || "",
    diagnostics: server.diagnostics || {}
  }, null, 2);
}

function _livecodeMcpDisplayName(name) {
  return String(name || "");
}

function _livecodeApplyMcpStatusPayload(payload) {
  if (!payload || !Array.isArray(payload.servers)) return;
  livecodeMcpServers = payload.servers;
  const availableServers = new Set(livecodeMcpServers.map(function(server) { return server.name; }).filter(Boolean));
  let changed = false;
  Array.from(livecodeMcpSelectedServers).forEach(function(name) {
    if (!availableServers.has(name)) {
      livecodeMcpSelectedServers.delete(name);
      changed = true;
    }
  });
  if (changed) _livecodePersistMcpSelection();
}

function _livecodeFindMcpServer(name) {
  return livecodeMcpServers.find(function(server) {
    return server && server.name === name;
  }) || null;
}

function openLiveCodeMcpToolsDialog(serverName) {
  _livecodeSettingsMcpFocus = serverName || "";
  openLiveCodeSettings("mcp");
}
window.openLiveCodeMcpToolsDialog = openLiveCodeMcpToolsDialog;

async function runLiveCodeMcpAction(action, serverName) {
  const actionName = String(action || "").trim();
  const activeServerName = serverName || "";
  const server = _livecodeFindMcpServer(activeServerName);
  const mcpProjectPath = _livecodeWorkspaceRefForMcp();
  if (actionName === "copy-diagnostics") {
    _livecodeCopyToClipboard(_livecodeMcpDiagnosticsText(server), "Copied MCP diagnostics");
    return;
  }
  if (!mcpProjectPath) {
    _livecodeShowIdeToast("Open a project before managing MCP servers.");
    return;
  }
  try {
    const resp = await fetch("/livecode-mcp/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_path: mcpProjectPath, workspace: _livecodeCurrentWorkspacePayload(), action: actionName, server_name: activeServerName, enabled_servers: Array.from(livecodeMcpSelectedServers) })
    });
    const payload = await resp.json();
    if (payload.status) _livecodeApplyMcpStatusPayload(payload.status);
    if (payload.error) _livecodeShowIdeToast(payload.error);
    else _livecodeShowIdeToast(activeServerName ? "Reconnected " + _livecodeMcpDisplayName(activeServerName) : "MCP servers reloaded");
  } catch (err) {
    _livecodeShowIdeToast("MCP action failed: " + (err && err.message ? err.message : String(err)));
  }
  _livecodeRenderMcpServers();
}
window.runLiveCodeMcpAction = runLiveCodeMcpAction;

function _livecodeRenderMcpServers() {
  const list = document.getElementById("livecode-mcp-list");
  const filter = document.getElementById("livecode-mcp-filter");
  _livecodeRefreshSettingsMcpIfVisible();
  if (!list) return;
  _livecodeBindMcpPopoverOnce(list);
  const query = (filter && filter.value ? filter.value : "").toLowerCase();
  const servers = livecodeMcpServers.filter(function(server) {
    if (!query) return true;
    const tools = Array.isArray(server.tools) ? server.tools : [];
    return String(server.name || "").toLowerCase().indexOf(query) !== -1 ||
      String(server.source || "").toLowerCase().indexOf(query) !== -1 ||
      tools.some(function(tool) {
        return String(tool.name || "").toLowerCase().indexOf(query) !== -1 || String(tool.description || "").toLowerCase().indexOf(query) !== -1;
      });
  });
  if (!servers.length) {
    list.innerHTML = '<div class="livecode-mcp-empty">' + (query ? "No servers or tools match." : "No MCP servers yet.") + "</div>";
    _livecodeUpdateMcpToggle();
    return;
  }
  const disabledTools = _livecodeMcpDisabledTools();
  list.innerHTML = servers.map(function(server) {
    const name = String(server.name || "");
    const esc = _livecodeEscapeHtml(name);
    const state = _livecodeMcpServerState(server);
    const on = livecodeMcpSelectedServers.has(name);
    const offCount = ((disabledTools[name]) || []).length;
    let meta = _livecodeMcpStateLabel(server, state);
    if (offCount && state === "ok") meta += " · " + offCount + " off";
    return '<div class="livecode-mcp-pop-row' + (on ? " is-on" : "") + (server.config_disabled ? " is-disabled" : "") + '" data-mcp-row="' + esc + '" role="button" tabindex="0" title="' + _livecodeEscapeHtml(server.error || _livecodeMcpSourceLabel(server)) + '">' +
      '<span class="livecode-mcp-dot is-' + state + '" aria-hidden="true"></span>' +
      '<span class="livecode-mcp-pop-name">' + _livecodeEscapeHtml(_livecodeMcpDisplayName(name)) + "</span>" +
      '<span class="livecode-mcp-pop-meta">' + _livecodeEscapeHtml(meta) + "</span>" +
      '<label class="lc-switch is-small" title="' + (on ? "Turn off" : "Turn on") + '"><input type="checkbox" data-mcp-toggle="' + esc + '"' + (on ? " checked" : "") + (server.config_disabled ? " disabled" : "") + ' aria-label="Use ' + esc + '"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label>' +
    "</div>";
  }).join("");
  _livecodeUpdateMcpToggle();
}
window._livecodeRenderMcpServers = _livecodeRenderMcpServers;

function _livecodeBindMcpPopoverOnce(list) {
  if (list._livecodeMcpBound) return;
  list._livecodeMcpBound = true;
  list.addEventListener("change", function(e) {
    const name = e.target && e.target.getAttribute ? e.target.getAttribute("data-mcp-toggle") : "";
    if (name) _livecodeSetMcpServerEnabled(name, !!e.target.checked);
  });
  list.addEventListener("click", function(e) {
    if (e.target.closest && e.target.closest(".lc-switch")) return;
    const row = e.target.closest ? e.target.closest("[data-mcp-row]") : null;
    if (!row || row.classList.contains("is-disabled")) return;
    e.preventDefault();
    const name = row.getAttribute("data-mcp-row");
    _livecodeSetMcpServerEnabled(name, !livecodeMcpSelectedServers.has(name));
  });
  list.addEventListener("keydown", function(e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest ? e.target.closest("[data-mcp-row]") : null;
    if (!row || row.classList.contains("is-disabled")) return;
    e.preventDefault();
    const name = row.getAttribute("data-mcp-row");
    _livecodeSetMcpServerEnabled(name, !livecodeMcpSelectedServers.has(name));
  });
}

function toggleLiveCodeMcpPanel(forceOpen) {
  const panel = document.getElementById("livecode-mcp-panel");
  if (!panel) return;
  const shouldOpen = typeof forceOpen === "boolean" ? forceOpen : panel.style.display === "none";
  panel.style.display = shouldOpen ? "flex" : "none";
  _livecodeUpdateMcpToggle();
  if (shouldOpen) {
    if (!livecodeMcpServers.length) refreshLiveCodeMcpStatus();
    else _livecodeRenderMcpServers();
  }
}
window.toggleLiveCodeMcpPanel = toggleLiveCodeMcpPanel;
if (!window._livecodeMcpPanelDismissBound) {
  window._livecodeMcpPanelDismissBound = true;
  document.addEventListener("click", function(e) {
    const panel = document.getElementById("livecode-mcp-panel");
    const toggle = document.getElementById("livecode-mcp-toggle");
    if (!panel || panel.style.display === "none") return;
    const path = typeof e.composedPath === "function" ? e.composedPath() : [];
    if (path.indexOf(panel) !== -1 || (toggle && path.indexOf(toggle) !== -1)) return;
    if ((panel.contains(e.target)) || (toggle && toggle.contains(e.target))) return;
    toggleLiveCodeMcpPanel(false);
  });
  document.addEventListener("keydown", function(e) {
    if (e.key !== "Escape") return;
    const panel = document.getElementById("livecode-mcp-panel");
    if (panel && panel.style.display !== "none") toggleLiveCodeMcpPanel(false);
  });
}

async function refreshLiveCodeMcpStatus(options) {
  const opts = options || {};
  const list = document.getElementById("livecode-mcp-list");
  const requestSeq = ++_livecodeMcpStatusRequestSeq;
  const workspaceKey = _livecodeMcpSelectionStorageKey();
  const mcpProjectPath = _livecodeWorkspaceRefForMcp();
  const hasExplicitProbeList = Array.isArray(opts.probeServers);
  const probeServers = hasExplicitProbeList ? opts.probeServers : (_livecodeMcpStatusLoadedWithoutProbe ? Array.from(livecodeMcpSelectedServers || []) : []);
  if (!mcpProjectPath) {
    if (list) list.innerHTML = '<div class="livecode-mcp-empty">Open a project to load MCP servers.</div>';
    _livecodeUpdateMcpToggle();
    return;
  }
  if (list && (!opts.preserveList || !livecodeMcpServers.length)) list.innerHTML = '<div class="livecode-mcp-empty">Loading MCP servers...</div>';
  try {
    const resp = await fetch("/livecode-mcp/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_path: mcpProjectPath, workspace: _livecodeCurrentWorkspacePayload(), enabled_servers: probeServers })
    });
    const payload = await resp.json();
    if (requestSeq !== _livecodeMcpStatusRequestSeq) return;
    if (workspaceKey !== _livecodeMcpSelectionStorageKey()) return;
    _livecodeApplyMcpStatusPayload(payload);
    if (!probeServers.length) _livecodeMcpStatusLoadedWithoutProbe = true;
  } catch (err) {
    if (requestSeq !== _livecodeMcpStatusRequestSeq) return;
    if (workspaceKey !== _livecodeMcpSelectionStorageKey()) return;
    livecodeMcpServers = [];
    _livecodeShowIdeToast("Could not load MCP servers: " + String(err));
  }
  _livecodeRenderMcpServers();
  if (opts.hydrateSelected && !probeServers.length) {
    const selectedServers = Array.from(livecodeMcpSelectedServers || []).filter(Boolean);
    if (selectedServers.length) {
      const scheduleHydration = typeof requestIdleCallback === "function"
        ? requestIdleCallback
        : function(cb) { setTimeout(cb, 1); };
      scheduleHydration(function() {
        if (requestSeq !== _livecodeMcpStatusRequestSeq) return;
        if (workspaceKey !== _livecodeMcpSelectionStorageKey()) return;
        refreshLiveCodeMcpStatus({ probeServers: selectedServers, preserveList: true });
      });
    }
  }
}
window.refreshLiveCodeMcpStatus = refreshLiveCodeMcpStatus;

async function refreshLiveCodeMcpConnections() {
  const mcpProjectPath = _livecodeWorkspaceRefForMcp();
  if (mcpProjectPath) {
    try {
      await fetch("/livecode-mcp/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_path: mcpProjectPath, workspace: _livecodeCurrentWorkspacePayload(), enabled_servers: Array.from(livecodeMcpSelectedServers || []) }) });
    } catch (err) {}
  }
  refreshLiveCodeMcpStatus();
}
window.refreshLiveCodeMcpConnections = refreshLiveCodeMcpConnections;

async function openLiveCodeMcpConfig() {
  if (!livecodeProjectPath) {
    _livecodeShowIdeToast("Open a project before editing MCP config.");
    return;
  }
  try {
    const resp = await fetch("/livecode-mcp/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_path: livecodeProjectPath }) });
    const payload = await resp.json();
    if (!resp.ok || !payload.path) throw new Error(payload.error || "Unable to create MCP config");
    openFileInEditorFromPath(payload.path);
  } catch (err) {
    _livecodeShowIdeToast("Couldn't open MCP config: " + (err && err.message ? err.message : String(err)));
  }
}
window.openLiveCodeMcpConfig = openLiveCodeMcpConfig;

function _livecodeInitChatInput(inputEl) {
  if (typeof window.getLivecodeComposerState === "function") {
    const state = window.getLivecodeComposerState();
    const hasAttachments = (state.attachments || []).length > 0;
    const hasText = !!(state.text || "").trim() || (state.segments || []).some(function(s) {
      return s && s.type === "file";
    });
    if (!hasText && !hasAttachments) return;
    if (typeof window.areLivecodeAttachmentsReady === "function" && hasAttachments && !window.areLivecodeAttachmentsReady()) return;
    window.sendLiveCodeAgentMessage(undefined);
    return;
  }
  if (!inputEl) return;
  const msg = String(inputEl.value != null ? inputEl.value : (inputEl.innerText || "")).trim();
  const hasAttachments = (window.livecodePendingAttachments || []).length > 0;
  if (!msg && !hasAttachments) return;
  if (typeof window.areLivecodeAttachmentsReady === "function" && hasAttachments && !window.areLivecodeAttachmentsReady()) return;
  window.sendLiveCodeAgentMessage(msg || undefined);
}
window._livecodeInitChatInput = _livecodeInitChatInput;


const LIVECODE_SETTINGS_STORAGE_KEY = "livecode_settings_v1";
const _LIVECODE_SETTING_DEFAULTS = {
  autoRunQueue: true,
  requireApproval: false,
  webTools: false,
  browserTools: true,
  conversationDensity: "detailed",
  stepGrouping: "grouped",
};

function _livecodeSettingsAll() {
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(LIVECODE_SETTINGS_STORAGE_KEY) || "{}") || {}; } catch (e) { stored = {}; }
  return Object.assign({}, _LIVECODE_SETTING_DEFAULTS, stored);
}

function _livecodeSettingsGet(key) {
  return _livecodeSettingsAll()[key];
}

function _livecodeSettingsSet(key, value) {
  const all = _livecodeSettingsAll();
  all[key] = value;
  try { localStorage.setItem(LIVECODE_SETTINGS_STORAGE_KEY, JSON.stringify(all)); } catch (e) {}
}


function _livecodeComposerPlainText() {
  const input = document.getElementById("livecode-chat-input");
  if (!input) return "";
  return String(input.value != null ? input.value : (input.innerText || "")).trim();
}

function _livecodeClearComposer() {
  if (typeof window.clearLivecodeComposer === "function") {
    window.clearLivecodeComposer();
  } else {
    const input = document.getElementById("livecode-chat-input");
    if (input) {
      if (input.value != null) {
        input.value = "";
        input.style.height = "auto";
      } else {
        input.textContent = "";
        input.classList.add("is-empty");
      }
    }
  }
  _livecodeSyncComposerDraftState();
}

function _livecodeComposerHasDraft() {
  if (typeof window.getLivecodeComposerState === "function") {
    try {
      const state = window.getLivecodeComposerState() || {};
      return !!String(state.text || "").trim() || (state.attachments || []).length > 0 ||
        (state.segments || []).some(function(s) { return s && s.type === "file"; });
    } catch (e) {}
  }
  return !!_livecodeComposerPlainText();
}

function _livecodeSyncComposerDraftState() {
  const tab = _livecodeGetActiveChatTab();
  const busy = !!(tab && tab.agentRunning);
  const hasDraft = busy && _livecodeComposerHasDraft();
  document.querySelectorAll('.chatbot-composer[data-sidebar-section="livecode"]').forEach(function(el) {
    el.classList.toggle("has-queue-draft", hasDraft);
    const send = el.querySelector(".chatbot-composer-send-btn");
    if (!send) return;
    send.style.display = busy && !hasDraft ? "none" : "";
    send.title = busy ? "Queue message (Enter) · Send now (⌘/Ctrl+Enter)" : "Send";
    send.setAttribute("aria-label", busy ? "Queue message" : "Send");
  });
}

function focusLiveCodeComposer() {
  const input = document.getElementById("livecode-chat-input");
  if (!input) return;
  toggleLiveCodeAgentPane(true);
  input.focus();
  if (input.isContentEditable && window.getSelection) {
    const range = document.createRange();
    range.selectNodeContents(input);
    range.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
}
window.focusLiveCodeComposer = focusLiveCodeComposer;


const LIVECODE_QUEUE_STORAGE_PREFIX = "livecode_queue_v1:";

function _livecodeTabQueue(tab) {
  if (!tab) return [];
  if (!Array.isArray(tab.queuedMessages)) tab.queuedMessages = _livecodeLoadQueue(tab);
  return tab.queuedMessages;
}

function _livecodeQueueStorageKey(tab) {
  const project = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(livecodeProjectPath);
  if (!project || !tab || !tab.sessionId) return "";
  return LIVECODE_QUEUE_STORAGE_PREFIX + project + ":" + tab.sessionId;
}

function _livecodeLoadQueue(tab) {
  const key = _livecodeQueueStorageKey(tab);
  if (!key) return [];
  try {
    const items = JSON.parse(_livecodeStorageGet(key) || "[]");
    if (!Array.isArray(items)) return [];
    const restored = items.filter(function(item) { return item && item.id && (item.text || "").trim(); });
    if (restored.length) tab.queuePaused = true;
    return restored;
  } catch (e) {
    return [];
  }
}

function _livecodePersistQueue(tab) {
  const key = _livecodeQueueStorageKey(tab);
  if (!key) return;
  const items = _livecodeTabQueue(tab)
    .filter(function(item) { return !(item.attachments || []).length; })
    .map(function(item) {
      return { id: item.id, text: item.text, displayPayload: item.displayPayload, mode: item.mode, options: item.options, createdAt: item.createdAt };
    });
  if (items.length) _livecodeStorageSet(key, JSON.stringify(items));
  else _livecodeStorageRemove(key);
}

function _livecodeNewQueueId() {
  return "q_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

function _livecodeCaptureComposerMessage(presetMessage, options) {
  const composerState = typeof window.getLivecodeComposerState === "function"
    ? window.getLivecodeComposerState()
    : { text: _livecodeComposerPlainText(), segments: [], attachments: [] };
  const attachments = typeof window.getLivecodeApiAttachments === "function"
    ? window.getLivecodeApiAttachments()
    : (composerState.attachments || []).slice();
  if (typeof window.areLivecodeAttachmentsReady === "function" && attachments.length && !window.areLivecodeAttachmentsReady()) {
    _livecodeShowIdeToast("Wait for the attachments to finish loading.");
    return null;
  }
  const text = String(presetMessage != null ? presetMessage : (composerState.text || "")).trim();
  const hasInlineFiles = (composerState.segments || []).some(function(s) { return s && s.type === "file"; });
  if (!text && !attachments.length && !hasInlineFiles) return null;
  const displayPayload = typeof window.serializeLivecodeDisplayPayload === "function"
    ? window.serializeLivecodeDisplayPayload(composerState)
    : { text: text, segments: [], attachments: composerState.attachments || [] };
  if (!displayPayload.text && text) displayPayload.text = text;
  const opts = options || {};
  return {
    id: _livecodeNewQueueId(),
    text: text,
    composerState: composerState,
    displayPayload: displayPayload,
    attachments: attachments,
    mode: opts.mode || window.livecodeChatMode || "agent",
    options: { planFile: opts.planFile || "", planTitle: opts.planTitle || "" },
    createdAt: Date.now(),
  };
}

function _livecodeQueueFromComposer(tab, presetMessage, options) {
  const item = _livecodeCaptureComposerMessage(presetMessage, options);
  if (!item) return null;
  _livecodeTabQueue(tab).push(item);
  tab.queueCollapsed = false;
  _livecodeClearComposer();
  _livecodePersistQueue(tab);
  _livecodeRenderQueueBar();
  return item;
}

const _LIVECODE_QUEUE_ICONS = {
  send: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13.25V3"></path><path d="M3.75 7.25 8 3l4.25 4.25"></path></svg>',
  trash: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.75 4.25h10.5"></path><path d="M6 4.25V2.75h4v1.5"></path><path d="M4 4.25l.7 8.6a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9l.7-8.6"></path><path d="M6.6 6.75v4.5M9.4 6.75v4.5"></path></svg>',
  remove: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>',
  chevron: '<svg class="livecode-queue-chevron" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="5.1 6.6 8 9.5 10.9 6.6"></polyline></svg>',
  more: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3.5" cy="8" r="1.25"></circle><circle cx="8" cy="8" r="1.25"></circle><circle cx="12.5" cy="8" r="1.25"></circle></svg>',
  link: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" aria-hidden="true"><path d="M6.79 9.25H7.25A3 3 0 0 0 7.25 3.25H4.75A3 3 0 0 0 4.75 9.25"></path><path d="M9.21 6.75H8.75A3 3 0 0 0 8.75 12.75H11.25A3 3 0 0 0 11.25 6.75"></path></svg>',
  folder: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M1.75 4.25a1 1 0 0 1 1-1h3l1.5 1.5h6a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1z"></path></svg>',
  clip: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>',
  check: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"></path></svg>',
};

const _LIVECODE_QUEUE_URL_RE = /\bhttps?:\/\/[^\s<>"'`]*[^\s<>"'`.,;:!?)\]}]/gi;

function _livecodeQueueChipHtml(kind, label, title) {
  let icon = _LIVECODE_QUEUE_ICONS[kind === "folder" ? "folder" : "link"];
  if (kind === "file") {
    icon = '<img class="livecode-queue-chip-icon" src="' + _livecodeEscapeHtml(_livecodeFileIcon(label)).replace(/"/g, "&quot;") + '" alt="">';
  }
  return '<span class="livecode-queue-chip is-' + kind + '" title="' + _livecodeEscapeHtml(title || label).replace(/"/g, "&quot;") + '">' +
    icon + '<span class="livecode-queue-chip-label">' + _livecodeEscapeHtml(label) + "</span></span>";
}

function _livecodeQueueLinkifiedHtml(text) {
  const value = String(text || "");
  let html = "";
  let last = 0;
  value.replace(_LIVECODE_QUEUE_URL_RE, function(url, offset) {
    html += _livecodeEscapeHtml(value.slice(last, offset));
    html += _livecodeQueueChipHtml("link", url.replace(/^https?:\/\//i, "").replace(/\/$/, ""), url);
    last = offset + url.length;
    return url;
  });
  return html + _livecodeEscapeHtml(value.slice(last));
}

function _livecodeQueueTextHtml(item) {
  const payload = item.displayPayload || {};
  const segments = Array.isArray(payload.segments) && payload.segments.length
    ? payload.segments
    : ((item.composerState && item.composerState.segments) || []);
  let html = "";
  if (segments.some(function(s) { return s && (s.type === "file" || s.type === "folder"); })) {
    html = segments.map(function(s) {
      if (!s) return "";
      if (s.type === "file" || s.type === "folder") {
        const path = String(s.path || s.value || s.name || "");
        const name = String(s.name || s.label || _livecodeBasename(path) || path);
        const isFolder = s.type === "folder" || !!(s.isDir || s.is_dir) || s.kind === "folder";
        return _livecodeQueueChipHtml(isFolder ? "folder" : "file", name, path);
      }
      return _livecodeQueueLinkifiedHtml(s.text != null ? s.text : (s.value || ""));
    }).join("");
  }
  if (!html.trim()) html = _livecodeQueueLinkifiedHtml(String(item.text || "").trim());
  if (!html.trim()) {
    const count = (item.attachments || []).length;
    html = '<span class="livecode-queue-muted">' + count + " attachment" + (count === 1 ? "" : "s") + "</span>";
  }
  return html;
}

function _livecodeRenderQueueBar() {
  const bar = document.getElementById("livecode-queue-bar");
  if (!bar) return;
  _livecodeBindQueueBarOnce(bar);
  const tab = _livecodeGetActiveChatTab();
  const queue = _livecodeTabQueue(tab);
  if (!queue.length) {
    bar.hidden = true;
    bar.innerHTML = "";
    _livecodeSyncComposerStack();
    return;
  }
  const running = !!(tab && tab.agentRunning);
  const paused = !!(tab && tab.queuePaused);
  const collapsed = !!(tab && tab.queueCollapsed);
  const auto = !!_livecodeSettingsGet("autoRunQueue");
  const waiting = !running && !paused && !auto;
  const note = paused ? "Paused" : (waiting ? "Waiting for you" : "");
  bar.hidden = false;
  bar.classList.toggle("is-collapsed", collapsed);
  bar.classList.toggle("is-paused", paused);
  const head =
    '<div class="livecode-queue-head">' +
      '<button type="button" class="livecode-queue-toggle lc-btn" data-queue-action="toggle" aria-expanded="' + (collapsed ? "false" : "true") + '">' +
        _LIVECODE_QUEUE_ICONS.chevron + '<span class="livecode-queue-count">' + queue.length + " Queued</span>" +
        (note ? '<span class="livecode-queue-note">· ' + note + "</span>" : "") +
      "</button>" +
      (paused || waiting ? '<button type="button" class="livecode-queue-head-btn lc-btn" data-queue-action="resume">' + (paused ? "Resume" : "Send next") + "</button>" : "") +
      '<button type="button" class="livecode-queue-more lc-btn" data-queue-action="menu" title="Queue options" aria-label="Queue options" aria-haspopup="menu" aria-expanded="false">' + _LIVECODE_QUEUE_ICONS.more + "</button>" +
    "</div>";
  const items = queue.map(function(item) {
    const mode = item.mode && item.mode !== "agent" ? '<span class="livecode-queue-mode">' + _livecodeEscapeHtml(item.mode.charAt(0).toUpperCase() + item.mode.slice(1)) + "</span>" : "";
    const attached = (item.attachments || []).length;
    const clips = attached && String(item.text || "").trim() ? '<span class="livecode-queue-clip" title="' + attached + " attachment" + (attached === 1 ? "" : "s") + '">' + _LIVECODE_QUEUE_ICONS.clip + attached + "</span>" : "";
    return '<div class="livecode-queue-item" role="listitem" draggable="true" data-queue-id="' + _livecodeEscapeHtml(item.id) + '">' +
      '<span class="livecode-queue-dot" title="Drag to reorder" aria-hidden="true"></span>' +
      '<span class="livecode-queue-text" data-queue-action="edit" role="button" tabindex="0" title="Edit in the composer">' + _livecodeQueueTextHtml(item) + mode + clips + "</span>" +
      '<span class="livecode-queue-actions">' +
        '<button type="button" class="livecode-queue-btn lc-btn" data-queue-action="send" title="Send now' + (running ? " (stops the current turn)" : "") + '" aria-label="Send now">' + _LIVECODE_QUEUE_ICONS.send + "</button>" +
        '<button type="button" class="livecode-queue-btn lc-btn" data-queue-action="remove" title="Remove from the queue" aria-label="Remove">' + _LIVECODE_QUEUE_ICONS.trash + "</button>" +
      "</span>" +
    "</div>";
  }).join("");
  bar.innerHTML = head + '<div class="livecode-queue-list" role="list" aria-label="Queued messages">' + items + "</div>";
  _livecodeSyncComposerStack();
}

function _livecodeCloseQueueMenu() {
  const menu = document.getElementById("livecode-queue-menu");
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  const more = document.querySelector('#livecode-queue-bar [data-queue-action="menu"]');
  if (more) more.setAttribute("aria-expanded", "false");
}

function _livecodeToggleQueueMenu(anchor) {
  const stack = document.getElementById("livecode-composer-stack");
  const tab = _livecodeGetActiveChatTab();
  if (!stack || !tab) return;
  let menu = document.getElementById("livecode-queue-menu");
  if (menu && !menu.hidden) {
    _livecodeCloseQueueMenu();
    return;
  }
  if (!menu) {
    menu = document.createElement("div");
    menu.id = "livecode-queue-menu";
    menu.className = "livecode-queue-menu theme-transition";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    stack.appendChild(menu);
    menu.addEventListener("click", function(e) {
      const btn = e.target.closest ? e.target.closest("[data-queue-menu]") : null;
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      _livecodeRunQueueMenuAction(btn.getAttribute("data-queue-menu"));
    });
    document.addEventListener("mousedown", function(e) {
      const open = document.getElementById("livecode-queue-menu");
      if (!open || open.hidden) return;
      if (open.contains(e.target) || (e.target.closest && e.target.closest('[data-queue-action="menu"]'))) return;
      _livecodeCloseQueueMenu();
    });
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") _livecodeCloseQueueMenu();
    });
  }
  const paused = !!tab.queuePaused;
  const auto = !!_livecodeSettingsGet("autoRunQueue");
  menu.innerHTML =
    '<button type="button" class="livecode-queue-menu-item lc-btn" role="menuitem" data-queue-menu="send-next">Send next now</button>' +
    '<button type="button" class="livecode-queue-menu-item lc-btn" role="menuitem" data-queue-menu="' + (paused ? "resume" : "pause") + '">' + (paused ? "Resume queue" : "Pause queue") + "</button>" +
    '<button type="button" class="livecode-queue-menu-item lc-btn" role="menuitemcheckbox" aria-checked="' + auto + '" data-queue-menu="auto">' +
      "<span>Run automatically</span>" + (auto ? _LIVECODE_QUEUE_ICONS.check : "") + "</button>" +
    '<div class="livecode-queue-menu-sep" role="separator"></div>' +
    '<button type="button" class="livecode-queue-menu-item lc-btn is-danger" role="menuitem" data-queue-menu="clear">Clear queue</button>';
  const stackRect = stack.getBoundingClientRect();
  const anchorRect = anchor.getBoundingClientRect();
  menu.style.top = Math.round(anchorRect.bottom - stackRect.top + 4) + "px";
  menu.style.right = Math.max(4, Math.round(stackRect.right - anchorRect.right)) + "px";
  menu.hidden = false;
  anchor.setAttribute("aria-expanded", "true");
  const first = menu.querySelector("button");
  if (first) first.focus();
}

function _livecodeRunQueueMenuAction(action) {
  const tab = _livecodeGetActiveChatTab();
  _livecodeCloseQueueMenu();
  if (!tab) return;
  const queue = _livecodeTabQueue(tab);
  if (action === "send-next" && queue.length) {
    _livecodeSendQueuedNow(tab, queue[0].id);
  } else if (action === "pause") {
    tab.queuePaused = true;
    _livecodeRenderQueueBar();
  } else if (action === "resume") {
    tab.queuePaused = false;
    _livecodeRenderQueueBar();
    _livecodeMaybeRunQueue(tab);
  } else if (action === "auto") {
    const auto = !_livecodeSettingsGet("autoRunQueue");
    _livecodeSettingsSet("autoRunQueue", auto);
    _livecodeRenderQueueBar();
    if (_livecodeSettingsVisible("agent")) _livecodeRenderSettingsPage();
    if (auto && !tab.queuePaused) _livecodeMaybeRunQueue(tab);
  } else if (action === "clear") {
    tab.queuedMessages = [];
    tab.queuePaused = false;
    _livecodePersistQueue(tab);
    _livecodeRenderQueueBar();
  }
}

function _livecodeQueueItemIndex(tab, id) {
  return _livecodeTabQueue(tab).findIndex(function(item) { return item.id === id; });
}

function _livecodeEditQueuedMessage(tab, id) {
  const queue = _livecodeTabQueue(tab);
  const index = _livecodeQueueItemIndex(tab, id);
  if (index < 0) return;
  const item = queue[index];
  const draft = _livecodeComposerHasDraft() ? _livecodeCaptureComposerMessage() : null;
  if (draft) queue.splice(index, 1, draft);
  else queue.splice(index, 1);
  _livecodeSetComposerText(item.text || "");
  _livecodePersistQueue(tab);
  _livecodeRenderQueueBar();
  _livecodeSyncComposerDraftState();
  focusLiveCodeComposer();
}

function _livecodeSendQueuedNow(tab, id) {
  const queue = _livecodeTabQueue(tab);
  const index = _livecodeQueueItemIndex(tab, id);
  if (index < 0 || !tab) return;
  const item = queue.splice(index, 1)[0];
  tab.queuePaused = false;
  _livecodePersistQueue(tab);
  _livecodeRenderQueueBar();
  const run = function() {
    if (!_livecodeIsActiveTab(tab)) _livecodeSwitchChatTab(tab.id);
    window.sendLiveCodeAgentMessage(item.text, Object.assign({}, item.options || {}, { mode: item.mode, queuedItem: item }));
  };
  if (!tab.agentRunning) {
    run();
    return;
  }
  const done = tab._turnDone || Promise.resolve();
  tab._suppressQueueOnce = true;
  _livecodeAbortTabTurn(tab);
  _livecodeClearQuestionRequest(tab);
  tab.buildingPlanFile = "";
  _livecodeSyncPlanCardButtons();
  _setLiveCodeChatBusy(false);
  livecodeAgentRunning = false;
  done.then(function() { setTimeout(run, 0); });
}

function _livecodeMaybeRunQueue(tab) {
  if (!tab || tab.agentRunning || tab.queuePaused) return;
  const queue = _livecodeTabQueue(tab);
  if (!queue.length) return;
  if (!_livecodeIsActiveTab(tab)) {
    tab.queueWaiting = true;
    return;
  }
  tab.queueWaiting = false;
  _livecodeSendQueuedNow(tab, queue[0].id);
}

function _livecodeAfterTurnQueue(tab, failed) {
  if (!tab) return;
  if (tab._suppressQueueOnce) {
    tab._suppressQueueOnce = false;
    _livecodeRenderQueueBar();
    return;
  }
  const queue = _livecodeTabQueue(tab);
  if (failed && queue.length) tab.queuePaused = true;
  _livecodeRenderQueueBar();
  _livecodeSyncComposerDraftState();
  if (!queue.length || tab.queuePaused || !_livecodeSettingsGet("autoRunQueue")) return;
  setTimeout(function() { _livecodeMaybeRunQueue(tab); }, 250);
}

let _livecodeQueueDragId = "";

function _livecodeBindQueueBarOnce(bar) {
  if (!bar || bar._livecodeQueueBound) return;
  bar._livecodeQueueBound = true;
  bar.addEventListener("click", function(e) {
    const btn = e.target && e.target.closest ? e.target.closest("[data-queue-action]") : null;
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const tab = _livecodeGetActiveChatTab();
    if (!tab) return;
    const action = btn.getAttribute("data-queue-action");
    const itemEl = btn.closest(".livecode-queue-item");
    const id = itemEl ? itemEl.getAttribute("data-queue-id") : "";
    if (action === "toggle") {
      tab.queueCollapsed = !tab.queueCollapsed;
      _livecodeRenderQueueBar();
    } else if (action === "menu") {
      _livecodeToggleQueueMenu(btn);
    } else if (action === "resume") {
      tab.queuePaused = false;
      _livecodeRenderQueueBar();
      _livecodeMaybeRunQueue(tab);
    } else if (action === "send" && id) {
      _livecodeSendQueuedNow(tab, id);
    } else if (action === "edit" && id) {
      _livecodeEditQueuedMessage(tab, id);
    } else if (action === "remove" && id) {
      const index = _livecodeQueueItemIndex(tab, id);
      if (index >= 0) _livecodeTabQueue(tab).splice(index, 1);
      if (!_livecodeTabQueue(tab).length) tab.queuePaused = false;
      _livecodePersistQueue(tab);
      _livecodeRenderQueueBar();
    }
  });
  bar.addEventListener("keydown", function(e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    const text = e.target && e.target.closest ? e.target.closest('.livecode-queue-text[data-queue-action="edit"]') : null;
    const itemEl = text && text.closest(".livecode-queue-item");
    const tab = _livecodeGetActiveChatTab();
    if (!itemEl || !tab) return;
    e.preventDefault();
    _livecodeEditQueuedMessage(tab, itemEl.getAttribute("data-queue-id"));
  });
  bar.addEventListener("dragstart", function(e) {
    const itemEl = e.target && e.target.closest ? e.target.closest(".livecode-queue-item") : null;
    if (!itemEl) return;
    _livecodeQueueDragId = itemEl.getAttribute("data-queue-id") || "";
    itemEl.classList.add("is-dragging");
    try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", _livecodeQueueDragId); } catch (err) {}
  });
  bar.addEventListener("dragover", function(e) {
    if (!_livecodeQueueDragId) return;
    const itemEl = e.target && e.target.closest ? e.target.closest(".livecode-queue-item") : null;
    e.preventDefault();
    bar.querySelectorAll(".livecode-queue-item").forEach(function(el) { el.classList.remove("drop-before", "drop-after"); });
    if (!itemEl || itemEl.getAttribute("data-queue-id") === _livecodeQueueDragId) return;
    const rect = itemEl.getBoundingClientRect();
    itemEl.classList.add(e.clientY < rect.top + rect.height / 2 ? "drop-before" : "drop-after");
  });
  bar.addEventListener("drop", function(e) {
    if (!_livecodeQueueDragId) return;
    e.preventDefault();
    const tab = _livecodeGetActiveChatTab();
    const target = bar.querySelector(".livecode-queue-item.drop-before, .livecode-queue-item.drop-after");
    if (tab && target) {
      const queue = _livecodeTabQueue(tab);
      const from = _livecodeQueueItemIndex(tab, _livecodeQueueDragId);
      if (from >= 0) {
        const moved = queue.splice(from, 1)[0];
        let to = _livecodeQueueItemIndex(tab, target.getAttribute("data-queue-id"));
        if (target.classList.contains("drop-after")) to += 1;
        queue.splice(Math.max(0, to), 0, moved);
        _livecodePersistQueue(tab);
      }
    }
    _livecodeQueueDragId = "";
    _livecodeRenderQueueBar();
  });
  bar.addEventListener("dragend", function() {
    _livecodeQueueDragId = "";
    bar.querySelectorAll(".livecode-queue-item").forEach(function(el) { el.classList.remove("is-dragging", "drop-before", "drop-after"); });
  });
}

function _livecodeComposerMenuOpen() {
  return ["livecode-mention-menu", "livecode-slash-menu"].some(function(id) {
    const menu = document.getElementById(id);
    return !!(menu && menu.style.display !== "none" && menu.offsetParent !== null);
  });
}

function _livecodeBindQueueKeysOnce() {
  const composer = document.getElementById("livecode-chat-composer");
  if (!composer || composer._livecodeQueueKeysBound) return;
  composer._livecodeQueueKeysBound = true;
  composer.addEventListener("keydown", function(e) {
    if (e.key !== "Enter" || e.isComposing || e.shiftKey || e.altKey) return;
    if (!e.target || e.target.id !== "livecode-chat-input") return;
    const tab = _livecodeGetActiveChatTab();
    if (!tab || !tab.agentRunning || tab.pendingQuestion || _livecodeComposerMenuOpen()) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const item = _livecodeQueueFromComposer(tab);
    if (item && (e.metaKey || e.ctrlKey)) {
      const queue = _livecodeTabQueue(tab);
      queue.splice(queue.indexOf(item), 1);
      queue.unshift(item);
      _livecodeSendQueuedNow(tab, item.id);
    }
  }, true);
  composer.addEventListener("input", _livecodeSyncComposerDraftState);
}

window.stopLiveCodeAgent = function() {
  const tab = _livecodeGetActiveChatTab();
  if (tab) {
    if (tab.agentRunning && _livecodeTabQueue(tab).length) tab.queuePaused = true;
    _livecodeAbortTabTurn(tab);
    _livecodeClearQuestionRequest(tab);
    tab.buildingPlanFile = "";
    _livecodeSyncPlanCardButtons();
  }
  _setLiveCodeChatBusy(false);
  livecodeAgentRunning = false;
  renderLiveCodeRecentSessions();
};

window.sendLiveCodeAgentMessage = async function(presetMessage, options) {
  const tab = _livecodeGetActiveChatTab();
  if (tab && tab.pendingQuestion) {
    _livecodeSubmitQuestions(false);
    return;
  }
  const turnOptions = options || {};
  const queued = turnOptions.queuedItem || null;
  if (tab && tab.agentRunning && !queued) {
    _livecodeQueueFromComposer(tab, presetMessage, turnOptions);
    return;
  }
  if (!tab || tab.agentRunning) return;
  if (_livecodeLlmSettings && !(_livecodeLlmSettings.providers || []).some(function(p) { return p.configured; })) {
    _livecodeModalConfirm({
      title: "Add an API key to start chatting",
      message: "LiveCode needs a model provider. Add a key for Gemini, OpenAI, Anthropic or another provider in Settings > Models.",
      confirmText: "Open Models settings",
    }).then(function(ok) { if (ok) window.openLiveCodeSettings("models"); });
    return;
  }
  const turnMode = turnOptions.mode || (queued && queued.mode) || window.livecodeChatMode || "agent";

  const turnPlanFile = turnOptions.planFile
    || (turnMode === "plan" ? (tab.planFile || "") : "");
  const composerState = queued
    ? (queued.composerState || { text: queued.text, segments: [], attachments: [] })
    : (typeof window.getLivecodeComposerState === "function"
      ? window.getLivecodeComposerState()
      : { text: _livecodeComposerPlainText(), segments: [], attachments: [] });
  const apiAttachments = queued
    ? (queued.attachments || []).slice()
    : (typeof window.getLivecodeApiAttachments === "function"
      ? window.getLivecodeApiAttachments()
      : (composerState.attachments || []).slice());
  const hasAttachments = apiAttachments.length > 0;
  if (!queued && typeof window.areLivecodeAttachmentsReady === "function" && hasAttachments && !window.areLivecodeAttachmentsReady()) return;
  let displayQuestion = String(presetMessage != null ? presetMessage : (composerState.text || "")).trim();
  let question = displayQuestion;
  if (!question && hasAttachments && typeof window.buildChatAttachmentPrompt === "function") {
    question = window.buildChatAttachmentPrompt(apiAttachments);
    displayQuestion = displayQuestion || question;
  }
  const hasInlineFiles = (composerState.segments || []).some(function(s) {
    return s && s.type === "file";
  });
  if (!question && !hasAttachments && !hasInlineFiles) return;
  const displayPayload = queued && queued.displayPayload
    ? Object.assign({}, queued.displayPayload)
    : (typeof window.serializeLivecodeDisplayPayload === "function"
      ? window.serializeLivecodeDisplayPayload(composerState)
      : { text: displayQuestion, segments: [], attachments: composerState.attachments || [] });
  if (!displayPayload.text && displayQuestion) displayPayload.text = displayQuestion;
  const buildPlanFile = turnMode === "agent" ? String(turnOptions.planFile || "") : "";
  if (buildPlanFile) displayPayload.plan_build = { file: buildPlanFile, title: turnOptions.planTitle || "" };
  if (!livecodeProjectPath) {
    alert("Open a project folder first.");
    return;
  }
  const turnSessionId = livecodeAgentSessionId;
  if (turnSessionId) {
    delete _livecodeLastProgressSeqBySession[turnSessionId];
    delete _livecodeActiveTurnIdBySession[turnSessionId];
  }
  toggleLiveCodeAgentPane(true);
  _livecodeShowChatContainer();
  _livecodeUpdateActiveChatTabTitle(displayQuestion || question);
  _livecodeUpsertPendingSession(
    turnSessionId,
    _livecodeTruncateTabTitle(displayQuestion || question, 120)
  );
  tab.agentRunning = true;
  tab._turnProjectPath = livecodeProjectPath;
  tab._turnStateKey = _livecodeWorkspaceStateKey() || _livecodeNormalizeProjectKey(livecodeProjectPath);
  tab.buildingPlanFile = buildPlanFile;
  tab._turnStreamComplete = false;
  tab._answerStreaming = false;
  tab._assistantStreamRow = null;
  let turnFailed = false;
  let resolveTurnDone = null;
  tab._turnDone = new Promise(function(resolve) { resolveTurnDone = resolve; });
  livecodeAgentRunning = true;
  renderLiveCodeRecentSessions();
  _livecodeRenderProjectTabs();
  if (!queued) _livecodeClearComposer();
  if (typeof window.updateLivecodeComposerSendState === "function") {
    window.updateLivecodeComposerSendState();
  }
  const out = getLiveCodeChatOutput();
  if (!out) {
    tab.agentRunning = false;
    livecodeAgentRunning = false;
    return;
  }
  _livecodeMarkChatOutputForTab(out, tab);
  const getStreamOut = function() {
    return _livecodeGetTabStreamOutput(tab) || out;
  };
  _livecodeResetTurnState();
  _livecodeClearWelcomePlaceholder(out);
  _livecodeSettleRunningTerminalCards(out, { keepEmpty: true });
  let urow = _livecodeRenderPlanBuildRow(out, displayPayload);
  if (!urow && typeof window.renderLivecodeUserMessage === "function") {
    urow = window.renderLivecodeUserMessage(out, displayPayload);
  }
  _livecodeSyncPlanCardButtons(out);
  if (!urow) {
    urow = document.createElement("div");
    urow.className = "chat-row livecode-user-row";
    urow.innerHTML = `<div class="chat-msg user"><span class="livecode-user-text">${_livecodeEscapeHtml(displayQuestion || question)}</span></div>`;
    out.appendChild(urow);
  }
  _livecodeCurrentUserRow = urow;
  _livecodeScheduleUserMessageCollapseState(out);
  _livecodeSaveTurnCtxToTab(tab);
  tab.messagesHtml = out.innerHTML;
  tab.chatStarted = true;
  _livecodeChatStarted = true;
  _livecodeSyncComposerPlaceholder();
  _livecodeScrollChatToBottom(out);
  _setLiveCodeChatBusy(true);
  initLiveCodeAgentSocket();
  _livecodeResetAnswerLiveStream();
  _livecodeShowImmediateThinking(undefined, { bootstrap: true });
  _livecodeSaveTurnCtxToTab(tab);
  _livecodeScrollChatToBottom(out);
  let fullText = "";
  let lastRenderedAnswer = "";
  let assistantRow = null;
  tab._assistantStreamRow = null;
  const ensureAssistantRow = function() {
    const streamOut = getStreamOut();
    if (!streamOut) return null;
    if (assistantRow && assistantRow.parentNode === streamOut) {
      return assistantRow.querySelector(".livecode-stream-msg") || assistantRow.querySelector(".chat-msg.assistant");
    }
    if (tab._assistantStreamRow && tab._assistantStreamRow.parentNode === streamOut) {
      assistantRow = tab._assistantStreamRow;
      return assistantRow.querySelector(".livecode-stream-msg") || assistantRow.querySelector(".chat-msg.assistant");
    }
    assistantRow = document.createElement("div");
    assistantRow.className = "chat-row livecode-assistant-row";
    assistantRow.innerHTML = '<div class="chat-msg assistant livecode-stream-msg livecode-plain-msg"></div>';

    streamOut.appendChild(assistantRow);
    tab._assistantStreamRow = assistantRow;
    if (_livecodeIsActiveTab(tab)) {
      _livecodeAssistantStreamEl = assistantRow.querySelector(".livecode-stream-msg");
    }
    return assistantRow.querySelector(".livecode-stream-msg");
  };
  const finalizeAssistantAnswerRow = function() {
    if (!assistantRow) return;
    const msgEl = assistantRow.querySelector(".chat-msg.assistant");
    if (msgEl) msgEl.classList.remove("livecode-stream-msg");
    if (tab._assistantStreamRow === assistantRow) tab._assistantStreamRow = null;
    if (_livecodeAssistantStreamEl === msgEl) _livecodeAssistantStreamEl = null;
  };
  const flushAnswerMarkdown = function() {
    const msgEl = ensureAssistantRow();
    if (msgEl) {
      _livecodeRenderAssistantMarkdown(msgEl, fullText);
      lastRenderedAnswer = fullText;
    }
    const streamOut = getStreamOut();
    if (streamOut) tab.messagesHtml = streamOut.innerHTML;
    _livecodeAutoScroll(streamOut);
  };
  if (tab.abortController) {
    try { tab.abortController.abort(); } catch (e) {}
  }
  tab.abortController = new AbortController();
  if (_livecodeIsActiveTab(tab)) {
    _livecodeChatAbortController = tab.abortController;
  }
  try {
    initLiveCodeAgentSocket();
    const agentSock = window._livecodeAgentSocket;
    const socketId = (agentSock && agentSock.id) || (typeof socket !== "undefined" && socket && socket.id) || "";
    const resp = await fetch("/livecode-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: tab.abortController.signal,
      body: JSON.stringify({
        project_path: livecodeProjectPath,
        question: question,
        attachments: apiAttachments,
        session_id: turnSessionId,
        socket_id: socketId || undefined,
        model: _livecodeChatModel(),
        display_payload: displayPayload,
        mode: turnMode,
        plan_file: turnPlanFile || undefined,
        enable_mcp_tools: livecodeMcpSelectedServers.size > 0,
        mcp_servers: Array.from(livecodeMcpSelectedServers),
        mcp_disabled_tools: _livecodeMcpDisabledToolsForRequest(),
        require_permissions: !!_livecodeSettingsGet("requireApproval"),
        enable_web_tools: _livecodeSettingsGet("webTools") ? true : undefined,
        enable_browser_tools: _livecodeSettingsGet("browserTools") !== false,
        workspace: _livecodeCurrentWorkspacePayload(),
      }),
    });
    const payload = await resp.json();
    const streamOut = getStreamOut();
    const isVisibleTab = _livecodeIsActiveTab(tab);
    if (!resp.ok || payload.error) {
      turnFailed = true;
      tab._answerStreaming = true;
      fullText = payload.error || "LiveCode request failed.";
      if (isVisibleTab) {
        _livecodeStopThinkingTicker();
        _livecodeFinalizeRunningActivity();
        _livecodeHideStatusRow();
      }
      flushAnswerMarkdown();
    } else {
      tab._answerStreaming = true;
      tab._turnStreamComplete = true;
      fullText = payload.answer || "";
      if (isVisibleTab) {
        _livecodeStopThinkingTicker();
        _livecodeFinalizeRunningActivity();
        _livecodeHideStatusRow();
      }
      if (payload.session_title && livecodeChatTabs.indexOf(tab) === -1) {
        tab.title = _livecodeTruncateTabTitle(payload.session_title);
      } else if (payload.session_title) {
        _livecodeUpsertPendingSession(turnSessionId, payload.session_title);
        const titleTab = livecodeChatTabs.find(function(t) { return t.sessionId === turnSessionId; });
        if (titleTab) {
          titleTab.title = _livecodeTruncateTabTitle(payload.session_title);
        }
        if (isVisibleTab) {
          _livecodeUpdateActiveChatTabTitle(payload.session_title, true);
        }
        renderLiveCodeRecentSessions();
      }
      flushAnswerMarkdown();
      finalizeAssistantAnswerRow();
    }
    if (streamOut) {
      tab.messagesHtml = streamOut.innerHTML;
      if (_livecodeIsActiveTab(tab)) {
        _livecodeAutoScroll(streamOut);
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      turnFailed = true;
      const msgEl = ensureAssistantRow();
      if (msgEl) msgEl.textContent = "Error: " + (err.message || String(err));
      _livecodeSyncTabStreamOutput(tab);
    }
  } finally {
    if (_livecodeIsActiveTab(tab)) {
      _livecodeStopThinkingTicker();
    }
    tab._turnStreamComplete = true;
    tab._answerStreaming = false;
    tab.abortController = null;
    tab.agentRunning = false;
    tab.buildingPlanFile = "";
    _livecodeClearQuestionRequest(tab);
    if (_livecodeIsActiveTab(tab)) _livecodeSyncPlanCardButtons();
    if (_livecodeIsActiveTab(tab)) {
      _livecodeChatAbortController = null;
      _livecodeClearPendingToolSteps();
    }
    _livecodeSyncGlobalRunningFromActiveTab();
    _livecodeRenderChatTabs();
    try {
      _livecodeSyncTabStreamOutput(tab);
      const persistOut = _livecodeIsActiveTab(tab)
        ? getLiveCodeChatOutput()
        : (tab._backgroundOutput || null);
      if (persistOut) {
        _livecodeFinalizeDomForSnapshot(persistOut);
        tab.messagesHtml = persistOut.innerHTML;
      } else if (tab._backgroundOutput) {
        tab.messagesHtml = tab._backgroundOutput.innerHTML;
      }
      if (tab.messagesHtml && tab.sessionId) {
        delete _livecodeLastProgressSeqBySession[tab.sessionId];
        delete _livecodeActiveTurnIdBySession[tab.sessionId];
        tab.chatStarted = true;
        const backgroundEntry = _livecodeBackgroundEntryForTab(tab);
        if (backgroundEntry) {
          _livecodePersistBackgroundProjectChats(backgroundEntry, tab);
        } else if (tab._turnProjectPath) {
          _livecodePersistChatSnapshot(tab._turnProjectPath, tab, tab._turnStateKey);
        }
      }
      if (_livecodeIsActiveTab(tab)) {
        if (fullText && _livecodeAssistantStreamEl && fullText !== lastRenderedAnswer) {
          _livecodeRenderAssistantMarkdown(_livecodeAssistantStreamEl, fullText);
          lastRenderedAnswer = fullText;
        }
        const liveOut = getLiveCodeChatOutput();
        _livecodeAutoScroll(liveOut);
      } else {
        tab.hasUnread = true;
      }
      if (livecodeProjectPath) renderLiveCodeRecentSessions();
      _livecodeRenderChatTabs();
      if (_livecodeIsActiveTab(tab)) _livecodeSchedulePendingChangesRefresh();
    } catch (cleanupErr) {
      if (window.console && console.warn) console.warn("LiveCode chat cleanup failed", cleanupErr);
    }
    if (resolveTurnDone) resolveTurnDone();
    _livecodeAfterTurnQueue(tab, turnFailed);
  }
};

function _livecodeBindUserMessageCollapseOnce() {
  const out = getLiveCodeChatOutput();
  if (!out || out.dataset.userCollapseBound === "1") return;
  out.dataset.userCollapseBound = "1";
  out.addEventListener("click", function(e) {
    const msg = e.target && e.target.closest ? e.target.closest(".chat-row.livecode-user-row .chat-msg.user") : null;
    if (!msg) return;
    const row = msg.closest(".chat-row.livecode-user-row");
    if (!row || !row.classList.contains("is-collapsible")) return;
    row.classList.toggle("is-expanded");
  });
}

document.addEventListener("DOMContentLoaded", function() {
  const el = document.getElementById("livecode-chat-input");
  const chatOut = getLiveCodeChatOutput();
  if (chatOut) chatOut.setAttribute("data-density", _livecodeConversationDensity());
  _livecodeBindChatScrollWheel();
  _livecodeBindUserMessageCollapseOnce();
  _livecodeBindThoughtTogglesOnce();
  _livecodeBindDiffFileNameClicksOnce();
  _livecodeBindTerminalCardsOnce();
  _livecodeBindPlanCardsOnce();
  _livecodeWatchModelLabelOnce();
  _livecodeFetchFxRateOnce();
  _livecodeBindQueueKeysOnce();
  if (!el) return;

  (function() {
    const approve = document.getElementById("livecode-permission-toolbar-approve");
    const deny = document.getElementById("livecode-permission-toolbar-deny");
    if (!approve || !deny) return;

    function submit(approved) {
      const tab = _livecodeGetActiveChatTab();
      const reqId = tab ? String(tab.pendingPermissionRequestId || "") : "";
      if (!tab || !reqId) return;
      _livecodeSubmitPermissionDecision(reqId, approved);
    }

    approve.addEventListener("click", function(e) {
      e.preventDefault();
      e.stopPropagation();
      submit(true);
    });
    deny.addEventListener("click", function(e) {
      e.preventDefault();
      e.stopPropagation();
      submit(false);
    });

    _livecodeSetPermissionToolbarVisible(false);
  })();

  if (typeof window.initLivecodeComposerInput === "function") {
    window.initLivecodeComposerInput();
  }
});

window.livecodeChatMode = localStorage.getItem(LIVECODE_CHAT_MODE_STORAGE_KEY) || "agent";

function _livecodeApplyChatModeToUI() {
  const modeDef = LIVECODE_CHAT_MODES.find(function(m) {
    return m.value === window.livecodeChatMode;
  }) || LIVECODE_CHAT_MODES[0];
  const trigger = document.querySelector("[data-livecode-mode-trigger]");
  if (trigger) trigger.setAttribute("data-mode", modeDef.value);
  const label = document.querySelector("[data-livecode-mode-label]");
  if (label) label.textContent = modeDef.label;
  const iconEl = document.querySelector(".chatbot-composer-mode-icon");
  if (iconEl) {
    iconEl.outerHTML = _livecodeGetModeIconHtml(modeDef.value, "chatbot-composer-mode-icon");
  }
  _livecodeSyncComposerPlaceholder();
}

function _livecodeSyncComposerPlaceholder() {
  const input = document.getElementById("livecode-chat-input");
  const composer = document.getElementById("livecode-chat-composer");
  if (!input || (composer && composer.classList.contains("has-pending-questionnaire"))) return;
  const modeDef = LIVECODE_CHAT_MODES.find(function(m) {
    return m.value === window.livecodeChatMode;
  }) || LIVECODE_CHAT_MODES[0];
  const placeholder = _livecodeChatStarted ? LIVECODE_FOLLOWUP_PLACEHOLDER : (modeDef.placeholder || LIVECODE_COMPOSER_PLACEHOLDER);
  if ("placeholder" in input) {
    input.placeholder = placeholder;
  }
  input.setAttribute("data-placeholder", placeholder);
}

function _livecodeEnsureModeDropdown() {
  let el = document.getElementById("livecode-chat-mode-dropdown");
  if (el) {
    if (el.style.position !== "fixed") {
      el.setAttribute("style", _LIVECODE_MODE_DROPDOWN_STYLE);
    }
    return el;
  }
  el = document.createElement("div");
  el.id = "livecode-chat-mode-dropdown";
  el.className = "airflow-dropdown-opaque chatbot-model-dropdown livecode-chat-mode-dropdown";
  el.setAttribute("style", _LIVECODE_MODE_DROPDOWN_STYLE);
  el.setAttribute("role", "listbox");
  el.setAttribute("aria-label", "Select mode");
  el.innerHTML =
    '<div id="livecode-chat-mode-dropdown-list" class="chatbot-model-dropdown-list" style="flex:1;overflow-y:auto;max-height:320px;"></div>';
  document.body.appendChild(el);
  return el;
}

function _livecodeRenderModeDropdownList() {
  const list = document.getElementById("livecode-chat-mode-dropdown-list");
  if (!list) return;
  const checkSvg =
    '<svg class="livecode-mode-dropdown-item-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="20 6 9 17 4 12"></polyline></svg>';
  list.innerHTML = LIVECODE_CHAT_MODES.map(function(mode) {
    const isSelected = mode.value === window.livecodeChatMode;
    return (
      '<div class="chatbot-model-dropdown-item livecode-mode-dropdown-item theme-transition' +
      (isSelected ? " is-selected" : "") +
      '" data-mode-value="' + mode.value + '" role="option" aria-selected="' + (isSelected ? "true" : "false") + '">' +
      '<span class="livecode-mode-dropdown-item-icon">' + _livecodeGetModeIconHtml(mode.value) + "</span>" +
      "<span>" + mode.label + "</span>" +
      checkSvg +
      "</div>"
    );
  }).join("");
  list.querySelectorAll(".livecode-mode-dropdown-item").forEach(function(item) {
    item.addEventListener("click", function(e) {
      e.preventDefault();
      e.stopPropagation();
      const nextMode = item.getAttribute("data-mode-value");
      if (!nextMode) return;
      window.livecodeChatMode = nextMode;
      localStorage.setItem(LIVECODE_CHAT_MODE_STORAGE_KEY, nextMode);
      _livecodeApplyChatModeToUI();
      window.closeLivecodeModeDropdown();
      _livecodeRenderModeDropdownList();
    });
  });
}

window.closeLivecodeModeDropdown = function() {
  const dropdown = document.getElementById("livecode-chat-mode-dropdown");
  const trigger = document.querySelector("[data-livecode-mode-trigger]");
  if (dropdown) {
    dropdown.classList.remove("is-open");
    dropdown.style.setProperty("display", "none", "important");
  }
  if (trigger) {
    trigger.classList.remove("is-open");
    trigger.setAttribute("aria-expanded", "false");
  }
};

window.toggleLivecodeModeDropdown = function(trigger) {
  const dropdown = _livecodeEnsureModeDropdown();
  if (!dropdown || !trigger) return;
  const wasOpen = dropdown.classList.contains("is-open") && trigger.classList.contains("is-open");
  window.closeLivecodeModeDropdown();
  if (wasOpen) return;
  if (typeof window.closeChatbotModelDropdown === "function") {
    window.closeChatbotModelDropdown();
  }
  _livecodeRenderModeDropdownList();
  const rect = trigger.getBoundingClientRect();
  dropdown.style.setProperty("display", "flex", "important");
  dropdown.classList.add("is-open");
  dropdown.style.left = rect.left + "px";
  dropdown.style.minWidth = Math.max(rect.width, 180) + "px";
  dropdown.style.bottom = (window.innerHeight - rect.top + 6) + "px";
  dropdown.style.zIndex = "10050";
  trigger.classList.add("is-open");
  trigger.setAttribute("aria-expanded", "true");
};

const _LIVECODE_MODEL_VARIANT_RE = /^(.*?\S)\s+((?:(?:high|medium|low|minimal|max|fast|thinking)\b\s*)+)$/i;

function _livecodeStyleModelLabel(label) {
  if (!label || label.querySelector(".livecode-model-variant")) return;
  const m = String(label.textContent || "").match(_LIVECODE_MODEL_VARIANT_RE);
  if (!m) return;
  const variant = document.createElement("span");
  variant.className = "livecode-model-variant";
  variant.textContent = m[2].trim();
  label.textContent = m[1] + " ";
  label.appendChild(variant);
}

function _livecodeWatchModelLabelOnce() {
  const label = document.querySelector("#livecode-chat-composer [data-chatbot-model-label]");
  if (!label || label._livecodeWatched) return;
  label._livecodeWatched = true;
  _livecodeStyleModelLabel(label);
  new MutationObserver(function() { _livecodeStyleModelLabel(label); })
    .observe(label, { childList: true, characterData: true, subtree: true });
}

let _livecodeModeSelectorBound = false;

window.initLivecodeModeSelector = function() {
  _livecodeApplyChatModeToUI();
  _livecodeWatchModelLabelOnce();
  if (!_livecodeLlmSettings) _livecodeLoadLlmSettings();
  if (_livecodeModeSelectorBound) return;
  const trigger = document.querySelector("[data-livecode-mode-trigger]");
  if (!trigger) return;
  _livecodeModeSelectorBound = true;
  _livecodeEnsureModeDropdown();
  trigger.addEventListener("click", function(e) {
    e.preventDefault();
    e.stopPropagation();
    window.toggleLivecodeModeDropdown(trigger);
  });
  trigger.addEventListener("keydown", function(e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      window.toggleLivecodeModeDropdown(trigger);
    }
  });
  if (!window._livecodeModeDropdownGlobalBound) {
    window._livecodeModeDropdownGlobalBound = true;
    document.addEventListener("click", function(e) {
      const dropdown = document.getElementById("livecode-chat-mode-dropdown");
      if (!dropdown || !dropdown.classList.contains("is-open")) return;
      if (e.target.closest("#livecode-chat-mode-dropdown") || e.target.closest("[data-livecode-mode-trigger]")) return;
      window.closeLivecodeModeDropdown();
    });
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") window.closeLivecodeModeDropdown();
    });
  }
};

(function() {
  let isResizingSidebar = false;
  let startX = 0;
  let startWidth = 0;
  let editorStartWidth = 0;
  let rafId = null;
  const EDITOR_MIN_WIDTH = 160;
  const divider = document.getElementById("ide-sidebar-divider");
  const handle = document.getElementById("ide-sidebar-divider-handle");
  const sidebar = document.getElementById("ide-sidebar");
  if (!divider || !sidebar) return;
  try {
    const saved = parseInt(localStorage.getItem("livecodeSidebarWidth") || "", 10);
    if (saved >= 150 && saved <= 700) sidebar.style.width = saved + "px";
  } catch (e) {}
  divider.onmousedown = function(e) {
    if (e.button !== 0) return;
    isResizingSidebar = true;
    startX = e.clientX;
    startWidth = sidebar.offsetWidth;
    const editorEl = document.getElementById("ide-main-container");
    editorStartWidth = editorEl ? editorEl.offsetWidth : 700;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    sidebar.style.transition = "none";
    e.preventDefault();
  };
  const handleSidebarMove = function(e) {
    if (!isResizingSidebar) return;
    if (rafId) cancelAnimationFrame(rafId);
    // Grow into the editor only while it keeps EDITOR_MIN_WIDTH.
    const room = startWidth + editorStartWidth - EDITOR_MIN_WIDTH;
    const newWidth = Math.max(150, Math.min(700, room, startWidth + (e.clientX - startX)));
    rafId = requestAnimationFrame(() => {
      sidebar.style.width = newWidth + "px";
      rafId = null;
    });
    e.preventDefault();
  };
  const stopSidebarResizing = function() {
    if (!isResizingSidebar) return;
    isResizingSidebar = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    sidebar.style.transition = "";
    try { localStorage.setItem("livecodeSidebarWidth", String(sidebar.offsetWidth)); } catch (e) {}
    if (window.ideEditor) {
      setTimeout(function() { try { window.ideEditor.layout(); } catch (_) {} }, 50);
    }
  };
  document.addEventListener("mousemove", handleSidebarMove, { passive: false });
  document.addEventListener("mouseup", stopSidebarResizing);
})();

(function() {
  let isResizingTerminal = false;
  let rafId = null;
  const divider = document.getElementById("ide-terminal-divider");
  const terminalPanel = document.getElementById("ide-terminal-panel");
  const mainContainer = document.getElementById("ide-main-container");
  if (!divider || !terminalPanel || !mainContainer) return;
  divider.onmousedown = function(e) {
    if (e.button !== 0) return;
    isResizingTerminal = true;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    terminalPanel.style.transition = "none";
    e.preventDefault();
  };
  const handleTerminalMove = function(e) {
    if (!isResizingTerminal) return;
    if (rafId) cancelAnimationFrame(rafId);
    const containerRect = mainContainer.getBoundingClientRect();
    const mouseY = e.clientY - containerRect.top;
    const newHeight = Math.max(100, Math.min(containerRect.height - 100, containerRect.height - mouseY));
    rafId = requestAnimationFrame(() => {
      terminalPanel.style.height = newHeight + "px";
      if (ideFitAddon) try { ideFitAddon.fit(); } catch (e) {}
      rafId = null;
    });
    e.preventDefault();
  };
  const stopTerminalResizing = function() {
    if (!isResizingTerminal) return;
    isResizingTerminal = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    terminalPanel.style.transition = "";
  };
  document.addEventListener("mousemove", handleTerminalMove, { passive: false });
  document.addEventListener("mouseup", stopTerminalResizing);
})();

(function() {
  let isResizingAgent = false;
  let rafId = null;
  const LIVECODE_AGENT_DEFAULT_WIDTH = 420;
  const divider = document.getElementById("ide-agent-divider");
  const agentPanel = document.getElementById("livecode-agent-panel");
  if (!divider || !agentPanel) return;
  let agentWidth = LIVECODE_AGENT_DEFAULT_WIDTH;
  try {
    const saved = parseInt(localStorage.getItem("livecodeAgentWidth") || "", 10);
    if (saved >= 280) agentWidth = saved;
  } catch (e) {}
  agentPanel.style.width = agentWidth + "px";
  window._livecodeAgentDefaultWidthApplied = true;
  divider.addEventListener("mousedown", function(e) {
    if (e.button !== 0) return;
    isResizingAgent = true;
    const startX = e.clientX;
    const startW = agentPanel.offsetWidth;
    const editorEl = document.getElementById("ide-main-container");
    const editorStartW = editorEl ? editorEl.offsetWidth : 0;
    divider.classList.add("active");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    agentPanel.style.transition = "none";
    function onMove(ev) {
      if (!isResizingAgent) return;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(function() {
        const w = startW - (ev.clientX - startX);
        // Grow into the editor only while it keeps 160px.
        const maxW = editorEl ? startW + editorStartW - 160 : 1000;
        agentPanel.style.width = Math.max(280, Math.min(maxW, w)) + "px";
        rafId = null;
      });
      ev.preventDefault();
    }
    function onUp() {
      if (!isResizingAgent) return;
      isResizingAgent = false;
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      divider.classList.remove("active");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      agentPanel.style.transition = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      try { localStorage.setItem("livecodeAgentWidth", String(agentPanel.offsetWidth)); } catch (e) {}
      if (window.ideEditor) {
        setTimeout(function() { try { window.ideEditor.layout(); } catch (_) {} }, 50);
      }
    }
    document.addEventListener("mousemove", onMove, { passive: false });
    document.addEventListener("mouseup", onUp);
    e.preventDefault();
  });
})();

(function() {
  const row = document.getElementById("ide-main-row");
  const bar = document.getElementById("ide-hscroll");
  const thumb = bar && bar.querySelector(".ide-hscroll-thumb");
  if (!row || !bar || !thumb) return;
  let frame = 0;
  let drag = null;
  function overflow() {
    if (getComputedStyle(row).overflowX === "hidden") return 0;
    return Math.max(0, row.scrollWidth - row.clientWidth);
  }
  function update() {
    frame = 0;
    const extra = overflow();
    bar.hidden = extra < 1;
    if (bar.hidden) return;
    const track = bar.clientWidth;
    const size = Math.max(24, Math.round(track * row.clientWidth / row.scrollWidth));
    thumb.style.width = size + "px";
    thumb.style.transform = "translateX(" + Math.round((track - size) * row.scrollLeft / extra) + "px)";
  }
  function schedule() {
    if (!frame) frame = requestAnimationFrame(update);
  }
  row.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(schedule);
    observer.observe(row);
    Array.from(row.children).forEach(function(child) { observer.observe(child); });
  }
  bar.addEventListener("pointerdown", function(e) {
    const extra = overflow();
    if (!extra || e.button > 0) return;
    const track = bar.getBoundingClientRect();
    const knob = thumb.getBoundingClientRect();
    const room = Math.max(1, track.width - knob.width);
    if (e.clientX < knob.left || e.clientX > knob.right) {
      const left = Math.min(Math.max(e.clientX - track.left - knob.width / 2, 0), room);
      row.scrollLeft = left / room * extra;
    }
    drag = { x: e.clientX, start: row.scrollLeft, ratio: extra / room };
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("is-dragging");
    e.preventDefault();
  });
  bar.addEventListener("pointermove", function(e) {
    if (drag) row.scrollLeft = drag.start + (e.clientX - drag.x) * drag.ratio;
  });
  function endDrag() {
    drag = null;
    bar.classList.remove("is-dragging");
  }
  bar.addEventListener("pointerup", endDrag);
  bar.addEventListener("pointercancel", endDrag);
  schedule();
})();


const LIVECODE_BROWSER_TAB_KEY = "livecode://browser";
const _LIVECODE_BROWSER_TAB_ICON = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#8b949e" stroke-width="1.2"><circle cx="8" cy="8" r="6.2"/><ellipse cx="8" cy="8" rx="2.6" ry="6.2"/><line x1="1.8" y1="8" x2="14.2" y2="8"/></svg>'
);
const _LIVECODE_BROWSER_INSTALL = "pip install playwright && python -m playwright install chromium";
const _LIVECODE_BROWSER_PAGE_ACTIONS = ["navigate", "back", "forward", "reload", "new_tab"];
const _LIVECODE_BROWSER_RESOLUTIONS = [
  { id: "desktop-hd", label: "Desktop HD", width: 1920, height: 1080 },
  { id: "desktop", label: "Desktop", width: 1440, height: 900 },
  { id: "laptop", label: "Laptop", width: 1280, height: 800 },
  { id: "tablet-landscape", label: "Tablet landscape", width: 1024, height: 768 },
  { id: "ipad-air", label: "iPad Air", width: 820, height: 1180 },
  { id: "tablet", label: "Tablet", width: 768, height: 1024 },
  { id: "android", label: "Android", width: 412, height: 915 },
  { id: "mobile", label: "Mobile", width: 390, height: 844 },
  { id: "iphone-se", label: "iPhone SE", width: 375, height: 667 },
];
const _LIVECODE_BROWSER_DIFF_THRESHOLD = 0.1;

const _livecodeBrowser = {
  project: "",
  state: null,
  seq: -1,
  pollTimer: null,
  polling: false,
  idleMs: 200,
  pending: 0,
  pendingUrl: "",
  lastInput: 0,
  agentUntil: 0,
  tookControl: false,
  inputWs: null,
  inputWsOpen: false,
  inputWsFailed: 0,
  inputWsRetryAt: 0,
  inputQueue: [],
  inputSeq: 0,
  inputAcked: 0,
  inputPosting: false,
  inputFrame: 0,
  probeTimer: null,
  dragButtons: 0,
  pointerDrag: null,
  deadKey: false,
  viewKey: "",
  viewTimer: null,
  streaming: false,
  streamFails: 0,
  streamRetryAt: 0,
  sizeKey: "",
  sizeTimer: null,
  unavailable: "",
  lastDialog: "",
  tabsKey: "",
  cookies: null,
  viewportMode: "fit",
  maximized: false,
  selecting: false,
  selection: null,
  hover: null,
  inspectSeq: 0,
  inspectTimer: null,
  drag: null,
  compare: { on: false, mode: "side", ref: null, image: null, scale: 1, opacity: 50, match: null, diffTimer: null, loaded: false },
};

function _livecodeBrowserReset() {
  clearTimeout(_livecodeBrowser.pollTimer);
  _livecodeBrowserStopStream();
  _livecodeBrowserInputClose();
  _livecodeBrowserEndDrag();
  _livecodeBrowser.viewKey = "";
  _livecodeBrowser.streamFails = 0;
  _livecodeBrowser.streamRetryAt = 0;
  _livecodeBrowser.project = livecodeProjectPath || "";
  _livecodeBrowser.state = null;
  _livecodeBrowser.seq = -1;
  _livecodeBrowser.agentUntil = 0;
  _livecodeBrowser.tookControl = false;
  _livecodeBrowser.sizeKey = "";
  _livecodeBrowser.unavailable = "";
  _livecodeBrowser.tabsKey = "";
  _livecodeBrowser.cookies = null;
  _livecodeBrowser.selection = null;
  _livecodeBrowser.compare = { on: false, mode: "side", ref: null, image: null, scale: 1, opacity: 50, match: null, diffTimer: null, loaded: false };
  const view = document.getElementById("ide-browser-view");
  const img = view && view.querySelector(".ide-browser-frame");
  if (img) {
    img.hidden = true;
    img.removeAttribute("src");
  }
  if (view && view._livecodeBrowserBuilt) _livecodeBrowserSetSelecting(false);
}

async function _livecodeBrowserPost(path, body) {
  const resp = await fetch("/livecode/browser/" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ project_path: livecodeProjectPath, workspace: _livecodeCurrentWorkspacePayload() }, body || {})),
  });
  let data = {};
  try { data = await resp.json(); } catch (e) {}
  if (!resp.ok || (data && data.error)) {
    const err = new Error((data && data.error) || "The browser request failed (" + resp.status + ").");
    err.unavailable = !!(data && data.unavailable);
    throw err;
  }
  return data || {};
}

window.openLiveCodeBrowser = function(url) {
  if (!livecodeProjectPath) {
    _livecodeShowIdeToast("Open a project to use the browser.");
    return;
  }
  if (!ideOpenFiles[LIVECODE_BROWSER_TAB_KEY]) {
    ideOpenFiles[LIVECODE_BROWSER_TAB_KEY] = {
      isBrowser: true,
      path: LIVECODE_BROWSER_TAB_KEY,
      name: "Browser",
      content: "",
      originalContent: "",
      modified: false,
    };
  }
  switchToFile(LIVECODE_BROWSER_TAB_KEY);
  if (url) _livecodeBrowserAction("navigate", { url: url });
};

function _livecodeBrowserTabInfo() {
  return ideOpenFiles[LIVECODE_BROWSER_TAB_KEY] || null;
}

function _livecodeBrowserView() {
  return document.getElementById("ide-browser-view");
}

function _livecodeBrowserEl(selector) {
  const view = _livecodeBrowserView();
  return view ? view.querySelector(selector) : null;
}

function _livecodeShowBrowserSurface() {
  const view = _livecodeBrowserView();
  if (!view) return;
  ["ide-editor-placeholder", "ide-monaco", "ide-code-editor"].forEach(function(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = "none";
  });
  _livecodeHidePlanSurface();
  _livecodeHideSettingsSurface();
  if (window.ideEditor) {
    try { window.ideEditor.setModel(null); } catch (e) {}
  }
  if (_livecodeBrowser.project !== (livecodeProjectPath || "")) _livecodeBrowserReset();
  _livecodeBrowserBuild(view);
  view.style.display = "flex";
  _livecodeBrowserRender();
  _livecodeBrowserSyncSize(true);
  _livecodeBrowserPost("status").then(function(data) {
    _livecodeBrowser.unavailable = data.available === false ? (data.error || "The built-in browser is not installed.") : "";
    _livecodeBrowserApplyState(data);
    if (!_livecodeBrowser.unavailable) _livecodeBrowserPollSoon(0);
  }).catch(function(err) {
    _livecodeBrowser.unavailable = err.unavailable ? err.message : "";
    _livecodeBrowserRender();
    if (!err.unavailable) _livecodeBrowserPollSoon(1500);
  });
}

function _livecodeHideBrowserSurface() {
  const view = _livecodeBrowserView();
  if (view) view.style.display = "none";
  clearTimeout(_livecodeBrowser.pollTimer);
  _livecodeBrowserStopStream();
  _livecodeBrowserInputClose();
  _livecodeBrowserCloseMenu();
  if (view && view._livecodeBrowserBuilt && _livecodeBrowser.selecting) _livecodeBrowserSetSelecting(false);
}

function _livecodeBrowserVisible() {
  const view = _livecodeBrowserView();
  return !!(view && view.style.display !== "none" && document.visibilityState !== "hidden");
}

const _LIVECODE_BROWSER_PICK_ICON = '<svg class="ide-browser-pick-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M6 6l7.6 2.9-3.3 1.4-1.4 3.3z"/><path d="M6 1.6v1.6M1.6 6h1.6M2.9 2.9l1.1 1.1M9.1 2.9L8 4M2.9 9.1L4 8"/></svg>';

function _livecodeBrowserButton(action, icon, title, cls) {
  const glyph = icon === "pick" ? _LIVECODE_BROWSER_PICK_ICON : _livecodeIcon(icon, { size: "base" });
  return '<button type="button" class="nav-button' + (cls ? " " + cls : "") + '" data-browser-nav="' + action + '" title="' + title + '" aria-label="' + title + '">' +
    glyph + "</button>";
}

function _livecodeBrowserBuild(view) {
  if (view._livecodeBrowserBuilt) return;
  view._livecodeBrowserBuilt = true;
  view.innerHTML =
    '<div class="ide-browser-titlebar">' +
      '<div class="ide-browser-tabstrip" role="tablist" aria-label="Browser tabs"></div>' +
      '<div class="ide-browser-window-actions">' +
        _livecodeBrowserButton("compare", "layout-split-vertical", "Compare with a reference image", "ide-browser-compare-toggle") +
        _livecodeBrowserButton("menu", "dots-3-vertical", "More actions") +
        _livecodeBrowserButton("maximize", "arrows-expand", "Maximize", "ide-browser-maximize") +
        _livecodeBrowserButton("close-view", "x", "Close browser tab") +
      "</div>" +
    "</div>" +
    '<div class="browser-navbar">' +
      '<div class="nav-controls">' +
        _livecodeBrowserButton("back", "arrow-left", "Navigate back") +
        _livecodeBrowserButton("forward", "arrow-right", "Navigate forward") +
        _livecodeBrowserButton("reload", "arrows-cw", "Reload") +
      "</div>" +
      '<div class="url-input-container"><div class="ide-browser-url">' +
        '<input class="url-input" type="text" placeholder="Search or enter URL" autocomplete="off" spellcheck="false" aria-label="Address">' +
        '<div class="ide-browser-url-display" aria-hidden="true"><span class="ide-browser-url-host"></span><span class="ide-browser-url-rest"></span></div>' +
      "</div></div>" +
      '<div class="nav-controls nav-controls--end">' +
        _livecodeBrowserButton("select", "pick", "Select an element or drag to crop", "ide-browser-select-toggle") +
        '<span class="ide-browser-engine-chip" hidden>Chrome</span>' +
        '<button type="button" class="ide-browser-size-chip" data-browser-nav="resolution" title="Resolution" hidden></button>' +
        _livecodeBrowserButton("resolution", "displays", "Resolution", "ide-browser-resolution-toggle") +
      "</div>" +
      '<div class="url-loading-bar" hidden><div class="url-loading-bar-progress"></div></div>' +
    "</div>" +
    '<div class="ide-browser-compare-bar" hidden>' +
      '<span class="ide-browser-compare-ref">' + _livecodeIcon("image", { size: "sm" }) + '<span class="ide-browser-compare-name">No reference image</span></span>' +
      '<button type="button" class="ide-browser-bar-btn" data-browser-compare="choose">Choose image…</button>' +
      '<button type="button" class="ide-browser-bar-icon" data-browser-compare="clear" title="Remove the reference" aria-label="Remove the reference">' + _livecodeIcon("x", { size: "sm" }) + "</button>" +
      '<div class="lc-segmented ide-browser-compare-modes" role="radiogroup" aria-label="Compare mode">' +
        '<button type="button" class="lc-btn" data-browser-compare-mode="side" role="radio">Side by side</button>' +
        '<button type="button" class="lc-btn" data-browser-compare-mode="overlay" role="radio">Overlay</button>' +
        '<button type="button" class="lc-btn" data-browser-compare-mode="diff" role="radio">Difference</button>' +
      "</div>" +
      '<input type="range" class="ide-browser-opacity" min="0" max="100" value="50" aria-label="Reference opacity" title="Reference opacity" hidden>' +
      '<label class="ide-browser-scale-label" title="The design was exported at this scale">Scale <select class="ide-browser-scale" aria-label="Design scale"><option value="1">1x</option><option value="2">2x</option><option value="3">3x</option></select></label>' +
      '<span class="ide-browser-match"></span>' +
      '<span class="ide-browser-bar-spacer"></span>' +
      '<button type="button" class="ide-browser-bar-btn is-primary" data-browser-compare="ask">Ask agent to match</button>' +
      '<input type="file" class="ide-browser-ref-input" accept="image/*" hidden>' +
    "</div>" +
    '<div class="ide-browser-body">' +
      '<div class="ide-browser-ref-pane" hidden tabindex="0" aria-label="Reference image">' +
        '<img class="ide-browser-ref-img" alt="Reference" draggable="false" hidden>' +
        '<div class="ide-browser-ref-drop">' + _livecodeIcon("image", { size: "lg" }) +
          '<div class="ide-browser-ref-drop-title">Add a reference image</div>' +
          '<div class="ide-browser-ref-drop-sub">Drop a design or screenshot here, paste it, or <button type="button" class="ide-browser-link" data-browser-compare="choose">choose a file</button>.</div>' +
        "</div>" +
      "</div>" +
      '<div class="browser-frame-container" tabindex="0" aria-label="Page">' +
        '<textarea class="ide-browser-keys" aria-label="Type into the page" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" tabindex="-1"></textarea>' +
        '<div class="ide-browser-stage">' +
          '<img class="ide-browser-frame" alt="" draggable="false" hidden>' +
          '<img class="ide-browser-overlay-ref" alt="" draggable="false" hidden>' +
          '<canvas class="ide-browser-diff" hidden></canvas>' +
          '<div class="ide-browser-select-layer" hidden>' +
            '<div class="ide-browser-hover-box" hidden><span class="ide-browser-hover-label"></span></div>' +
            '<div class="ide-browser-select-box" hidden></div>' +
          "</div>" +
        "</div>" +
        '<div class="browser-recording-indicator" hidden role="status"><span class="recording-dot"></span><span class="ide-browser-agent-label">Agent is using the browser</span>' +
          '<button type="button" class="browser-indicator-take" data-browser-nav="take-control">Take control</button></div>' +
        '<div class="browser-lock-overlay" hidden><button type="button" class="ui-pill browser-lock-take-control-button" data-variant="ghost" data-browser-nav="take-control">' +
          '<span class="ui-pill__icon">' + _livecodeIcon("pointer-arrow", { size: "base" }) + '</span><span class="ui-pill__label">Take control</span></button></div>' +
        '<div class="ide-browser-crop-bar" hidden>' +
          '<span class="ide-browser-crop-label"></span>' +
          '<button type="button" class="ide-browser-bar-btn is-primary" data-browser-crop="chat">Add to chat</button>' +
          '<button type="button" class="ide-browser-bar-btn" data-browser-crop="design" title="Compare pages with this image (the agent\'s default design)">Use as design</button>' +
          '<button type="button" class="ide-browser-bar-btn" data-browser-crop="copy">Copy</button>' +
          '<button type="button" class="ide-browser-bar-btn" data-browser-crop="save">Save</button>' +
          '<button type="button" class="ide-browser-bar-icon" data-browser-crop="cancel" title="Cancel (Esc)" aria-label="Cancel">' + _livecodeIcon("x", { size: "sm" }) + "</button>" +
        "</div>" +
        '<div class="browser-error-overlay ide-browser-empty" hidden></div>' +
      "</div>" +
    "</div>" +
    '<div class="ide-browser-menu" role="menu" hidden></div>';
  _livecodeBrowserBind(view);
  if (typeof ResizeObserver === "function") {
    const frame = view.querySelector(".browser-frame-container");
    new ResizeObserver(function() { _livecodeBrowserSyncSize(false); }).observe(frame);
  }
  _livecodeBrowserWatchDpr();
}


function _livecodeBrowserApplyState(state) {
  if (!state) return;
  _livecodeBrowser.state = state;
  const mode = state.viewport_mode === "fixed" ? "fixed" : "fit";
  if (mode !== _livecodeBrowser.viewportMode) {
    _livecodeBrowser.viewportMode = mode;
    if (mode === "fit") {
      _livecodeBrowser.sizeKey = "";
      setTimeout(function() { _livecodeBrowserSyncSize(true); }, 0);
    } else {
      _livecodeBrowserLayoutFrame();
    }
  }
  const dialog = String(state.dialog || "");
  if (dialog && dialog !== _livecodeBrowser.lastDialog) _livecodeShowIdeToast("The page opened a dialog: " + dialog);
  _livecodeBrowser.lastDialog = dialog;
  _livecodeBrowserRender();
}

function _livecodeBrowserAgentActive() {
  const busy = _livecodeBrowser.state && _livecodeBrowser.state.busy;
  return Date.now() < _livecodeBrowser.agentUntil || (!!busy && _livecodeBrowser.pending === 0);
}

function _livecodeBrowserUrlParts(url) {
  const text = String(url || "");
  if (!text || text === "about:blank") return { host: "", rest: "" };
  const m = text.match(/^https?:\/\/([^/?#]+)(.*)$/i);
  if (!m) return { host: text, rest: "" };
  const rest = m[2] === "/" ? "" : m[2];
  return { host: m[1], rest: rest };
}

function _livecodeBrowserRender() {
  const view = _livecodeBrowserView();
  if (!view || !view._livecodeBrowserBuilt) return;
  const state = _livecodeBrowser.state || {};
  const tabs = state.tabs || [];
  const hasPage = tabs.length > 0;
  const busy = !!state.busy || _livecodeBrowser.pending > 0;
  const compare = _livecodeBrowser.compare;

  const input = view.querySelector(".url-input");
  const urlBox = view.querySelector(".ide-browser-url");
  const stateUrl = state.url && state.url !== "about:blank" ? state.url : "";
  const url = _livecodeBrowser.pendingUrl || stateUrl;
  if (input && document.activeElement !== input) {
    input.value = url;
    const parts = _livecodeBrowserUrlParts(url);
    view.querySelector(".ide-browser-url-host").textContent = parts.host;
    view.querySelector(".ide-browser-url-rest").textContent = parts.rest;
    urlBox.classList.toggle("has-url", !!url);
  }
  view.querySelectorAll('[data-browser-nav="back"], [data-browser-nav="forward"], [data-browser-nav="reload"]').forEach(function(btn) {
    btn.disabled = !hasPage;
  });
  const bar = view.querySelector(".url-loading-bar");
  const loadingTab = (state.tabs || []).filter(function(t) { return t.id === state.active; })[0];
  if (bar) bar.hidden = !(busy || (loadingTab && loadingTab.loading));

  const img = view.querySelector(".ide-browser-frame");
  if (img && !hasPage) img.hidden = true;
  const frame = view.querySelector(".browser-frame-container");
  const showsPage = hasPage && img && !img.hidden;
  frame.toggleAttribute("data-loaded", !!showsPage);
  const kind = _livecodeBrowserFrameKind();
  frame.classList.toggle("is-device", kind === "device");
  frame.classList.toggle("is-fixed", kind === "fixed");

  const agent = _livecodeBrowserAgentActive();
  const indicator = view.querySelector(".browser-recording-indicator");
  indicator.hidden = !agent;
  const label = indicator.querySelector(".ide-browser-agent-label");
  const labelText = _livecodeBrowser.tookControl ? "You have control" :
    state.busy && _livecodeBrowser.pending === 0 ? _livecodeBrowserBusyLabel(state.busy) : "Agent is using the browser";
  if (label && label.textContent !== labelText) label.textContent = labelText;
  indicator.classList.toggle("is-yours", !!_livecodeBrowser.tookControl);
  const take = indicator.querySelector(".browser-indicator-take");
  if (take) take.hidden = !!_livecodeBrowser.tookControl || !hasPage;
  view.querySelector(".browser-lock-overlay").hidden = !(agent && hasPage && !_livecodeBrowser.tookControl);

  const empty = view.querySelector(".ide-browser-empty");
  const showEmpty = !!_livecodeBrowser.unavailable || (!hasPage && !busy);
  empty.hidden = !showEmpty;
  if (showEmpty) {
    const html = _livecodeBrowser.unavailable ? _livecodeBrowserUnavailableHtml(_livecodeBrowser.unavailable) : _livecodeBrowserEmptyHtml();
    if (empty._html !== html) {
      empty.innerHTML = html;
      empty._html = html;
    }
  }

  view.querySelector(".ide-browser-select-toggle").classList.toggle("is-active", _livecodeBrowser.selecting);
  view.querySelector(".ide-browser-select-toggle").disabled = !hasPage;
  const fixed = _livecodeBrowser.viewportMode === "fixed";
  const vp = state.viewport || {};
  view.querySelector(".ide-browser-resolution-toggle").classList.toggle("is-active", fixed);
  const chip = view.querySelector(".ide-browser-size-chip");
  const chipText = fixed && vp.width ? vp.width + " × " + vp.height : "";
  chip.hidden = !chipText;
  if (chip.textContent !== chipText) chip.textContent = chipText;
  const engine = view.querySelector(".ide-browser-engine-chip");
  engine.hidden = state.engine !== "chrome";
  engine.title = "The tabs are in your own Chrome, attached over CDP (Settings > Agent > Browser)";
  view.querySelector(".ide-browser-compare-toggle").classList.toggle("is-active", compare.on);
  view.classList.toggle("is-maximized", _livecodeBrowser.maximized);
  const max = view.querySelector(".ide-browser-maximize");
  if (max) {
    const want = _livecodeBrowser.maximized ? "arrows-contract" : "arrows-expand";
    if (max.getAttribute("data-icon-state") !== want) {
      max.innerHTML = _livecodeIcon(want, { size: "base" });
      max.setAttribute("data-icon-state", want);
      max.title = _livecodeBrowser.maximized ? "Restore" : "Maximize";
      max.setAttribute("aria-label", max.title);
    }
  }
  _livecodeBrowserRenderCompare(view);
  _livecodeBrowserRenderTabs(view, tabs, state.active || "");
}

function _livecodeBrowserBusyLabel(action) {
  const labels = {
    navigate: "Agent is opening a page", click: "Agent is clicking", type: "Agent is typing", press: "Agent is pressing a key",
    screenshot: "Agent is taking a screenshot", crop: "Cropping", compare: "Comparing with the reference",
    snapshot: "Agent is reading the page", javascript_exec: "Agent is running a page script",
    scroll: "Agent is scrolling", wait: "Agent is waiting for the page", back: "Agent is going back", forward: "Agent is going forward",
    reload: "Agent is reloading the page", cookies: "Updating cookies", resize: "Resizing the page",
    inspect: "Agent is inspecting the page", connect: "Attaching to Chrome", figma: "Reading the Figma design",
  };
  return labels[action] || "Agent is using the browser";
}

function _livecodeBrowserEmptyHtml() {
  const servers = _livecodeBrowser.devServers;
  let found = "";
  if (servers && servers.length) {
    found = '<div class="ide-browser-dev-list">' + servers.map(function(s) {
      return '<button type="button" class="ide-browser-dev-server lc-btn" data-browser-open="' + _livecodeEscapeHtml(s.url) + '">' +
        _livecodeIcon("globe", { size: "sm" }) + "<span>" + _livecodeEscapeHtml(s.url.replace(/^https?:\/\//, "").replace(/\/$/, "")) + "</span></button>";
    }).join("") + "</div>";
  } else if (servers) {
    found = '<p class="browser-error-subtitle ide-browser-note">No dev server answered on the usual ports. Start one in the terminal, then try again.</p>';
  }
  return '<div class="browser-error-content">' +
    '<div class="browser-error-icon">' + _livecodeIcon("globe", { size: "lg" }) + "</div>" +
    '<h3 class="browser-error-title">Browse with the agent</h3>' +
    '<p class="browser-error-subtitle">Type a URL or ask the agent to open a site. The agent can read, click, and type.</p>' +
    '<p class="ide-browser-preview"><span>Preview your app instead?</span><button type="button" class="ide-browser-detect" data-browser-nav="detect">' +
      (_livecodeBrowser.detecting ? "Detecting…" : "Detect dev server") + "</button></p>" +
    found +
    "</div>";
}

function _livecodeBrowserUnavailableHtml(message) {
  return '<div class="browser-error-content">' +
    '<div class="browser-error-icon">' + _livecodeIcon("globe", { size: "lg" }) + "</div>" +
    '<h3 class="browser-error-title">The built-in browser isn’t set up</h3>' +
    '<p class="browser-error-subtitle">' + _livecodeEscapeHtml(message) + "</p>" +
    '<pre class="ide-browser-code">' + _LIVECODE_BROWSER_INSTALL + "</pre>" +
    '<button type="button" class="livecode-btn ide-browser-retry" data-browser-nav="retry">Try again</button>' +
    "</div>";
}

function _livecodeBrowserTabTitle(tab) {
  const title = String((tab && tab.title) || "").trim();
  if (title) return title;
  const url = String((tab && tab.url) || "");
  if (!url || url === "about:blank") return "New tab";
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function _livecodeBrowserOrderedTabs(tabs) {
  const order = _livecodeBrowser.tabOrder || [];
  return (tabs || []).slice().sort(function(a, b) {
    const ia = order.indexOf(a.id), ib = order.indexOf(b.id);
    return (ia < 0 ? 1e6 : ia) - (ib < 0 ? 1e6 : ib);
  });
}

function _livecodeBrowserTabIconHtml(tab) {
  if (tab.crashed || tab.failed) return '<span class="ide-browser-tab-icon is-alert">' + _livecodeIcon("error", { size: "sm" }) + "</span>";
  if (tab.loading) return '<span class="ide-browser-tab-icon"><span class="ide-browser-tab-spinner"></span></span>';
  const globe = _livecodeIcon("globe", { size: "sm" });
  if (tab.favicon) {
    return '<span class="ide-browser-tab-icon"><img class="ide-browser-tab-favicon" src="' + _livecodeEscapeHtml(tab.favicon) + '" alt="" referrerpolicy="no-referrer" draggable="false">' +
      '<span class="ide-browser-tab-globe">' + globe + "</span></span>";
  }
  return '<span class="ide-browser-tab-icon"><span class="ide-browser-tab-globe is-shown">' + globe + "</span></span>";
}

function _livecodeBrowserTabTooltip(tab, title) {
  const lines = [tab.url || title];
  if (tab.agent) lines.push((tab.agent_done ? "Opened by subagent (finished): " : "A subagent is browsing here: ") + tab.agent);
  if (tab.crashed) lines.push("This tab crashed. Reload it.");
  else if (tab.failed) lines.push("Could not load: " + tab.failed);
  else if (tab.status >= 400) lines.push("The page answered HTTP " + tab.status);
  return lines.join("\n");
}

function _livecodeBrowserRenderTabs(view, tabs, active) {
  const strip = view.querySelector(".ide-browser-tabstrip");
  if (!strip) return;
  const ordered = _livecodeBrowserOrderedTabs(tabs);
  const key = JSON.stringify([ordered.map(function(t) { return [t.id, _livecodeBrowserTabTitle(t), t.url, t.favicon, t.loading, t.failed, t.crashed, t.status, t.agent || "", !!t.agent_done]; }), active]);
  if (key === _livecodeBrowser.tabsKey) return;
  _livecodeBrowser.tabsKey = key;
  const items = ordered.length ? ordered.map(function(tab) {
    const title = _livecodeBrowserTabTitle(tab);
    const isActive = tab.id === active;
    const cls = "ide-browser-tab" + (isActive ? " is-active" : "") + (tab.loading ? " is-loading" : "") + ((tab.failed || tab.crashed) ? " is-failed" : "") + (tab.agent ? " is-agent" : "");
    return '<div class="' + cls + '" role="tab" draggable="true" aria-selected="' + isActive + '" data-browser-tab="' + _livecodeEscapeHtml(tab.id) + '" title="' + _livecodeEscapeHtml(_livecodeBrowserTabTooltip(tab, title)) + '">' +
      _livecodeBrowserTabIconHtml(tab) +
      (tab.agent ? '<span class="ide-browser-tab-agent' + (tab.agent_done ? " is-done" : "") + '" aria-label="Subagent tab">' + _livecodeIcon("infinity", { size: "sm" }) + "</span>" : "") +
      '<span class="ide-browser-tab-title">' + _livecodeEscapeHtml(title) + "</span>" +
      (tab.status >= 400 && !tab.failed ? '<span class="ide-browser-tab-status">' + tab.status + "</span>" : "") +
      '<button type="button" class="ide-browser-tab-close" data-browser-close-tab="' + _livecodeEscapeHtml(tab.id) + '" title="Close tab" aria-label="Close tab">' + _livecodeIcon("x", { size: "sm" }) + "</button></div>";
  }).join("") : '<div class="ide-browser-tab is-active is-placeholder" role="tab" aria-selected="true"><span class="ide-browser-tab-title">New tab</span></div>';
  strip.innerHTML = items +
    '<button type="button" class="ide-browser-tab-add" data-browser-nav="new-tab" title="New tab" aria-label="New tab">' + _livecodeIcon("plus", { size: "base" }) + "</button>";
}

function _livecodeBrowserTabMenu(view, tabId, e) {
  const menu = view.querySelector(".ide-browser-menu");
  if (!menu) return;
  const tabs = (_livecodeBrowser.state && _livecodeBrowser.state.tabs) || [];
  const item = function(id, label, enabled) {
    return '<button type="button" class="ide-browser-menu-item" role="menuitem" data-browser-menu="' + id + ":" + _livecodeEscapeHtml(tabId) + '"' + (enabled === false ? " disabled" : "") + ">" + label + "</button>";
  };
  menu.setAttribute("data-menu", "tab");
  menu.innerHTML = item("tab-reload", "Reload tab") + item("tab-duplicate", "Duplicate tab") + '<div class="ide-browser-menu-sep"></div>' +
    item("tab-close", "Close tab") + item("tab-close-others", "Close other tabs", tabs.length > 1);
  menu.hidden = false;
  const host = view.getBoundingClientRect();
  menu.style.right = "auto";
  menu.style.left = Math.round(Math.min(e.clientX - host.left, host.width - 190)) + "px";
  menu.style.top = Math.round(e.clientY - host.top + 4) + "px";
}

function _livecodeBrowserTabMenuAction(action, tabId) {
  const tabs = (_livecodeBrowser.state && _livecodeBrowser.state.tabs) || [];
  const tab = tabs.filter(function(t) { return t.id === tabId; })[0];
  if (action === "tab-duplicate") {
    if (tab && tab.url && tab.url !== "about:blank") _livecodeBrowserAction("new_tab", { url: tab.url });
    else _livecodeBrowserAction("new_tab", {});
  } else if (action === "tab-reload") {
    _livecodeBrowserAction("switch_tab", { tab_id: tabId }).then(function() { return _livecodeBrowserAction("reload", {}); });
  } else if (action === "tab-close") {
    _livecodeBrowserAction("close_tab", { tab_id: tabId });
  } else if (action === "tab-close-others") {
    tabs.filter(function(t) { return t.id !== tabId; }).reduce(function(chain, t) {
      return chain.then(function() { return _livecodeBrowserAction("close_tab", { tab_id: t.id }); });
    }, Promise.resolve());
  }
}

function _livecodeBrowserBindExtras(view) {
  const strip = view.querySelector(".ide-browser-tabstrip");
  if (!strip || strip._livecodeExtras) return;
  strip._livecodeExtras = true;
  strip.addEventListener("error", function(e) {
    const img = e.target;
    if (img && img.classList && img.classList.contains("ide-browser-tab-favicon")) img.parentElement.classList.add("is-broken");
  }, true);
  strip.addEventListener("dragstart", function(e) {
    const tab = e.target.closest ? e.target.closest("[data-browser-tab]") : null;
    if (!tab) return;
    _livecodeBrowser.dragTab = tab.getAttribute("data-browser-tab");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", _livecodeBrowser.dragTab); } catch (_) {}
    tab.classList.add("is-dragging");
  });
  strip.addEventListener("dragover", function(e) {
    if (!_livecodeBrowser.dragTab) return;
    const over = e.target.closest ? e.target.closest("[data-browser-tab]") : null;
    if (!over) return;
    e.preventDefault();
    strip.querySelectorAll(".is-drop-target").forEach(function(el) { el.classList.remove("is-drop-target"); });
    if (over.getAttribute("data-browser-tab") !== _livecodeBrowser.dragTab) over.classList.add("is-drop-target");
  });
  strip.addEventListener("dragend", function() {
    _livecodeBrowser.dragTab = "";
    strip.querySelectorAll(".is-dragging, .is-drop-target").forEach(function(el) { el.classList.remove("is-dragging", "is-drop-target"); });
  });
  strip.addEventListener("drop", function(e) {
    const dragged = _livecodeBrowser.dragTab;
    const over = e.target.closest ? e.target.closest("[data-browser-tab]") : null;
    _livecodeBrowser.dragTab = "";
    if (!dragged || !over) return;
    e.preventDefault();
    const targetId = over.getAttribute("data-browser-tab");
    if (targetId === dragged) return;
    const ids = _livecodeBrowserOrderedTabs((_livecodeBrowser.state && _livecodeBrowser.state.tabs) || []).map(function(t) { return t.id; }).filter(function(id) { return id !== dragged; });
    ids.splice(ids.indexOf(targetId), 0, dragged);
    _livecodeBrowser.tabOrder = ids;
    _livecodeBrowser.tabsKey = "";
    _livecodeBrowserRenderTabs(view, (_livecodeBrowser.state && _livecodeBrowser.state.tabs) || [], (_livecodeBrowser.state && _livecodeBrowser.state.active) || "");
  });
  strip.addEventListener("contextmenu", function(e) {
    const tab = e.target.closest ? e.target.closest("[data-browser-tab]") : null;
    if (!tab) return;
    e.preventDefault();
    _livecodeBrowserTabMenu(view, tab.getAttribute("data-browser-tab"), e);
  });
  view.addEventListener("keydown", function(e) {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && String(e.key).toLowerCase() === "f" && _livecodeBrowserHasPage()) {
      e.preventDefault();
      _livecodeBrowserOpenFind();
    }
  });
}

function _livecodeBrowserOpenFind() {
  const view = _livecodeBrowserView();
  if (!view) return;
  let bar = view.querySelector(".ide-browser-find");
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "ide-browser-find";
    bar.innerHTML = '<input type="search" class="ide-browser-find-input" placeholder="Find in page" autocomplete="off" spellcheck="false" aria-label="Find in page">' +
      '<span class="ide-browser-find-count"></span>' +
      '<button type="button" class="ide-browser-bar-icon" data-find="prev" title="Previous match" aria-label="Previous match">' + _livecodeIcon("chevron-up", { size: "sm" }) + "</button>" +
      '<button type="button" class="ide-browser-bar-icon" data-find="next" title="Next match" aria-label="Next match">' + _livecodeIcon("chevron-down", { size: "sm" }) + "</button>" +
      '<button type="button" class="ide-browser-bar-icon" data-find="close" title="Close" aria-label="Close find">' + _livecodeIcon("x", { size: "sm" }) + "</button>";
    view.appendChild(bar);
    const input = bar.querySelector(".ide-browser-find-input");
    let timer = null;
    input.addEventListener("input", function() {
      clearTimeout(timer);
      timer = setTimeout(function() { _livecodeBrowserFind(0, true); }, 180);
    });
    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") { e.preventDefault(); _livecodeBrowserFind(e.shiftKey ? -1 : 1, false); }
      else if (e.key === "Escape") { e.preventDefault(); _livecodeBrowserCloseFind(); }
    });
    bar.addEventListener("click", function(e) {
      const btn = e.target.closest ? e.target.closest("[data-find]") : null;
      if (!btn) return;
      const what = btn.getAttribute("data-find");
      if (what === "close") _livecodeBrowserCloseFind();
      else _livecodeBrowserFind(what === "prev" ? -1 : 1, false);
    });
  }
  bar.hidden = false;
  const field = bar.querySelector(".ide-browser-find-input");
  field.focus();
  field.select();
}

function _livecodeBrowserCloseFind() {
  const view = _livecodeBrowserView();
  const bar = view && view.querySelector(".ide-browser-find");
  if (bar) bar.hidden = true;
  _livecodeBrowser.findIndex = 0;
  _livecodeBrowserPost("action", { action: "find", args: { text: "", clear: true } }).catch(function() {});
}

function _livecodeBrowserFind(step, reset) {
  const view = _livecodeBrowserView();
  const bar = view && view.querySelector(".ide-browser-find");
  if (!bar) return;
  const text = bar.querySelector(".ide-browser-find-input").value;
  const count = bar.querySelector(".ide-browser-find-count");
  if (!text) {
    count.textContent = "";
    _livecodeBrowserPost("action", { action: "find", args: { text: "", clear: true } }).catch(function() {});
    return;
  }
  const index = reset ? 0 : (_livecodeBrowser.findIndex || 0) + step;
  _livecodeBrowserPost("action", { action: "find", args: { text: text, index: index } }).then(function(data) {
    const found = (data && data.find) || { count: 0, current: 0 };
    _livecodeBrowser.findIndex = found.current || 0;
    count.textContent = found.count ? (found.current + 1) + " of " + found.count : "No matches";
    count.classList.toggle("is-empty", !found.count);
  }).catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
}

const _LIVECODE_BROWSER_ZOOMS = [0.5, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 2];

function _livecodeBrowserZoom(direction) {
  const current = Number((_livecodeBrowser.state && _livecodeBrowser.state.zoom) || _livecodeBrowser.zoom || 1);
  let index = _LIVECODE_BROWSER_ZOOMS.reduce(function(best, z, i) { return Math.abs(z - current) < Math.abs(_LIVECODE_BROWSER_ZOOMS[best] - current) ? i : best; }, 0);
  if (direction === 0) index = _LIVECODE_BROWSER_ZOOMS.indexOf(1);
  else index = Math.min(_LIVECODE_BROWSER_ZOOMS.length - 1, Math.max(0, index + direction));
  _livecodeBrowserAction("zoom", { level: _LIVECODE_BROWSER_ZOOMS[index] }).then(function(data) {
    if (data && data.zoom) _livecodeBrowser.zoom = data.zoom;
  });
}

function _livecodeBrowserInfoDialog(kind) {
  let overlay = document.getElementById("ide-browser-info-dialog");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "ide-browser-info-dialog";
    overlay.className = "ide-modal-overlay ide-browser-info-overlay theme-transition";
    overlay.hidden = true;
    overlay.innerHTML = '<div class="ide-modal ide-browser-info-modal theme-transition" role="dialog" aria-modal="true">' +
      '<button type="button" class="ide-modal-close" data-info="close" aria-label="Close">' + _livecodeIcon("x", { size: "sm" }) + "</button>" +
      '<div class="ide-modal-title ide-browser-info-title"></div>' +
      '<div class="ide-browser-info-tools"></div>' +
      '<div class="ide-browser-info-list" role="list"></div>' +
      '<div class="ide-modal-actions"><button type="button" class="ide-modal-btn" data-info="clear">Clear</button><span class="ide-cookie-spacer"></span><button type="button" class="ide-modal-btn" data-info="refresh">Refresh</button><button type="button" class="ide-modal-btn ide-modal-btn-primary" data-info="close">Close</button></div></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener("mousedown", function(e) { if (e.target === overlay) overlay.hidden = true; });
    overlay.addEventListener("keydown", function(e) { if (e.key === "Escape") overlay.hidden = true; });
    overlay.addEventListener("click", function(e) {
      const btn = e.target.closest ? e.target.closest("[data-info]") : null;
      if (btn) {
        const what = btn.getAttribute("data-info");
        if (what === "close") overlay.hidden = true;
        else if (what === "refresh") _livecodeBrowserInfoLoad(overlay);
        else if (what === "clear") {
          _livecodeBrowserPost("info", { kind: "clear", args: { what: overlay._tab || overlay._kind } }).then(function() { _livecodeBrowserInfoLoad(overlay); }).catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
        } else if (what === "tab") {
          overlay._tab = btn.getAttribute("data-tab");
          _livecodeBrowserInfoLoad(overlay);
        }
        return;
      }
      const go = e.target.closest ? e.target.closest("[data-info-open]") : null;
      if (go) {
        e.preventDefault();
        overlay.hidden = true;
        _livecodeBrowserAction(_livecodeBrowserHasPage() ? "navigate" : "new_tab", { url: go.getAttribute("data-info-open") });
      }
    });
    overlay.addEventListener("input", function(e) {
      if (e.target && e.target.classList && (e.target.classList.contains("ide-browser-info-filter"))) _livecodeBrowserInfoLoad(overlay, true);
    });
    overlay.addEventListener("change", function(e) {
      if (e.target && e.target.classList && e.target.classList.contains("ide-browser-info-failed")) _livecodeBrowserInfoLoad(overlay, true);
    });
  }
  overlay._kind = kind;
  overlay._tab = kind === "console" ? "console" : kind;
  overlay.hidden = false;
  _livecodeBrowserInfoLoad(overlay);
}

function _livecodeBrowserInfoLoad(overlay, keepTools) {
  const kind = overlay._kind, tab = overlay._tab;
  const title = overlay.querySelector(".ide-browser-info-title");
  const tools = overlay.querySelector(".ide-browser-info-tools");
  const list = overlay.querySelector(".ide-browser-info-list");
  const clear = overlay.querySelector('[data-info="clear"]');
  title.textContent = kind === "downloads" ? "Downloads" : kind === "history" ? "History" : "Console and network";
  clear.hidden = kind === "downloads";
  const filterField = tools.querySelector(".ide-browser-info-filter");
  const filter = filterField ? filterField.value : "";
  const failedBox = tools.querySelector(".ide-browser-info-failed");
  const failedOnly = !!(failedBox && failedBox.checked);
  if (!keepTools) {
    tools.innerHTML = kind === "console"
      ? '<div class="lc-segmented" role="tablist">' +
          '<button type="button" class="lc-btn' + (tab === "console" ? " is-active" : "") + '" data-info="tab" data-tab="console">Console</button>' +
          '<button type="button" class="lc-btn' + (tab === "network" ? " is-active" : "") + '" data-info="tab" data-tab="network">Network</button></div>' +
        '<input type="search" class="ide-modal-input ide-browser-info-filter" placeholder="Filter" autocomplete="off" spellcheck="false">' +
        (tab === "network" ? '<label class="ide-browser-info-check"><input type="checkbox" class="ide-browser-info-failed"> Failed only</label>' : "")
      : "";
  }
  const args = { limit: 200 };
  const useKind = kind === "console" ? tab : kind;
  if (useKind === "network") {
    if (filter) args.filter = filter;
    if (failedOnly) args.status = "failed";
  }
  _livecodeBrowserPost("info", { kind: useKind, args: args }).then(function(data) {
    const entries = data.entries || [];
    const esc = _livecodeEscapeHtml;
    const when = function(t) { const d = new Date(t * 1000); return d.toLocaleTimeString([], { hour12: false }); };
    let rows = "";
    if (useKind === "console") {
      const needle = filter.toLowerCase();
      rows = entries.filter(function(e) { return !needle || (e.text + " " + e.source).toLowerCase().indexOf(needle) >= 0; }).map(function(e) {
        return '<div class="ide-browser-info-row is-' + esc(e.level) + '"><span class="ide-browser-info-time">' + when(e.t) + '</span><span class="ide-browser-info-level">' + esc(e.level) + "</span>" +
          '<span class="ide-browser-info-text">' + esc(e.text) + (e.source ? '<span class="ide-browser-info-sub">' + esc(e.source) + "</span>" : "") + "</span></div>";
      }).join("");
    } else if (useKind === "network") {
      rows = entries.map(function(e) {
        const bad = !e.status || e.status >= 400;
        return '<div class="ide-browser-info-row' + (bad ? " is-error" : "") + '"><span class="ide-browser-info-time">' + when(e.t) + '</span><span class="ide-browser-info-level">' + (e.status || "failed") + "</span>" +
          '<span class="ide-browser-info-text"><span class="ide-browser-info-method">' + esc(e.method) + " " + esc(e.type || "") + "</span> " + esc(e.url) + (e.error ? '<span class="ide-browser-info-sub">' + esc(e.error) + "</span>" : "") + "</span></div>";
      }).join("");
    } else if (useKind === "downloads") {
      rows = entries.map(function(e) {
        const size = e.size != null ? (e.size > 1048576 ? (e.size / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(e.size / 1024)) + " KB") : "";
        return '<div class="ide-browser-info-row' + (e.error ? " is-error" : "") + '"><span class="ide-browser-info-time">' + when(e.t) + "</span>" +
          '<span class="ide-browser-info-text">' + (e.url_local ? '<a class="ide-browser-link" href="' + esc(e.url_local) + '" download>' + esc(e.name) + "</a>" : esc(e.name)) +
          '<span class="ide-browser-info-sub">' + esc(size) + (e.error ? " · " + esc(e.error) : "") + "</span></span></div>";
      }).join("");
    } else {
      rows = entries.map(function(e) {
        return '<div class="ide-browser-info-row"><span class="ide-browser-info-time">' + when(e.t) + "</span>" +
          '<span class="ide-browser-info-text"><a class="ide-browser-link" href="#" data-info-open="' + esc(e.url) + '">' + esc(e.title || e.url) + '</a><span class="ide-browser-info-sub">' + esc(e.url) + "</span></span></div>";
      }).join("");
    }
    const empty = useKind === "downloads" ? "No downloads yet. Files a page downloads are saved with the project's browser profile."
      : useKind === "history" ? "No pages visited in this browser session yet."
      : useKind === "network" ? "No requests recorded yet." : "The console is empty.";
    list.innerHTML = rows || '<div class="ide-cookie-empty">' + empty + "</div>";
  }).catch(function(err) {
    list.innerHTML = '<div class="ide-cookie-empty">' + _livecodeEscapeHtml(err.message || String(err)) + "</div>";
  });
}


function _livecodeBrowserPollSoon(delay) {
  clearTimeout(_livecodeBrowser.pollTimer);
  if (!_livecodeBrowserVisible() || _livecodeBrowser.unavailable) return;
  _livecodeBrowser.pollTimer = setTimeout(_livecodeBrowserPoll, Math.max(0, delay || 0));
}

function _livecodeBrowserStreamUrl() {
  const params = new URLSearchParams({
    project_path: livecodeProjectPath || "",
    workspace: JSON.stringify(_livecodeCurrentWorkspacePayload()),
    t: String(Date.now()),
  });
  const box = _livecodeBrowserViewBox();
  if (box) {
    params.set("w", String(box.w));
    params.set("h", String(box.h));
    _livecodeBrowser.viewKey = box.w + "x" + box.h;
  }
  return "/livecode/browser/stream?" + params.toString();
}

function _livecodeBrowserViewBox() {
  const stage = _livecodeBrowserEl(".ide-browser-stage");
  if (!stage || !stage.clientWidth || !stage.clientHeight) return null;
  const dpr = window.devicePixelRatio || 1;
  return { w: Math.round(stage.clientWidth * dpr), h: Math.round(stage.clientHeight * dpr) };
}

function _livecodeBrowserReportView() {
  const box = _livecodeBrowserViewBox();
  if (!box || !livecodeProjectPath) return;
  const key = box.w + "x" + box.h;
  if (key === _livecodeBrowser.viewKey) return;
  _livecodeBrowser.viewKey = key;
  clearTimeout(_livecodeBrowser.viewTimer);
  _livecodeBrowser.viewTimer = setTimeout(function() {
    _livecodeBrowserPost("view", { w: box.w, h: box.h }).catch(function() {});
  }, 120);
}

function _livecodeBrowserRefreshView() {
  _livecodeBrowser.viewKey = "";
  if (_livecodeBrowser.streaming) _livecodeBrowserReportView();
}

function _livecodeBrowserSnapStage(stage, img) {
  const dpr = window.devicePixelRatio || 1;
  stage.style.transform = "";
  const rect = (img && !img.hidden ? img : stage).getBoundingClientRect();
  const dx = (Math.round(rect.left * dpr) - rect.left * dpr) / dpr;
  const dy = (Math.round(rect.top * dpr) - rect.top * dpr) / dpr;
  if (Math.abs(dx) > 0.001 || Math.abs(dy) > 0.001) {
    stage.style.transform = "translate(" + dx.toFixed(3) + "px, " + dy.toFixed(3) + "px)";
  }
}

function _livecodeBrowserWatchDpr() {
  if (typeof window.matchMedia !== "function") return;
  const query = window.matchMedia("(resolution: " + (window.devicePixelRatio || 1) + "dppx)");
  const changed = function() {
    _livecodeBrowserLayoutFrame();
    _livecodeBrowserWatchDpr();
  };
  if (query.addEventListener) query.addEventListener("change", changed, { once: true });
}

function _livecodeBrowserPageSize(img) {
  const vp = (_livecodeBrowser.state && _livecodeBrowser.state.viewport) || {};
  return {
    width: Number(img && img.dataset.vw) || vp.width || 1280,
    height: Number(img && img.dataset.vh) || vp.height || 800,
  };
}

function _livecodeBrowserStartStream() {
  const b = _livecodeBrowser;
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (!img || b.streaming || b.streamFails >= 3 || Date.now() < b.streamRetryAt) return;
  if (!_livecodeBrowserVisible() || !_livecodeBrowserHasPage() || b.unavailable) return;
  b.streaming = true;
  img.src = _livecodeBrowserStreamUrl();
}

function _livecodeBrowserStopStream() {
  const b = _livecodeBrowser;
  if (!b.streaming) return;
  b.streaming = false;
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (img) img.removeAttribute("src");
}

function _livecodeBrowserStreamFailed() {
  const b = _livecodeBrowser;
  if (!b.streaming) return;
  b.streaming = false;
  b.streamFails += 1;
  b.streamRetryAt = Date.now() + 1500 * b.streamFails;
  b.seq = -1;
  _livecodeBrowserPollSoon(0);
}

function _livecodeBrowserStreamLoaded() {
  const b = _livecodeBrowser;
  if (!b.streaming) return;
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (!img) return;
  b.streamFails = 0;
  if (img.hidden) {
    img.hidden = false;
    const frame = _livecodeBrowserEl(".browser-frame-container");
    if (frame) frame.setAttribute("data-loaded", "");
  }
  _livecodeBrowserLayoutFrame();
  if (b.compare.on && b.compare.image) _livecodeBrowserScheduleDiff();
}

function _livecodeBrowserSyncViewport(viewport) {
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (!img || !viewport || !viewport.width) return;
  if (Number(img.dataset.vw) === viewport.width && Number(img.dataset.vh) === viewport.height) return;
  img.dataset.vw = viewport.width;
  img.dataset.vh = viewport.height;
  _livecodeBrowserLayoutFrame();
}

async function _livecodeBrowserPoll() {
  if (_livecodeBrowser.polling || !_livecodeBrowserVisible()) return;
  _livecodeBrowser.polling = true;
  const project = livecodeProjectPath;
  let next = 1500;
  try {
    const streaming = _livecodeBrowser.streaming;
    const data = await _livecodeBrowserPost("frame", { since: _livecodeBrowser.seq, image: !streaming });
    if (project !== livecodeProjectPath) return;
    if (data.frame) _livecodeBrowserShowFrame(data.frame, data.viewport);
    else if (streaming) _livecodeBrowserSyncViewport(data.viewport);
    if (typeof data.seq === "number") _livecodeBrowser.seq = data.seq;
    _livecodeBrowserApplyState(data);
    if (!streaming && _livecodeBrowserHasPage()) _livecodeBrowserStartStream();
    if (streaming) {
      const shown = _livecodeBrowserEl(".ide-browser-frame");
      if (!_livecodeBrowserHasPage()) _livecodeBrowserStopStream();
      else if (shown && shown.hidden && shown.naturalWidth) _livecodeBrowserStreamLoaded();
    }
    const lively = Date.now() - _livecodeBrowser.lastInput < 5000 || _livecodeBrowserAgentActive();
    if (streaming) {
      next = data.busy || lively ? 300 : Math.min(Math.round((_livecodeBrowser.idleMs || 300) * 1.3), 1200);
      _livecodeBrowser.idleMs = next;
    } else {
      if (data.busy) next = 300;
      else if (data.frame) next = 150;
      else next = lively ? 250 : Math.min(Math.round((_livecodeBrowser.idleMs || 250) * 1.4), 1500);
      _livecodeBrowser.idleMs = data.frame ? 250 : next;
    }
  } catch (err) {
    if (err.unavailable) {
      _livecodeBrowser.unavailable = err.message;
      _livecodeBrowserRender();
      next = 0;
    } else {
      next = 3000;
    }
  } finally {
    _livecodeBrowser.polling = false;
  }
  if (next) _livecodeBrowserPollSoon(next);
}

function _livecodeBrowserShowFrame(src, viewport) {
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (!img) return;
  img.src = src;
  img.hidden = false;
  if (viewport && viewport.width) {
    img.dataset.vw = viewport.width;
    img.dataset.vh = viewport.height;
  }
  _livecodeBrowserLayoutFrame();
  const frame = _livecodeBrowserEl(".browser-frame-container");
  if (frame) frame.setAttribute("data-loaded", "");
  if (_livecodeBrowser.compare.on && _livecodeBrowser.compare.image) _livecodeBrowserScheduleDiff();
}

function _livecodeBrowserFrameKind() {
  if (_livecodeBrowser.viewportMode !== "fixed") return "fit";
  const vp = (_livecodeBrowser.state && _livecodeBrowser.state.viewport) || {};
  return vp.width && vp.width <= 500 ? "device" : "fixed";
}

function _livecodeBrowserLayoutFrame() {
  const img = _livecodeBrowserEl(".ide-browser-frame");
  const frame = _livecodeBrowserEl(".browser-frame-container");
  const stage = _livecodeBrowserEl(".ide-browser-stage");
  if (!img || !frame || !stage) return;
  const size = _livecodeBrowserPageSize(img);
  const vw = size.width, vh = size.height;
  const kind = _livecodeBrowserFrameKind();
  // Only the phone-sized device preview gets a margin; fit and fixed pages fill the pane edge to edge.
  const pad = kind === "device" ? 24 : 0;
  const scale = Math.min((frame.clientWidth - pad * 2) / vw, (frame.clientHeight - pad * 2) / vh, 1) || 1;
  stage.style.width = Math.max(1, Math.round(vw * scale)) + "px";
  stage.style.height = Math.max(1, Math.round(vh * scale)) + "px";
  stage.style.marginTop = pad + "px";
  stage.dataset.scale = String(scale);
  _livecodeBrowserSnapStage(stage, img);
  const refImg = _livecodeBrowserEl(".ide-browser-ref-img");
  if (refImg) refImg.style.width = stage.style.width;
  if (_livecodeBrowser.streaming) _livecodeBrowserReportView();
}

function _livecodeBrowserDesiredViewport(frame) {
  const pw = Math.max(1, Math.round(frame.clientWidth));
  const ph = Math.max(1, Math.round(frame.clientHeight));
  const compare = _livecodeBrowser.compare;
  if (compare.on && compare.ref) {
    const k = compare.scale || 1;
    return {
      width: Math.max(320, Math.min(2560, Math.round(compare.ref.width / k))),
      height: Math.max(320, Math.min(1600, Math.round(compare.ref.height / k))),
    };
  }
  return { width: pw, height: ph };
}

function _livecodeBrowserSyncSize(immediate) {
  clearTimeout(_livecodeBrowser.sizeTimer);
  const run = function() {
    const frame = _livecodeBrowserEl(".browser-frame-container");
    if (!frame || !_livecodeBrowserVisible() || _livecodeBrowser.unavailable) return;
    _livecodeBrowserLayoutFrame();
    if (frame.clientWidth < 50 || frame.clientHeight < 50) return;
    if (_livecodeBrowser.viewportMode === "fixed") return;
    const size = _livecodeBrowserDesiredViewport(frame);
    const key = size.width + "x" + size.height;
    if (key === _livecodeBrowser.sizeKey) return;
    _livecodeBrowser.sizeKey = key;
    _livecodeBrowserPost("action", { action: "resize", args: Object.assign({ mode: "fit" }, size) })
      .then(function(data) { _livecodeBrowserApplyState(data); _livecodeBrowserPollSoon(60); })
      .catch(function() {});
  };
  if (immediate) run();
  else _livecodeBrowser.sizeTimer = setTimeout(run, 250);
}


async function _livecodeBrowserAction(action, args) {
  if (!livecodeProjectPath) return null;
  const pendingUrlForThisCall = (action === "navigate" || action === "new_tab") ? _livecodeBrowser.pendingUrl : "";
  _livecodeBrowser.pending += 1;
  _livecodeBrowser.lastInput = Date.now();
  if (action === "navigate" || action === "new_tab") _livecodeBrowser.tookControl = true;
  _livecodeBrowserRender();
  try {
    const data = await _livecodeBrowserPost("action", { action: action, args: args || {} });
    _livecodeBrowser.unavailable = "";
    _livecodeBrowserApplyState(data);
    return data;
  } catch (err) {
    if (err.unavailable) _livecodeBrowser.unavailable = err.message;
    else _livecodeShowIdeToast(err.message || String(err));
    return null;
  } finally {
    if (pendingUrlForThisCall && _livecodeBrowser.pendingUrl === pendingUrlForThisCall) _livecodeBrowser.pendingUrl = "";
    _livecodeBrowser.pending = Math.max(0, _livecodeBrowser.pending - 1);
    _livecodeBrowserRender();
    _livecodeBrowserPollSoon(40);
  }
}

function _livecodeBrowserPoint(e, clamp) {
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (!img || img.hidden) return null;
  const rect = img.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const size = _livecodeBrowserPageSize(img);
  let x = (e.clientX - rect.left) * (size.width / rect.width);
  let y = (e.clientY - rect.top) * (size.height / rect.height);
  if (x < 0 || y < 0 || x > size.width || y > size.height) {
    if (!clamp) return null;
    x = Math.min(Math.max(x, 0), size.width - 1);
    y = Math.min(Math.max(y, 0), size.height - 1);
  }
  return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 };
}

function _livecodeBrowserStageScale() {
  const img = _livecodeBrowserEl(".ide-browser-frame");
  if (!img) return 1;
  const rect = img.getBoundingClientRect();
  return rect.width / _livecodeBrowserPageSize(img).width || 1;
}

const _LIVECODE_BROWSER_MODIFIER_KEYS = { Shift: 1, Control: 1, Alt: 1, Meta: 1, AltGraph: 1, CapsLock: 1, Fn: 1 };
const _LIVECODE_BROWSER_IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
const _LIVECODE_BROWSER_MOD = { ALT: 1, CTRL: 2, META: 4, SHIFT: 8 };
const _LIVECODE_BROWSER_MAX_UNACKED = 4;

function _livecodeBrowserMods(e) {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

function _livecodeBrowserMapShortcut(key, mods) {
  const M = _LIVECODE_BROWSER_MOD;
  const server = String((_livecodeBrowser.state && _livecodeBrowser.state.platform) || "");
  if (!server || _LIVECODE_BROWSER_IS_MAC === (server === "darwin")) return { key: key, m: mods };
  if (!_LIVECODE_BROWSER_IS_MAC) return { key: key, m: mods & M.CTRL ? (mods & ~M.CTRL) | M.META : mods };
  if (mods & M.META) {
    if (key === "ArrowLeft" || key === "ArrowRight") return { key: key === "ArrowLeft" ? "Home" : "End", m: mods & ~M.META };
    if (key === "ArrowUp" || key === "ArrowDown") return { key: key === "ArrowUp" ? "Home" : "End", m: (mods & ~M.META) | M.CTRL };
    return { key: key, m: (mods & ~M.META) | M.CTRL };
  }
  if (mods & M.ALT && /^(ArrowLeft|ArrowRight|Backspace|Delete)$/.test(key)) return { key: key, m: (mods & ~M.ALT) | M.CTRL };
  return { key: key, m: mods };
}

function _livecodeBrowserPointerMods(e) {
  return _livecodeBrowserMapShortcut("", _livecodeBrowserMods(e)).m;
}

function _livecodeBrowserInputUrl() {
  const params = new URLSearchParams({
    project_path: livecodeProjectPath || "",
    workspace: JSON.stringify(_livecodeCurrentWorkspacePayload()),
  });
  return (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/livecode/browser/input/ws?" + params.toString();
}

function _livecodeBrowserInputConnect() {
  const b = _livecodeBrowser;
  if (b.inputWs || (b.inputWsFailed || 0) >= 3 || typeof WebSocket !== "function" || !livecodeProjectPath) return;
  if (Date.now() < (b.inputWsRetryAt || 0)) return;
  let ws;
  try {
    ws = new WebSocket(_livecodeBrowserInputUrl());
  } catch (e) {
    b.inputWsFailed = (b.inputWsFailed || 0) + 1;
    return;
  }
  b.inputWs = ws;
  b.inputWsOpen = false;
  ws.onopen = function() {
    if (b.inputWs !== ws) return;
    b.inputWsOpen = true;
    b.inputWsFailed = 0;
    b.inputAcked = b.inputSeq || 0;
    _livecodeBrowserFlushInput();
  };
  ws.onmessage = function(msg) {
    if (b.inputWs !== ws) return;
    let data = null;
    try { data = JSON.parse(msg.data); } catch (e) { return; }
    _livecodeBrowserInputReply(data);
    _livecodeBrowserFlushInput();
  };
  ws.onclose = function() {
    if (b.inputWs !== ws) return;
    if (!b.inputWsOpen) b.inputWsFailed = (b.inputWsFailed || 0) + 1;
    b.inputWs = null;
    b.inputWsOpen = false;
    b.inputWsRetryAt = Date.now() + 2000;
    _livecodeBrowserFlushInput();
  };
}

function _livecodeBrowserInputClose() {
  const b = _livecodeBrowser;
  const ws = b.inputWs;
  b.inputWs = null;
  b.inputWsOpen = false;
  b.inputQueue = [];
  if (ws) {
    try { ws.close(); } catch (e) {}
  }
}

function _livecodeBrowserSend(ev) {
  const b = _livecodeBrowser;
  if (!livecodeProjectPath || !_livecodeBrowserHasPage()) return;
  b.lastInput = Date.now();
  const queue = b.inputQueue || (b.inputQueue = []);
  const last = queue[queue.length - 1];
  if (last && ev.t === "move" && last.t === "move") {
    queue[queue.length - 1] = ev;
  } else if (last && ev.t === "wheel" && last.t === "wheel" && last.m === ev.m) {
    last.dx += ev.dx;
    last.dy += ev.dy;
    last.x = ev.x;
    last.y = ev.y;
  } else {
    queue.push(ev);
  }
  if (ev.t !== "move" && ev.t !== "wheel") {
    _livecodeBrowserFlushInput();
  } else if (!b.inputFrame) {
    b.inputFrame = requestAnimationFrame(function() {
      b.inputFrame = 0;
      _livecodeBrowserFlushInput();
    });
  }
}

function _livecodeBrowserFlushInput() {
  const b = _livecodeBrowser;
  const queue = b.inputQueue || [];
  if (!queue.length) return;
  _livecodeBrowserInputConnect();
  if (b.inputWs) {
    if (!b.inputWsOpen || (b.inputSeq || 0) - (b.inputAcked || 0) >= _LIVECODE_BROWSER_MAX_UNACKED) return;
    b.inputQueue = [];
    b.inputSeq = (b.inputSeq || 0) + 1;
    queue[queue.length - 1].i = b.inputSeq;
    try {
      b.inputWs.send(JSON.stringify(queue));
    } catch (e) {
      b.inputQueue = queue.concat(b.inputQueue);
    }
    return;
  }
  if (b.inputPosting) return;
  b.inputQueue = [];
  b.inputPosting = true;
  _livecodeBrowserPost("input", { events: queue }).then(_livecodeBrowserInputReply).catch(function(err) {
    if (err.unavailable) {
      b.unavailable = err.message;
      _livecodeBrowserRender();
    }
  }).then(function() {
    b.inputPosting = false;
    _livecodeBrowserFlushInput();
  });
}

function _livecodeBrowserInputReply(data) {
  if (!data) return;
  const b = _livecodeBrowser;
  if (typeof data.i === "number") b.inputAcked = Math.max(b.inputAcked || 0, data.i);
  if (data.cursor) {
    const img = _livecodeBrowserEl(".ide-browser-frame");
    if (img) img.style.cursor = data.cursor;
  }
  if (typeof data.clipboard === "string" && data.clipboard && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(data.clipboard).catch(function() {});
  }
}

function _livecodeBrowserSendMove(point, e) {
  const b = _livecodeBrowser;
  _livecodeBrowserSend({ t: "move", x: point.x, y: point.y, m: _livecodeBrowserPointerMods(e) });
  clearTimeout(b.probeTimer);
  b.probeTimer = setTimeout(function() { _livecodeBrowserSend({ t: "probe", x: point.x, y: point.y }); }, 120);
}

function _livecodeBrowserMouseButton(e) {
  return _LIVECODE_BROWSER_IS_MAC && e.button === 0 && e.ctrlKey && !e.metaKey ? 2 : e.button;
}

function _livecodeBrowserBeginDrag() {
  const b = _livecodeBrowser;
  if (b.pointerDrag) return;
  const move = function(e) {
    const point = _livecodeBrowserPoint(e, true);
    if (point) _livecodeBrowserSendMove(point, e);
  };
  const up = function(e) {
    const button = _livecodeBrowserMouseButton(e);
    b.dragButtons &= ~(1 << button);
    const point = _livecodeBrowserPoint(e, true);
    if (point) _livecodeBrowserSend({ t: "up", x: point.x, y: point.y, b: button, n: e.detail || 1, m: _livecodeBrowserPointerMods(e) & ~(button === 2 && _LIVECODE_BROWSER_IS_MAC ? _LIVECODE_BROWSER_MOD.CTRL : 0) });
    if (!b.dragButtons) _livecodeBrowserEndDrag();
  };
  b.pointerDrag = { move: move, up: up };
  document.addEventListener("mousemove", move, true);
  document.addEventListener("mouseup", up, true);
}

function _livecodeBrowserEndDrag() {
  const b = _livecodeBrowser;
  b.dragButtons = 0;
  if (!b.pointerDrag) return;
  document.removeEventListener("mousemove", b.pointerDrag.move, true);
  document.removeEventListener("mouseup", b.pointerDrag.up, true);
  b.pointerDrag = null;
}

function _livecodeBrowserKeyDown(e) {
  const b = _livecodeBrowser;
  if (b.selecting || !_livecodeBrowserHasPage()) return;
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Dead") {
    b.deadKey = true;
    return;
  }
  const printable = Array.from(e.key || "").length === 1;
  if (b.deadKey && printable) {
    b.deadKey = false;
    return;
  }
  b.deadKey = false;
  const mods = _livecodeBrowserMods(e);
  if (_LIVECODE_BROWSER_MODIFIER_KEYS[e.key]) {
    if (e.key !== "CapsLock" && e.key !== "Fn") _livecodeBrowserSend({ t: "sync", m: _livecodeBrowserMapShortcut("", mods).m });
    return;
  }
  const accel = _LIVECODE_BROWSER_IS_MAC ? e.metaKey : e.ctrlKey;
  const letter = String(e.key || "").toLowerCase();
  if (accel && !e.altKey && !e.shiftKey) {
    if (letter === "f" || letter === "v") return;
    if (letter === "l") {
      e.preventDefault();
      const url = _livecodeBrowserEl(".url-input");
      if (url) url.focus();
      return;
    }
    if (letter === "r" || e.key === "[" || e.key === "]") {
      e.preventDefault();
      _livecodeBrowserAction(letter === "r" ? "reload" : e.key === "[" ? "back" : "forward", {});
      return;
    }
  }
  if (!_LIVECODE_BROWSER_IS_MAC && e.altKey && !e.ctrlKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    e.preventDefault();
    _livecodeBrowserAction(e.key === "ArrowLeft" ? "back" : "forward", {});
    return;
  }
  e.preventDefault();
  const altGraph = !!(e.getModifierState && e.getModifierState("AltGraph"));
  if (printable && (altGraph || (!e.ctrlKey && !e.metaKey && (!e.altKey || _LIVECODE_BROWSER_IS_MAC)))) {
    _livecodeBrowserSend({ t: "char", c: e.key, m: e.shiftKey ? _LIVECODE_BROWSER_MOD.SHIFT : 0 });
    return;
  }
  if (accel && !e.altKey && (letter === "c" || letter === "x")) _livecodeBrowserSend({ t: "copy" });
  const mapped = _livecodeBrowserMapShortcut(e.key, mods);
  _livecodeBrowserSend({ t: "key", k: mapped.key, m: mapped.m });
}

function _livecodeBrowserBindInput(view) {
  const frame = view.querySelector(".browser-frame-container");
  const img = view.querySelector(".ide-browser-frame");
  const keys = view.querySelector(".ide-browser-keys");
  img.addEventListener("mousedown", function(e) {
    e.preventDefault();
    keys.focus({ preventScroll: true });
    const point = _livecodeBrowserPoint(e);
    if (!point || _livecodeBrowser.selecting) return;
    if (e.button === 3 || e.button === 4) {
      _livecodeBrowserAction(e.button === 3 ? "back" : "forward", {});
      return;
    }
    const button = _livecodeBrowserMouseButton(e);
    _livecodeBrowser.dragButtons = (_livecodeBrowser.dragButtons || 0) | (1 << button);
    _livecodeBrowserBeginDrag();
    const mods = _livecodeBrowserPointerMods(e) & ~(button !== e.button ? _LIVECODE_BROWSER_MOD.CTRL : 0);
    _livecodeBrowserSend({ t: "down", x: point.x, y: point.y, b: button, n: e.detail || 1, m: mods });
  });
  img.addEventListener("mousemove", function(e) {
    if (_livecodeBrowser.pointerDrag || _livecodeBrowser.selecting) return;
    const point = _livecodeBrowserPoint(e);
    if (point) _livecodeBrowserSendMove(point, e);
  });
  img.addEventListener("contextmenu", function(e) { e.preventDefault(); });
  img.addEventListener("dragstart", function(e) { e.preventDefault(); });
  frame.addEventListener("wheel", function(e) {
    if (!_livecodeBrowserHasPage() || _livecodeBrowser.selecting) return;
    e.preventDefault();
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? frame.clientHeight : 1;
    const point = _livecodeBrowserPoint(e, true) || { x: 0, y: 0 };
    _livecodeBrowserSend({ t: "wheel", x: point.x, y: point.y, dx: e.deltaX * unit, dy: e.deltaY * unit, m: _livecodeBrowserPointerMods(e) });
  }, { passive: false });
  frame.addEventListener("focus", function(e) {
    if (e.target === frame && !_livecodeBrowser.selecting) keys.focus({ preventScroll: true });
  });
  keys.addEventListener("keydown", _livecodeBrowserKeyDown);
  keys.addEventListener("keyup", function(e) {
    if (_LIVECODE_BROWSER_MODIFIER_KEYS[e.key] && _livecodeBrowserHasPage()) {
      _livecodeBrowserSend({ t: "sync", m: _livecodeBrowserMapShortcut("", _livecodeBrowserMods(e)).m });
    }
  });
  keys.addEventListener("input", function(e) {
    if (e.isComposing) return;
    const text = keys.value;
    keys.value = "";
    if (text) _livecodeBrowserSend({ t: "text", s: text });
  });
  keys.addEventListener("compositionend", function(e) {
    const text = keys.value || e.data || "";
    keys.value = "";
    if (text) _livecodeBrowserSend({ t: "text", s: text });
  });
  keys.addEventListener("paste", function(e) {
    const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
    e.preventDefault();
    if (text) _livecodeBrowserSend({ t: "paste", s: text });
  });
  keys.addEventListener("blur", function() {
    _livecodeBrowserEndDrag();
    _livecodeBrowser.deadKey = false;
    if (_livecodeBrowserHasPage()) _livecodeBrowserSend({ t: "release" });
  });
}

function _livecodeBrowserHasPage() {
  return !!(_livecodeBrowser.state && (_livecodeBrowser.state.tabs || []).length);
}

function _livecodeBrowserBind(view) {
  _livecodeBrowserBindExtras(view);
  const input = view.querySelector(".url-input");
  const urlBox = view.querySelector(".ide-browser-url");
  input.addEventListener("focus", function() {
    urlBox.classList.add("is-editing");
    input.select();
  });
  input.addEventListener("blur", function() {
    urlBox.classList.remove("is-editing");
    _livecodeBrowserRender();
  });
  input.addEventListener("keydown", function(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      _livecodeBrowser.pendingUrl = value;
      input.blur();
      _livecodeBrowserAction(_livecodeBrowserHasPage() ? "navigate" : "new_tab", { url: value });
    } else if (e.key === "Escape") {
      e.preventDefault();
      _livecodeBrowser.pendingUrl = "";
      input.value = (_livecodeBrowser.state && _livecodeBrowser.state.url) || "";
      input.blur();
    }
  });

  view.addEventListener("click", function(e) {
    const target = e.target && e.target.closest ? e.target : null;
    if (!target) return;
    const nav = target.closest("[data-browser-nav]");
    if (nav) {
      e.preventDefault();
      if (!nav.disabled) _livecodeBrowserNav(nav.getAttribute("data-browser-nav"), nav);
      return;
    }
    const open = target.closest("[data-browser-open]");
    if (open) {
      e.preventDefault();
      _livecodeBrowserAction("new_tab", { url: open.getAttribute("data-browser-open") });
      return;
    }
    const closeTab = target.closest("[data-browser-close-tab]");
    if (closeTab) {
      e.preventDefault();
      e.stopPropagation();
      _livecodeBrowserAction("close_tab", { tab_id: closeTab.getAttribute("data-browser-close-tab") });
      return;
    }
    const tab = target.closest("[data-browser-tab]");
    if (tab) {
      e.preventDefault();
      _livecodeBrowserAction("switch_tab", { tab_id: tab.getAttribute("data-browser-tab") });
      return;
    }
    const item = target.closest("[data-browser-menu]");
    if (item) {
      e.preventDefault();
      _livecodeBrowserMenuAction(item.getAttribute("data-browser-menu"));
      return;
    }
    const crop = target.closest("[data-browser-crop]");
    if (crop) {
      e.preventDefault();
      _livecodeBrowserCropAction(crop.getAttribute("data-browser-crop"));
      return;
    }
    const cmp = target.closest("[data-browser-compare]");
    if (cmp) {
      e.preventDefault();
      _livecodeBrowserCompareAction(cmp.getAttribute("data-browser-compare"));
      return;
    }
    const mode = target.closest("[data-browser-compare-mode]");
    if (mode) {
      e.preventDefault();
      _livecodeBrowserSetCompareMode(mode.getAttribute("data-browser-compare-mode"));
      return;
    }
    if (!target.closest(".ide-browser-menu")) _livecodeBrowserCloseMenu();
  });

  const frame = view.querySelector(".browser-frame-container");
  const img = view.querySelector(".ide-browser-frame");
  img.addEventListener("load", function() {
    if (_livecodeBrowser.streaming) _livecodeBrowserStreamLoaded();
    else _livecodeBrowserLayoutFrame();
  });
  img.addEventListener("error", _livecodeBrowserStreamFailed);
  frame.addEventListener("keydown", function(e) {
    if (e.key === "Escape" && _livecodeBrowser.selecting) {
      e.preventDefault();
      _livecodeBrowserSetSelecting(false);
    }
  });
  _livecodeBrowserBindInput(view);

  _livecodeBrowserBindSelect(view);
  _livecodeBrowserBindCompare(view);

  document.addEventListener("visibilitychange", function() {
    if (_livecodeBrowserVisible()) _livecodeBrowserPollSoon(0);
  });
  document.addEventListener("mousedown", function(e) {
    const menu = view.querySelector(".ide-browser-menu");
    if (!menu || menu.hidden || !e.target.closest) return;
    if (!e.target.closest(".ide-browser-menu") && !e.target.closest('[data-browser-nav="menu"], [data-browser-nav="resolution"]')) _livecodeBrowserCloseMenu();
  });
  document.addEventListener("keydown", function(e) {
    if (e.key !== "Escape" || !_livecodeBrowser.maximized || !_livecodeBrowserVisible()) return;
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#ide-browser-view .browser-frame-container")) return;
    _livecodeBrowser.maximized = false;
    _livecodeBrowserRender();
    _livecodeBrowserSyncSize(false);
  });
}

function _livecodeBrowserNav(action, btn) {
  const state = _livecodeBrowser.state || {};
  if (action === "back" || action === "forward" || action === "reload") {
    _livecodeBrowserAction(action, {});
  } else if (action === "new-tab") {
    _livecodeBrowserAction("new_tab", {}).then(function() {
      const input = _livecodeBrowserEl(".url-input");
      if (input) input.focus();
    });
  } else if (action === "take-control") {
    _livecodeBrowser.tookControl = true;
    _livecodeBrowserRender();
    const keys = _livecodeBrowserEl(".ide-browser-keys");
    if (keys) keys.focus({ preventScroll: true });
  } else if (action === "cookies") {
    openLiveCodeBrowserCookies();
  } else if (action === "menu") {
    _livecodeBrowserToggleMenu(btn);
  } else if (action === "select") {
    _livecodeBrowserSetSelecting(!_livecodeBrowser.selecting);
  } else if (action === "resolution") {
    _livecodeBrowserToggleResolutionMenu(btn);
  } else if (action === "compare") {
    _livecodeBrowserSetCompare(!_livecodeBrowser.compare.on);
  } else if (action === "maximize") {
    _livecodeBrowser.maximized = !_livecodeBrowser.maximized;
    _livecodeBrowserRender();
    _livecodeBrowserSyncSize(false);
  } else if (action === "close-view") {
    _livecodeBrowser.maximized = false;
    closeFile(LIVECODE_BROWSER_TAB_KEY);
  } else if (action === "retry") {
    _livecodeBrowser.unavailable = "";
    _livecodeShowBrowserSurface();
  } else if (action === "detect") {
    _livecodeBrowser.detecting = true;
    _livecodeBrowserRender();
    fetch("/livecode/browser/dev-servers", { method: "POST" })
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        const servers = (data && data.servers) || [];
        _livecodeBrowser.devServers = servers;
        if (servers.length === 1) _livecodeBrowserAction(state.tabs && state.tabs.length ? "navigate" : "new_tab", { url: servers[0].url });
      })
      .catch(function() { _livecodeBrowser.devServers = []; })
      .finally(function() {
        _livecodeBrowser.detecting = false;
        _livecodeBrowserRender();
      });
  }
}

function _livecodeBrowserCloseMenu() {
  const menu = _livecodeBrowserEl(".ide-browser-menu");
  if (menu) {
    menu.hidden = true;
    menu.removeAttribute("data-menu");
  }
}

function _livecodeBrowserPlaceMenu(view, menu, anchor) {
  menu.hidden = false;
  const box = anchor.getBoundingClientRect();
  const host = view.getBoundingClientRect();
  menu.style.left = "";
  menu.style.top = Math.round(box.bottom - host.top + 4) + "px";
  menu.style.right = Math.round(host.right - box.right) + "px";
}

function _livecodeBrowserToggleResolutionMenu(anchor) {
  const view = _livecodeBrowserView();
  const menu = view && view.querySelector(".ide-browser-menu");
  if (!menu || !anchor) return;
  if (!menu.hidden && menu.getAttribute("data-menu") === "resolution") {
    _livecodeBrowserCloseMenu();
    return;
  }
  const vp = (_livecodeBrowser.state && _livecodeBrowser.state.viewport) || {};
  const fixed = _livecodeBrowser.viewportMode === "fixed";
  const check = function(on) {
    return '<span class="ide-browser-menu-check">' + (on ? _livecodeIcon("check", { size: "sm" }) : "") + "</span>";
  };
  const item = function(id, label, hint, checked) {
    return '<button type="button" class="ide-browser-menu-item" role="menuitemradio" aria-checked="' + !!checked + '" data-browser-menu="' + id + '">' +
      check(checked) + '<span class="ide-browser-menu-label">' + label + "</span>" +
      (hint ? '<span class="ide-browser-menu-hint">' + hint + "</span>" : "") + "</button>";
  };
  let html = item("res:fit", "Fit to tab", fixed ? "" : (vp.width ? vp.width + " × " + vp.height : ""), !fixed) + '<div class="ide-browser-menu-sep"></div>';
  let matched = false;
  _LIVECODE_BROWSER_RESOLUTIONS.forEach(function(r) {
    const on = fixed && vp.width === r.width && vp.height === r.height;
    matched = matched || on;
    html += item("res:" + r.id, r.label, r.width + " × " + r.height, on);
  });
  html += '<div class="ide-browser-menu-sep"></div>' +
    item("res:custom", "Custom size…", fixed && !matched && vp.width ? vp.width + " × " + vp.height : "", fixed && !matched) +
    (fixed && vp.width !== vp.height ? item("res:rotate", "Rotate", vp.height + " × " + vp.width, false) : "");
  menu.innerHTML = html;
  menu.setAttribute("data-menu", "resolution");
  _livecodeBrowserPlaceMenu(view, menu, anchor);
}

function _livecodeBrowserSetResolution(args) {
  return _livecodeBrowserAction("resize", Object.assign({ mode: "fixed" }, args)).then(function(data) {
    if (data && data.warning) _livecodeShowIdeToast(data.warning);
    _livecodeBrowserLayoutFrame();
    return data;
  });
}

function _livecodeBrowserResolutionAction(id) {
  const vp = (_livecodeBrowser.state && _livecodeBrowser.state.viewport) || {};
  if (id === "fit") {
    _livecodeBrowserSetResolution({ device: "fit" });
  } else if (id === "rotate") {
    if (vp.width && vp.height) _livecodeBrowserSetResolution({ width: vp.height, height: vp.width });
  } else if (id === "custom") {
    _livecodeModalPrompt({
      title: "Custom size",
      message: "Width × height in CSS pixels, e.g. 1366 × 768 (320–2560 wide, 320–1600 tall).",
      value: vp.width ? vp.width + " × " + vp.height : "",
      placeholder: "1366 × 768",
      confirmText: "Set size",
    }).then(function(text) {
      if (text == null) return;
      const m = String(text).match(/(\d{2,4})\s*[x×*, ]\s*(\d{2,4})/i);
      if (!m) {
        _livecodeShowIdeToast("Enter a size like 1366 × 768.");
        return;
      }
      _livecodeBrowserSetResolution({ width: Number(m[1]), height: Number(m[2]) });
    });
  } else {
    _livecodeBrowserSetResolution({ device: id });
  }
}

function _livecodeBrowserToggleMenu(anchor) {
  const view = _livecodeBrowserView();
  const menu = view && view.querySelector(".ide-browser-menu");
  if (!menu) return;
  if (!menu.hidden && menu.getAttribute("data-menu") === "more") {
    _livecodeBrowserCloseMenu();
    return;
  }
  const state = _livecodeBrowser.state || {};
  const hasPage = (state.tabs || []).length > 0;
  const url = state.url && state.url !== "about:blank" ? state.url : "";
  const item = function(id, label, enabled) {
    return '<button type="button" class="ide-browser-menu-item" role="menuitem" data-browser-menu="' + id + '"' + (enabled === false ? " disabled" : "") + ">" + label + "</button>";
  };
  menu.setAttribute("data-menu", "more");
  menu.innerHTML =
    item("screenshot", "Screenshot…", hasPage) +
    '<div class="ide-browser-menu-sep"></div>' +
    item("new-tab", "New tab") +
    item("copy-url", "Copy URL", !!url) +
    item("external", "Open in system browser", !!url) +
    '<div class="ide-browser-menu-sep"></div>' +
    item("find", "Find in page…", hasPage) +
    item("zoom-in", "Zoom in", hasPage) +
    item("zoom-out", "Zoom out", hasPage) +
    item("zoom-reset", "Actual size", hasPage) +
    '<div class="ide-browser-menu-sep"></div>' +
    item("downloads", "Downloads…") +
    item("history", "History…") +
    item("console", "Console and network…", hasPage) +
    '<div class="ide-browser-menu-sep"></div>' +
    item("cookies", "Import cookies…") +
    item("clear-cookies", "Clear cookies…") +
    '<div class="ide-browser-menu-sep"></div>' +
    item("close", "Close browser", hasPage);
  _livecodeBrowserPlaceMenu(view, menu, anchor);
}

function _livecodeBrowserMenuAction(id) {
  _livecodeBrowserCloseMenu();
  if (id.indexOf("res:") === 0) {
    _livecodeBrowserResolutionAction(id.slice(4));
    return;
  }
  if (id.indexOf("tab-") === 0) {
    const cut = id.indexOf(":");
    _livecodeBrowserTabMenuAction(id.slice(0, cut), id.slice(cut + 1));
    return;
  }
  const state = _livecodeBrowser.state || {};
  const url = state.url && state.url !== "about:blank" ? state.url : "";
  if (id === "find") _livecodeBrowserOpenFind();
  else if (id === "zoom-in") _livecodeBrowserZoom(1);
  else if (id === "zoom-out") _livecodeBrowserZoom(-1);
  else if (id === "zoom-reset") _livecodeBrowserZoom(0);
  else if (id === "downloads" || id === "history" || id === "console") _livecodeBrowserInfoDialog(id);
  else if (id === "screenshot") {
    const vp = state.viewport || {};
    _livecodeBrowserSetSelecting(true);
    _livecodeBrowserFinishSelection({ x: 0, y: 0, width: vp.width || 1280, height: vp.height || 800 }, "Screenshot");
  } else if (id === "new-tab") _livecodeBrowserNav("new-tab");
  else if (id === "copy-url" && url) _livecodeCopyToClipboard(url, "Copied URL");
  else if (id === "external" && url) window.open(url, "_blank", "noopener");
  else if (id === "cookies") openLiveCodeBrowserCookies();
  else if (id === "clear-cookies") _livecodeBrowserClearCookies("");
  else if (id === "close") {
    _livecodeBrowserPost("close").then(function() {
      _livecodeBrowser.seq = -1;
      _livecodeBrowser.tabsKey = "";
      _livecodeBrowserApplyState({ tabs: [], url: "", title: "", active: "" });
    }).catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
  }
}


function _livecodeBrowserSetSelecting(on) {
  const view = _livecodeBrowserView();
  if (!view || !view._livecodeBrowserBuilt) return;
  _livecodeBrowser.selecting = !!on;
  _livecodeBrowser.selection = null;
  _livecodeBrowser.hover = null;
  _livecodeBrowser.drag = null;
  view.querySelector(".ide-browser-select-layer").hidden = !on;
  view.querySelector(".ide-browser-hover-box").hidden = true;
  view.querySelector(".ide-browser-select-box").hidden = true;
  view.querySelector(".ide-browser-crop-bar").hidden = true;
  view.classList.toggle("is-selecting", !!on);
  if (on) {
    const frame = view.querySelector(".browser-frame-container");
    if (frame) frame.focus({ preventScroll: true });
  }
  _livecodeBrowserRender();
}

function _livecodeBrowserPlaceBox(box, rect) {
  const scale = _livecodeBrowserStageScale();
  box.style.left = Math.round(rect.x * scale) + "px";
  box.style.top = Math.round(rect.y * scale) + "px";
  box.style.width = Math.max(1, Math.round(rect.width * scale)) + "px";
  box.style.height = Math.max(1, Math.round(rect.height * scale)) + "px";
  box.hidden = false;
}

function _livecodeBrowserInspectAt(point) {
  clearTimeout(_livecodeBrowser.inspectTimer);
  _livecodeBrowser.inspectTimer = setTimeout(function() {
    const seq = ++_livecodeBrowser.inspectSeq;
    _livecodeBrowserPost("action", { action: "inspect_point", args: point }).then(function(data) {
      if (seq !== _livecodeBrowser.inspectSeq || !_livecodeBrowser.selecting || _livecodeBrowser.drag || _livecodeBrowser.selection) return;
      const el = data && data.element;
      const box = _livecodeBrowserEl(".ide-browser-hover-box");
      if (!el || !el.rect || el.rect.width < 1 || el.rect.height < 1) {
        _livecodeBrowser.hover = null;
        box.hidden = true;
        return;
      }
      _livecodeBrowser.hover = el;
      _livecodeBrowserPlaceBox(box, el.rect);
      box.querySelector(".ide-browser-hover-label").textContent = el.label + "  " + el.rect.width + "×" + el.rect.height;
    }).catch(function() {});
  }, 70);
}

function _livecodeBrowserFinishSelection(rect, label) {
  const view = _livecodeBrowserView();
  _livecodeBrowser.selection = rect;
  _livecodeBrowser.selectionLabel = label;
  view.querySelector(".ide-browser-hover-box").hidden = true;
  _livecodeBrowserPlaceBox(view.querySelector(".ide-browser-select-box"), rect);
  const bar = view.querySelector(".ide-browser-crop-bar");
  bar.querySelector(".ide-browser-crop-label").textContent = label + " · " + rect.width + "×" + rect.height;
  bar.hidden = false;
}

function _livecodeBrowserBindSelect(view) {
  const layer = view.querySelector(".ide-browser-select-layer");
  const toPage = function(e) {
    const rect = layer.getBoundingClientRect();
    const scale = _livecodeBrowserStageScale();
    return { x: (e.clientX - rect.left) / scale, y: (e.clientY - rect.top) / scale };
  };
  layer.addEventListener("mousedown", function(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    if (_livecodeBrowser.selection) {
      _livecodeBrowser.selection = null;
      view.querySelector(".ide-browser-select-box").hidden = true;
      view.querySelector(".ide-browser-crop-bar").hidden = true;
    }
    _livecodeBrowser.drag = { start: toPage(e), moved: false };
  });
  layer.addEventListener("mousemove", function(e) {
    const p = toPage(e);
    const drag = _livecodeBrowser.drag;
    if (drag) {
      if (Math.abs(p.x - drag.start.x) + Math.abs(p.y - drag.start.y) > 4) drag.moved = true;
      if (!drag.moved) return;
      view.querySelector(".ide-browser-hover-box").hidden = true;
      _livecodeBrowserPlaceBox(view.querySelector(".ide-browser-select-box"), {
        x: Math.min(p.x, drag.start.x), y: Math.min(p.y, drag.start.y),
        width: Math.abs(p.x - drag.start.x), height: Math.abs(p.y - drag.start.y),
      });
      return;
    }
    if (!_livecodeBrowser.selection) _livecodeBrowserInspectAt({ x: Math.round(p.x), y: Math.round(p.y) });
  });
  const finish = function(e) {
    const drag = _livecodeBrowser.drag;
    if (!drag) return;
    _livecodeBrowser.drag = null;
    const p = toPage(e);
    if (drag.moved) {
      const rect = {
        x: Math.round(Math.min(p.x, drag.start.x)), y: Math.round(Math.min(p.y, drag.start.y)),
        width: Math.round(Math.abs(p.x - drag.start.x)), height: Math.round(Math.abs(p.y - drag.start.y)),
      };
      if (rect.width >= 4 && rect.height >= 4) _livecodeBrowserFinishSelection(rect, "Region");
      else view.querySelector(".ide-browser-select-box").hidden = true;
    } else if (_livecodeBrowser.hover) {
      _livecodeBrowserFinishSelection(_livecodeBrowser.hover.rect, _livecodeBrowser.hover.label);
    }
  };
  layer.addEventListener("mouseup", finish);
  layer.addEventListener("mouseleave", function(e) {
    if (_livecodeBrowser.drag) finish(e);
    else if (!_livecodeBrowser.selection) view.querySelector(".ide-browser-hover-box").hidden = true;
  });
}

function _livecodeAttachImageToChat(blob, name) {
  const input = document.getElementById("livecode-attach-file-input");
  if (!input || typeof DataTransfer !== "function") return false;
  try {
    const transfer = new DataTransfer();
    transfer.items.add(new File([blob], name, { type: blob.type || "image/jpeg" }));
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  } catch (e) {
    return false;
  }
}

async function _livecodeBrowserCropAction(kind) {
  if (kind === "cancel") {
    _livecodeBrowserSetSelecting(false);
    return;
  }
  const sel = _livecodeBrowser.selection;
  if (!sel) return;
  const data = await _livecodeBrowserAction("crop", { x: sel.x, y: sel.y, width: sel.width, height: sel.height });
  if (!data || !data.shot_url) return;
  let blob;
  try {
    blob = await (await fetch(data.shot_url)).blob();
  } catch (e) {
    _livecodeShowIdeToast("Could not read the cropped image.");
    return;
  }
  const name = "browser-" + ((_livecodeBrowserUrlParts(data.url).host || "page").replace(/[^\w.-]+/g, "-")) + "-" + sel.width + "x" + sel.height + ".jpg";
  if (kind === "chat") {
    if (_livecodeAttachImageToChat(blob, name)) {
      _livecodeShowIdeToast("Added the crop to the chat");
      if (typeof focusLiveCodeComposer === "function") focusLiveCodeComposer();
    } else {
      _livecodeOpenImageLightbox(data.shot_url, name, sel.width + " × " + sel.height);
      _livecodeShowIdeToast("Attaching images is not available here; the crop is open instead.");
    }
  } else if (kind === "copy") {
    try {
      const bitmap = await createImageBitmap(blob);
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      const png = await new Promise(function(resolve) { canvas.toBlob(resolve, "image/png"); });
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      _livecodeShowIdeToast("Copied the crop");
    } catch (e) {
      _livecodeShowIdeToast("The clipboard refused the image.");
      return;
    }
  } else if (kind === "design") {
    const src = await new Promise(function(resolve) {
      const reader = new FileReader();
      reader.onload = function() { resolve(String(reader.result || "")); };
      reader.onerror = function() { resolve(""); };
      reader.readAsDataURL(blob);
    });
    if (!src) {
      _livecodeShowIdeToast("Could not read the cropped image.");
      return;
    }
    _livecodeBrowserUseReference(src, name);
    _livecodeShowIdeToast("The agent compares pages with this design now");
  } else if (kind === "save") {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.appendChild(link);
    link.click();
    setTimeout(function() { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
  }
  _livecodeBrowserSetSelecting(false);
}


function _livecodeBrowserSetCompare(on) {
  const compare = _livecodeBrowser.compare;
  compare.on = !!on;
  if (compare.on && !compare.loaded) {
    compare.loaded = true;
    _livecodeBrowserPost("reference", { get: true }).then(function(data) {
      if (data && data.image && !compare.ref) _livecodeBrowserUseReference(data.image, data.name || "reference", { upload: false });
    }).catch(function() {});
  }
  _livecodeBrowserRender();
  _livecodeBrowserSyncSize(true);
  if (compare.on) _livecodeBrowserScheduleDiff();
}

function _livecodeBrowserUseReference(src, name, opts) {
  const compare = _livecodeBrowser.compare;
  const image = new Image();
  image.onload = function() {
    compare.image = image;
    compare.ref = { src: src, name: name || "reference", width: image.naturalWidth, height: image.naturalHeight };
    compare.scale = image.naturalWidth > 2000 ? 2 : 1;
    const select = _livecodeBrowserEl(".ide-browser-scale");
    if (select) select.value = String(compare.scale);
    const refImg = _livecodeBrowserEl(".ide-browser-ref-img");
    const overlay = _livecodeBrowserEl(".ide-browser-overlay-ref");
    if (refImg) refImg.src = src;
    if (overlay) overlay.src = src;
    _livecodeBrowserRender();
    _livecodeBrowserSyncSize(true);
    _livecodeBrowserScheduleDiff();
    if (!opts || opts.upload !== false) {
      _livecodeBrowserPost("reference", { image: src, name: compare.ref.name }).catch(function(err) {
        _livecodeShowIdeToast("The agent can't see this reference: " + (err.message || err));
      });
    }
  };
  image.onerror = function() { _livecodeShowIdeToast("That file is not an image the browser can show."); };
  image.src = src;
}

function _livecodeBrowserReadReferenceFile(file) {
  if (!file || !/^image\//.test(file.type || "")) {
    _livecodeShowIdeToast("Choose an image file.");
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    _livecodeShowIdeToast("That image is larger than 20 MB.");
    return;
  }
  const reader = new FileReader();
  reader.onload = function() { _livecodeBrowserUseReference(String(reader.result || ""), file.name); };
  reader.readAsDataURL(file);
}

function _livecodeBrowserCompareAction(action) {
  const compare = _livecodeBrowser.compare;
  if (action === "choose") {
    const input = _livecodeBrowserEl(".ide-browser-ref-input");
    if (input) input.click();
  } else if (action === "clear") {
    compare.ref = null;
    compare.image = null;
    compare.match = null;
    _livecodeBrowserPost("reference", { clear: true }).catch(function() {});
    _livecodeBrowserRender();
    _livecodeBrowserSyncSize(true);
  } else if (action === "ask") {
    if (!compare.ref) {
      _livecodeShowIdeToast("Add a reference image first.");
      return;
    }
    const prompt = "Make the page in the Browser tab match the reference image in its compare view, then check it with the browser's compare action.";
    if (typeof _livecodeComposerHasDraft === "function" && _livecodeComposerHasDraft()) {
      _livecodeShowIdeToast("The reference is ready: ask the agent to compare the page with it.");
      if (typeof focusLiveCodeComposer === "function") focusLiveCodeComposer();
    } else {
      _livecodeSetComposerText(prompt);
    }
  }
}

function _livecodeBrowserSetCompareMode(mode) {
  const compare = _livecodeBrowser.compare;
  compare.mode = mode === "overlay" || mode === "diff" ? mode : "side";
  _livecodeBrowserRender();
  _livecodeBrowserSyncSize(true);
  _livecodeBrowserScheduleDiff();
}

function _livecodeBrowserBindCompare(view) {
  const input = view.querySelector(".ide-browser-ref-input");
  input.addEventListener("change", function() {
    const file = input.files && input.files[0];
    input.value = "";
    if (file) _livecodeBrowserReadReferenceFile(file);
  });
  const pane = view.querySelector(".ide-browser-ref-pane");
  pane.addEventListener("dragover", function(e) {
    e.preventDefault();
    pane.classList.add("is-dragover");
  });
  pane.addEventListener("dragleave", function() { pane.classList.remove("is-dragover"); });
  pane.addEventListener("drop", function(e) {
    e.preventDefault();
    pane.classList.remove("is-dragover");
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) _livecodeBrowserReadReferenceFile(file);
  });
  pane.addEventListener("paste", function(e) {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind === "file" && /^image\//.test(items[i].type)) {
        e.preventDefault();
        _livecodeBrowserReadReferenceFile(items[i].getAsFile());
        return;
      }
    }
  });
  view.querySelector(".ide-browser-opacity").addEventListener("input", function(e) {
    _livecodeBrowser.compare.opacity = Number(e.target.value) || 0;
    const overlay = view.querySelector(".ide-browser-overlay-ref");
    overlay.style.opacity = String(_livecodeBrowser.compare.opacity / 100);
  });
  view.querySelector(".ide-browser-scale").addEventListener("change", function(e) {
    _livecodeBrowser.compare.scale = Number(e.target.value) || 1;
    _livecodeBrowserSyncSize(true);
    _livecodeBrowserScheduleDiff();
  });
}

function _livecodeBrowserRenderCompare(view) {
  const compare = _livecodeBrowser.compare;
  const bar = view.querySelector(".ide-browser-compare-bar");
  const pane = view.querySelector(".ide-browser-ref-pane");
  const overlay = view.querySelector(".ide-browser-overlay-ref");
  const diff = view.querySelector(".ide-browser-diff");
  view.classList.toggle("is-comparing", compare.on);
  view.querySelector(".browser-frame-container").classList.toggle("is-diff", !!(compare.on && compare.ref && compare.mode === "diff"));
  view.setAttribute("data-compare-mode", compare.on ? compare.mode : "");
  bar.hidden = !compare.on;
  pane.hidden = !(compare.on && (compare.mode === "side" || !compare.ref));
  const refImg = pane.querySelector(".ide-browser-ref-img");
  refImg.hidden = !compare.ref;
  pane.querySelector(".ide-browser-ref-drop").hidden = !!compare.ref;
  overlay.hidden = !(compare.on && compare.ref && compare.mode === "overlay");
  overlay.style.opacity = String(compare.opacity / 100);
  diff.hidden = !(compare.on && compare.ref && compare.mode === "diff");
  if (!compare.on) return;
  bar.querySelector(".ide-browser-compare-name").textContent = compare.ref
    ? compare.ref.name + " · " + compare.ref.width + "×" + compare.ref.height
    : "No reference image";
  bar.querySelector('[data-browser-compare="clear"]').hidden = !compare.ref;
  bar.querySelector(".ide-browser-opacity").hidden = compare.mode !== "overlay" || !compare.ref;
  bar.querySelector(".ide-browser-scale-label").hidden = !compare.ref;
  bar.querySelectorAll("[data-browser-compare-mode]").forEach(function(btn) {
    const on = btn.getAttribute("data-browser-compare-mode") === compare.mode;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.disabled = !compare.ref;
  });
  const match = bar.querySelector(".ide-browser-match");
  match.textContent = compare.ref && compare.match != null ? compare.match + "% match" : "";
  match.classList.toggle("is-good", compare.match != null && compare.match >= 98);
}

function _livecodeBrowserScheduleDiff() {
  const compare = _livecodeBrowser.compare;
  if (!compare.on || !compare.image) return;
  if (compare.diffTimer) return;
  compare.diffTimer = setTimeout(function() {
    compare.diffTimer = null;
    _livecodeBrowserComputeDiff();
  }, 300);
}

function _livecodeBrowserComputeDiff() {
  const compare = _livecodeBrowser.compare;
  const img = _livecodeBrowserEl(".ide-browser-frame");
  const canvas = _livecodeBrowserEl(".ide-browser-diff");
  if (!compare.on || !compare.image || !img || img.hidden || !img.naturalWidth || !canvas) return;
  const W = img.naturalWidth, H = img.naturalHeight;
  const ref = compare.image;
  const refH = Math.max(1, Math.round(ref.naturalHeight * W / ref.naturalWidth));
  const h = Math.min(H, refH);
  const s = Math.min(1, 720 / W);
  const cw = Math.max(1, Math.round(W * s)), ch = Math.max(1, Math.round(h * s));
  const draw = function(source, sh) {
    const c = document.createElement("canvas");
    c.width = cw;
    c.height = ch;
    const x = c.getContext("2d", { willReadFrequently: true });
    x.imageSmoothingQuality = "high";
    x.drawImage(source, 0, 0, source === ref ? ref.naturalWidth : W, sh, 0, 0, cw, ch);
    return x.getImageData(0, 0, cw, ch).data;
  };
  let A, B;
  try {
    A = draw(ref, Math.round(h * ref.naturalWidth / W));
    B = draw(img, h);
  } catch (e) {
    return;
  }
  const limit = 35215 * _LIVECODE_BROWSER_DIFF_THRESHOLD * _LIVECODE_BROWSER_DIFF_THRESHOLD;
  const delta = function(P, i, Q, j) {
    const r = P[i] - Q[j], g = P[i + 1] - Q[j + 1], b = P[i + 2] - Q[j + 2];
    const y = 0.29889531 * r + 0.58662247 * g + 0.11448223 * b;
    const iq = 0.59597799 * r - 0.2741761 * g - 0.32180189 * b;
    const q = 0.21147017 * r - 0.52261711 * g + 0.31114694 * b;
    return 0.5053 * y * y + 0.299 * iq * iq + 0.1957 * q * q;
  };
  const unmatched = function(P, Q, x, y, i) {
    for (let dy = -1; dy <= 1; dy++) {
      const yy = y + dy;
      if (yy < 0 || yy >= ch) continue;
      for (let dx = -1; dx <= 1; dx++) {
        const xx = x + dx;
        if (xx < 0 || xx >= cw) continue;
        if (delta(P, i, Q, (yy * cw + xx) * 4) <= limit) return false;
      }
    }
    return true;
  };
  canvas.width = cw;
  canvas.height = ch;
  const ctx = canvas.getContext("2d");
  const out = ctx.createImageData(cw, ch);
  let differ = 0;
  for (let y = 0; y < ch; y++) {
    for (let x = 0; x < cw; x++) {
      const i = (y * cw + x) * 4;
      if (delta(A, i, B, i) > limit && unmatched(A, B, x, y, i) && unmatched(B, A, x, y, i)) {
        differ++;
        out.data[i] = 255; out.data[i + 1] = 45; out.data[i + 2] = 140; out.data[i + 3] = 255;
      }
    }
  }
  ctx.putImageData(out, 0, 0);
  canvas.style.height = (100 * h / H) + "%";
  compare.match = differ ? Math.min(99.9, Math.floor(1000 - 1000 * differ / (cw * ch)) / 10) : 100;
  _livecodeBrowserRenderCompare(_livecodeBrowserView());
}


function _livecodeBrowserAgentActivity(done) {
  _livecodeBrowser.agentUntil = Date.now() + (done ? 4000 : 20000);
  if (!done) _livecodeBrowser.tookControl = false;
  if (!_livecodeBrowserTabInfo() && livecodeProjectPath) {
    window.openLiveCodeBrowser();
    return;
  }
  if (_livecodeBrowserVisible()) {
    _livecodeBrowserRender();
    _livecodeBrowserPollSoon(done ? 30 : 200);
  }
}

function _livecodeBrowserShortUrl(url) {
  const text = String(url || "").trim();
  if (!text) return "";
  const bare = text.replace(/^https?:\/\//, "").replace(/\/$/, "");
  return bare.length > 60 ? bare.slice(0, 57) + "…" : bare;
}

function _livecodeBrowserTarget(a, r) {
  if (r && r.clicked) return String(r.clicked);
  if (r && r.typed_into) return String(r.typed_into);
  if (a.ref != null && a.ref !== "") return "[" + a.ref + "]";
  if (a.selector) return String(a.selector);
  if (a.text) return "“" + String(a.text).slice(0, 40) + "”";
  return "";
}

const _LIVECODE_BROWSER_FAILED_VERBS = {
  "Navigated to": "Navigate to", "Took snapshot": "Take snapshot", "Took screenshot": "Take screenshot",
  Cropped: "Crop", "Compared with": "Compare with", Inspected: "Inspect", "Resized browser to": "Resize browser to",
  "Fit browser to tab": "Fit browser to tab", "Took screenshot of tab": "Take screenshot of tab", "Opened background tab": "Open background tab",
  "Loaded Figma design": "Load Figma design", "Read Figma layer": "Read Figma layer", "Opened Figma link in tab": "Open Figma link in tab",
  "Scrolled to": "Scroll to",
  Clicked: "Click", "Clicked on page": "Click on page", Typed: "Type", Pressed: "Press", Scrolled: "Scroll",
  "Waited for": "Wait for", Waited: "Wait", "Ran page script": "Run page script", "Navigated back": "Navigate back",
  "Navigated forward": "Navigate forward", "Reloaded page": "Reload page", "Opened new tab": "Open new tab",
  "Switched tab": "Switch tab", "Closed tab": "Close tab",
  Hovered: "Hover", Dragged: "Drag", Uploaded: "Upload", Selected: "Select", Checked: "Check", Unchecked: "Uncheck",
  "Searched page for": "Search page for", "Zoomed page to": "Zoom page to", "Read console": "Read console",
  "Read network requests": "Read network requests", "Checked downloads": "Check downloads",
};

function _livecodeBrowserParts(args, result) {
  const a = args || {};
  const r = result || null;
  const action = String(a.action || (r && r.action) || "").toLowerCase();
  let verb = "Used browser";
  let detail = "";
  if (action === "navigate") {
    verb = "Navigated to";
    detail = _livecodeBrowserShortUrl(a.url || (r && r.url));
  } else if (action === "snapshot") {
    verb = "Took snapshot";
  } else if (action === "screenshot") {
    verb = a.tab_id ? "Took screenshot of tab" : "Took screenshot";
    detail = (a.tab_id ? String(a.tab_id) + (a.full_page ? " · full page" : "") : (a.full_page ? "of the full page" : ""));
  } else if (action === "crop") {
    verb = "Cropped";
    const image = String(a.image || a.reference || "");
    if ((r && r.source === "image") || (image && !/^tab:/i.test(image))) detail = (r && r.target) || "part of " + image;
    else detail = (image ? "tab " + image.slice(4) + " · " : "") + ((r && r.target) || _livecodeBrowserTarget(a, null) || (a.width ? Math.round(a.width) + "×" + Math.round(a.height) + " region" : ""));
  } else if (action === "compare") {
    verb = "Compared with";
    const what = _livecodeBrowserTarget(a, null) || (a.full_page ? "full page" : "");
    detail = ((r && r.reference) || (a.reference ? String(a.reference) : "the design")) + (what ? " · " + what : "") +
      (r && r.similarity != null ? " · " + r.similarity + "% match" + (r.verdict ? ", " + r.verdict : "") : "");
  } else if (action === "inspect") {
    verb = "Inspected";
    detail = _livecodeBrowserTarget(a, null) || (r && r.target && r.target !== "page" ? r.target : "") || "page layout";
  } else if (action === "resize") {
    const device = String(a.device || (r && r.device) || "");
    if (device === "fit") {
      verb = "Fit browser to tab";
    } else {
      verb = "Resized browser to";
      const vp = (r && r.viewport) || {};
      detail = vp.width ? vp.width + "×" + vp.height : (a.width ? Math.round(a.width) + "×" + Math.round(a.height || 0) : device);
      if (device && vp.width) detail += " (" + device + ")";
    }
  } else if (action === "figma") {
    const layer = (r && r.layer) || null;
    const design = (r && r.design) || {};
    if (r && r.fallback) {
      verb = "Opened Figma link in tab";
      detail = String(r.tab_id || "") + (r.reason ? " · " + r.reason : "");
    } else if (a.node && !a.url) {
      verb = "Read Figma layer";
      detail = (layer && layer.name) || String(a.node);
    } else {
      verb = "Loaded Figma design";
      detail = design.name ? design.name + (design.width ? " · " + design.width + "×" + design.height : "") + (r && r.cached ? " · reused" : "") : "";
    }
  } else if (action === "click") {
    const atPoint = a.x != null && a.y != null && (a.ref == null || a.ref === "") && !a.selector && !a.text;
    verb = atPoint ? "Clicked on page" : "Clicked";
    detail = atPoint ? "" : _livecodeBrowserTarget(a, r);
  } else if (action === "type") {
    verb = "Typed";
    const text = String(a.text || "");
    const shown = text.length > 20 ? text.slice(0, 17) + "..." : text;
    const into = (r && r.typed_into) || (a.ref != null && a.ref !== "" ? "[" + a.ref + "]" : a.selector || "");
    detail = shown ? '"' + shown + '"' + (into ? " into " + into : "") : "";
  } else if (action === "press") {
    verb = "Pressed";
    detail = String(a.key || a.text || "");
  } else if (action === "scroll") {
    const target = _livecodeBrowserTarget(a, null) || (r && r.target) || "";
    if (target || a.to || a.to_y != null) {
      verb = "Scrolled to";
      detail = target || (a.to ? String(a.to) : "y " + a.to_y);
    } else {
      verb = "Scrolled";
      detail = String(a.direction || "down");
    }
  } else if (action === "wait") {
    if (a.text || a.selector) {
      verb = "Waited for";
      detail = a.text ? '"' + String(a.text).slice(0, 25) + '"' : String(a.selector);
    } else {
      verb = "Waited";
      detail = a.seconds ? a.seconds + "s" : "";
    }
  } else if (action === "javascript_exec") {
    verb = "Ran page script";
  } else if (action === "hover") {
    verb = "Hovered";
    detail = _livecodeBrowserTarget(a, r) || (r && r.hovered) || "";
  } else if (action === "drag") {
    verb = "Dragged";
    detail = (r && r.dragged) || _livecodeBrowserTarget(a, r);
  } else if (action === "upload") {
    verb = "Uploaded";
    detail = ((r && r.uploaded) || a.files || []).join(", ");
  } else if (action === "select") {
    verb = "Selected";
    detail = (r && r.selected) || String(a.value || "");
  } else if (action === "check") {
    verb = a.checked === false ? "Unchecked" : "Checked";
    detail = _livecodeBrowserTarget(a, r);
  } else if (action === "dialog") {
    verb = a.accept === false ? "Will dismiss the next dialog" : "Will accept the next dialog";
  } else if (action === "find") {
    verb = "Searched page for";
    detail = '"' + String(a.text || a.query || "").slice(0, 30) + '"' + (r && r.find ? " · " + r.find.count + " match" + (r.find.count === 1 ? "" : "es") : "");
  } else if (action === "zoom") {
    verb = "Zoomed page to";
    detail = Math.round(((r && r.zoom) || Number(a.level) || 1) * 100) + "%";
  } else if (action === "console") {
    verb = "Read console";
    detail = r && r.count != null ? r.count + " message" + (r.count === 1 ? "" : "s") : "";
  } else if (action === "network") {
    verb = "Read network requests";
    detail = String(a.filter || a.status || a.type || "");
  } else if (action === "downloads") {
    verb = "Checked downloads";
  } else if (action === "batch") {
    verb = "Ran";
    detail = ((a.actions || []).length || ((r && r.steps) || []).length) + " browser actions";
  } else if (action === "back") {
    verb = "Navigated back";
  } else if (action === "forward") {
    verb = "Navigated forward";
  } else if (action === "reload") {
    verb = "Reloaded page";
  } else if (action === "tabs") {
    verb = "Browser tabs";
  } else if (action === "new_tab") {
    verb = a.background ? "Opened background tab" : "Opened new tab";
    detail = _livecodeBrowserShortUrl(a.url);
  } else if (action === "switch_tab") {
    verb = "Switched tab";
    detail = String(a.tab_id || "");
  } else if (action === "close_tab") {
    verb = "Closed tab";
  }
  if (r && r.error) verb = _LIVECODE_BROWSER_FAILED_VERBS[verb] || verb;
  return {
    verb: verb,
    detail: detail,
    meta: "",
    kind: "browser",
    isBrowser: true,
    browserAction: action,
    browserArgs: a,
    browserResult: r,
    isError: !!(r && r.error),
  };
}

let _livecodeMonacoLoading = null;
let _livecodeScriptHighlightBound = false;

function _livecodeCurrentMonacoThemeName() {
  const cls = ["dark", "white", "pink", "black"].filter(function(name) { return document.body && document.body.classList.contains(name + "-theme"); })[0];
  return cls || localStorage.getItem("livecode-theme") || "dark";
}

function _livecodeLoadMonaco() {
  if (window.monaco && window.monaco.editor) return Promise.resolve(window.monaco);
  if (_livecodeMonacoLoading) return _livecodeMonacoLoading;
  if (typeof require === "undefined" || typeof require.config !== "function") return Promise.resolve(null);
  _livecodeMonacoLoading = new Promise(function(resolve) {
    try {
      require.config({ paths: { vs: window.LIVECODE_MONACO_VS || "/templates/js/monaco-editor/vs" } });
      require(["vs/editor/editor.main"], function() {
        if (typeof window.installLivecodeMonacoDefaults === "function") window.installLivecodeMonacoDefaults();
        if (typeof window.applyIDEDynamicTheme === "function") window.applyIDEDynamicTheme(_livecodeCurrentMonacoThemeName());
        resolve(window.monaco || null);
      }, function() { resolve(null); });
    } catch (_) {
      resolve(null);
    }
  });
  return _livecodeMonacoLoading;
}

function _livecodeHighlightScripts(root) {
  root.querySelectorAll(".livecode-browser-script-code:not([data-hl])").forEach(function(el) {
    el.setAttribute("data-hl", "1");
    const text = el.textContent;
    _livecodeLoadMonaco().then(function(m) {
      return m ? m.editor.colorize(text, el.getAttribute("data-lang") || "javascript", { tabSize: 2 }) : null;
    }).then(function(html) {
      if (!html || !el.isConnected) return;
      el.innerHTML = html;
      el.classList.add("is-highlighted");
    }).catch(function() {});
  });
}

function _livecodeBindScriptHighlight() {
  if (_livecodeScriptHighlightBound || !document.body) return;
  _livecodeScriptHighlightBound = true;
  let queued = false;
  new MutationObserver(function() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function() {
      queued = false;
      _livecodeHighlightScripts(document);
    });
  }).observe(document.body, { childList: true, subtree: true });
  new MutationObserver(function() {
    if (window.monaco && typeof window.applyIDEDynamicTheme === "function") window.applyIDEDynamicTheme(_livecodeCurrentMonacoThemeName());
  }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
}

function _livecodeBrowserArgsHtml(a) {
  a = a || {};
  const isScript = a.action === "javascript_exec";
  const script = isScript ? String(a.script || a.text || "") : "";
  const shown = {};
  Object.keys(a).forEach(function(key) {
    const value = a[key];
    if (key === "action" || (isScript && (key === "script" || key === "text"))) return;
    if (value !== undefined && value !== null && value !== "") shown[key] = value;
  });
  const json = Object.keys(shown).length ? '<pre class="livecode-mcp-call-args">' + _livecodeHighlightJson(JSON.stringify(shown, null, 2)) + "</pre>" : "";
  const code = script.trim()
    ? '<div class="livecode-browser-script"><div class="livecode-browser-script-label">Script run in the page</div>' +
      '<div class="monaco-editor livecode-browser-script-shell"><pre class="livecode-mcp-call-args livecode-browser-script-code" data-lang="javascript">' + _livecodeEscapeHtml(script) + "</pre></div></div>"
    : "";
  if (code) _livecodeBindScriptHighlight();
  return json + code;
}

function _livecodeBuildBrowserStepHtml(p, errClass, running) {
  const a = p.browserArgs || {};
  const r = running ? null : (p.browserResult || null);
  const action = p.browserAction || "";
  const verb = running ? _livecodeRunningVerb(p.verb) : p.verb;
  let result = "";
  if (r && !r.error) {
    if (action === "batch" && r.steps && r.steps.length) {
      result = '<div class="livecode-browser-kv">' + r.steps.map(function(step) {
        return '<span class="livecode-browser-kv-key">' + step.step + ". " + _livecodeEscapeHtml(String(step.action || "")) + '</span><span class="livecode-browser-kv-val">' +
          _livecodeEscapeHtml(String(step.detail || step.url || "") + (step.ok ? "" : " — failed: " + (step.error || ""))) + "</span>";
      }).join("") + "</div>";
    } else if (["console", "network", "find", "downloads", "javascript_exec"].indexOf(action) >= 0 && r.result != null && r.result !== "") {
      result = '<pre class="livecode-browser-step-output">' + _livecodeEscapeHtml(String(r.result)) + "</pre>";
    } else if (action === "compare" && r.similarity != null) {
      result = _livecodeBrowserCompareHtml(r);
    } else if (action === "inspect" && (r.outline || r.element)) {
      result = _livecodeBrowserInspectHtml(r);
    } else if (action === "figma" && (r.outline || r.design)) {
      result = _livecodeBrowserFigmaHtml(r);
    } else if (action === "figma" && r.fallback && r.hint) {
      result = '<pre class="livecode-browser-step-output">' + _livecodeEscapeHtml(String(r.hint)) + "</pre>";
    } else if (action === "resize" && r.viewport) {
      const fixed = r.viewport_mode === "fixed";
      const rows = [
        ["Viewport", r.viewport.width + " × " + r.viewport.height],
        ["Mode", fixed ? "Fixed" + (r.device && r.device !== "fit" ? " · " + r.device : "") : "Fits the Browser tab"],
      ];
      if (r.page_height) rows.push(["Page height", r.page_height + " px"]);
      result = '<div class="livecode-browser-kv">' + rows.map(function(row) {
        return '<span class="livecode-browser-kv-key">' + _livecodeEscapeHtml(row[0]) + '</span><span class="livecode-browser-kv-val">' + _livecodeEscapeHtml(row[1]) + "</span>";
      }).join("") +
        (r.warning ? '<span class="livecode-browser-kv-note is-warning">' + _livecodeEscapeHtml(String(r.warning)) + "</span>" : "") +
        (r.note ? '<span class="livecode-browser-kv-note">' + _livecodeEscapeHtml(String(r.note)) + "</span>" : "") + "</div>";
    } else if (r.snapshot) {
      const snap = String(r.snapshot);
      result = '<pre class="livecode-browser-step-output">' + _livecodeEscapeHtml(snap.length > 8000 ? snap.slice(0, 8000) + "\n…" : snap) + "</pre>";
    } else if (r.url && ["screenshot", "crop", "compare", "inspect", "resize", "figma"].indexOf(action) < 0) {
      result = '<div class="livecode-browser-step-page"><span class="livecode-browser-step-page-title">' + _livecodeEscapeHtml(r.title || "") + '</span><span class="livecode-browser-step-page-url">' + _livecodeEscapeHtml(r.url) + "</span></div>";
    }
  }
  const argHtml = _livecodeBrowserArgsHtml(a);
  const hasBody = !!(argHtml || result);
  const status = !running && r && p.isError
    ? '<span class="livecode-mcp-call-status is-error" aria-hidden="true">' + _LIVECODE_MCP_ERROR_SVG + "</span>"
    : "";
  const label = (hasBody ? _LIVECODE_MCP_CALL_CHEVRON_SVG : "") +
    '<span class="livecode-activity-text' + (running ? " ui-shimmer" : "") + '">' +
    '<span class="livecode-activity-verb">' + _livecodeEscapeHtml(verb) + "</span>" +
    (p.detail ? '<span class="livecode-activity-detail">' + _livecodeEscapeHtml(p.detail) + "</span>" : "") +
    "</span>" + status;
  const lineClass = "livecode-activity-line livecode-thought-toggle livecode-mcp-call-toggle livecode-browser-step-toggle" +
    (running ? " is-running" : "") + errClass;
  const autoExpand = action === "compare" && hasBody;
  const body = hasBody
    ? '<div class="livecode-browser-step-body livecode-thought-body livecode-mcp-call-body"' + (autoExpand ? "" : " hidden") + ">" +
      argHtml + result + "</div>"
    : "";
  const error = r && r.error ? '<div class="livecode-browser-step-error">' + _livecodeEscapeHtml(String(r.error)) + "</div>" : "";
  let shot = "";
  if (r && r.shot_url) {
    const title = String(r.title || r.url || "Screenshot");
    shot = '<button type="button" class="livecode-browser-shot" data-browser-shot="' + _livecodeEscapeHtml(r.shot_url) + '" data-shot-title="' + _livecodeEscapeHtml(title) + '"' +
      (r.width ? ' data-shot-size="' + r.width + " × " + r.height + '"' : "") + ' title="Open screenshot">' +
      '<img src="' + _livecodeEscapeHtml(r.shot_url) + '" alt="Screenshot of ' + _livecodeEscapeHtml(title) + '" loading="lazy"' +
      (r.width ? ' width="' + r.width + '" height="' + r.height + '"' : "") + "></button>";
  }
  return '<div class="livecode-activity-wrap livecode-thought-wrap livecode-mcp-call-wrap livecode-browser-step' + (hasBody ? " is-expandable" + (autoExpand ? " is-expanded" : " is-collapsed") : "") + '" data-step-kind="browser" data-browser-action="' + _livecodeEscapeHtml(action) + '"' + (shot ? " data-browser-shot-step" : "") + (r ? "" : " data-browser-pending") + ">" +
    '<span class="' + lineClass + '"' + (hasBody ? ' data-expandable role="button" tabindex="0"' : "") + ' aria-expanded="' + (autoExpand ? "true" : "false") + '">' + label + "</span>" +
    body + error + shot + "</div>";
}

const _LIVECODE_COMPARE_NOUNS = {
  p: "Text", span: "Text", label: "Text", h1: "Heading", h2: "Heading", h3: "Heading", h4: "Heading", h5: "Heading", h6: "Heading",
  button: "Button", a: "Link", img: "Image", svg: "Icon", li: "List item", ul: "List", ol: "List", input: "Field", textarea: "Field",
  select: "Dropdown", table: "Table", nav: "Navigation", header: "Header", footer: "Footer", section: "Section", article: "Card",
  main: "Main area", aside: "Sidebar", form: "Form", div: "Box",
};

function _livecodeCompareDescribe(element) {
  const m = /^([a-z][a-z0-9]*)((?:\.[^\s"]+)*)\s*(?:"([\s\S]*)")?$/i.exec(String(element || "").trim());
  if (!m) return { name: String(element || "Area"), selector: "" };
  const noun = _LIVECODE_COMPARE_NOUNS[m[1].toLowerCase()] || "Element";
  const text = m[3] ? "“" + m[3] + "”" : "";
  return { name: text ? noun + " " + text : noun, selector: m[1] + m[2] };
}

function _livecodeCompareWhere(d, size) {
  if (!size || !size[0] || !size[1]) return "";
  const cx = d.x + d.width / 2, cy = d.y + d.height / 2;
  const h = cx < size[0] / 3 ? "left" : cx < size[0] * 2 / 3 ? "center" : "right";
  const v = cy < size[1] / 3 ? "Top" : cy < size[1] * 2 / 3 ? "Middle" : "Bottom";
  return v === "Middle" && h === "center" ? "Center" : v + " " + h;
}

function _livecodeBrowserCompareHtml(r) {
  const verdict = String(r.verdict || "");
  const tone = verdict === "identical" ? "is-identical" : verdict === "different" ? "is-different" : "is-nearly";
  const headline = verdict === "identical" ? "Identical to the design" : verdict === "different" ? "Doesn’t match the design" : "Matches the design";
  const areas = r.differences || [];
  const major = areas.filter(function(d) { return !d.minor; });
  const minor = areas.filter(function(d) { return d.minor; });
  const found = major.length
    ? major.length + (major.length === 1 ? " thing looks" : " things look") + " different"
    : verdict === "identical" ? "No visible differences" : "Only tiny rendering differences";
  let html = '<div class="livecode-browser-compare">' +
    '<div class="livecode-browser-compare-head"><span class="livecode-browser-verdict ' + tone + '">' + headline + "</span></div>" +
    '<div class="livecode-browser-compare-sub">' + _livecodeEscapeHtml(found + " · " + r.similarity + "% of the content matches" + (r.threshold ? " (needs " + r.threshold + "%)" : "")) + "</div>";
  const notes = [r.size_diff, r.notes].filter(Boolean).join(" ").split(/(?<=[.:])\s+(?=[A-Z])/).filter(Boolean);
  if (notes.length) {
    html += '<div class="livecode-browser-compare-notes"><div class="livecode-browser-compare-title">Worth knowing</div>' +
      notes.map(function(n) { return "<p>" + _livecodeEscapeHtml(n) + "</p>"; }).join("") + "</div>";
  }
  if (major.length) {
    html += '<div class="livecode-browser-compare-title">What looks different</div><div class="livecode-browser-compare-areas">' + major.map(function(d) {
      const info = _livecodeCompareDescribe(d.element);
      const size = d.kind === "size";
      const level = size ? "is-size" : d.differs_pct >= 25 ? "is-major" : d.differs_pct >= 10 ? "is-medium" : "is-slight";
      const label = size ? "Size off" : d.differs_pct >= 25 ? "Major" : d.differs_pct >= 10 ? "Noticeable" : "Slight";
      const where = [_livecodeCompareWhere(d, r.page_size), d.width + " × " + d.height + " px", info.selector, d.design_layer ? "design layer " + d.design_layer : ""].filter(Boolean).join(" · ");
      return '<div class="livecode-browser-area"><span class="livecode-browser-area-pct ' + level + '">' + (d.n ? '<i class="livecode-browser-area-n">' + d.n + "</i>" : "") + label + "</span>" +
        '<span class="livecode-browser-area-what">' + _livecodeEscapeHtml(d.element ? info.name : "Area at x " + d.x + ", y " + d.y) + "</span>" +
        '<span class="livecode-browser-area-where">' + _livecodeEscapeHtml(where) + "</span></div>";
    }).join("") + "</div>";
  }
  if (minor.length) {
    html += '<div class="livecode-browser-compare-minor">' + minor.length + " tiny " + (minor.length === 1 ? "difference" : "differences") + " from text or edge rendering, not worth fixing</div>";
  }
  (r.hotspots || []).forEach(function(spot) {
    html += "<p>" + _livecodeEscapeHtml("Most different: " + spot.area + " (x " + spot.x + "–" + (spot.x + spot.width) + ", y " + spot.y + "–" + (spot.y + spot.height) + ") " + spot.differs_pct + "%") + "</p>";
  });
  return html + "</div>";
}

function _livecodeBrowserStyleLine(styles) {
  return Object.keys(styles || {}).map(function(key) { return key.replace(/_/g, " ") + " " + styles[key]; }).join(" · ");
}

function _livecodeBrowserInspectHtml(r) {
  const lines = [];
  const el = r.element;
  if (el) {
    const b = el.box || {};
    lines.push(el.name + " · " + b.x + "," + b.y + " " + b.width + "×" + b.height);
    const style = _livecodeBrowserStyleLine(el.styles);
    if (style) lines.push(style);
    if (el.selector) lines.push("selector " + el.selector);
    if (r.outline) lines.push("");
  }
  if (r.outline) lines.push(String(r.outline));
  return '<pre class="livecode-browser-step-output">' + _livecodeEscapeHtml(lines.join("\n")) + "</pre>";
}

function _livecodeBrowserFigmaHtml(r) {
  const d = r.design || {};
  const lines = [(d.name || "Figma frame") + " · " + (d.type || "frame") + " " + d.width + "×" + d.height + (r.scale ? " · rendered at " + r.scale + "x" : "") + (r.cached ? " · reused, no API call" : "")];
  if (r.layer) {
    const b = r.layer.box || {};
    const style = _livecodeBrowserStyleLine(Object.fromEntries(Object.entries(r.layer).filter(function(kv) { return ["id", "name", "type", "box"].indexOf(kv[0]) < 0; })));
    lines.push("Layer " + r.layer.name + " (" + String(r.layer.type || "").toLowerCase() + ") " + b.x + "," + b.y + " " + b.width + "×" + b.height + (style ? " · " + style : ""));
  }
  if (r.outline) lines.push("", String(r.outline));
  return '<pre class="livecode-browser-step-output">' + _livecodeEscapeHtml(lines.join("\n")) + "</pre>";
}

function _livecodeFinishBrowserStep(args, result, output) {
  output = output || getLiveCodeChatOutput();
  if (!output) return;
  const parts = _livecodeBrowserParts(args, result);
  _livecodeResolveRunningActivityEl(output);
  let el = _livecodeRunningActivityEl && _livecodeRunningActivityEl.querySelector('[data-step-kind="browser"]') ? _livecodeRunningActivityEl : null;
  if (el) {
    _livecodeRunningActivityEl = null;
  } else {
    const pending = output.querySelectorAll(".livecode-browser-step[data-browser-pending]");
    const last = pending.length ? pending[pending.length - 1] : null;
    el = last ? last.closest(".livecode-activity-wrap-outer") : null;
    if (!el) _livecodeFinalizeRunningActivity(undefined, output);
  }
  if (el) {
    el.classList.remove("is-running");
    el.innerHTML = _livecodeBuildActivityHtml(parts, false);
    _livecodeScheduleRegroup(output);
  } else {
    _livecodeAppendActivityParts(parts, false, output);
  }
  _livecodeAutoScroll(output);
}

function _livecodeBrowserApproval(args) {
  const a = args || {};
  const action = String(a.action || "").toLowerCase();
  const page = _livecodeBrowserShortUrl((_livecodeBrowser.state && _livecodeBrowser.state.url) || "");
  if (action === "javascript_exec") return { verb: "Run", detail: "page script", preview: String(a.script || a.text || "") };
  if (action === "type") return { verb: "Type", detail: "into " + (_livecodeBrowserTarget({ ref: a.ref, selector: a.selector }) || "the page"), preview: String(a.text || "") };
  if (action === "press") return { verb: "Press", detail: String(a.key || ""), preview: page };
  const target = _livecodeBrowserTarget(a, null) || (a.x != null ? "(" + a.x + ", " + a.y + ")" : "the page");
  return { verb: "Click", detail: target, preview: page };
}

function _livecodeOpenImageLightbox(src, title, info) {
  const modal = document.getElementById("livecodeImageViewerModal");
  if (!modal) {
    window.open(src, "_blank", "noopener");
    return;
  }
  const img = document.getElementById("livecode-image-viewer-img");
  const name = document.getElementById("livecode-image-viewer-title");
  const size = document.getElementById("livecode-image-viewer-info");
  if (img) img.src = src;
  if (name) name.textContent = title || "";
  if (size) size.textContent = info || "";
  modal.style.display = "flex";
  requestAnimationFrame(function() { modal.classList.add("is-open"); });
}

if (typeof window.closeLivecodeImageViewer !== "function") {
  window.closeLivecodeImageViewer = function() {
    const modal = document.getElementById("livecodeImageViewerModal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.style.display = "none";
  };
}

document.addEventListener("click", function(e) {
  const btn = e.target && e.target.closest ? e.target.closest(".livecode-browser-shot") : null;
  if (!btn) return;
  e.preventDefault();
  _livecodeOpenImageLightbox(btn.getAttribute("data-browser-shot"), btn.getAttribute("data-shot-title") || "Screenshot", btn.getAttribute("data-shot-size") || "");
});

document.addEventListener("keydown", function(e) {
  if (e.key !== "Escape") return;
  const modal = document.getElementById("livecodeImageViewerModal");
  if (modal && modal.style.display !== "none" && modal.classList.contains("is-open")) window.closeLivecodeImageViewer();
});


window.openLiveCodeBrowserCookies = function() {
  if (!livecodeProjectPath) {
    _livecodeShowIdeToast("Open a project to manage the browser's cookies.");
    return;
  }
  const overlay = _livecodeEnsureCookieDialog();
  overlay.hidden = false;
  overlay.querySelector(".ide-cookie-status").textContent = "";
  overlay.querySelector(".ide-cookie-status").className = "ide-cookie-status";
  _livecodeRefreshCookieSummary();
  setTimeout(function() {
    const text = overlay.querySelector(".ide-cookie-text");
    if (text && overlay.getAttribute("data-source") !== "local") text.focus();
  }, 30);
};

const _LIVECODE_COOKIE_BROWSERS = [["chrome", "Chrome"], ["edge", "Edge"], ["brave", "Brave"], ["chromium", "Chromium"], ["firefox", "Firefox"], ["opera", "Opera"], ["vivaldi", "Vivaldi"], ["safari", "Safari"], ["island", "Island"]];
const _LIVECODE_COOKIE_MANUAL = ["manual", "Pasted or uploaded"];

function _livecodeCookieSourceLabel(id) {
  const all = _LIVECODE_COOKIE_BROWSERS.concat([_LIVECODE_COOKIE_MANUAL]);
  for (let i = 0; i < all.length; i++) if (all[i][0] === id) return all[i][1];
  return id;
}

function _livecodeEnsureCookieDialog() {
  let overlay = document.getElementById("ide-cookie-dialog");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "ide-cookie-dialog";
  overlay.className = "ide-modal-overlay ide-cookie-overlay theme-transition";
  overlay.hidden = true;
  overlay.setAttribute("data-source", "paste");
  const browsers = _LIVECODE_COOKIE_BROWSERS;
  overlay.innerHTML =
    '<div class="ide-modal ide-cookie-modal theme-transition" role="dialog" aria-modal="true" aria-labelledby="ide-cookie-title">' +
      '<button type="button" class="ide-modal-close" data-cookie-action="close" aria-label="Close">' + _livecodeIcon("x", { size: "sm" }) + "</button>" +
      '<div class="ide-modal-title" id="ide-cookie-title">Browser cookies</div>' +
      '<div class="ide-modal-message">The built-in browser keeps this project’s cookies and logins on this machine. Import cookies from your own browser to open sites already signed in.</div>' +
      '<div class="ide-cookie-summary"></div>' +
      '<div class="ide-cookie-row ide-cookie-default-row">' +
        '<label class="ide-cookie-label">Default browser<select class="ide-modal-input ide-cookie-default">' +
          browsers.concat([_LIVECODE_COOKIE_MANUAL]).map(function(b) { return '<option value="' + b[0] + '">' + b[1] + "</option>"; }).join("") +
        "</select></label>" +
        '<button type="button" class="ide-modal-btn ide-modal-btn-primary ide-cookie-default-save" data-cookie-action="save-default" disabled>Save</button>' +
      "</div>" +
      '<div class="lc-segmented ide-cookie-source" role="radiogroup" aria-label="Import from">' +
        '<button type="button" class="lc-btn is-active" data-cookie-source="local" role="radio" aria-checked="true">From a browser on this machine</button>' +
        '<button type="button" class="lc-btn" data-cookie-source="paste" role="radio" aria-checked="false">Paste or upload</button>' +
      "</div>" +
      '<div class="ide-cookie-pane" data-cookie-pane="local">' +
        '<div class="ide-cookie-row">' +
          '<label class="ide-cookie-label">Browser<select class="ide-modal-input ide-cookie-browser">' +
            browsers.map(function(b) { return '<option value="' + b[0] + '">' + b[1] + "</option>"; }).join("") +
          "</select></label>" +
          '<label class="ide-cookie-label">Only this site (optional)<input type="text" class="ide-modal-input ide-cookie-local-domain" placeholder="github.com" autocomplete="off" spellcheck="false"></label>' +
        "</div>" +
        '<div class="ide-cookie-hint ide-cookie-local-hint"></div>' +
      "</div>" +
      '<div class="ide-cookie-pane" data-cookie-pane="paste" hidden>' +
        '<textarea class="ide-cookie-text ide-modal-input" spellcheck="false" placeholder="Paste a JSON export (Cookie-Editor, EditThisCookie), a cookies.txt file, or a Cookie: header"></textarea>' +
        '<div class="ide-cookie-row">' +
          '<button type="button" class="ide-modal-btn" data-cookie-action="upload">Choose file…</button>' +
          '<span class="ide-cookie-file"></span>' +
          '<input type="file" class="ide-cookie-file-input" accept=".json,.txt,text/plain,application/json" hidden>' +
        "</div>" +
        '<label class="ide-cookie-label">Site, for a Cookie header<input type="text" class="ide-modal-input ide-cookie-domain" placeholder="example.com" autocomplete="off" spellcheck="false"></label>' +
        '<div class="ide-cookie-hint">Export with a cookie extension: Cookie-Editor → Export → JSON, or “Get cookies.txt LOCALLY”.</div>' +
      "</div>" +
      '<div class="ide-cookie-status" role="status"></div>' +
      '<div class="ide-modal-actions">' +
        '<button type="button" class="ide-modal-btn ide-modal-btn-danger ide-cookie-clear" data-cookie-action="clear">Clear all</button>' +
        '<span class="ide-cookie-spacer"></span>' +
        '<button type="button" class="ide-modal-btn" data-cookie-action="close">Close</button>' +
        '<button type="button" class="ide-modal-btn ide-modal-btn-primary" data-cookie-action="import">Import</button>' +
      "</div>" +
    "</div>";
  document.body.appendChild(overlay);
  overlay.setAttribute("data-source", "local");

  const fileInput = overlay.querySelector(".ide-cookie-file-input");
  overlay.addEventListener("input", function(e) {
    if (e.target && e.target.classList && e.target.classList.contains("ide-cookie-search")) _livecodeRenderCookieSites(overlay);
  });
  overlay.addEventListener("change", function(e) {
    const target = e.target;
    if (!target || !target.classList) return;
    if (target.classList.contains("ide-cookie-browser")) {
      target.setAttribute("data-touched", "1");
      _livecodeSyncCookieDefault(overlay, overlay._cookieData || {});
    } else if (target.classList.contains("ide-cookie-default")) {
      _livecodeSyncCookieDefaultSave(overlay);
    }
  });
  overlay.addEventListener("mousedown", function(e) {
    if (e.target === overlay) overlay.hidden = true;
  });
  overlay.addEventListener("keydown", function(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      overlay.hidden = true;
    }
  });
  overlay.addEventListener("click", function(e) {
    const source = e.target.closest ? e.target.closest("[data-cookie-source]") : null;
    if (source) {
      e.preventDefault();
      _livecodeSetCookieSource(overlay, source.getAttribute("data-cookie-source"));
      return;
    }
    const clear = e.target.closest ? e.target.closest("[data-cookie-clear-site]") : null;
    if (clear) {
      e.preventDefault();
      _livecodeBrowserClearCookies(clear.getAttribute("data-cookie-clear-site"));
      return;
    }
    const btn = e.target.closest ? e.target.closest("[data-cookie-action]") : null;
    if (!btn) return;
    e.preventDefault();
    const action = btn.getAttribute("data-cookie-action");
    if (action === "close") overlay.hidden = true;
    else if (action === "upload") fileInput.click();
    else if (action === "clear") _livecodeBrowserClearCookies("");
    else if (action === "import") _livecodeImportCookies(overlay);
    else if (action === "save-default") _livecodeSetDefaultCookieBrowser(overlay, overlay.querySelector(".ide-cookie-default").value);
  });
  fileInput.addEventListener("change", function() {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      _livecodeCookieStatus(overlay, "That file is larger than 5 MB.", true);
      return;
    }
    const reader = new FileReader();
    reader.onload = function() {
      overlay.querySelector(".ide-cookie-text").value = String(reader.result || "");
      overlay.querySelector(".ide-cookie-file").textContent = file.name;
    };
    reader.readAsText(file);
    fileInput.value = "";
  });
  return overlay;
}

function _livecodeSetCookieSource(overlay, source) {
  overlay.setAttribute("data-source", source);
  overlay.querySelectorAll("[data-cookie-source]").forEach(function(b) {
    const on = b.getAttribute("data-cookie-source") === source;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
  overlay.querySelectorAll("[data-cookie-pane]").forEach(function(pane) {
    pane.hidden = pane.getAttribute("data-cookie-pane") !== source;
  });
  _livecodeSyncCookieLocalHint(overlay);
}

function _livecodeSyncCookieDefaultSave(overlay) {
  const select = overlay.querySelector(".ide-cookie-default");
  const save = overlay.querySelector(".ide-cookie-default-save");
  if (select && save) save.disabled = select.value === overlay._defaultSaved;
}

function _livecodeSyncCookieDefault(overlay, data) {
  const select = overlay.querySelector(".ide-cookie-default");
  if (!select) return;
  const saved = String((data && data.default_browser) || overlay._defaultSaved || "chrome");
  const stores = (data && data.stores) || {};
  Array.prototype.forEach.call(select.options, function(option) {
    const n = Number(stores[option.value]) || 0;
    option.textContent = _livecodeCookieSourceLabel(option.value) + (n ? " (" + n + ")" : "");
  });
  const changed = overlay._defaultSaved !== saved;
  const pending = overlay._defaultSaved != null && select.value !== overlay._defaultSaved && !changed;
  overlay._defaultSaved = saved;
  if (!pending) select.value = saved;
  const importSelect = overlay.querySelector(".ide-cookie-browser");
  if (importSelect && !importSelect.getAttribute("data-touched") && saved !== "manual") importSelect.value = saved;
  _livecodeSyncCookieDefaultSave(overlay);
}

function _livecodeSetDefaultCookieBrowser(overlay, browser) {
  _livecodeCookieStatus(overlay, "Switching…");
  _livecodeBrowserPost("cookies/default", { browser: browser }).then(function(data) {
    _livecodeBrowser.cookies = Object.assign({}, _livecodeBrowser.cookies || {}, data);
    _livecodeRenderCookieSummary(overlay, data);
    _livecodeSyncCookieDefault(overlay, data);
    _livecodeCookieStatus(overlay, "Now using " + _livecodeCookieSourceLabel(data.default_browser || browser) + "’s cookies." + (Number(data.reloaded) ? " Reloaded " + data.reloaded + " open page" + (data.reloaded === 1 ? "" : "s") + "." : ""));
  }).catch(function(err) {
    _livecodeCookieStatus(overlay, err.message || String(err), true);
    _livecodeRefreshCookieSummary();
  });
}

function _livecodeSyncCookieLocalHint(overlay) {
  const hint = overlay.querySelector(".ide-cookie-local-hint");
  const available = !!(_livecodeBrowser.cookies && _livecodeBrowser.cookies.local_cookie_import);
  if (hint) {
    hint.textContent = available
      ? "Reads the cookies of that browser’s default profile on the LiveCode server’s machine. Close the browser first if it can’t read them."
      : "Needs the browser-cookie3 package on the LiveCode server: pip install browser-cookie3";
  }
  const importBtn = overlay.querySelector('[data-cookie-action="import"]');
  if (importBtn) importBtn.disabled = overlay.getAttribute("data-source") === "local" && !available;
}

function _livecodeCookieStatus(overlay, text, isError) {
  const el = overlay.querySelector(".ide-cookie-status");
  if (!el) return;
  el.textContent = text || "";
  el.className = "ide-cookie-status" + (isError ? " is-error" : "");
}

function _livecodeRenderCookieSites(overlay) {
  const data = overlay._cookieData || {};
  const list = overlay.querySelector(".ide-cookie-sites");
  const search = overlay.querySelector(".ide-cookie-search");
  if (!list) return;
  const query = search ? search.value.trim().toLowerCase().replace(/^\./, "") : "";
  const all = data.sites || [];
  const shown = query ? all.filter(function(site) { return String(site.domain).toLowerCase().indexOf(query) >= 0; }) : all;
  const rows = shown.map(function(site) {
    return '<div class="ide-cookie-site"><span class="ide-cookie-site-name">' + _livecodeEscapeHtml(site.domain) + "</span>" +
      '<span class="ide-cookie-site-count">' + site.count + "</span>" +
      '<button type="button" class="ide-cookie-site-clear" data-cookie-clear-site="' + _livecodeEscapeHtml(site.domain) + '" title="Remove this site’s cookies" aria-label="Remove ' + _livecodeEscapeHtml(site.domain) + ' cookies">' + _livecodeIcon("x", { size: "sm" }) + "</button></div>";
  }).join("");
  const hidden = (Number(data.site_count) || 0) - all.length;
  const more = !query && hidden > 0 ? '<div class="ide-cookie-empty">and ' + hidden + " more sites</div>" : "";
  const none = query && !shown.length ? '<div class="ide-cookie-empty">No sites match “' + _livecodeEscapeHtml(search.value.trim()) + '”.</div>' : "";
  list.innerHTML = rows + more + none;
}

function _livecodeRenderCookieSummary(overlay, data) {
  const box = overlay.querySelector(".ide-cookie-summary");
  if (!box) return;
  overlay._cookieData = data || {};
  _livecodeSyncCookieDefault(overlay, data || {});
  const total = Number(data && data.total) || 0;
  if (!total) {
    box.innerHTML = '<div class="ide-cookie-empty">No cookies yet.</div>';
    overlay.querySelector(".ide-cookie-clear").hidden = true;
    return;
  }
  const previous = box.querySelector(".ide-cookie-search");
  const query = previous ? previous.value : "";
  box.innerHTML = '<div class="ide-cookie-total">' + total + " cookie" + (total === 1 ? "" : "s") + " · " + data.site_count + " site" + (data.site_count === 1 ? "" : "s") + "</div>" +
    '<input type="search" class="ide-modal-input ide-cookie-search" placeholder="Search imported cookies by site" autocomplete="off" spellcheck="false">' +
    '<div class="ide-cookie-sites"></div>';
  box.querySelector(".ide-cookie-search").value = query;
  _livecodeRenderCookieSites(overlay);
  overlay.querySelector(".ide-cookie-clear").hidden = false;
}

function _livecodeRefreshCookieSummary() {
  const overlay = document.getElementById("ide-cookie-dialog");
  return _livecodeBrowserPost("cookies").then(function(data) {
    _livecodeBrowser.cookies = data;
    if (overlay) {
      _livecodeRenderCookieSummary(overlay, data);
      _livecodeSyncCookieLocalHint(overlay);
    }
    return data;
  }).catch(function(err) {
    if (overlay) _livecodeCookieStatus(overlay, err.message || String(err), true);
  });
}

function _livecodeImportCookies(overlay) {
  const local = overlay.getAttribute("data-source") === "local";
  const body = local
    ? { source: "local", browser: overlay.querySelector(".ide-cookie-browser").value, domain: overlay.querySelector(".ide-cookie-local-domain").value.trim() }
    : { text: overlay.querySelector(".ide-cookie-text").value, domain: overlay.querySelector(".ide-cookie-domain").value.trim() };
  if (!local && !body.text.trim()) {
    _livecodeCookieStatus(overlay, "Paste cookies or choose a file first.", true);
    return;
  }
  const btn = overlay.querySelector('[data-cookie-action="import"]');
  btn.disabled = true;
  _livecodeCookieStatus(overlay, "Importing…");
  _livecodeBrowserPost("cookies/import", body).then(function(data) {
    const skipped = (Number(data.skipped) || 0) + (Number(data.rejected) || 0);
    const source = _livecodeCookieSourceLabel(data.stored_in || "");
    _livecodeCookieStatus(overlay, "Imported " + data.imported + " cookie" + (data.imported === 1 ? "" : "s") + " from " + source + (skipped ? " (" + skipped + " skipped)" : "") + "." +
      (data.active === false ? " Saved separately, not in use: make " + source + " the default browser to use them." : "") +
      (Number(data.reloaded) ? " Reloaded " + data.reloaded + " open page" + (data.reloaded === 1 ? "" : "s") + "." : ""));
    overlay.querySelector(".ide-cookie-text").value = "";
    overlay.querySelector(".ide-cookie-file").textContent = "";
    _livecodeBrowser.cookies = Object.assign({}, _livecodeBrowser.cookies || {}, data);
    _livecodeRenderCookieSummary(overlay, data);
    _livecodeRefreshCookieSummary();
  }).catch(function(err) {
    _livecodeCookieStatus(overlay, err.message || String(err), true);
  }).finally(function() {
    btn.disabled = false;
    _livecodeSyncCookieLocalHint(overlay);
  });
}

function _livecodeBrowserClearCookies(domain) {
  const site = String(domain || "");
  _livecodeModalConfirm({
    title: site ? "Remove " + site + "’s cookies?" : "Clear all cookies?",
    message: site ? "The browser signs out of " + site + "." : "The built-in browser signs out of every site for this project.",
    confirmText: site ? "Remove" : "Clear all",
    danger: true,
  }).then(function(ok) {
    if (!ok) return;
    _livecodeBrowserPost("cookies/clear", { domain: site }).then(function(data) {
      _livecodeBrowser.cookies = Object.assign({}, _livecodeBrowser.cookies || {}, data);
      const overlay = document.getElementById("ide-cookie-dialog");
      if (overlay && !overlay.hidden) {
        _livecodeRenderCookieSummary(overlay, data);
        _livecodeCookieStatus(overlay, site ? "Removed " + site + "’s cookies." : "Cleared all cookies.");
      } else {
        _livecodeShowIdeToast(site ? "Removed " + site + "’s cookies" : "Cleared the browser’s cookies");
      }
    }).catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
  });
}


const LIVECODE_SETTINGS_TAB_KEY = "livecode://settings";
const _LIVECODE_SETTINGS_TAB_ICON = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8b949e" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>'
);

const _LIVECODE_SETTINGS_SECTIONS = [
  { id: "general", label: "General", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"></path></svg>' },
  { id: "appearance", label: "Appearance", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13.5" cy="6.5" r="1.2"></circle><circle cx="17.5" cy="10.5" r="1.2"></circle><circle cx="8.5" cy="7.5" r="1.2"></circle><circle cx="6.5" cy="12.5" r="1.2"></circle><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.9 0 1.7-.8 1.7-1.7 0-.4-.2-.8-.4-1.1-.3-.3-.4-.7-.4-1.1 0-.9.8-1.7 1.7-1.7h2c3 0 5.5-2.5 5.5-5.5C22 6 17.5 2 12 2z"></path></svg>' },
  { id: "agent", label: "Agent", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"></path><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"></path></svg>' },
  { id: "harness", label: "Harness", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"></path></svg>' },
  { id: "plan", label: "Plan mode", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"></path><rect x="9" y="3" width="6" height="4" rx="1"></rect><path d="M9 12l2 2 4-4"></path></svg>' },
  { id: "memory", label: "Memory", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24A2.5 2.5 0 0 1 9.5 2z"></path><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24A2.5 2.5 0 0 0 14.5 2z"></path></svg>' },
  { id: "browser", label: "Browser", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"></path></svg>' },
  { id: "models", label: "Models", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="13" width="3.5" height="7" rx="1"></rect><rect x="10.25" y="8" width="3.5" height="12" rx="1"></rect><rect x="16.5" y="4" width="3.5" height="16" rx="1"></rect></svg>' },
  { id: "rules", label: "Rules", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"></path><path d="M14 3v5h5"></path><line x1="9" y1="13" x2="15" y2="13"></line><line x1="9" y1="17" x2="13" y2="17"></line></svg>' },
  { id: "mcp", label: "MCP", icon: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 1.75v3M10 1.75v3"></path><path d="M4.25 4.75h7.5v2.5a3.75 3.75 0 0 1-7.5 0z"></path><path d="M8 11v3.25"></path></svg>' },
  { id: "indexing", label: "Indexing", icon: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="5.5" rx="7.5" ry="2.5"></ellipse><path d="M4.5 5.5v6c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-6"></path><path d="M4.5 11.5v6c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-6"></path></svg>' },
];

let _livecodeSettingsSection = "general";
let _livecodeSettingsRules = null;
let _livecodeSettingsMcpExpanded = {};
let _livecodeSettingsMcpFocus = "";

window.openLiveCodeSettings = function(section) {
  if (section && _LIVECODE_SETTINGS_SECTIONS.some(function(s) { return s.id === section; })) {
    _livecodeSettingsSection = section;
  }
  if (!ideOpenFiles[LIVECODE_SETTINGS_TAB_KEY]) {
    ideOpenFiles[LIVECODE_SETTINGS_TAB_KEY] = {
      isSettings: true,
      path: LIVECODE_SETTINGS_TAB_KEY,
      name: "Settings",
      content: "",
      originalContent: "",
      modified: false,
    };
  }
  toggleLiveCodeMcpPanel(false);
  switchToFile(LIVECODE_SETTINGS_TAB_KEY);
  if (_livecodeSettingsSection === "mcp" && !livecodeMcpServers.length) refreshLiveCodeMcpStatus({ probeServers: Array.from(livecodeMcpSelectedServers) });
};

function _livecodeShowSettingsSurface() {
  const view = document.getElementById("ide-settings-view");
  if (!view) return;
  ["ide-editor-placeholder", "ide-monaco", "ide-code-editor"].forEach(function(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = "none";
  });
  _livecodeHidePlanSurface();
  if (window.ideEditor) {
    try { window.ideEditor.setModel(null); } catch (e) {}
  }
  view.style.display = "flex";
  _livecodeRenderSettings();
}

function _livecodeHideSettingsSurface() {
  const view = document.getElementById("ide-settings-view");
  if (view) view.style.display = "none";
}

function _livecodeSettingsVisible(section) {
  const view = document.getElementById("ide-settings-view");
  return !!(view && view.style.display !== "none" && (!section || _livecodeSettingsSection === section));
}

function _livecodeRenderSettings() {
  const view = document.getElementById("ide-settings-view");
  if (!view) return;
  _livecodeBindSettingsOnce(view);
  const nav = _LIVECODE_SETTINGS_SECTIONS.map(function(s) {
    const active = s.id === _livecodeSettingsSection && !String(window._livecodeSettingsQuery || "").trim();
    return '<button type="button" class="livecode-settings-nav-item lc-btn' + (active ? " is-active" : "") + '" data-settings-section="' + s.id + '" title="' + s.label + '" aria-label="' + s.label + '"' + (active ? ' aria-current="page"' : "") + ">" +
      s.icon + "<span>" + s.label + "</span></button>";
  }).join("");
  const query = String(window._livecodeSettingsQuery || "");
  const search = '<div class="livecode-settings-search"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg>' +
    '<input type="search" data-settings-search placeholder="Search settings" value="' + _livecodeEscapeHtml(query).replace(/"/g, "&quot;") + '" aria-label="Search settings" spellcheck="false" autocomplete="off"></div>';
  view.innerHTML =
    '<nav class="livecode-settings-nav" aria-label="Settings sections"><div class="livecode-settings-nav-title">Settings</div>' + search + nav + "</nav>" +
    '<div class="livecode-settings-main"><div class="livecode-settings-page" id="livecode-settings-page"></div></div>';
  _livecodeRenderSettingsPage();
}

function _livecodeSettingsRenderers() {
  return {
    general: _livecodeSettingsGeneralHtml,
    appearance: window._livecodeSettingsAppearanceHtml,
    agent: _livecodeSettingsAgentHtml,
    harness: window._livecodeSettingsHarnessHtml,
    plan: window._livecodeSettingsPlanHtml,
    memory: window._livecodeSettingsMemoryHtml,
    browser: window._livecodeSettingsBrowserHtml,
    models: _livecodeSettingsModelsHtml,
    rules: _livecodeSettingsRulesHtml,
    mcp: _livecodeSettingsMcpHtml,
    indexing: _livecodeSettingsIndexingHtml,
  };
}

function _livecodeRenderSettingsPage() {
  const page = document.getElementById("livecode-settings-page");
  if (!page) return;
  const main = page.parentElement;
  const keepScroll = main ? main.scrollTop : 0;
  const query = String(window._livecodeSettingsQuery || "").trim();
  const render = query && window._livecodeSettingsSearchHtml
    ? function() { return window._livecodeSettingsSearchHtml(query); }
    : _livecodeSettingsRenderers()[_livecodeSettingsSection] || _livecodeSettingsGeneralHtml;
  page.innerHTML = render();
  if (main) main.scrollTop = keepScroll;
  if (_livecodeSettingsSection === "rules" && _livecodeSettingsRules === null) _livecodeLoadSettingsRules();
  if (_livecodeSettingsSection === "mcp" && _livecodeSettingsMcpFocus) {
    const card = page.querySelector('[data-mcp-card="' + CSS.escape(_livecodeSettingsMcpFocus) + '"]');
    _livecodeSettingsMcpFocus = "";
    if (card) {
      card.classList.add("is-focused");
      card.scrollIntoView({ block: "nearest" });
    }
  }
}

function _livecodeSettingSwitchHtml(key, title, desc) {
  const on = !!_livecodeSettingsGet(key);
  return '<div class="livecode-settings-row"><div class="livecode-settings-row-text"><div class="livecode-settings-row-title">' + title + "</div>" +
    (desc ? '<div class="livecode-settings-row-desc">' + desc + "</div>" : "") + "</div>" +
    '<label class="lc-switch"><input type="checkbox" data-setting="' + key + '"' + (on ? " checked" : "") + ' aria-label="' + _livecodeEscapeHtml(title) + '"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label></div>';
}

function _livecodeSettingsRowHtml(title, desc, controlHtml) {
  return '<div class="livecode-settings-row"><div class="livecode-settings-row-text"><div class="livecode-settings-row-title">' + title + "</div>" +
    (desc ? '<div class="livecode-settings-row-desc">' + desc + "</div>" : "") + "</div>" +
    '<div class="livecode-settings-row-control">' + (controlHtml || "") + "</div></div>";
}

function _livecodeSettingsButton(label, action, extra, primary) {
  return '<button type="button" class="lc-btn livecode-settings-btn' + (primary ? " is-primary" : "") + '" data-settings-action="' + action + '"' + (extra || "") + ">" + label + "</button>";
}

function _livecodeShortPath(path) {
  const p = String(path || "");
  const home = _livecodeBrowserHomePath || "";
  if (home && home !== "~" && (p === home || p.indexOf(home + "/") === 0)) return "~" + p.slice(home.length);
  const m = p.match(/^(\/Users\/[^/]+|\/home\/[^/]+)(\/.*)?$/);
  if (m) return "~" + (m[2] || "");
  return p;
}

function _livecodeSettingsPathHtml(path, suffix) {
  const full = String(path || "");
  return '<span class="livecode-settings-path" title="' + _livecodeEscapeHtml(full) + '">‎' +
    _livecodeEscapeHtml(_livecodeShortPath(full)) + (suffix ? _livecodeEscapeHtml(suffix) : "") + "‎</span>";
}

function _livecodeProjectDisplayName(path) {
  const name = String(path || "").split(/[\\/]/).filter(Boolean).pop() || String(path || "");
  return name.replace(/\.(livecode-workspace(\.json)?|code-workspace)$/i, "");
}

function _livecodeSettingsGeneralHtml() {
  const project = livecodeProjectPath;
  const folders = (livecodeWorkspaceFolders || []).length;
  let html = '<h2 class="livecode-settings-h">General</h2><div class="livecode-settings-group">';
  html += _livecodeSettingsRowHtml(
    "Project",
    project ? _livecodeSettingsPathHtml(project) : "No project open",
    _livecodeSettingsButton("Open…", "open-project") + _livecodeSettingsButton("Clone…", "clone")
  );
  html += _livecodeSettingsRowHtml(
    "Workspace folders",
    project ? folders + " folder" + (folders === 1 ? "" : "s") + (livecodeWorkspacePath ? _livecodeSettingsPathHtml(livecodeWorkspacePath) : "") : "Open a project to add folders",
    project ? _livecodeSettingsButton("Manage…", "workspace-manager") : ""
  );
  html += "</div>";
  html += '<h3 class="livecode-settings-subh">Folder browser</h3><div class="livecode-settings-group">';
  html += _livecodeSettingSwitchHtml("showHiddenFiles", "Show hidden files", "List dotfiles and hidden folders when opening a project.");
  html += "</div>";
  const recents = getLiveCodeRecentProjects();
  html += '<h3 class="livecode-settings-subh">Recent projects</h3><div class="livecode-settings-group">';
  if (!recents.length) {
    html += '<div class="livecode-settings-empty">Projects you open appear here.</div>';
  } else {
    html += recents.map(function(path) {
      const esc = _livecodeEscapeHtml(path);
      const active = _livecodeRecentEntryIsActive(path);
      return '<div class="livecode-settings-row is-compact"><button type="button" class="lc-btn livecode-settings-link-row" data-settings-action="open-recent" data-path="' + esc + '" title="' + esc + '">' +
        '<span class="livecode-settings-row-title">' + _livecodeEscapeHtml(_livecodeProjectDisplayName(path)) + (active ? ' <span class="livecode-settings-badge">Open</span>' : "") + "</span>" +
        '<span class="livecode-settings-row-desc">' + _livecodeSettingsPathHtml(path) + "</span></button>" +
        '<button type="button" class="lc-btn livecode-settings-icon-btn" data-settings-action="remove-recent" data-path="' + esc + '" title="Remove from recents and delete its chats" aria-label="Remove ' + esc + '">' + _LIVECODE_QUEUE_ICONS.remove + "</button></div>";
    }).join("");
  }
  html += "</div>";
  if (typeof window._livecodeSettingsGeneralExtraHtml === "function") html += window._livecodeSettingsGeneralExtraHtml();
  return html;
}

function _livecodeSettingsAgentHtml() {
  const mode = window.livecodeChatMode || "agent";
  const seg = LIVECODE_CHAT_MODES.map(function(m) {
    return '<button type="button" class="lc-btn' + (m.value === mode ? " is-active" : "") + '" data-settings-action="set-mode" data-mode="' + m.value + '" role="radio" aria-checked="' + (m.value === mode) + '">' + m.label + "</button>";
  }).join("");
  let html = '<h2 class="livecode-settings-h">Agent</h2><div class="livecode-settings-group">';
  html += _livecodeSettingsRowHtml("Default mode", "Agent edits and runs commands, Plan writes a plan first, Ask only reads.", '<div class="lc-segmented" role="radiogroup" aria-label="Default mode">' + seg + "</div>");
  html += _livecodeSettingSwitchHtml("requireApproval", "Ask before edits, commands, and MCP tools", "The agent waits for your approval before it writes a file, runs a command, or calls an MCP tool. Destructive commands (rm -rf, git reset --hard, force push) always ask.");
  html += _livecodeSettingSwitchHtml("webTools", "Web search and fetch", "Let the agent search the web and read pages. It can always do this when you ask for it in your message.");
  html += "</div>";
  html += '<h3 class="livecode-settings-subh">More agent settings</h3><div class="livecode-settings-group">';
  html += _livecodeSettingsRowHtml("Browser, display and limits", "The agent's browser is under Browser, conversation display under Appearance, and step limits, retries and safety rails under Harness.",
    _livecodeSettingsButton("Browser", "goto-section", ' data-section="browser"') + _livecodeSettingsButton("Harness", "goto-section", ' data-section="harness"'));
  html += "</div>";
  html += '<h3 class="livecode-settings-subh">Queued messages</h3><div class="livecode-settings-group">';
  html += _livecodeSettingSwitchHtml("autoRunQueue", "Run queued messages automatically", "Messages you send while the agent works start one after another when each turn ends. Turn off to send each one yourself.");
  html += _livecodeSettingsRowHtml("Keys", "Enter queues a message while the agent works · ⌘/Ctrl+Enter stops the turn and sends it now · Stop pauses the queue.", "");
  html += "</div>";
  if (typeof window._livecodeSettingsAgentExtraHtml === "function") html += window._livecodeSettingsAgentExtraHtml();
  return html;
}

let _livecodeBrowserConnection = null;

function _livecodeLoadBrowserConnection() {
  return fetch("/livecode/browser/connection")
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      _livecodeBrowserConnection = data && data.success ? data : { engine: "builtin" };
      if (_livecodeSettingsSection === "browser" || window._livecodeSettingsQuery) _livecodeRenderSettingsPage();
    })
    .catch(function() {});
}

function _livecodeSettingsChromeRowHtml() {
  const c = _livecodeBrowserConnection;
  if (!c) {
    _livecodeLoadBrowserConnection();
    return _livecodeSettingsRowHtml("Use your Chrome", "Checking…", "");
  }
  const isEnv = c.source === "env";
  const toggleHtml = '<label class="lc-switch"><input type="checkbox" data-chrome-toggle' + (c.engine === "chrome" ? " checked" : "") +
    (isEnv ? " disabled" : "") + ' aria-label="Use your Chrome"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label>';
  if (c.engine === "chrome") {
    const where = _livecodeEscapeHtml(String(c.endpoint || "").replace(/^https?:\/\//, ""));
    const desc = "The agent's tabs open in the Chrome at " + where + (c.version ? " (" + _livecodeEscapeHtml(c.version) + ")" : "") +
      ", with that profile's logins" + (isEnv ? " (set by LIVECODE_BROWSER_CDP_URL on the server)." : ".") +
      (c.connected === false ? " It is not answering right now." : "");
    return _livecodeSettingsRowHtml("Use your Chrome", desc, toggleHtml);
  }
  return _livecodeSettingsRowHtml("Use your Chrome",
    "Attach a Chrome you started with remote debugging (chrome --remote-debugging-port=9222 --user-data-dir=&lt;a profile folder&gt;), so the agent's tabs open there, signed in to your design tools and sites. The built-in browser is used otherwise.",
    toggleHtml);
}

let _livecodeFigmaStatus = null;

function _livecodeLoadFigmaStatus() {
  return fetch("/livecode/figma/token")
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      _livecodeFigmaStatus = data && data.success ? data : { configured: false, source: "" };
      if (_livecodeSettingsSection === "browser" || window._livecodeSettingsQuery) _livecodeRenderSettingsPage();
    })
    .catch(function() {});
}

let _livecodeBrowserSettings = null;

function _livecodeLoadBrowserSettings() {
  return fetch("/livecode/browser/settings")
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      _livecodeBrowserSettings = data && data.success ? data : Object.assign({}, _LIVECODE_BROWSER_SETTING_DEFAULTS);
      if (_livecodeSettingsSection === "browser" || window._livecodeSettingsQuery) _livecodeRenderSettingsPage();
    })
    .catch(function() {
      if (_livecodeBrowserSettings) return;
      _livecodeBrowserSettings = Object.assign({}, _LIVECODE_BROWSER_SETTING_DEFAULTS);
      if (_livecodeSettingsSection === "browser" || window._livecodeSettingsQuery) _livecodeRenderSettingsPage();
    });
}

function _livecodeAccuracyMeaning(value, fallback) {
  if (value >= 100) return "Exact: every element must match the design pixel for pixel. A page drawn by the browser and a design drawn by a design tool almost always differ in anti-aliasing and font rendering, so expect the agent to keep polishing tiny differences.";
  if (value > fallback) return "Stricter than the default: smaller moves, size and colour differences count, and near 100% an element's pixels must match too.";
  if (value === fallback) return "The default: only anti-aliasing and font rendering may differ, so a page that passes looks exactly like the design.";
  if (value >= 75) return "More forgiving than the default: differences of a few pixels and slight colour shifts pass.";
  return "Loose: only clear differences in layout, size and colour count.";
}

function _livecodeSettingsMatchRowHtml() {
  const settings = _livecodeBrowserSettings;
  if (!settings) {
    _livecodeLoadBrowserSettings();
    return _livecodeSettingsRowHtml("Design accuracy", "Loading…", "");
  }
  const value = Number(settings.design_accuracy) || 90;
  const fallback = Number(settings.default_design_accuracy) || 90;
  const least = Number(settings.min_design_accuracy) || 50;
  const desc = "How closely the page must match a design before the agent stops: it compares element by element and keeps fixing until this is met. " +
    '<span class="livecode-accuracy-meaning" data-accuracy-meaning>' + _livecodeEscapeHtml(_livecodeAccuracyMeaning(value, fallback)) + "</span> Default " + fallback + "%.";
  return _livecodeSettingsRowHtml("Design accuracy", desc,
    '<div class="livecode-accuracy-control">' +
      '<input type="range" class="livecode-accuracy-range" min="' + least + '" max="100" step="1" value="' + _livecodeEscapeHtml(String(value)) + '" data-design-accuracy aria-label="Design accuracy">' +
      '<input type="number" class="livecode-settings-number" min="' + least + '" max="100" step="1" value="' + _livecodeEscapeHtml(String(value)) + '" data-design-accuracy aria-label="Design accuracy percent"> %' +
      (value !== fallback ? " " + _livecodeSettingsButton("Reset", "match-threshold-reset") : "") +
    "</div>");
}

const _LIVECODE_BROWSER_SETTING_DEFAULTS = {
  design_accuracy: 90,
  default_design_accuracy: 90,
  min_design_accuracy: 50,
  design_gate: true,
  agent_tabs: true,
  view_quality: "sharp",
  default_viewport: "fit",
  reduce_automation_signals: true,
};

function _livecodeBrowserSetting(key) {
  const settings = _livecodeBrowserSettings || {};
  return settings[key] === undefined || settings[key] === null ? _LIVECODE_BROWSER_SETTING_DEFAULTS[key] : settings[key];
}

function _livecodeBrowserSwitchRowHtml(key, title, desc) {
  if (!_livecodeBrowserSettings) return "";
  const on = _livecodeBrowserSetting(key) !== false;
  return '<div class="livecode-settings-row"><div class="livecode-settings-row-text"><div class="livecode-settings-row-title">' + title + "</div>" +
    '<div class="livecode-settings-row-desc">' + desc + "</div></div>" +
    '<label class="lc-switch"><input type="checkbox" data-browser-setting="' + key + '"' + (on ? " checked" : "") + ' aria-label="' + _livecodeEscapeHtml(title) + '"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label></div>';
}

function _livecodeBrowserSegmentRowHtml(key, title, desc, options) {
  if (!_livecodeBrowserSettings) return "";
  const current = String(_livecodeBrowserSetting(key));
  const seg = options.map(function(o) {
    const on = o.value === current;
    return '<button type="button" class="lc-btn' + (on ? " is-active" : "") + '" data-settings-action="browser-choice" data-browser-key="' + key +
      '" data-value="' + o.value + '" role="radio" aria-checked="' + on + '">' + o.label + "</button>";
  }).join("");
  return _livecodeSettingsRowHtml(title, desc, '<div class="lc-segmented" role="radiogroup" aria-label="' + _livecodeEscapeHtml(title) + '">' + seg + "</div>");
}

function _livecodeSettingsDesignGateRowHtml() {
  return _livecodeBrowserSwitchRowHtml("design_gate", "Keep working until the design matches",
    "The agent keeps comparing and fixing until the page matches the design at the accuracy above.");
}

function _livecodeSettingsBrowserViewRowsHtml() {
  return _livecodeBrowserSwitchRowHtml("agent_tabs", "Subagents browse in their own tabs", "Each subagent opens its own tab instead of using yours.") +
    _livecodeBrowserSegmentRowHtml("view_quality", "Live view quality", "Sharp streams at your screen's full resolution; Standard uses less bandwidth.",
      [{ value: "sharp", label: "Sharp" }, { value: "standard", label: "Standard" }]) +
    _livecodeBrowserSegmentRowHtml("default_viewport", "Default viewport", "Size used for new browser tabs.",
      [{ value: "fit", label: "Fit pane" }, { value: "desktop", label: "Desktop 1920×1080" }]);
}

function _livecodeSettingsAutomationRowHtml() {
  return _livecodeBrowserSwitchRowHtml("reduce_automation_signals", "Reduce automation signals",
    "Hides the obvious markers that a page is driven by automation (navigator.webdriver, a missing chrome object, an empty plugin list) so sites you test behave as they do for a normal visitor. It does not solve captchas or hide your address; a site with strong bot protection can still refuse the built-in browser, and attaching your own Chrome is the answer then. Applies to browsers opened after the change.");
}

function _livecodeSaveBrowserSetting(key, value) {
  const body = {};
  body[key] = value;
  return fetch("/livecode/browser/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data || {} }; }); })
    .then(function(res) {
      if (!res.ok || res.data.error) throw new Error(res.data.error || "Could not save the setting.");
      _livecodeBrowserSettings = Object.assign({}, _livecodeBrowserSettings || {}, body, res.data);
      _livecodeRenderSettingsPage();
      if (key === "view_quality") _livecodeBrowserRefreshView();
    })
    .catch(function(err) {
      _livecodeShowIdeToast(err.message || String(err));
      _livecodeLoadBrowserSettings();
    });
}

function _livecodeSaveMatchThreshold(value) {
  return fetch("/livecode/browser/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ design_accuracy: value }) })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data || {} }; }); })
    .then(function(res) {
      if (!res.ok || res.data.error) throw new Error(res.data.error || "Could not save the accuracy.");
      _livecodeBrowserSettings = Object.assign({}, _livecodeBrowserSettings || {}, { design_accuracy: value }, res.data);
      _livecodeRenderSettingsPage();
    })
    .catch(function(err) {
      _livecodeShowIdeToast(err.message || String(err));
      _livecodeLoadBrowserSettings();
    });
}

function _livecodeSettingsFigmaRowHtml() {
  const status = _livecodeFigmaStatus;
  if (!status) {
    _livecodeLoadFigmaStatus();
    return _livecodeSettingsRowHtml("Figma (optional)", "Checking for a Figma token…", "");
  }
  const without = " Without it, Figma links open in the browser like any design tool's, and screenshots work too.";
  if (status.source === "env") {
    return _livecodeSettingsRowHtml("Figma (optional)", "The agent reads Figma links through Figma's API with the token in " +
      _livecodeEscapeHtml(status.env || "FIGMA_TOKEN") + " on the LiveCode server. Frames are fetched once and reused, since Figma limits API calls.", "");
  }
  if (status.configured) {
    return _livecodeSettingsRowHtml("Figma (optional)", "A token is saved: the agent reads Figma links through Figma's API (exact layer boxes, colours and fonts), fetching each frame once, since Figma limits API calls." + without,
      _livecodeSettingsButton("Replace…", "figma-token") + " " + _livecodeSettingsButton("Remove", "figma-clear"));
  }
  return _livecodeSettingsRowHtml("Figma (optional)", "Add a personal access token (in Figma: Settings → Security → Personal access tokens, with read access to files) so the agent reads Figma frames through Figma's API: exact layer boxes, colours and fonts." + without,
    _livecodeSettingsButton("Add token…", "figma-token"));
}

function _livecodeSaveFigmaToken(body) {
  return fetch("/livecode/figma/token", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data || {} }; }); })
    .then(function(res) {
      if (!res.ok || res.data.error) throw new Error(res.data.error || "Could not save the token.");
      _livecodeFigmaStatus = res.data;
      _livecodeRenderSettingsPage();
      return res.data;
    });
}

function _livecodeSetBrowserConnection(body) {
  return fetch("/livecode/browser/connection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data || {} }; }); })
    .then(function(res) {
      if (!res.ok || res.data.error) throw new Error(res.data.error || "Could not change the browser.");
      _livecodeBrowserConnection = res.data;
      _livecodeRenderSettingsPage();
      if (typeof _livecodeBrowserReset === "function") {
        _livecodeBrowserReset();
        if (_livecodeBrowserVisible()) _livecodeShowBrowserSurface();
      }
      return res.data;
    });
}

let _livecodeLlmSettings = null;
let _livecodeLlmSettingsLoading = false;

function _livecodeLoadLlmSettings() {
  if (_livecodeLlmSettingsLoading) return;
  _livecodeLlmSettingsLoading = true;
  fetch("/livecode/llm/settings").then(function(r) { return r.json(); }).then(function(data) {
    if (data && data.success) _livecodeLlmSettings = data;
  }).catch(function() {}).finally(function() {
    _livecodeLlmSettingsLoading = false;
    _livecodeSyncComposerModels();
    if (_livecodeSettingsVisible("models")) _livecodeRenderSettingsPage();
  });
}

function _livecodeSaveLlmSettings(payload, toast) {
  return fetch("/livecode/llm/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data || !data.success) throw new Error((data && data.error) || "Could not save");
      _livecodeLlmSettings = data;
      _livecodeSyncComposerModels();
      if (toast) _livecodeShowIdeToast(toast);
      if (_livecodeSettingsVisible("models")) _livecodeRenderSettingsPage();
      return data;
    })
    .catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
}

function _livecodeChatModel() {
  if (_livecodeLlmSettings && _livecodeLlmSettings.best_auto) return "auto";
  return typeof window.getLivecodeAiModel === "function" ? window.getLivecodeAiModel() : undefined;
}

// Composer dropdown: Auto plus the selected provider's models.
function _livecodeSyncComposerModels() {
  document.body.classList.toggle("livecode-best-auto", !!(_livecodeLlmSettings && _livecodeLlmSettings.best_auto));
  const models = (_livecodeLlmSettings && _livecodeLlmSettings.models) || [];
  window.LIVECODE_MODEL_OPTIONS = [{ value: "auto", label: "Auto" }].concat(models.map(function(m) {
    return { value: m.value, label: m.label + (m.vision === false ? " (text)" : "") };
  }));
  const current = typeof window.getLivecodeAiModel === "function" ? window.getLivecodeAiModel() : "auto";
  const known = window.LIVECODE_MODEL_OPTIONS.some(function(o) { return o.value === current; });
  if (!known && typeof window.setChatbotModelValue === "function") window.setChatbotModelValue("auto");
  else if (typeof window.initChatbotModelSelectors === "function") window.initChatbotModelSelectors();
}

function _livecodeSettingsModelsHtml() {
  if (!_livecodeLlmSettings) _livecodeLoadLlmSettings();
  const st = _livecodeLlmSettings;
  const tab = _livecodeGetActiveChatTab();
  const used = tab ? Number(tab.contextUsed || 0) : 0;
  const limit = tab ? Number(tab.contextLimit || 0) : 0;
  const cost = tab ? Number(tab.costUsd || 0) : 0;
  let html = '<h2 class="livecode-settings-h">Models</h2>';
  if (!st) return html + '<div class="livecode-settings-group"><div class="livecode-settings-empty">Loading model settings…</div></div>';

  const bestToggle = '<label class="lc-switch"><input type="checkbox" data-llm-toggle="best_auto"' + (st.best_auto ? " checked" : "") +
    ' aria-label="Best model auto pick"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label>';
  const labelOf = function(value) {
    if (!value) return "none";
    const i = value.indexOf(":");
    const p = i > 0 ? _livecodeLlmProvider(value.slice(0, i)) : null;
    return _livecodeEscapeHtml((p && p.label ? p.label + " · " : "") + value.slice(i + 1));
  };
  html += '<div class="livecode-settings-group">';
  html += _livecodeSettingsRowHtml("Best model auto pick", "Picks the best model from every provider with a key, and hides the model menu in chat.", bestToggle);
  html += "</div>";

  // One row per provider: key status, key buttons, and which one chat uses.
  html += '<h3 class="livecode-settings-subh">Providers</h3><div class="livecode-settings-group">';
  st.providers.forEach(function(p) {
    const isDefault = !st.best_auto && p.id === st.provider && p.configured;
    let ctl = "";
    if (!st.best_auto && p.configured && !isDefault) ctl += _livecodeSettingsButton("Make default", "llm-default", ' data-provider="' + p.id + '"');
    ctl += _livecodeSettingsButton(p.configured ? "Change key…" : "Add key…", "llm-key", ' data-provider="' + p.id + '"');
    if (p.configured) ctl += _livecodeSettingsButton("Remove", "llm-clear", ' data-provider="' + p.id + '"');
    const title = _livecodeEscapeHtml(p.label) + (isDefault ? ' <span class="livecode-settings-badge">Default</span>' : "");
    html += _livecodeSettingsRowHtml(title, p.configured ? "Key saved (" + _livecodeEscapeHtml(p.key_preview) + ")" : "No key", ctl);
  });
  html += "</div>";

  if (st.best_auto) {
    if (st.large_model) {
      html += '<div class="livecode-settings-group">' + _livecodeSettingsRowHtml("Auto picks",
        "Hard tasks: " + labelOf(st.large_model) + ". Quick tasks: " + labelOf(st.fast_model) + ".",
        _livecodeSettingsButton("Test", "llm-test")) + "</div>";
    }
  } else if (st.models.length && st.providers.some(function(p) { return p.id === st.provider && p.configured; })) {
    const names = st.models.map(function(m) { return _livecodeEscapeHtml(m.label) + (m.vision === false ? " (text only)" : ""); }).join(", ");
    const byId = function(id) {
      const bare = String(id || "").replace(/^[a-z]+:/, "");
      const m = st.models.find(function(x) { return x.id === id || x.id === bare; });
      return m ? _livecodeEscapeHtml(m.label) : labelOf(id);
    };
    let desc = names + ". Auto uses " + byId(st.large_model) + " for hard tasks and " + byId(st.fast_model) + " for quick ones";
    if (st.code_model && st.code_model !== st.fast_model) desc += ", and " + byId(st.code_model) + " for code changes";
    desc += ".";
    if (st.models.some(function(m) { return m.vision === false; })) {
      desc += st.image_model
        ? " Requests with images go to a model that reads them; a text-only model you pick gets them described by " + byId(st.image_model) + "."
        : " No model here reads images, so image attachments need a key for a provider whose models do.";
    }
    html += '<div class="livecode-settings-group">' + _livecodeSettingsRowHtml("Chat models", desc,
      _livecodeSettingsButton("Test", "llm-test")) + "</div>";
  } else {
    html += '<div class="livecode-settings-group"><div class="livecode-settings-empty">Add a key to a provider above to start chatting.</div></div>';
  }

  html += '<h3 class="livecode-settings-subh">This chat</h3><div class="livecode-settings-group">';
  html += _livecodeSettingsRowHtml("Context", limit ? used.toLocaleString() + " of " + limit.toLocaleString() + " tokens (" + Math.round((used / limit) * 100) + "%)" : "Shown after the first reply.", "");
  html += _livecodeSettingsRowHtml("Cost", cost > 0 ? "$" + cost.toFixed(cost < 0.01 ? 4 : 2) + " estimated" : "No usage yet.", "");
  html += "</div>";
  return html;
}

function _livecodeLlmProvider(id) {
  return ((_livecodeLlmSettings && _livecodeLlmSettings.providers) || []).find(function(p) { return p.id === id; }) || { id: id, label: id };
}

function _livecodeHandleLlmAction(action, btn) {
  const pid = btn.getAttribute("data-provider") || "";
  const p = _livecodeLlmProvider(pid);
  if (action === "llm-key") {
    _livecodeModalPrompt({
      title: p.label + " API key",
      message: (p.key_url ? "Get a key at " + p.key_url + ". " : "") + "It is saved on this computer, readable only by you.",
      placeholder: p.key_hint || "API key",
      inputType: "password",
      confirmText: "Save key",
    }).then(function(key) {
      if (key == null || !String(key).trim()) return;
      _livecodeShowIdeToast("Checking key…");
      _livecodeSaveLlmSettings({ provider: pid, api_key: String(key).trim() }, p.label + " key saved");
    });
  } else if (action === "llm-default") {
    _livecodeSaveLlmSettings({ provider: pid }, "Chat now uses " + p.label);
  } else if (action === "llm-clear") {
    _livecodeModalConfirm({ title: "Remove the " + p.label + " key?", message: "Chats can no longer use its models until you add a key again.", confirmText: "Remove", danger: true }).then(function(ok) {
      if (ok) _livecodeSaveLlmSettings({ provider: pid, clear: true }, p.label + " key removed");
    });
  } else if (action === "llm-test") {
    const model = _livecodeChatModel() || "auto";
    _livecodeShowIdeToast("Testing " + model + "…");
    fetch("/livecode/llm/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: model }) })
      .then(function(r) { return r.json(); })
      .then(function(data) { _livecodeShowIdeToast(data && data.success ? "Model replied: " + (data.reply || "(empty)") : "Test failed: " + ((data && data.error) || "unknown error")); })
      .catch(function(err) { _livecodeShowIdeToast("Test failed: " + (err.message || err)); });
  }
}

function _livecodeSettingsRulesHtml() {
  let html = '<h2 class="livecode-settings-h">Rules</h2><p class="livecode-settings-lead">The agent reads AGENTS.md, CLAUDE.md, and .claude/rules/*.md from the project (and parent folders up to the git root) at the start of every turn.</p><div class="livecode-settings-group">';
  if (!livecodeProjectPath) {
    html += '<div class="livecode-settings-empty">Open a project to see its rules.</div>';
  } else if (_livecodeSettingsRules === null) {
    html += '<div class="livecode-settings-empty">Looking for rule files…</div>';
  } else if (!_livecodeSettingsRules.length) {
    html += _livecodeSettingsRowHtml("No rule files", "Add AGENTS.md to tell the agent how to build, test, and style code in this project.", _livecodeSettingsButton("Create AGENTS.md", "create-rule", "", true));
  } else {
    html += _livecodeSettingsRules.map(function(rule) {
      const esc = _livecodeEscapeHtml(rule.path);
      return _livecodeSettingsRowHtml(
        _livecodeEscapeHtml(rule.name),
        _livecodeSettingsPathHtml(rule.path) + Number(rule.chars || 0).toLocaleString() + " characters",
        _livecodeSettingsButton("Open", "open-file", ' data-path="' + esc + '"')
      );
    }).join("");
  }
  html += "</div>";
  return html;
}

function _livecodeLoadSettingsRules() {
  if (!livecodeProjectPath) return;
  fetch("/livecode/rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_path: livecodeProjectPath, workspace: _livecodeCurrentWorkspacePayload() }),
  }).then(function(resp) { return resp.json(); }).then(function(data) {
    _livecodeSettingsRules = data && data.success ? (data.files || []) : [];
  }).catch(function() {
    _livecodeSettingsRules = [];
  }).finally(function() {
    if (_livecodeSettingsVisible("rules")) _livecodeRenderSettingsPage();
  });
}

function _livecodeSettingsIndexingHtml() {
  let html = '<h2 class="livecode-settings-h">Indexing</h2><p class="livecode-settings-lead">The codebase index powers file search, symbol lookup, and the project layout the agent sees.</p><div class="livecode-settings-group">';
  const count = function(n, noun) { return n.toLocaleString() + " " + noun + (n === 1 ? "" : "s"); };
  let status = !livecodeProjectPath ? "Open a project to index it." : (livecodeIndexReady ? count(livecodeIndexFileCount, "file") : "Indexing…");
  if (livecodeProjectPath && livecodeIndexReady) {
    status += livecodeIndexSymbolCount ? " · " + count(livecodeIndexSymbolCount, "symbol") : "";
    if (livecodeIndexTruncated) status += " · large project: the file list stops at " + livecodeIndexFileCount.toLocaleString() + " files (search still covers everything)";
  }
  html += _livecodeSettingsRowHtml("Codebase index", status, livecodeProjectPath ? _livecodeSettingsButton("Re-index", "reindex") : "");
  html += "</div>";
  return html;
}


const LIVECODE_MCP_DISABLED_TOOLS_PREFIX = "livecode_mcp_disabled_tools_v1:";
const _LIVECODE_MCP_CHIP_LIMIT = 24;

function _livecodeMcpDisabledToolsKey() {
  const identity = _livecodeMcpWorkspaceIdentity();
  return identity ? LIVECODE_MCP_DISABLED_TOOLS_PREFIX + identity : "";
}

function _livecodeMcpDisabledTools() {
  const key = _livecodeMcpDisabledToolsKey();
  if (!key) return {};
  try {
    const parsed = JSON.parse(_livecodeStorageGet(key) || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (e) {
    return {};
  }
}

function _livecodeSaveMcpDisabledTools(map) {
  const key = _livecodeMcpDisabledToolsKey();
  if (!key) return;
  const clean = {};
  Object.keys(map || {}).forEach(function(server) {
    const tools = (map[server] || []).filter(Boolean);
    if (tools.length) clean[server] = tools;
  });
  if (Object.keys(clean).length) _livecodeStorageSet(key, JSON.stringify(clean));
  else _livecodeStorageRemove(key);
}

function _livecodeMcpDisabledToolsForRequest() {
  const map = _livecodeMcpDisabledTools();
  const out = {};
  livecodeMcpSelectedServers.forEach(function(server) {
    if (map[server] && map[server].length) out[server] = map[server].slice();
  });
  return Object.keys(out).length ? out : undefined;
}

function _livecodeToggleMcpTool(server, tool) {
  const map = _livecodeMcpDisabledTools();
  const list = map[server] || [];
  const index = list.indexOf(tool);
  if (index >= 0) list.splice(index, 1);
  else list.push(tool);
  map[server] = list;
  _livecodeSaveMcpDisabledTools(map);
}

function _livecodeMcpServerState(server) {
  if (server.config_disabled) return "disabled";
  if (!livecodeMcpSelectedServers.has(server.name)) return "off";
  if (server.error) return "error";
  return server.connected ? "ok" : "pending";
}

function _livecodeMcpStateLabel(server, state) {
  const tools = Number(server.tool_count || 0);
  if (state === "disabled") return "Disabled in config";
  if (state === "off") return tools ? tools + " tools · off" : "Off";
  if (state === "error") return "Error";
  if (state === "pending") return "Connecting…";
  return tools === 1 ? "1 tool" : tools + " tools";
}

function _livecodeMcpSourceLabel(server) {
  if (server.builtin) return "Built in";
  const path = _livecodeShortPath(server.config_path || "");
  const where = server.source === "project" && server.folder_name ? server.folder_name + " · " : "";
  return where + path;
}

function _livecodeMcpServerCardHtml(server) {
  const name = String(server.name || "");
  const esc = _livecodeEscapeHtml(name);
  const state = _livecodeMcpServerState(server);
  const enabled = livecodeMcpSelectedServers.has(name);
  const off = new Set((_livecodeMcpDisabledTools()[name]) || []);
  const tools = Array.isArray(server.tools) ? server.tools : [];
  const expanded = !!_livecodeSettingsMcpExpanded[name];
  const shown = expanded ? tools : tools.slice(0, _LIVECODE_MCP_CHIP_LIMIT);
  let chips = shown.map(function(tool) {
    const toolName = String(tool.name || "");
    const isOff = off.has(toolName);
    const tip = (isOff ? "Turned off · click to turn on" : "Click to turn off") + (tool.description ? " — " + tool.description : "");
    return '<button type="button" class="lc-btn livecode-mcp-chip' + (isOff ? " is-off" : "") + '" data-settings-action="mcp-tool" data-server="' + esc + '" data-tool="' + _livecodeEscapeHtml(toolName) + '" title="' + _livecodeEscapeHtml(tip) + '" aria-pressed="' + (!isOff) + '">' + _livecodeEscapeHtml(toolName) + "</button>";
  }).join("");
  if (tools.length > shown.length) {
    chips += '<button type="button" class="lc-btn livecode-mcp-chip is-more" data-settings-action="mcp-more" data-server="' + esc + '">+' + (tools.length - shown.length) + " more</button>";
  }
  const offCount = tools.filter(function(t) { return off.has(String(t.name || "")); }).length;
  const endpointText = server.transport === "stdio" ? (server.command_line || server.command || "") : (server.url || "");
  const endpoint = '<div class="livecode-mcp-card-line"><span class="livecode-mcp-card-key">' +
    (server.transport === "stdio" ? "Command" : _livecodeEscapeHtml(String(server.transport || "http").toUpperCase())) +
    '</span><code title="' + _livecodeEscapeHtml(endpointText) + '">' + _livecodeEscapeHtml(endpointText) + "</code></div>";
  const source = server.builtin
    ? "<span>Built in</span>"
    : _livecodeSettingsPathHtml(server.config_path || "", server.source === "project" && server.folder_name ? " · " + server.folder_name : "");
  const error = server.error && state === "error"
    ? '<div class="livecode-mcp-card-error"><div class="livecode-mcp-card-error-text">' + _livecodeEscapeHtml(String(server.error).slice(0, 600)) + "</div>" +
      (server.hint ? '<div class="livecode-mcp-card-hint">' + _livecodeEscapeHtml(server.hint) + "</div>" : "") +
      '<button type="button" class="lc-btn livecode-settings-link" data-settings-action="mcp-copy" data-server="' + esc + '">Copy diagnostics</button></div>'
    : "";
  const toolsBlock = tools.length
    ? '<div class="livecode-mcp-card-tools"><span class="livecode-mcp-card-key">Tools' + (offCount ? " · " + offCount + " off" : "") + '</span><div class="livecode-mcp-chips">' + chips + "</div></div>"
    : (state === "off" ? '<div class="livecode-mcp-card-note">Turn the server on to load its tools.</div>' : "");
  return '<div class="livecode-mcp-card is-' + state + '" data-mcp-card="' + esc + '">' +
    '<div class="livecode-mcp-card-head">' +
      '<span class="livecode-mcp-dot is-' + state + '" aria-hidden="true"></span>' +
      '<span class="livecode-mcp-card-name">' + _livecodeEscapeHtml(_livecodeMcpDisplayName(name)) + "</span>" +
      '<span class="livecode-mcp-card-state">' + _livecodeEscapeHtml(_livecodeMcpStateLabel(server, state)) + "</span>" +
      '<span class="livecode-mcp-card-actions">' +
        '<label class="lc-switch" title="' + (server.config_disabled ? "Disabled in its config file" : (enabled ? "Turn off" : "Turn on")) + '"><input type="checkbox" data-mcp-toggle="' + esc + '"' + (enabled ? " checked" : "") + (server.config_disabled ? " disabled" : "") + ' aria-label="Use ' + esc + '"><span class="lc-switch-track"><span class="lc-switch-thumb"></span></span></label>' +
        '<button type="button" class="lc-btn livecode-settings-icon-btn" data-settings-action="mcp-restart" data-server="' + esc + '" title="Reconnect" aria-label="Reconnect ' + esc + '"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.9-3.9L4 8.5"></path><path d="M4 4v4.5h4.5"></path><path d="M4 13a8 8 0 0 0 14.9 3.9l1.1-1.4"></path><path d="M20 20v-4.5h-4.5"></path></svg></button>' +
        (server.config_path && !server.builtin ? '<button type="button" class="lc-btn livecode-settings-icon-btn" data-settings-action="mcp-edit" data-server="' + esc + '" title="Edit its config file" aria-label="Edit ' + esc + ' config"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg></button>' : "") +
        (server.editable ? '<button type="button" class="lc-btn livecode-settings-icon-btn" data-settings-action="mcp-remove" data-server="' + esc + '" title="Remove" aria-label="Remove ' + esc + '"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"></path></svg></button>' : "") +
      "</span>" +
    "</div>" +
    '<div class="livecode-mcp-card-body">' + toolsBlock + endpoint +
      '<div class="livecode-mcp-card-line is-muted"><span class="livecode-mcp-card-key">Source</span>' + source + "</div>" +
      error +
    "</div></div>";
}

function _livecodeSettingsMcpHtml() {
  let html = '<div class="livecode-settings-head-row"><h2 class="livecode-settings-h">MCP Servers</h2><div class="livecode-settings-head-actions">' +
    _livecodeSettingsButton("Refresh", "mcp-refresh") +
    _livecodeSettingsButton('<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>Add new MCP server', "mcp-add", "", true) +
    "</div></div>" +
    '<p class="livecode-settings-lead">Model Context Protocol servers give the agent more tools. Turn a server on to offer its tools to the agent, and click a tool to turn it off. Servers are read from .mcp.json and .vscode/mcp.json in the project, and from ~/.claude.json, and LiveCode\'s own config.</p>';
  if (!livecodeProjectPath) {
    return html + '<div class="livecode-settings-group"><div class="livecode-settings-empty">Open a project to manage its MCP servers.</div></div>';
  }
  const servers = livecodeMcpServers || [];
  if (!servers.length) {
    return html + '<div class="livecode-settings-group"><div class="livecode-settings-empty">Loading MCP servers…</div></div>';
  }
  return html + '<div class="livecode-mcp-cards">' + servers.map(_livecodeMcpServerCardHtml).join("") + "</div>";
}

function _livecodeRefreshSettingsMcpIfVisible() {
  if (_livecodeSettingsVisible("mcp")) _livecodeRenderSettingsPage();
}

function _livecodeSetMcpServerEnabled(name, enabled) {
  if (!name) return;
  if (enabled) livecodeMcpSelectedServers.add(name);
  else livecodeMcpSelectedServers.delete(name);
  _livecodePersistMcpSelection();
  _livecodeUpdateMcpToggle();
  _livecodeRenderMcpServers();
  if (enabled) refreshLiveCodeMcpStatus({ probeServers: Array.from(livecodeMcpSelectedServers), preserveList: true });
}

async function _livecodeMcpServerRequest(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({
      project_path: _livecodeWorkspaceRefForMcp(),
      workspace: _livecodeCurrentWorkspacePayload(),
      enabled_servers: Array.from(livecodeMcpSelectedServers),
    }, body || {})),
  });
  const payload = await resp.json().catch(function() { return {}; });
  if (!resp.ok || !payload.success) throw new Error(payload.error || "Request failed (HTTP " + resp.status + ")");
  if (payload.status) _livecodeApplyMcpStatusPayload(payload.status);
  return payload;
}

async function _livecodeRemoveMcpServer(name) {
  const ok = await _livecodeModalConfirm({ title: "Remove " + name + "?", message: "It is deleted from its config file.", confirmText: "Remove", danger: true });
  if (!ok) return;
  try {
    await _livecodeMcpServerRequest("/livecode-mcp/servers/remove", { name: name });
    livecodeMcpSelectedServers.delete(name);
    _livecodePersistMcpSelection();
    _livecodeShowIdeToast("Removed " + name);
  } catch (err) {
    _livecodeShowIdeToast(err.message || String(err));
  }
  _livecodeRenderMcpServers();
}

function _livecodeBindSettingsOnce(view) {
  if (view._livecodeSettingsBound) return;
  view._livecodeSettingsBound = true;
  view.addEventListener("keydown", function(e) {
    const input = e.target;
    if (e.key !== "Escape" || !input || !input.hasAttribute || !input.hasAttribute("data-settings-search") || !input.value) return;
    e.stopPropagation();
    input.value = "";
    window._livecodeSettingsQuery = "";
    _livecodeRenderSettings();
  });
  view.addEventListener("click", function(e) {
    const sectionBtn = e.target.closest ? e.target.closest("[data-settings-section]") : null;
    if (sectionBtn) {
      e.preventDefault();
      window._livecodeSettingsQuery = "";
      _livecodeSettingsSection = sectionBtn.getAttribute("data-settings-section");
      if (_livecodeSettingsSection === "rules") _livecodeSettingsRules = null;
      _livecodeRenderSettings();
      if (_livecodeSettingsSection === "mcp") refreshLiveCodeMcpStatus({ probeServers: Array.from(livecodeMcpSelectedServers), preserveList: true });
      return;
    }
    const btn = e.target.closest ? e.target.closest("[data-settings-action]") : null;
    if (!btn) return;
    e.preventDefault();
    const action = btn.getAttribute("data-settings-action");
    const path = btn.getAttribute("data-path") || "";
    const server = btn.getAttribute("data-server") || "";
    if (typeof window._livecodeHandleSettingsActionExt === "function" && window._livecodeHandleSettingsActionExt(action, btn)) return;
    if (action === "open-project") openLiveCodeProjectBrowser();
    else if (action === "clone") openLiveCodeCloneDialog();
    else if (action === "workspace-manager") openLiveCodeWorkspaceManager();
    else if (action === "open-recent" && path) {
      _livecodeOpenProjectOrWorkspace(path);
      setTimeout(_livecodeRenderSettingsPage, 300);
    } else if (action === "remove-recent" && path) {
      _livecodeModalConfirm({ title: "Remove " + _livecodeProjectDisplayName(path) + " from recents?", message: "Its saved chats and memory are deleted too. Files in the folder are not touched.", confirmText: "Remove", danger: true }).then(function(ok) {
        if (!ok) return;
        removeLiveCodeRecentProject(path);
        _livecodeRenderSettingsPage();
      });
    } else if (action === "goto-section") {
      window._livecodeSettingsQuery = "";
      _livecodeSettingsSection = btn.getAttribute("data-section") || "general";
      _livecodeRenderSettings();
    } else if (action === "set-mode") {
      const mode = btn.getAttribute("data-mode") || "agent";
      window.livecodeChatMode = mode;
      try { localStorage.setItem(LIVECODE_CHAT_MODE_STORAGE_KEY, mode); } catch (err) {}
      _livecodeApplyChatModeToUI();
      _livecodeRenderSettingsPage();
    } else if (action === "set-density") {
      _livecodeSettingsSet("conversationDensity", btn.getAttribute("data-density") || "detailed");
      _livecodeApplyConversationDensity();
      _livecodeRenderSettingsPage();
    } else if (action === "set-step-grouping") {
      _livecodeSettingsSet("stepGrouping", btn.getAttribute("data-grouping") || "grouped");
      _livecodeApplyConversationDensity();
      _livecodeRenderSettingsPage();
    } else if (action === "browser-choice") {
      const key = btn.getAttribute("data-browser-key") || "";
      const value = btn.getAttribute("data-value") || "";
      if (key && value && String(_livecodeBrowserSetting(key)) !== value) _livecodeSaveBrowserSetting(key, value);
    } else if (action === "match-threshold-reset") {
      _livecodeSaveMatchThreshold((_livecodeBrowserSettings && _livecodeBrowserSettings.default_design_accuracy) || 90);
    } else if (action === "browser-cookies") {
      openLiveCodeBrowserCookies();
    } else if (action === "open-browser") {
      window.openLiveCodeBrowser();
    } else if (action === "figma-token") {
      _livecodeModalPrompt({
        title: "Figma access token",
        message: "Paste a personal access token (Figma → Settings → Security → Personal access tokens) with read access to files. It is kept on the LiveCode server, readable only by you.",
        placeholder: "figd_…",
        inputType: "password",
        confirmText: "Save token",
      }).then(function(token) {
        if (token == null || !String(token).trim()) return;
        _livecodeSaveFigmaToken({ token: String(token).trim() })
          .then(function() { _livecodeShowIdeToast("Figma token saved"); })
          .catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
      });
    } else if (action === "figma-clear") {
      _livecodeModalConfirm({ title: "Remove the Figma token?", message: "Figma links then open in the browser like any design tool's.", confirmText: "Remove", danger: true }).then(function(ok) {
        if (!ok) return;
        _livecodeSaveFigmaToken({ clear: true }).catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
      });
    } else if (action.indexOf("llm-") === 0) {
      _livecodeHandleLlmAction(action, btn);
    } else if (action === "change-model") {
      const trigger = document.querySelector("[data-chatbot-model-trigger]");
      if (trigger) trigger.click();
    } else if (action === "open-file" && path) {
      openFileInEditorFromPath(path);
    } else if (action === "create-rule") {
      fetch("/livecode/rules/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_path: livecodeProjectPath }) })
        .then(function(resp) { return resp.json(); })
        .then(function(data) {
          if (!data || !data.success) throw new Error((data && data.error) || "Could not create AGENTS.md");
          _livecodeSettingsRules = null;
          _livecodeScheduleSilentTreeRefresh();
          openFileInEditorFromPath(data.path);
        })
        .catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); });
    } else if (action === "reindex") {
      btn.disabled = true;
      btn.textContent = "Indexing…";
      livecodeIndexReady = false;
      fetch("/livecode/index", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_path: livecodeProjectPath, workspace: _livecodeCurrentWorkspacePayload(), force: true }) })
        .then(function(resp) { return resp.json(); })
        .then(function(data) {
          if (data && data.success) _livecodeApplyIndexResult(data);
        })
        .catch(function() {})
        .finally(function() { if (_livecodeSettingsVisible("indexing")) _livecodeRenderSettingsPage(); });
    } else if (action === "mcp-add") {
      openLiveCodeMcpAddDialog();
    } else if (action === "mcp-refresh") {
      refreshLiveCodeMcpConnections();
    } else if (action === "mcp-restart" && server) {
      if (!livecodeMcpSelectedServers.has(server)) _livecodeSetMcpServerEnabled(server, true);
      else runLiveCodeMcpAction("restart", server);
    } else if (action === "mcp-edit" && server) {
      const found = _livecodeFindMcpServer(server);
      if (found && found.config_path) openFileInEditorFromPath(found.config_path);
    } else if (action === "mcp-remove" && server) {
      _livecodeRemoveMcpServer(server);
    } else if (action === "mcp-copy" && server) {
      _livecodeCopyToClipboard(_livecodeMcpDiagnosticsText(_livecodeFindMcpServer(server)), "Copied MCP diagnostics");
    } else if (action === "mcp-more" && server) {
      _livecodeSettingsMcpExpanded[server] = true;
      _livecodeRenderSettingsPage();
    } else if (action === "mcp-tool" && server) {
      _livecodeToggleMcpTool(server, btn.getAttribute("data-tool") || "");
      _livecodeRenderSettingsPage();
      _livecodeRenderMcpServers();
    }
  });
  view.addEventListener("input", function(e) {
    const input = e.target;
    if (input && typeof window._livecodeHandleSettingsInputExt === "function") window._livecodeHandleSettingsInputExt(input);
    if (!input || !input.hasAttribute || !input.hasAttribute("data-design-accuracy")) return;
    const number = Number(input.value);
    if (!Number.isFinite(number)) return;
    view.querySelectorAll("[data-design-accuracy]").forEach(function(other) { if (other !== input) other.value = String(number); });
    const meaning = view.querySelector("[data-accuracy-meaning]");
    const fallback = (_livecodeBrowserSettings && Number(_livecodeBrowserSettings.default_design_accuracy)) || 90;
    if (meaning) meaning.textContent = _livecodeAccuracyMeaning(number, fallback);
  });
  view.addEventListener("change", function(e) {
    const input = e.target;
    if (input && input.getAttribute && input.getAttribute("data-llm-toggle") === "best_auto") {
      _livecodeSaveLlmSettings({ best_auto: !!input.checked });
      return;
    }
    if (input && input.getAttribute && typeof window._livecodeHandleSettingsChangeExt === "function" && window._livecodeHandleSettingsChangeExt(input)) return;
    if (!input || input.tagName !== "INPUT") return;
    const setting = input.getAttribute("data-setting");
    if (setting) {
      _livecodeSettingsSet(setting, !!input.checked);
      if (setting === "showHiddenFiles") {
        _livecodeSyncFinderHiddenToggle();
        const search = document.getElementById("livecode-finder-search");
        if (typeof window.livecodeBrowserFilterList === "function" && document.getElementById("livecode-finder-show-hidden")) window.livecodeBrowserFilterList(search ? search.value : "");
      }
      if (setting === "autoRunQueue") {
        _livecodeRenderQueueBar();
        const tab = _livecodeGetActiveChatTab();
        if (input.checked && tab && !tab.queuePaused) _livecodeMaybeRunQueue(tab);
      }
      return;
    }
    if (input.hasAttribute("data-chrome-toggle")) {
      if (input.checked) {
        _livecodeModalPrompt({
          title: "Attach your Chrome",
          message: "Start Chrome with --remote-debugging-port=9222 --user-data-dir=<a profile folder> (Chrome needs a profile of its own for this), sign in to what the agent should see, then enter its address. The browser's open tabs close.",
          value: "http://127.0.0.1:9222",
          placeholder: "http://127.0.0.1:9222",
          confirmText: "Attach",
        }).then(function(url) {
          if (url == null || !String(url).trim()) { _livecodeRenderSettingsPage(); return; }
          _livecodeShowIdeToast("Attaching to Chrome…");
          _livecodeSetBrowserConnection({ cdp_url: String(url).trim() })
            .then(function(data) { _livecodeShowIdeToast("Attached to Chrome" + (data.version ? " " + data.version : "")); })
            .catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); _livecodeRenderSettingsPage(); });
        });
      } else {
        _livecodeSetBrowserConnection({ disconnect: true })
          .then(function() { _livecodeShowIdeToast("Detached from Chrome"); })
          .catch(function(err) { _livecodeShowIdeToast(err.message || String(err)); _livecodeRenderSettingsPage(); });
      }
      return;
    }
    const browserSetting = input.getAttribute("data-browser-setting");
    if (browserSetting) {
      _livecodeSaveBrowserSetting(browserSetting, !!input.checked);
      return;
    }
    if (input.hasAttribute("data-design-accuracy")) {
      const number = Number(input.value);
      const least = (_livecodeBrowserSettings && Number(_livecodeBrowserSettings.min_design_accuracy)) || 50;
      if (!Number.isFinite(number) || number < least || number > 100) {
        _livecodeShowIdeToast("Enter a percentage from " + least + " to 100.");
        _livecodeRenderSettingsPage();
        return;
      }
      _livecodeSaveMatchThreshold(number);
      return;
    }
    const server = input.getAttribute("data-mcp-toggle");
    if (server) _livecodeSetMcpServerEnabled(server, !!input.checked);
  });
}


function _livecodeCurDialog(id, innerHtml) {
  let overlay = document.getElementById(id);
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = id;
    overlay.className = "livecode-dialog-overlay theme-transition";
    overlay.hidden = true;
    document.body.appendChild(overlay);
    overlay.addEventListener("mousedown", function(e) {
      if (e.target === overlay && !overlay.classList.contains("is-busy")) overlay.hidden = true;
    });
    overlay.addEventListener("keydown", function(e) {
      if (e.key === "Escape" && !overlay.classList.contains("is-busy")) {
        e.stopPropagation();
        overlay.hidden = true;
      }
    });
  }
  overlay.innerHTML = innerHtml;
  overlay.classList.remove("is-busy");
  overlay.hidden = false;
  return overlay;
}

function _livecodeDialogError(overlay, message) {
  const el = overlay.querySelector(".livecode-dialog-error");
  if (!el) return;
  el.textContent = message || "";
  el.hidden = !message;
}

function _livecodeParseKeyValueLines(text, separator) {
  const out = {};
  String(text || "").split("\n").forEach(function(line) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.charAt(0) === "#") return;
    const index = trimmed.indexOf(separator);
    if (index <= 0) return;
    out[trimmed.slice(0, index).trim()] = trimmed.slice(index + separator.length).trim();
  });
  return out;
}

function _livecodeFormField(form, name) {
  return form.elements.namedItem(name);
}

function openLiveCodeMcpAddDialog() {
  if (!livecodeProjectPath) {
    _livecodeShowIdeToast("Open a project before adding MCP servers.");
    return;
  }
  toggleLiveCodeMcpPanel(false);
  const overlay = _livecodeCurDialog("livecode-mcp-add-dialog",
    '<form class="livecode-dialog" role="dialog" aria-modal="true" aria-labelledby="livecode-mcp-add-title" novalidate>' +
      '<div class="livecode-dialog-title" id="livecode-mcp-add-title">Add MCP server</div>' +
      '<label class="livecode-field"><span>Name</span><input name="name" placeholder="github" autocomplete="off" spellcheck="false"></label>' +
      '<div class="livecode-field"><span>Type</span><div class="lc-segmented" data-group="type">' +
        '<button type="button" class="lc-btn is-active" data-value="stdio">Command</button>' +
        '<button type="button" class="lc-btn" data-value="http">HTTP</button>' +
        '<button type="button" class="lc-btn" data-value="sse">SSE</button>' +
      "</div></div>" +
      '<label class="livecode-field" data-for="stdio"><span>Command</span><input name="command" placeholder="npx" autocomplete="off" spellcheck="false"></label>' +
      '<label class="livecode-field" data-for="stdio"><span>Arguments</span><input name="args" placeholder="-y @modelcontextprotocol/server-github" autocomplete="off" spellcheck="false"></label>' +
      '<label class="livecode-field" data-for="stdio"><span>Environment <em>one KEY=value per line</em></span><textarea name="env" rows="3" placeholder="GITHUB_TOKEN=${env:GITHUB_TOKEN}" spellcheck="false"></textarea></label>' +
      '<label class="livecode-field" data-for="http sse" hidden><span>URL</span><input name="url" placeholder="https://example.com/mcp" autocomplete="off" spellcheck="false"></label>' +
      '<label class="livecode-field" data-for="http sse" hidden><span>Headers <em>one Name: value per line</em></span><textarea name="headers" rows="3" placeholder="Authorization: Bearer ${env:API_TOKEN}" spellcheck="false"></textarea></label>' +
      '<div class="livecode-field"><span>Save to</span><div class="lc-segmented" data-group="scope">' +
        '<button type="button" class="lc-btn is-active" data-value="project">This project</button>' +
        '<button type="button" class="lc-btn" data-value="global">All projects</button>' +
      "</div></div>" +
      '<div class="livecode-dialog-error" role="alert" hidden></div>' +
      '<div class="livecode-dialog-actions">' +
        '<button type="button" class="lc-btn livecode-settings-btn" data-dialog="cancel">Cancel</button>' +
        '<button type="submit" class="lc-btn livecode-settings-btn is-primary">Add server</button>' +
      "</div>" +
    "</form>");
  const form = overlay.querySelector("form");
  const state = { type: "stdio", scope: "project" };
  overlay.querySelectorAll(".lc-segmented").forEach(function(group) {
    group.addEventListener("click", function(e) {
      const btn = e.target.closest("[data-value]");
      if (!btn) return;
      e.preventDefault();
      group.querySelectorAll("[data-value]").forEach(function(b) { b.classList.toggle("is-active", b === btn); });
      state[group.getAttribute("data-group")] = btn.getAttribute("data-value");
      if (group.getAttribute("data-group") === "type") {
        overlay.querySelectorAll("[data-for]").forEach(function(field) {
          field.hidden = field.getAttribute("data-for").split(" ").indexOf(state.type) === -1;
        });
      }
    });
  });
  overlay.querySelector('[data-dialog="cancel"]').addEventListener("click", function() { overlay.hidden = true; });
  form.addEventListener("submit", async function(e) {
    e.preventDefault();
    const name = _livecodeFormField(form, "name").value.trim();
    if (!name) {
      _livecodeDialogError(overlay, "Give the server a name.");
      return;
    }
    const spec = { type: state.type };
    if (state.type === "stdio") {
      spec.command = _livecodeFormField(form, "command").value.trim();
      spec.args = _livecodeFormField(form, "args").value.trim();
      spec.env = _livecodeParseKeyValueLines(_livecodeFormField(form, "env").value, "=");
    } else {
      spec.url = _livecodeFormField(form, "url").value.trim();
      spec.headers = _livecodeParseKeyValueLines(_livecodeFormField(form, "headers").value, ":");
    }
    overlay.classList.add("is-busy");
    _livecodeDialogError(overlay, "");
    try {
      await _livecodeMcpServerRequest("/livecode-mcp/servers/add", { name: name, scope: state.scope, server: spec });
      overlay.hidden = true;
      _livecodeShowIdeToast("Added " + name);
      _livecodeSetMcpServerEnabled(name, true);
      _livecodeSettingsMcpFocus = name;
      if (_livecodeSettingsVisible()) _livecodeSettingsSection = "mcp";
      openLiveCodeSettings("mcp");
    } catch (err) {
      _livecodeDialogError(overlay, err.message || String(err));
    } finally {
      overlay.classList.remove("is-busy");
    }
  });
  setTimeout(function() { _livecodeFormField(form, "name").focus(); }, 30);
}
window.openLiveCodeMcpAddDialog = openLiveCodeMcpAddDialog;

function _livecodeDefaultCloneParent() {
  const recent = getLiveCodeRecentProjects().find(function(p) { return !_livecodeIsWorkspaceFilePath(p); });
  if (recent) return recent.replace(/[\\/][^\\/]+[\\/]?$/, "") || "~";
  return "~";
}

function openLiveCodeCloneDialog() {
  const overlay = _livecodeCurDialog("livecode-clone-dialog",
    '<form class="livecode-dialog" role="dialog" aria-modal="true" aria-labelledby="livecode-clone-title" novalidate>' +
      '<div class="livecode-dialog-title" id="livecode-clone-title">Clone repository</div>' +
      '<label class="livecode-field"><span>Repository URL</span><input name="url" placeholder="https://github.com/org/repo.git" autocomplete="off" spellcheck="false"></label>' +
      '<div class="livecode-field"><span>Clone into</span><div class="livecode-inline"><input name="parent" autocomplete="off" spellcheck="false"><button type="button" class="lc-btn livecode-settings-btn" data-dialog="browse">Browse…</button></div></div>' +
      '<div class="livecode-dialog-error" role="alert" hidden></div>' +
      '<div class="livecode-dialog-actions">' +
        '<button type="button" class="lc-btn livecode-settings-btn" data-dialog="cancel">Cancel</button>' +
        '<button type="submit" class="lc-btn livecode-settings-btn is-primary" data-dialog="submit">Clone</button>' +
      "</div>" +
    "</form>");
  const form = overlay.querySelector("form");
  _livecodeFormField(form, "parent").value = _livecodeDefaultCloneParent();
  overlay.querySelector('[data-dialog="cancel"]').addEventListener("click", function() { overlay.hidden = true; });
  overlay.querySelector('[data-dialog="browse"]').addEventListener("click", function() {
    overlay.hidden = true;
    window.openLiveCodeFolderBrowser(function(path) {
      overlay.hidden = false;
      if (path) _livecodeFormField(form, "parent").value = path;
      _livecodeFormField(form, "url").focus();
    }, _livecodeFormField(form, "parent").value || "~");
  });
  form.addEventListener("submit", async function(e) {
    e.preventDefault();
    const url = _livecodeFormField(form, "url").value.trim();
    if (!url) {
      _livecodeDialogError(overlay, "Paste the repository URL.");
      return;
    }
    const submit = overlay.querySelector('[data-dialog="submit"]');
    overlay.classList.add("is-busy");
    submit.textContent = "Cloning…";
    _livecodeDialogError(overlay, "");
    try {
      const resp = await fetch("/livecode/clone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, parent_dir: _livecodeFormField(form, "parent").value.trim() || "~" }),
      });
      const data = await resp.json().catch(function() { return {}; });
      if (!resp.ok || !data.success) throw new Error(data.error || "git clone failed");
      overlay.hidden = true;
      setLiveCodeProject(data.path);
      _livecodeShowIdeToast("Cloned into " + _livecodeShortPath(data.path));
    } catch (err) {
      _livecodeDialogError(overlay, err.message || String(err));
    } finally {
      overlay.classList.remove("is-busy");
      submit.textContent = "Clone";
    }
  });
  setTimeout(function() { _livecodeFormField(form, "url").focus(); }, 30);
}
window.openLiveCodeCloneDialog = openLiveCodeCloneDialog;

function openLiveCodeNewProjectDialog() {
  const overlay = _livecodeCurDialog("livecode-new-project-dialog",
    '<form class="livecode-dialog" role="dialog" aria-modal="true" aria-labelledby="livecode-new-project-title" novalidate>' +
      '<div class="livecode-dialog-title" id="livecode-new-project-title">New project</div>' +
      '<label class="livecode-field"><span>Project name</span><input name="name" placeholder="my-project" autocomplete="off" spellcheck="false"></label>' +
      '<div class="livecode-field"><span>Create in</span><div class="livecode-inline"><input name="parent" autocomplete="off" spellcheck="false"><button type="button" class="lc-btn livecode-settings-btn" data-dialog="browse">Browse…</button></div></div>' +
      '<div class="livecode-dialog-error" role="alert" hidden></div>' +
      '<div class="livecode-dialog-actions">' +
        '<button type="button" class="lc-btn livecode-settings-btn" data-dialog="cancel">Cancel</button>' +
        '<button type="submit" class="lc-btn livecode-settings-btn is-primary" data-dialog="submit">Create</button>' +
      "</div>" +
    "</form>");
  const form = overlay.querySelector("form");
  _livecodeFormField(form, "parent").value = _livecodeDefaultCloneParent();
  overlay.querySelector('[data-dialog="cancel"]').addEventListener("click", function() { overlay.hidden = true; });
  overlay.querySelector('[data-dialog="browse"]').addEventListener("click", function() {
    overlay.hidden = true;
    window.openLiveCodeFolderBrowser(function(path) {
      overlay.hidden = false;
      if (path) _livecodeFormField(form, "parent").value = path;
      _livecodeFormField(form, "name").focus();
    }, _livecodeFormField(form, "parent").value || "~");
  });
  form.addEventListener("submit", async function(e) {
    e.preventDefault();
    const name = _livecodeFormField(form, "name").value.trim();
    if (!name) {
      _livecodeDialogError(overlay, "Enter a project name.");
      return;
    }
    const submit = overlay.querySelector('[data-dialog="submit"]');
    overlay.classList.add("is-busy");
    submit.textContent = "Creating…";
    _livecodeDialogError(overlay, "");
    try {
      const resp = await fetch("/livecode/new-project", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name, parent_dir: _livecodeFormField(form, "parent").value.trim() || "~" }),
      });
      const data = await resp.json().catch(function() { return {}; });
      if (!resp.ok || !data.success) throw new Error(data.error || "Could not create the project.");
      overlay.hidden = true;
      setLiveCodeProject(data.path);
      _livecodeShowIdeToast("Created " + _livecodeShortPath(data.path));
    } catch (err) {
      _livecodeDialogError(overlay, err.message || String(err));
    } finally {
      overlay.classList.remove("is-busy");
      submit.textContent = "Create";
    }
  });
  setTimeout(function() { _livecodeFormField(form, "name").focus(); }, 30);
}
window.openLiveCodeNewProjectDialog = openLiveCodeNewProjectDialog;


function _livecodeRenderWelcomeRecents() {
  const wrap = document.getElementById("livecode-welcome-recent");
  const list = document.getElementById("livecode-welcome-recent-list");
  if (!wrap || !list) return;
  const items = getLiveCodeRecentProjects().slice(0, 6);
  wrap.hidden = !items.length;
  list.innerHTML = items.map(function(path) {
    const esc = _livecodeEscapeHtml(path);
    const parent = _livecodeShortPath(path.replace(/[\\/][^\\/]+[\\/]?$/, "") || path);
    return '<button type="button" class="livecode-welcome-recent-item lc-btn" data-path="' + esc + '" title="' + esc + '">' +
      '<span class="livecode-welcome-recent-name">' + _livecodeEscapeHtml(_livecodeProjectDisplayName(path)) + "</span>" +
      '<span class="livecode-welcome-recent-path">' + _livecodeEscapeHtml(parent) + "</span></button>";
  }).join("");
  if (!list._livecodeBound) {
    list._livecodeBound = true;
    list.addEventListener("click", function(e) {
      const item = e.target.closest ? e.target.closest("[data-path]") : null;
      if (!item) return;
      e.preventDefault();
      _livecodeOpenProjectOrWorkspace(item.getAttribute("data-path"));
    });
  }
}
