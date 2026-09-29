/* LiveCode — browser-only TypeScript/JavaScript code intelligence.
 *
 * Monaco already ships a TypeScript worker (templates/js/monaco-editor/vs/language/
 * typescript). It just needs: (1) React/JSX-aware compiler options, (2) the ambient
 * React `.d.ts` (react-types.bundle.js -> window.WB_REACT_DTS), and (3) the project's
 * own source files loaded as hidden models so cross-file go-to-definition / find-
 * references / rename resolve. No server process.
 *
 * Public surface (called from livecode.js):
 *   installLivecodeTsIntel()                 - once, after vs/editor/editor.main loads
 *   window.WBTsIntel.onProjectOpen(next, prev)
 *   window.WBTsIntel.disposeAll()
 */
(function () {
  "use strict";

  var TS_EXTS = ["ts", "tsx", "js", "jsx", "mts", "cts", "mjs", "cjs"];
  var _installed = false;
  var _preloadedUris = new Set(); // model URIs we created (and therefore own)
  var _lastProject = null;
  var _preloadedProject = null; // project we've already built hidden models for
  var _reqSeq = 0;
  // Each hidden model adds Monaco language-change listeners; keep the count
  // bounded so a large repo can't trip the internal listener-leak guard.
  var PRELOAD_MODEL_CAP = 200;

  function langForPath(p) {
    var l = (p || "").toLowerCase();
    if (l.endsWith(".tsx")) return "typescript";
    if (l.endsWith(".ts") || l.endsWith(".mts") || l.endsWith(".cts")) return "typescript";
    if (l.endsWith(".jsx")) return "javascript";
    return "javascript";
  }

  function applyCompilerOptions(tsconfigText) {
    var ts = window.monaco.languages.typescript;
    var base = {
      target: ts.ScriptTarget.ESNext,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.NodeJs,
      jsx: ts.JsxEmit.ReactJSX,
      allowJs: true,
      checkJs: false,
      allowNonTsExtensions: true,
      esModuleInterop: true,
      allowSyntheticDefaultImports: true,
      skipLibCheck: true,
      resolveJsonModule: true,
      baseUrl: ".",
      lib: ["esnext", "dom", "dom.iterable"],
    };
    // Merge the project's real tsconfig/jsconfig compilerOptions + paths when present.
    if (tsconfigText) {
      try {
        var parsed = JSON.parse(tsconfigText.replace(/\/\*[\s\S]*?\*\/|(^|[^:])\/\/.*$/gm, "$1"));
        var co = (parsed && parsed.compilerOptions) || {};
        if (co.paths) base.paths = co.paths;
        if (co.baseUrl) base.baseUrl = co.baseUrl;
        if (co.jsxImportSource) base.jsxImportSource = co.jsxImportSource;
        if (typeof co.strict === "boolean") base.strict = co.strict;
      } catch (e) {
        /* keep defaults */
      }
    }
    ts.typescriptDefaults.setCompilerOptions(base);
    ts.javascriptDefaults.setCompilerOptions(base);
    ts.typescriptDefaults.setEagerModelSync(true);
    ts.javascriptDefaults.setEagerModelSync(true);
    var diag = { noSemanticValidation: false, noSyntaxValidation: false, onlyVisible: false };
    ts.typescriptDefaults.setDiagnosticsOptions(diag);
    ts.javascriptDefaults.setDiagnosticsOptions(diag);
  }

  function loadAmbientReactTypes() {
    var map = window.WB_REACT_DTS;
    if (!map) return;
    var ts = window.monaco.languages.typescript;
    Object.keys(map).forEach(function (uri) {
      try {
        ts.typescriptDefaults.addExtraLib(map[uri], uri);
        ts.javascriptDefaults.addExtraLib(map[uri], uri);
      } catch (e) {
        /* duplicate uri across redeploys is fine */
      }
    });
  }

  window.installLivecodeTsIntel = function installLivecodeTsIntel(tsconfigText) {
    if (_installed) return true;
    if (!window.monaco || !window.monaco.languages || !window.monaco.languages.typescript) {
      return false;
    }
    _installed = true;
    applyCompilerOptions(tsconfigText);
    loadAmbientReactTypes();
    return true;
  };

  function disposeAll() {
    if (!window.monaco || !window.monaco.editor) {
      _preloadedUris.clear();
      return;
    }
    _preloadedUris.forEach(function (uriStr) {
      try {
        var m = window.monaco.editor.getModel(window.monaco.Uri.parse(uriStr));
        // Never dispose a model that is currently attached to the editor / an open tab.
        if (m && !m.isAttachedToEditor()) m.dispose();
      } catch (e) {}
    });
    _preloadedUris.clear();
    _preloadedProject = null;
  }

  function preloadProject(projectPath) {
    if (!projectPath || !window.monaco || !window.monaco.editor) return;
    if (projectPath === _preloadedProject) return; // already built for this project
    _preloadedProject = projectPath;
    var mySeq = ++_reqSeq;
    var url =
      "/livecode/source-models?project=" +
      encodeURIComponent(projectPath) +
      "&exts=" +
      TS_EXTS.join(",");
    fetch(url)
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || mySeq !== _reqSeq) return; // superseded by a newer project switch
        if (!_installed) window.installLivecodeTsIntel(data.tsconfig);
        else if (data.tsconfig) applyCompilerOptions(data.tsconfig);
        var files = data.files || [];
        var capped = files.length > PRELOAD_MODEL_CAP;
        files.slice(0, PRELOAD_MODEL_CAP).forEach(function (f) {
          if (!f || !f.path) return;
          var uri = window.monaco.Uri.file(f.path);
          if (window.monaco.editor.getModel(uri)) return; // already open as a tab
          try {
            window.monaco.editor.createModel(f.content || "", langForPath(f.path), uri);
            _preloadedUris.add(uri.toString());
          } catch (e) {}
        });
        // Intentionally suppress this warning toast in LiveCode.
        // TS/JS intelligence still functions; we just avoid spamming users on large repos.
        // (data.truncated || capped) indicates we preloaded only a capped subset of models.
      })
      .catch(function () {});
  }

  window.WBTsIntel = {
    onProjectOpen: function (nextPath, prevPath) {
      if (prevPath && prevPath !== nextPath) disposeAll();
      _lastProject = nextPath;
      preloadProject(nextPath);
    },
    disposeAll: disposeAll,
    _debug: function () {
      return { installed: _installed, preloaded: _preloadedUris.size, project: _lastProject };
    },
  };
})();
